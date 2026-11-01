"""Transport-independent orchestration: auth, quotas, locks, routing, replies.

Telegram supplies a Channel; nothing here imports Telegram.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .config import Settings
from .live.plan import Approver
from .live.session import LiveRunner
from .state.db import Database

log = logging.getLogger("bridge")

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

HELP = ("Ask a question about Oracle Fusion data and I'll look it up. I can only read.\n"
        "/ask <question>, or just type your question\n"
        "/new - start fresh   /usage - your runs and the monthly budget\n"
        "Before I read any data I show you a plan to approve.")


@dataclass
class Channel:
    user_id: int
    chat_id: int
    send_text: Callable[[str], Awaitable[None]]
    approve: Approver
    status: Callable[[str], Awaitable[None]]


def cycle_start(now: dt.datetime, day: int) -> float:
    """Start (UTC epoch) of the current credit cycle, which renews on `day` of each month."""
    if now.day >= day:
        d = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    else:
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev = first - dt.timedelta(days=1)
        d = prev.replace(day=min(day, 28), hour=0, minute=0, second=0, microsecond=0)
    return d.replace(tzinfo=dt.timezone.utc).timestamp()


def _u16(text: str) -> int:
    """Length in UTF-16 code units, which is how Telegram counts: an emoji is 2, not 1."""
    return len(text.encode("utf-16-le")) // 2


def _cut_index(line: str, limit: int) -> int:
    """How many characters of `line` fit in `limit` UTF-16 units, preferring to stop at a space."""
    used = 0
    for i, ch in enumerate(line):
        used += 2 if ord(ch) > 0xFFFF else 1
        if used > limit:
            sp = line.rfind(" ", 0, i)
            return sp if sp > i // 2 else i
    return len(line)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Cut an answer into pieces no longer than `limit` UTF-16 units, preferring line and word boundaries (FR-09)."""
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while _u16(line) > limit:                      # a single line longer than a message
            cut = _cut_index(line, limit)
            head, line = line[:cut], line[cut:].lstrip()
            if cur:
                pieces.append(cur.rstrip())
                cur = ""
            pieces.append(head)
        if _u16(cur) + _u16(line) + 1 > limit:
            pieces.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        pieces.append(cur.rstrip())
    return [p for p in pieces if p]


def make_footer(res, show_cost: bool) -> str:
    """One short line: did this answer read Fusion just now, what it cost (if the user may see it) and how long it took.
    Decided by the bridge from what really happened, not by the model."""
    parts = []
    if res.status == "ok":
        parts.append("Fresh from Fusion" if res.data_calls > 0 else "No new Fusion read")
    if show_cost:
        parts.append(f"${res.cost_usd:.3f}")
    parts.append(f"{res.duration_s:.0f}s")
    return "(" + " \u00b7 ".join(parts) + ")"


def fusion_hint(errors: list[str]) -> str:
    """Turn the first tool failure into one plain sentence for the user (FR-10)."""
    if not errors:
        return ""
    e = errors[0].lower()
    if "name or service not known" in e or "connecterror" in e or "connection" in e or "timed out" in e:
        return "Fusion could not be reached from the bridge host."
    if "401" in e or "unauthor" in e:
        return "Fusion rejected the bridge's login."
    if "403" in e or "forbidden" in e:
        return "The Fusion account is not allowed to read that."
    return "Fusion reported an error: " + errors[0][:120].replace("\n", " ")


ENGINE_ERRORS = {
    "billing_error": "The model credit looks used up, so I can't answer until it renews (it renews on the 7th). Please tell the admin.",
    "authentication_failed": "The bridge's model key was rejected. Please tell the admin.",
    "rate_limit": "The model service is busy right now. Wait a minute and ask again.",
    "server_error": "The model service had a problem. Try again in a minute.",
    "invalid_request": "The model rejected that request. Send /new and ask again, or rephrase.",
}


def compact_tools(calls: list[dict], limit: int = 14) -> str:
    """['Skill', 'server_info', 'query_resource x3'] in order, runs of the same tool folded together."""
    out: list[str] = []
    for c in calls:
        name = c["name"]
        if out and out[-1][0] == name:
            out[-1][1] += 1
        else:
            out.append([name, 1])
    parts = [n if k == 1 else f"{n}x{k}" for n, k in out]
    return ",".join(parts[:limit]) + ("..." if len(parts) > limit else "")


def run_summary(user_id: int, res, resumed: bool, question: str = "", show_question: bool = False) -> str:
    q = f' q="{question[:60]}"' if show_question else f" q_chars={len(question)}"
    session = f" session=${res.session_cost_total:.4f}" if resumed and res.session_cost_total else ""
    return (f"run user={user_id} status={res.status}{' error=' + res.error_kind if res.error_kind else ''} cost=${res.cost_usd:.4f}{session} "
            f"secs={res.duration_s:.0f} turns={res.turns} plans={res.plans} data_ok={res.data_calls} data_tried={res.data_attempts} "
            f"tool_errors={len(res.tool_errors)} cut={res.results_cut} resumed={'yes' if resumed else 'no'} "
            f"tools={compact_tools(res.tool_calls)}{q}")


FAILURES = {
    "denied": "Cancelled: the query plan wasn't approved, so nothing was read.",
    "timeout": "That took too long and was stopped. Try a narrower question.",
    "budget": "That question needed more than the per-question cost limit. Try something narrower.",
    "turns": "That needed more steps than I'm allowed in one question. Try a narrower question, or ask for one thing at a time.",
    "no_answer": "I couldn't produce an answer. Try rephrasing, or name the resource and the period you need.",
}


RunnerFactory = Callable[[Approver], LiveRunner]


class Bridge:
    def __init__(self, settings: Settings, db: Database, make_runner: RunnerFactory):
        """make_runner(approver) returns an object with an async run(user_id, question, resume, on_status)."""
        self.s, self.db, self.make_runner = settings, db, make_runner
        self.sem = asyncio.Semaphore(settings.max_concurrent_runs)
        self.active: set[int] = set()
        self._warned: set[int] = set()
        self.stopping = False                                  # set when the bot is shutting down
        self._running: dict[int, tuple[asyncio.Task, Channel]] = {}      # the question each user has in flight

    async def begin_shutdown(self, grace_s: float = 5.0) -> int:
        """Tell everyone with a question in flight that the bot is restarting, give those runs a moment to finish,
        then cancel the rest so their engine processes are stopped properly. Returns how many were in flight."""
        self.stopping = True
        running = {uid: (t, ch) for uid, (t, ch) in self._running.items() if not t.done()}
        for uid, (_task, ch) in running.items():
            try:
                await ch.send_text("The bot is restarting, so your question was stopped. Please ask it again in a minute.")
            except Exception:
                log.exception("Could not tell user %s about the restart", uid)
        if running:
            _done, pending = await asyncio.wait([t for t, _ in running.values()], timeout=grace_s)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            log.info("Shutdown: %d question(s) were in flight; %d had to be cancelled", len(running), len(pending))
        return len(running)

    async def handle(self, command: str, text: str, ch: Channel) -> None:
        if not await self.db.is_allowed(ch.user_id):                        # FR-01: silent
            await self.db.audit(ch.user_id, "rejected_user", command)
            if ch.user_id not in self._warned:                              # tell the operator once; stay silent in the chat
                self._warned.add(ch.user_id)
                log.warning("Ignored /%s from Telegram ID %s: not registered. To allow this person, put the ID in "
                            "ALLOWED_TELEGRAM_USER_IDS in .env and restart the bot.", command, ch.user_id)
            return
        text = text.strip()
        if command in ("start", "help"):
            return await ch.send_text(HELP)
        if command == "new":
            await self.db.reset_chat(ch.chat_id)
            return await ch.send_text("Started fresh. Your previous conversation is cleared.")
        if command == "usage":
            return await ch.send_text(await self._usage(ch.user_id))
        if command != "ask":
            return await ch.send_text(HELP)
        if not text:
            return await ch.send_text("Add your question after /ask, or just type it.")
        if not self.s.live_ready:                                           # FR-14
            return await ch.send_text("Live queries aren't configured on this bridge yet.")
        if self.s.user_daily_runs > 0 and await self.db.runs_today(ch.user_id) >= self.s.user_daily_runs:
            return await ch.send_text(f"Daily limit of {self.s.user_daily_runs} questions reached. It resets at 00:00 UTC.")
        if self.s.burst_runs > 0 and await self.db.runs_since(ch.user_id, self.s.burst_minutes * 60) >= self.s.burst_runs:
            return await ch.send_text(f"That is a lot of questions in a short time ({self.s.burst_runs} in {self.s.burst_minutes} minutes). "
                                      "Please wait a few minutes and try again.")
        if ch.user_id in self.active:                                       # one active run per user
            return await ch.send_text("A question is already running. Wait for it to finish.")
        self.active.add(ch.user_id)
        task = asyncio.current_task()
        if task is not None:
            self._running[ch.user_id] = (task, ch)
        try:
            async with self.sem:                                            # at most N concurrent runs
                await self._ask(text, ch)
        except asyncio.CancelledError:
            raise
        except Exception:                                                   # the user must never be left waiting in silence
            log.exception("Question failed unexpectedly for user %s", ch.user_id)
            try:
                await ch.send_text("Something went wrong and has been logged. Try again, or send /new first.")
            except Exception:
                log.exception("Could not tell user %s about the failure either", ch.user_id)
        finally:
            self.active.discard(ch.user_id)
            self._running.pop(ch.user_id, None)

    def usable_session(self, session_id: Optional[str]) -> Optional[str]:
        """A stored session the engine can resume. Live mode needs a real session id (a UUID); the demo agent's
        invented ids, or anything else, are dropped so they can never block a chat."""
        if not session_id:
            return None
        if self.s.demo_mode:
            return session_id
        return session_id if _UUID.match(session_id) else None

    async def _ask(self, text: str, ch: Channel) -> None:
        chat = await self.db.get_chat(ch.chat_id)
        stored = chat["live_session_id"]
        resume = self.usable_session(stored)
        if stored and not resume:
            log.warning("Dropping stored session %r: it is not a resumable session id (left over from demo mode?)", stored)
            await self.db.set_live_session(ch.chat_id, None)
        limit_s = self.s.session_idle_minutes * 60
        if resume and limit_s and chat.get("updated") and time.time() - chat["updated"] > limit_s:
            resume = None                                                   # old results may be stale: begin again
            await self.db.set_live_session(ch.chat_id, None)
            await ch.send_text(f"Starting a fresh conversation: the last one was idle for more than "
                               f"{self.s.session_idle_minutes} minutes, so its earlier results may be out of date.")
        await ch.send_text("Working on it...")
        prior = float(chat.get("session_cost") or 0.0) if resume else 0.0
        res = await self.make_runner(ch.approve).run(ch.user_id, text, resume, ch.status, prior_cost=prior)
        if resume and res.status == "error" and res.turns == 0 and not res.tool_calls:
            # The engine refused to resume (an expired or missing session). Start a fresh conversation, once.
            log.warning("Could not resume session %s (%s); starting a new conversation", resume, res.message or "no details")
            await self.db.set_live_session(ch.chat_id, None)
            res = await self.make_runner(ch.approve).run(ch.user_id, text, None, ch.status)
        log.info("%s", run_summary(ch.user_id, res, bool(resume), text, self.s.log_questions))
        if self.stopping and res.status != "ok":                 # the restart notice has been sent; no second, confusing message
            await self.db.record_run(ch.user_id, "ask", res.status, res.cost_usd, res.duration_s, text, {"stopped_by_restart": True})
            return
        if res.status == "error":
            log.error("Run failed for user %s: %s | engine said: %s", ch.user_id, res.message or "no details from the engine",
                      " / ".join(res.engine_lines[-3:]) or "nothing on stderr")
        await self.db.record_run(ch.user_id, "ask", res.status, res.cost_usd, res.duration_s, text, {
            "turns": res.turns, "plans": res.plans, "data_ok": res.data_calls, "data_tried": res.data_attempts,
            "tool_errors": [e[:120] for e in res.tool_errors[:5]], "results_cut": res.results_cut, "error_kind": res.error_kind,
            "api_status": res.api_status, "resumed": bool(resume), "model": self.s.model, "skill_mode": self.s.effective_skill_mode,
            "tools": compact_tools(res.tool_calls, 40), "engine_lines": res.engine_lines[-5:],
            "session_cost_total": res.session_cost_total})
        if res.session_id and res.status in ("ok", "no_answer"):
            await self.db.set_live_session(ch.chat_id, res.session_id, res.session_cost_total or res.cost_usd)
        footer = make_footer(res, self.s.can_see_cost(ch.user_id))
        if res.status == "ok" and res.answer:
            await self._deliver(res.answer, footer, ch)
            return
        msg = ENGINE_ERRORS.get(res.error_kind) if res.status == "error" and res.error_kind in ENGINE_ERRORS else None
        msg = msg or FAILURES.get(res.status, "Something went wrong. It has been logged.")
        hint = fusion_hint(res.tool_errors)
        await ch.send_text(" ".join(x for x in (msg, hint, footer) if x))

    async def _deliver(self, answer: str, footer: str, ch: Channel) -> None:
        pieces = split_message(answer, self.s.message_limit)
        if pieces and _u16(pieces[-1]) + _u16(footer) + 2 <= self.s.message_limit:
            pieces[-1] = pieces[-1] + "\n\n" + footer
        else:
            pieces.append(footer)
        for i, piece in enumerate(pieces):
            if i:
                await asyncio.sleep(self.s.send_pace_s)
            await ch.send_text(piece)

    async def _usage(self, user_id: int) -> str:
        n = await self.db.runs_today(user_id)
        spent = await self.db.spend_since(cycle_start(dt.datetime.now(dt.timezone.utc), self.s.credit_cycle_day))
        left = max(self.s.monthly_credit_usd - spent, 0.0)
        limit = f" of {self.s.user_daily_runs}" if self.s.user_daily_runs > 0 else ""
        if not self.s.can_see_cost(user_id):
            return f"Your questions today: {n}{limit}"
        return (f"Your questions today: {n}{limit}\n"
                f"Bridge spend this cycle: ${spent:.2f} of ${self.s.monthly_credit_usd:.0f} (about ${left:.2f} left)")

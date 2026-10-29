"""Transport-independent orchestration: auth, quotas, locks, routing, replies.

Telegram supplies a Channel; nothing here imports Telegram.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Awaitable, Callable

from .config import Settings
from .live.plan import Approver
from .live.session import LiveRunner
from .state.db import Database

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


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Cut an answer into pieces no longer than `limit`, preferring line and word boundaries (FR-09)."""
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:                       # a single line longer than a message
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            head, line = line[:cut], line[cut:].lstrip()
            if cur:
                pieces.append(cur.rstrip())
                cur = ""
            pieces.append(head)
        if len(cur) + len(line) + 1 > limit:
            pieces.append(cur.rstrip())
            cur = ""
        cur += line + "\n"
    if cur.strip():
        pieces.append(cur.rstrip())
    return [p for p in pieces if p]


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

    async def handle(self, command: str, text: str, ch: Channel) -> None:
        if not await self.db.is_allowed(ch.user_id):                        # FR-01: silent
            await self.db.audit(ch.user_id, "rejected_user", command)
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
        if await self.db.runs_today(ch.user_id) >= self.s.user_daily_runs:
            return await ch.send_text(f"Daily limit of {self.s.user_daily_runs} questions reached. It resets at 00:00 UTC.")
        if ch.user_id in self.active:                                       # one active run per user
            return await ch.send_text("A question is already running. Wait for it to finish.")
        self.active.add(ch.user_id)
        try:
            async with self.sem:                                            # at most N concurrent runs
                await self._ask(text, ch)
        finally:
            self.active.discard(ch.user_id)

    async def _ask(self, text: str, ch: Channel) -> None:
        chat = await self.db.get_chat(ch.chat_id)
        await ch.send_text("Working on it. I'll show you a plan to approve before any data is read.")
        res = await self.make_runner(ch.approve).run(ch.user_id, text, chat["live_session_id"], ch.status)
        await self.db.record_run(ch.user_id, "ask", res.status, res.cost_usd, res.duration_s, text)
        if res.session_id and res.status in ("ok", "no_answer"):
            await self.db.set_live_session(ch.chat_id, res.session_id)
        footer = f"(${res.cost_usd:.2f}, {res.duration_s:.0f}s)"
        if res.status == "ok" and res.answer:
            await self._deliver(res.answer, footer, ch)
            return
        msg = FAILURES.get(res.status, "Something went wrong. It has been logged.")
        hint = fusion_hint(res.tool_errors)
        await ch.send_text(" ".join(x for x in (msg, hint, footer) if x))

    async def _deliver(self, answer: str, footer: str, ch: Channel) -> None:
        pieces = split_message(answer, self.s.message_limit)
        if pieces and len(pieces[-1]) + len(footer) + 2 <= self.s.message_limit:
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
        return (f"Your questions today: {n} of {self.s.user_daily_runs}\n"
                f"Bridge spend this cycle: ${spent:.2f} of ${self.s.monthly_credit_usd:.0f} (about ${left:.2f} left)")

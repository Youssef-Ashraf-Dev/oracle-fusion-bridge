"""Day 1 spike: run representative questions through the live pipeline and measure what they cost.

A variant is model/skill-mode, for example haiku/native. Only Haiku is used by default.

Nothing here talks to Telegram. It uses the same LiveRunner, policy and plan gate as the bot, so what it
measures is what the bot will do.
"""
from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Optional
from urllib.parse import urlparse

from .config import Settings
from .live import permissions as P
from .live.plan import Plan
from .live.session import LiveResult, LiveRunner
from .state.db import Database


# ---- questions ------------------------------------------------------------------------

@dataclass(frozen=True)
class Question:
    qid: int
    text: str
    expect: str = "answer"          # "answer" or "refuse" (a write or personal-data request)


def load_questions(path: Path) -> list[Question]:
    """One question per line. '#' starts a comment. A trailing [refuse] marks a request that must be declined."""
    out: list[Question] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        expect = "answer"
        if line.lower().endswith("[refuse]"):
            line, expect = line[:-8].strip(), "refuse"
        out.append(Question(len(out) + 1, line, expect))
    return out


# ---- records --------------------------------------------------------------------------

@dataclass
class RunRecord:
    qid: int
    variant: str
    question: str
    expect: str
    status: str
    cost_usd: float
    turns: int
    duration_s: float
    data_calls: int
    discovery_calls: int
    tool_calls: list[str]
    tool_errors: int
    write_attempts: int
    sensitive_attempts: int
    plans: list[str]
    answer: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    message: str = ""
    ts: str = ""


def to_record(q: Question, variant: str, res: LiveResult, plans: list[str]) -> RunRecord:
    names = [t["name"] for t in res.tool_calls]
    u = res.usage or {}
    return RunRecord(
        qid=q.qid, variant=variant, question=q.text, expect=q.expect, status=res.status,
        cost_usd=round(res.cost_usd, 5), turns=res.turns, duration_s=res.duration_s, data_calls=res.data_calls,
        discovery_calls=sum(1 for n in names if n in P.DISCOVERY),
        tool_calls=names, tool_errors=len(res.tool_errors),
        write_attempts=sum(1 for n in names if n in P.WRITE_WORKFLOW),
        sensitive_attempts=sum(1 for n in names if n in P.SENSITIVE),
        plans=plans, answer=res.answer, message=res.message,
        input_tokens=int(u.get("input_tokens", 0) or 0), output_tokens=int(u.get("output_tokens", 0) or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
        ts=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))


# ---- approver -------------------------------------------------------------------------

class RecordingApprover:
    """Records every plan shown. mode 'auto' approves; 'ask' puts the plan to a person at the terminal."""

    def __init__(self, mode: str = "auto", ask: Optional[Callable[[str], Awaitable[bool]]] = None):
        self.mode, self.ask, self.plans = mode, ask, []

    async def __call__(self, plan: Plan) -> bool:
        text = plan.render()
        self.plans.append(text)
        if self.mode == "ask" and self.ask is not None:
            return await self.ask(text)
        return True


# ---- the approval prompt ---------------------------------------------------------------

def parse_yes_no(answer: str) -> Optional[bool]:
    """y/yes -> True; n/no/empty -> False; anything else (pasted text, a typo) -> None, meaning ask again."""
    a = answer.strip().lower()
    if a in ("y", "yes"):
        return True
    if a in ("", "n", "no"):
        return False
    return None


async def ask_yes_no(read: Callable[[str], Awaitable[str]], prompt: str, say: Callable[[str], None] = print) -> bool:
    """Keep asking until the answer is clearly yes or no, so a pasted question cannot silently count as a denial."""
    while True:
        verdict = parse_yes_no(await read(prompt))
        if verdict is not None:
            return verdict
        say("Please type y or n.")


# ---- running --------------------------------------------------------------------------

MODEL_ALIASES = {"haiku": "claude-haiku-5-5"}          # any other model must be given by its full id
MODES = ("native", "inline", "none")


def parse_variant(label: str) -> tuple[str, str]:
    """'haiku/native' -> ('claude-haiku-5-5', 'native'). The mode is how the planner skill reaches the model:
    native = the Skill tool, as in chat; inline = pasted into the system prompt; none = not used."""
    model, _, mode = label.partition("/")
    mode = mode or "native"
    if mode not in MODES:
        raise ValueError(f"unknown skill mode {mode!r} in {label!r}; use one of {MODES}")
    if not model:
        raise ValueError(f"missing model in {label!r}")
    return MODEL_ALIASES.get(model, model), mode


def has_skill(base: Settings) -> bool:
    return bool(base.planner_skill and Path(base.planner_skill).is_file())


def variant_settings(base: Settings, label: str) -> Settings:
    model, mode = parse_variant(label)
    if mode in ("native", "inline") and not has_skill(base):
        raise ValueError(f"{label!r} needs PLANNER_SKILL_PATH to point to a readable SKILL.md")
    return dataclasses.replace(base, model=model, skill_mode=mode)


def default_variants(base: Settings) -> list[str]:
    """Haiku, with the skill loaded the way chat does it (or without it when no skill file is configured)."""
    mode = "native" if has_skill(base) else "none"
    return [f"haiku/{mode}"]


async def run_spike(base: Settings, db: Database, questions: Iterable[Question], variants: list[str],
                    approver_mode: str = "auto", ask: Optional[Callable[[str], Awaitable[bool]]] = None,
                    client_factory=None, tick_s: float = 1.0,
                    progress: Optional[Callable[[str], None]] = None) -> list[RunRecord]:
    records: list[RunRecord] = []
    qs = list(questions)
    for variant in variants:
        s = variant_settings(base, variant)
        for q in qs:
            approver = RecordingApprover(approver_mode, ask)
            runner = LiveRunner(s, db, approver, client_factory, tick_s)
            if progress:
                progress(f"[{variant}] Q{q.qid}: {q.text}")
            res = await runner.run(user_id=0, question=q.text)           # every question is a fresh session
            rec = to_record(q, variant, res, approver.plans)
            records.append(rec)
            if progress:
                progress(f"    -> {rec.status}, ${rec.cost_usd:.3f}, {rec.turns} turns, "
                         f"{rec.discovery_calls} discovery + {rec.data_calls} data calls, {rec.duration_s:.0f}s")
    return records


# ---- statistics -----------------------------------------------------------------------

def p90(values: list[float]) -> float:
    """Nearest-rank 90th percentile, in integer arithmetic."""
    if not values:
        return 0.0
    v = sorted(values)
    return v[(9 * len(v) + 9) // 10 - 1]


def summarise(records: list[RunRecord], credit_usd: float = 240.0) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for variant in dict.fromkeys(r.variant for r in records):
        rs = [r for r in records if r.variant == variant]
        costs = [r.cost_usd for r in rs]
        answers = [r for r in rs if r.expect == "answer"]
        refuse = [r for r in rs if r.expect == "refuse"]
        avg = statistics.mean(costs) if costs else 0.0
        out[variant] = {
            "runs": len(rs),
            "statuses": {s: sum(1 for r in rs if r.status == s) for s in sorted({r.status for r in rs})},
            "cost_total": round(sum(costs), 4), "cost_avg": round(avg, 4),
            "cost_median": round(statistics.median(costs), 4) if costs else 0.0,
            "cost_p90": round(p90(costs), 4), "cost_max": round(max(costs), 4) if costs else 0.0,
            "turns_avg": round(statistics.mean(r.turns for r in rs), 1) if rs else 0.0,
            "data_calls_avg": round(statistics.mean(r.data_calls for r in rs), 1) if rs else 0.0,
            "duration_avg": round(statistics.mean(r.duration_s for r in rs), 1) if rs else 0.0,
            "duration_p90": round(p90([r.duration_s for r in rs]), 1),
            "answered": sum(1 for r in answers if r.status == "ok"),
            "answer_questions": len(answers),
            "suggested_cap_usd": max(0.20, round(p90(costs) * 1.5, 2)),
            "questions_per_credit_at_avg": int(credit_usd / avg) if avg > 0 else 0,
            "questions_per_credit_at_p90": int(credit_usd / p90(costs)) if p90(costs) > 0 else 0,
            "tool_failures": sum(r.tool_errors for r in rs),
            "refusal_questions": len(refuse),
            "write_attempts": sum(r.write_attempts for r in rs),
            "sensitive_attempts": sum(r.sensitive_attempts for r in rs),
        }
    return out


# ---- report ---------------------------------------------------------------------------

def _host(url: str) -> str:
    return urlparse(url).netloc or url


def render_markdown(records: list[RunRecord], summary: dict[str, dict], base: Settings,
                    credit_usd: float = 240.0) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L: list[str] = [
        "# Day 1 results", "",
        f"Run: {now}  |  variants: {', '.join(summary)}  |  pod: {_host(base.oracle_base_url)}  |  "
        f"per-run cap used: ${base.run_budget_usd:.2f}  |  max turns: {base.max_turns}  |  effort: {base.effort or 'default'}", "",
        "## Summary", "",
        "| Variant | Runs | Cost avg | Median | p90 | Max | Suggested cap | Questions per credit (avg / p90) | Turns avg | Data calls avg | Time avg / p90 (s) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for v, s in summary.items():
        L.append(f"| {v} | {s['runs']} | ${s['cost_avg']:.3f} | ${s['cost_median']:.3f} | ${s['cost_p90']:.3f} | "
                 f"${s['cost_max']:.3f} | ${s['suggested_cap_usd']:.2f} | {s['questions_per_credit_at_avg']} / "
                 f"{s['questions_per_credit_at_p90']} (of ${credit_usd:.0f}) | {s['turns_avg']} | {s['data_calls_avg']} | "
                 f"{s['duration_avg']} / {s['duration_p90']} |")
    L += ["", "Statuses: " + "; ".join(f"{v}: {s['statuses']}" for v, s in summary.items()), "",
          "Safety observations (requests that should be declined):", ""]
    for v, s in summary.items():
        L.append(f"- {v}: {s['refusal_questions']} decline-type questions; write-tool attempts {s['write_attempts']}, "
                 f"personal-data tool attempts {s['sensitive_attempts']} (every attempt is denied by the policy; "
                 "a non-zero count means the prompt is not steering the model away on its own).")
    L += ["", "## Every run", "",
          "| Q | Variant | Status | Cost | Turns | Discovery | Data | Tool errors | Seconds | Quality 0-2 | Notes |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in records:
        L.append(f"| {r.qid} | {r.variant} | {r.status} | ${r.cost_usd:.3f} | {r.turns} | {r.discovery_calls} | "
                 f"{r.data_calls} | {r.tool_errors} | {r.duration_s:.0f} |  |  |")
    L += ["", "Quality scale: 0 = wrong or no answer, 1 = right but thin or badly formatted for a phone, 2 = right and clear.", "",
          "## Answers and plans", "",
          "Contains data read from the pod. Keep this file on the host; do not paste it into shared chats.", ""]
    for r in records:
        L += [f"### Q{r.qid} ({r.variant}): {r.question}", "",
              f"Status {r.status}, ${r.cost_usd:.3f}, {r.turns} turns. Tools: {', '.join(r.tool_calls) or 'none'}", ""]
        for p in r.plans:
            L += ["Plan shown:", "", "```text", p, "```", ""]
        L += ["Answer:", "", "```text", r.answer or "(no answer)", "```", ""]
        if r.message:
            L += [f"Message: {r.message}", ""]
    L += ["## Gate for Day 1", "",
          "- [ ] Cost per question measured (see Summary).",
          "- [ ] Every Quality cell filled in; the answers are accepted.",
          "- [ ] Per-run cap chosen (start from Suggested cap) and set as RUN_BUDGET_USD.",
          "- [ ] Decline-type questions were declined; no data was read for them.",
          "- [ ] The model and the skill mode (native, inline or none) are chosen from the variants compared.", ""]
    return "\n".join(L)


def write_outputs(records: list[RunRecord], base: Settings, out_dir: Path, credit_usd: float = 240.0) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarise(records, credit_usd)
    paths = {"jsonl": out_dir / "day1_results.jsonl", "csv": out_dir / "day1_results.csv", "md": out_dir / "day1_results.md"}
    with paths["jsonl"].open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(dataclasses.asdict(r), ensure_ascii=False) + "\n")
    cols = ["qid", "variant", "status", "cost_usd", "turns", "duration_s", "data_calls", "discovery_calls", "tool_errors",
            "write_attempts", "sensitive_attempts", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "question"]
    with paths["csv"].open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in records:
            d = dataclasses.asdict(r)
            w.writerow({c: d[c] for c in cols})
    paths["md"].write_text(render_markdown(records, summary, base, credit_usd), encoding="utf-8")
    return paths

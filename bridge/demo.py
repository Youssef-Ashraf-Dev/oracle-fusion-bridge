"""Demo mode: a stand-in for the agent, so the Telegram side can be tested with no Oracle and no model.

It goes through the real plan-approval path (RunContext.propose_query_plan), so the Approve and Deny buttons, the
approval timeout, the audit log and the stored plans behave exactly as they do in a live run. Only the agent is fake.

Keywords in the question steer it:  long -> a 10,000+ character answer   slow -> 25 seconds of progress updates
error -> a failed run             anything else -> a short invented answer
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Optional

from .config import Settings
from .live.plan import Approver
from .live.session import LiveResult, RunContext, StatusFn
from .state.db import Database

BANNER = ("[DEMO MODE: no Oracle and no model were used. The plan and this answer are canned, the same whatever you ask, "
          "and do not depend on your question.]")


def make_answer(question: str, resumed: bool) -> str:
    q = question.lower()
    lines = [BANNER, f"You asked: {question[:120]}"]
    if resumed:
        lines.append("(This continues your earlier conversation.)")
    if "long" in q:
        lines += [f"Invoice DEMO-{i:04d}: Example Supplier {i % 7}, EGP {(i * 137) % 9000 + 100}.00, Approved"
                  for i in range(1, 260)]
    else:
        lines += ["3 open invoices:", "DEMO-0001: Example Supplier 1, EGP 1,200.00, Approved",
                  "DEMO-0002: Example Supplier 2, EGP 340.50, On hold", "DEMO-0003: Example Supplier 3, EGP 98.00, Approved"]
    lines.append("Pod demo.example.com, as of the time you asked.")
    return "\n".join(lines)


class DemoRunner:
    """Same interface as LiveRunner: run(user_id, question, resume, on_status) -> LiveResult."""

    def __init__(self, settings: Settings, db: Database, approver: Approver, pause_s: float = 0.5,
                 slow_s: float = 25.0, status_every_s: float = 10.0):
        self.s, self.db, self.approver = settings, db, approver
        self.pause_s, self.slow_s, self.status_every_s = pause_s, slow_s, status_every_s

    async def run(self, user_id: int, question: str, resume: Optional[str] = None,
                  on_status: Optional[StatusFn] = None) -> LiveResult:
        t0 = time.monotonic()
        res = LiveResult(session_id=resume or f"demo-{uuid.uuid4().hex[:8]}")
        q = question.lower()
        ctx = RunContext(self.s, self.db, user_id, self.approver)
        await asyncio.sleep(self.pause_s)                                   # "discovery"
        if "error" in q:
            res.status, res.message = "error", "demo failure"
            res.duration_s = round(time.monotonic() - t0, 2)
            await self.db.audit(user_id, "ask_finished", "error (demo)")
            return res
        _msg, denied = await ctx.propose_query_plan({
            "summary": f"DEMO plan (canned, not based on your question): {question[:60]}",
            "steps": [{"tool": "search_invoices", "note": "demo step, always the same. The real agent plans from your question"}]})
        if denied:
            res.status = "denied"
        else:
            if "slow" in q:
                waited = 0.0
                while waited < self.slow_s:
                    step = min(self.status_every_s, self.slow_s - waited)
                    await asyncio.sleep(step)
                    waited += step
                    if on_status and waited < self.slow_s:
                        await on_status(f"Working... {int(waited)}s")
            else:
                await asyncio.sleep(self.pause_s)
            res.status, res.data_calls = "ok", 1
            res.answer = make_answer(question, resume is not None)
        res.duration_s = round(time.monotonic() - t0 - ctx.approval_seconds(), 2)
        await self.db.audit(user_id, "ask_finished", f"{res.status} (demo)")
        return res

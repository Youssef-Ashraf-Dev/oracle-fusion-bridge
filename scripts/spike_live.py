"""One question, one run, printed in full. You approve the plan at the prompt.

    python scripts/spike_live.py "list the ledgers"
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.config import load_settings                                   # noqa: E402
from bridge.day1 import ask_yes_no                                        # noqa: E402
from bridge.live.session import LiveRunner                                # noqa: E402
from bridge.state.db import Database                                      # noqa: E402


async def console_approver(plan) -> bool:
    print("\n" + plan.render())
    return await ask_yes_no(lambda prompt: asyncio.to_thread(input, prompt), "Approve these data calls? [y/N] ")


async def say(text: str) -> None:
    print(text)


async def main(question: str) -> None:
    s = load_settings()
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await Database(s.db_path).open()
    res = await LiveRunner(s, db, console_approver).run(1, question, on_status=say)
    print(f"\nstatus={res.status} cost=${res.cost_usd:.4f} turns={res.turns} data_calls={res.data_calls} {res.duration_s}s")
    print("tools:", ", ".join(t["name"] for t in res.tool_calls) or "none")
    for e in res.tool_errors:
        print("tool failure:", e)
    print("\nanswer:\n" + (res.answer or "(none)"))
    await db.close()

asyncio.run(main(" ".join(sys.argv[1:]) or "list the ledgers"))

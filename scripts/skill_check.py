"""Did the planner skill really reach the model? One cheap question (about a cent) that proves it.

    python scripts/skill_check.py

The agent is asked to load its Fusion skill and recite the titles of its sections. PASS means it had the file's content.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.config import load_settings                                   # noqa: E402
from bridge.live.session import LiveRunner                                # noqa: E402
from bridge.skillcheck import QUESTION, headings, score, verdict          # noqa: E402
from bridge.state.db import Database                                      # noqa: E402


async def deny(_plan) -> bool:
    return False


async def main() -> int:
    s = load_settings()
    if s.effective_skill_mode == "none":
        print("No skill is configured (PLANNER_SKILL_PATH or SKILL_MODE=none). Nothing to check.")
        return 2
    if not (s.live_ready and s.anthropic_api_key):
        print("Set ANTHROPIC_API_KEY and ORACLE_* first (run scripts/day1_preflight.py).")
        return 2
    titles = headings(s.planner_skill)
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await Database(s.db_path).open()
    res = await LiveRunner(s, db, deny).run(0, QUESTION)
    await db.close()
    hits, total, missing = score(res.answer, titles)
    names = [t["name"] for t in res.tool_calls]
    print(f"mode: {s.effective_skill_mode}   status: {res.status}   cost: ${res.cost_usd:.4f}   turns: {res.turns}")
    print("tools used:", ", ".join(names) or "none")
    print(f"first tool was Skill: {'yes' if names and names[0] == 'Skill' else 'NO'}")
    print(f"section titles recalled: {hits} of {total}")
    if missing:
        print("not recalled:", "; ".join(missing[:8]) + (" ..." if len(missing) > 8 else ""))
    v = verdict(hits, total)
    print(f"\n{v}: " + {"PASS": "the model had the skill's content.",
                       "WARN": "the model had part of the skill. Run it again; if it stays low, tell me.",
                       "FAIL": "the model did not have the skill's content. Do not trust skill behaviour yet."}[v])
    print("\nWhat it answered:\n" + (res.answer or "(nothing)"))
    return 0 if v == "PASS" else 1


sys.exit(asyncio.run(main()))

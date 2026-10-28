"""Day 1, step 2: run representative questions through the live pipeline and measure cost and quality.

    python scripts/day1_spike.py                              # Haiku, skill loaded as chat does (native)
    python scripts/day1_spike.py --runs haiku/native,haiku/inline,haiku/none   # how the skill reaches the model
    python scripts/day1_spike.py --approve ask --only 3,4     # approve plans yourself for a few questions
    python scripts/day1_spike.py --budget 1.00                # raise the per-run cap for this run only

Results go to data/day1/day1_results.{md,csv,jsonl}. The .md has every answer and plan for grading.
"""
import argparse
import asyncio
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.config import load_settings                                   # noqa: E402
from bridge.day1 import (ask_yes_no, default_variants, has_skill, load_questions, run_spike, summarise,  # noqa: E402
                         variant_settings, write_outputs)
from bridge.state.db import Database                                      # noqa: E402


async def ask_at_terminal(plan_text: str) -> bool:
    print("\n" + plan_text)
    return await ask_yes_no(lambda prompt: asyncio.to_thread(input, prompt), "Approve these data calls? [y/N] ")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(Path(__file__).with_name("day1_questions.txt")))
    ap.add_argument("--runs", default=None, help="comma list of model/skill-mode, e.g. haiku/native,haiku/inline (default: haiku/native)")
    ap.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--approve", choices=["auto", "ask"], default="auto")
    ap.add_argument("--only", default="", help="comma list of question numbers to run")
    ap.add_argument("--budget", type=float, default=None, help="per-run cost cap in USD for this run")
    ap.add_argument("--out", default="data/day1")
    a = ap.parse_args()

    s = load_settings()
    if a.budget is not None:
        s = dataclasses.replace(s, run_budget_usd=a.budget)
    if not (s.live_ready and s.anthropic_api_key):
        print("Set ANTHROPIC_API_KEY and ORACLE_* first (run scripts/day1_preflight.py).")
        return 2
    if a.effort:
        s = dataclasses.replace(s, effort=a.effort)
    variants = a.runs.split(",") if a.runs else default_variants(s)
    try:
        for v in variants:
            variant_settings(s, v)
    except ValueError as e:
        print(e)
        return 2
    if not has_skill(s):
        print("Note: PLANNER_SKILL_PATH is not set, so the planner skill is not used (chat would load it).")
    qs = load_questions(Path(a.questions))
    if a.only:
        keep = {int(x) for x in a.only.split(",") if x.strip()}
        qs = [q for q in qs if q.qid in keep]
    print(f"{len(qs)} questions x {len(variants)} variant(s) = {len(qs) * len(variants)} runs; "
          f"per-run cap ${s.run_budget_usd:.2f}; plans approved: {a.approve}\n")

    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await Database(Path(a.out) / "spike.db").open()
    records = await run_spike(s, db, qs, variants, a.approve, ask_at_terminal, progress=print)
    await db.close()
    paths = write_outputs(records, s, Path(a.out), s.monthly_credit_usd)
    print("\n== Summary ==")
    for v, st in summarise(records, s.monthly_credit_usd).items():
        print(f"{v}: {st['runs']} runs, avg ${st['cost_avg']:.3f}, p90 ${st['cost_p90']:.3f}, max ${st['cost_max']:.3f}, "
              f"suggested cap ${st['suggested_cap_usd']:.2f}, about {st['questions_per_credit_at_avg']} questions per ${s.monthly_credit_usd:.0f}")
    print(f"\nWrote {paths['md']}\nGrade the Quality column there, then send me the .md (it contains pod data: your call).")
    return 0


sys.exit(asyncio.run(main()))

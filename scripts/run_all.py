"""One command that runs every check we have: offline tests, setup preflight, skill check, live scenarios, Telegram token.

    python scripts/run_all.py                 # everything (about 5 to 8 minutes, a few cents of model credit)
    python scripts/run_all.py --quick         # no model credit: tests, preflight, Telegram token
    python scripts/run_all.py --list          # show the scenarios and what each one guards against
    python scripts/run_all.py --only S1,S5    # run only some scenarios
    python scripts/run_all.py --only S8 --repeat 5 --skip-tests   # run a scenario five times to see how often it passes
    python scripts/run_all.py --only S10,S16 --repeat 3 --skip-tests --no-recipes   # the same without the pod notes: what do they change?
    python scripts/run_all.py --skip-tests    # skip the offline test suite

Exit code 0 when nothing FAILED (warnings are allowed), 1 otherwise. A report is written to data/runall/report.md.
The report holds checks and statistics only, never answers, unless you pass --keep-answers.
"""
import argparse
import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge.config import load_settings                                   # noqa: E402
from bridge.preflight import (FAIL as PF_FAIL, WARN as PF_WARN, check_anthropic, check_config, check_mcp,  # noqa: E402
                              check_runtime, check_sdk)
from bridge.runall import FAIL, MANUAL, PASS, SKIP, WARN, Stage, aggregate, overall, parse_pytest  # noqa: E402
from bridge.scenarios import render_report, run_scenario, scenarios      # noqa: E402
from bridge.skillcheck import QUESTION, headings, score, verdict         # noqa: E402
from bridge.state.db import Database                                      # noqa: E402


def say(text: str = "") -> None:
    print(text, flush=True)


def stage_tests() -> Stage:
    say("[1/5] Offline tests (includes the real engine and the real MCP bundle)...")
    env = {**os.environ, "RUN_MCP_TESTS": "1"}
    t0 = time.time()
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=1200)
    passed, failed, skipped, names = parse_pytest(p.stdout + p.stderr)
    detail = f"{passed} passed, {failed} failed, {skipped} skipped in {time.time() - t0:.0f}s"
    return Stage("Offline tests", PASS if p.returncode == 0 else FAIL, detail, [f"failed: {n}" for n in names[:15]])


async def stage_preflight(s) -> Stage:
    say("[2/5] Setup preflight (no model credit)...")
    checks = check_runtime() + check_config(s) + check_anthropic(s)
    blocking = any(c.status == PF_FAIL and c.name.startswith(("Required", "Pod guard", "Pod URL")) for c in checks)
    if not blocking:
        checks += await check_mcp(s)
        checks += await check_sdk(s)
    # the pod-host line is always a WARN that asks a person to confirm; it should not turn the whole run yellow
    real = [c for c in checks if c.status != "PASS" and not c.name.startswith("Pod host")]
    status = FAIL if any(c.status == PF_FAIL for c in checks) else (WARN if any(c.status == PF_WARN for c in real) else PASS)
    ok = sum(c.status == "PASS" for c in checks)
    return Stage("Setup preflight", status, f"{ok} of {len(checks)} checks passed",
                 [f"{c.status} {c.name}: {c.detail}" for c in checks if c.status != "PASS"])


async def stage_skill(s, db) -> Stage:
    say("[3/5] Skill check (about one cent)...")
    if s.effective_skill_mode == "none":
        return Stage("Skill check", SKIP, "no skill configured")
    from bridge.live.session import LiveRunner

    async def deny(_p):
        return False
    res = await LiveRunner(s, db, deny).run(0, QUESTION)
    hits, total, missing = score(res.answer, headings(s.planner_skill))
    v = verdict(hits, total)
    first = res.tool_calls[0]["name"] if res.tool_calls else "none"
    st = {"PASS": PASS, "WARN": WARN, "FAIL": FAIL}[v]
    return Stage("Skill check", st, f"{hits} of {total} section titles recalled, first tool {first}, ${res.cost_usd:.4f}",
                 [f"not recalled: {'; '.join(missing[:6])}"] if missing else [])


async def stage_scenarios(s, db, only, keep, repeat: int = 1) -> Stage:
    chosen = [sc for sc in scenarios() if not only or sc.sid in only]
    say(f"[4/5] Live scenarios ({len(chosen)}{', each ' + str(repeat) + ' times' if repeat > 1 else ''}). Plans are approved "
        "automatically; answers are not saved unless --keep-answers.")
    results = []
    for sc in chosen:
        for i in range(repeat):
            r = await run_scenario(sc, s, db)
            results.append(r)
            bad = [c for c in r.checks if c.status != "PASS"]
            tag = f" run {i + 1}/{repeat}" if repeat > 1 else ""
            soft = "  [soft scenario: this would be a FAIL]" if r.raw_status == FAIL and r.status != FAIL else ""
            say(f"      {r.status:4} {sc.sid} {sc.title}{tag}  (${r.cost:.3f}, {r.seconds:.0f}s)" + (f"  <- {bad[0].name}: {bad[0].detail or bad[0].status}" if bad else "") + soft)
        if r.status != PASS:                              # the reason, without the answer: tool errors and engine lines
            for o in r.outcomes:
                for e in o.res.tool_errors[:3]:
                    say(f"           tool error: {e[:400]}")
                for line in o.res.engine_lines[-2:]:
                    say(f"           engine: {line[:150]}")
    out = ROOT / "data" / "runall"
    out.mkdir(parents=True, exist_ok=True)
    body = "# Live scenario results\n\n" + render_report(results) + "\n"
    if keep:
        body += "\n## Answers (contain pod data)\n\n" + "\n\n".join(
            f"### {r.scenario.sid}\n" + "\n\n".join(f"Q: {o.question}\n\n{o.res.answer}" for o in r.outcomes) for r in results)
    (out / "scenarios.md").write_text(body, encoding="utf-8")
    status = aggregate([r.status for r in results])
    total = sum(r.cost for r in results)
    flaky = [sc.sid for sc in chosen if 0 < sum(r.status == PASS for r in results if r.scenario.sid == sc.sid) < repeat]
    note = f"; flaky (passes only sometimes): {', '.join(flaky)}" if flaky else ""
    hidden = sum(1 for r in results if r.raw_status == FAIL and r.status != FAIL)
    note += f"; {hidden} soft failure(s) shown as WARN" if hidden else ""
    return Stage("Live scenarios", status, f"{sum(r.status == PASS for r in results)} of {len(results)} pass, ${total:.3f} spent{note}",
                 [f"{r.status} {r.scenario.sid} {r.scenario.title}: " + "; ".join(f"{c.name} ({c.detail or c.status})" for c in r.checks if c.status != "PASS")
                  for r in results if r.status != PASS] + (
                     ["calls made and refused: python scripts/audit_tail.py --db data/runall/run.db   (the bot's own log is data/bridge.db)"]
                     if status != PASS else []))


async def stage_telegram(s) -> Stage:
    say("[5/5] Telegram token...")
    if not s.telegram_token:
        return Stage("Telegram token", SKIP, "TELEGRAM_BOT_TOKEN is not set")
    from telegram import Bot
    from telegram.error import InvalidToken, NetworkError
    try:
        async with Bot(s.telegram_token) as bot:
            me = await bot.get_me()
        return Stage("Telegram token", PASS, f"accepted: @{me.username}")
    except InvalidToken:
        return Stage("Telegram token", FAIL, "Telegram rejected the token")
    except NetworkError as e:
        return Stage("Telegram token", FAIL, f"could not reach Telegram: {e}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="no model credit: tests, preflight, Telegram token")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--only", default="", help="comma list of scenario ids, e.g. S1,S5")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--keep-answers", action="store_true", help="store answers in the scenario report (contains pod data)")
    ap.add_argument("--no-recipes", action="store_true", help="leave skill/recipes.md out, to measure what the notes change")
    ap.add_argument("--repeat", type=int, default=1, help="run each chosen scenario this many times (the model is not deterministic)")
    a = ap.parse_args()
    if a.list:
        for sc in scenarios():
            say(f"{sc.sid}  {sc.title}{'  (soft)' if sc.soft else ''}\n      guards: {sc.guards}")
        return 0

    s = load_settings()
    if a.no_recipes:
        import dataclasses
        s = dataclasses.replace(s, recipes_path=None)
        say("Recipes are OFF for this run (--no-recipes).")
    only = {x.strip().upper() for x in a.only.split(",") if x.strip()}
    stages: list[Stage] = []
    t0 = time.time()
    stages.append(Stage("Offline tests", SKIP, "skipped") if a.skip_tests else stage_tests())
    stages.append(await stage_preflight(s))
    can_live = s.live_ready and bool(s.anthropic_api_key) and not any(st.status == FAIL for st in stages[1:2])
    if a.quick or not can_live:
        why = "--quick" if a.quick else "setup preflight failed or ANTHROPIC_API_KEY / ORACLE_* missing"
        stages.append(Stage("Skill check", SKIP, why))
        stages.append(Stage("Live scenarios", SKIP, why))
    else:
        (ROOT / "data" / "runall").mkdir(parents=True, exist_ok=True)
        db = await Database(ROOT / "data" / "runall" / "run.db").open()
        try:
            stages.append(await stage_skill(s, db))
            stages.append(await stage_scenarios(s, db, only, a.keep_answers, max(1, a.repeat)))
        finally:
            await db.close()
    stages.append(await stage_telegram(s))

    say("\n" + "=" * 78)
    for st in stages:
        say(f"{st.status:4}  {st.name:18} {st.detail}")
        for line in st.lines:
            say(f"        {line[:170]}")
    final = overall(stages)
    say("=" * 78)
    say(f"RESULT: {final}   ({time.time() - t0:.0f}s)   " + {"PASS": "everything automatic passed.", "WARN": "no failures; read the warnings.",
                                                          "FAIL": "something failed; see above."}[final])
    say("\nStill manual (needs a person with Telegram):")
    for i, m in enumerate(MANUAL, 1):
        say(f"  {i}. {m}")
    (ROOT / "data" / "runall").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "runall" / "report.md").write_text(
        "# Run-all report\n\n" + "\n".join(f"- {st.status} {st.name}: {st.detail}" + "".join(f"\n    - {l}" for l in st.lines) for st in stages) + f"\n\nResult: {final}\n",
        encoding="utf-8")
    return 1 if final == FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

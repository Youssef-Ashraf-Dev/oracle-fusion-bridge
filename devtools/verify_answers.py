"""Is the bot's answer RIGHT? Ask Fusion the same thing directly and compare the numbers.

    python devtools/verify_answers.py                 # every case once (about 3 cents each)
    python devtools/verify_answers.py --only V1,V3 --repeat 3
    python devtools/verify_answers.py --list

For each case it (1) asks Fusion for the true count with a direct query, (2) asks the bot the question the way a person would,
(3) asks Fusion again, in case the data changed, and (4) checks that the number in the bot's answer matches. The report holds
numbers and verdicts only: no answers, no names. Add your colleagues' real questions as new cases in bridge/verify.py.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.logsafe import safe_streams     # noqa: E402
safe_streams()

from mcp import ClientSession, StdioServerParameters                       # noqa: E402
from mcp.client.stdio import stdio_client                                  # noqa: E402

from bridge.config import ROOT, load_settings                              # noqa: E402
from bridge.live.session import LiveRunner, mcp_env                        # noqa: E402
from bridge.mcp_util import ToolError, unwrap                              # noqa: E402
from bridge.scenarios import RecordingApprover                             # noqa: E402
from bridge.state.db import Database                                       # noqa: E402
from bridge.verify import Asked, cases, render                             # noqa: E402
from bridge.verify import verify_case                                      # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--show-answers", action="store_true",
                    help="print the bot's answer when a case fails, to read what it actually said. The cases are counts, so no names; "
                         "do not use this on a case that asks about people")
    a = ap.parse_args()
    chosen = [c for c in cases() if not a.only or c.vid in {x.strip().upper() for x in a.only.split(",")}]
    if a.list:
        for c in cases():
            print(f"{c.vid}  {c.label}\n      {c.question}")
        return 0
    s = load_settings()
    if not (s.live_ready and s.anthropic_api_key):
        print("Set ANTHROPIC_API_KEY and ORACLE_* in .env first.")
        return 2
    (ROOT / "data" / "verify").mkdir(parents=True, exist_ok=True)
    db = await Database(ROOT / "data" / "verify" / "run.db").open()
    env = {**os.environ, **{k: v for k, v in mcp_env(s).items() if v}}
    params = StdioServerParameters(command="uv", args=["run", "--directory", str(s.mcp_dir.resolve()), "server.py"], env=env)
    results = []
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 180)

                async def call(name, args):
                    return unwrap(await asyncio.wait_for(session.call_tool(name, args), 120))

                async def ask(question):
                    r = await LiveRunner(s, db, RecordingApprover("auto")).run(0, question)
                    return Asked(r.answer, r.status, r.cost_usd, r.duration_s)
                print(f"Verifying {len(chosen)} case(s), {a.repeat} time(s) each. The bot is asked in a fresh conversation every time.\n")
                for c in chosen:
                    for i in range(a.repeat):
                        v = await verify_case(c, call, ask)
                        results.append((c, v))
                        print(f"  {v.status:4} {c.vid} {c.label}  Fusion {v.expected_before} / bot {v.found[:4]}  (${v.cost_usd:.3f}, {v.seconds:.0f}s)"
                              + (f"  <- {v.note}" if v.note else ""))
                        if a.show_answers and v.status == "FAIL" and v.answer:
                            print("       the bot said: " + v.answer.strip().replace("\n", " | ")[:700])
    finally:
        await db.close()
    report = ROOT / "data" / "verify" / "report.md"
    report.write_text("# Answer verification\n\n" + render(results) + "\n", encoding="utf-8")
    bad = [v for _, v in results if v.status == "FAIL"]
    print(f"\n{sum(v.status == 'PASS' for _, v in results)} of {len(results)} verified correct, {len(bad)} wrong. Report: {report}")
    return 1 if bad else 0


sys.exit(asyncio.run(main()))

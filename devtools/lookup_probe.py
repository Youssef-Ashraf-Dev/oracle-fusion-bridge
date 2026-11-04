"""Why did only one name come back? Look the same people up in several ways and compare, without showing any name.

    python devtools/lookup_probe.py --from-absences 10        # take 10 person numbers from recent absences and test the lookups
    python devtools/lookup_probe.py --values 17894,26608,21267
    python devtools/lookup_probe.py --from-absences 10 --as-of 2026-04-01

It asks the workers resource for the same people (a) in one query with all of them joined by or, quoted and not quoted, (b) in one
query with IN (...), (c) in batches of 5 and (d) one at a time, and prints how many each way returned and which person numbers were missing. Names are never printed.
"""
import argparse
import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.logsafe import safe_streams     # noqa: E402
safe_streams()

from mcp import ClientSession, StdioServerParameters                       # noqa: E402
from mcp.client.stdio import stdio_client                                  # noqa: E402

from bridge.config import load_settings                                    # noqa: E402
from bridge.live.session import mcp_env                                    # noqa: E402
from bridge.lookupprobe import advise, probe_lookup, rows_of, values_of    # noqa: E402
from bridge.mcp_util import ToolError, unwrap                              # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--values", default="", help="comma list of key values (person numbers) to look up")
    ap.add_argument("--from-absences", type=int, default=0, help="take this many person numbers from recent absences")
    ap.add_argument("--resource", default="workers")
    ap.add_argument("--key", default="PersonNumber")
    ap.add_argument("--fields", default="PersonNumber,DisplayName")
    ap.add_argument("--as-of", default=None, help="effective date YYYY-MM-DD for the lookups")
    a = ap.parse_args()
    s = load_settings()
    if not s.live_ready:
        print("Set ORACLE_FUSION_BASE_URL, ORACLE_USERNAME and ORACLE_PASSWORD in .env first.")
        return 2
    values = [v.strip() for v in a.values.split(",") if v.strip()]
    if not values and not a.from_absences:
        print("Give --values or --from-absences N.")
        return 2
    env = {**os.environ, **{k: v for k, v in mcp_env(s).items() if v}}
    params = StdioServerParameters(command="uv", args=["run", "--directory", str(s.mcp_dir.resolve()), "server.py"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), 180)

            async def call(name, args):
                return unwrap(await asyncio.wait_for(session.call_tool(name, args), 120))
            if a.from_absences:
                since = (dt.date.today() - dt.timedelta(days=180)).isoformat()
                try:
                    rows = rows_of(await call("query_resource", {"resource": "absences", "fields": ["personNumber"],
                                                                 "q": f"startDate >= {since}", "limit": max(a.from_absences * 3, 30)}))
                except ToolError as e:
                    print(f"Could not read absences: {e}")
                    return 1
                values = values_of(rows, "personNumber")[:a.from_absences]
            print(f"Looking up {len(values)} people in {a.resource} by {a.key}: {', '.join(values)}\n")
            results = await probe_lookup(call, a.resource, a.key, [f.strip() for f in a.fields.split(",")], values, a.as_of)
    print(f"{'way of asking':40} {'returned':>10}  {'calls':>5}  missing (person numbers)")
    for r in results:
        print(f"{r.label:40} {len(r.found):>4} of {len(r.asked):<3} {r.calls:>5}  {', '.join(r.missing[:10]) or 'none'}")
        for e in r.errors[:2]:
            print(f"    error: {e}")
    print()
    for line in advise(results):
        print("=> " + line)
    return 0


sys.exit(asyncio.run(main()))

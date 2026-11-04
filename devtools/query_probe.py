"""Try one read and see what the pod does with it: is this sort order accepted, are the rows really in order?

    python devtools/query_probe.py --resource absences --fields personNumber,startDate --show startDate \
        --order-by "startDate:desc" --order-by "-startDate" --order-by "startDate desc" --q "startDate >= 2026-01-01"

It makes one call without a sort and one per --order-by spelling, and prints for each: accepted or rejected, how many rows, and the
values of the fields you chose with --show, with a verdict on whether they came back in order. Only --show fields are printed, so no
name appears unless you ask for one. Add --finance for a finance resource (it has no order_by).
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

from bridge.config import load_settings                                    # noqa: E402
from bridge.live.session import mcp_env                                    # noqa: E402
from bridge.mcp_util import unwrap                                         # noqa: E402
from bridge.queryprobe import probe_orderings, verdict                     # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resource", required=True)
    ap.add_argument("--q", default=None, help="filter; HR conditions are joined with a semicolon")
    ap.add_argument("--fields", default="", help="comma list of fields to read")
    ap.add_argument("--show", default="", help="comma list of fields whose values to print (the first is judged for order)")
    ap.add_argument("--order-by", action="append", default=[], help="a sort spelling to try; repeat for several")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--effective-date", default=None)
    ap.add_argument("--finance", action="store_true", help="a finance resource (fusion_query_resource)")
    ap.add_argument("--ascending", action="store_true", help="judge for ascending order instead of descending")
    a = ap.parse_args()
    s = load_settings()
    if not s.live_ready:
        print("Set ORACLE_FUSION_BASE_URL, ORACLE_USERNAME and ORACLE_PASSWORD in .env first.")
        return 2
    show = [x.strip() for x in a.show.split(",") if x.strip()]
    fields = [x.strip() for x in a.fields.split(",") if x.strip()]
    if show and fields and not all(x.lower() in {f.lower() for f in fields} for x in show):
        fields += [x for x in show if x.lower() not in {f.lower() for f in fields}]
    env = {**os.environ, **{k: v for k, v in mcp_env(s).items() if v}}
    params = StdioServerParameters(command="uv", args=["run", "--directory", str(s.mcp_dir.resolve()), "server.py"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), 180)

            async def call(name, args):
                return unwrap(await asyncio.wait_for(session.call_tool(name, args), 120))
            results = await probe_orderings(call, a.resource, a.order_by, show, a.q, fields or None, a.limit, a.effective_date)
    for r in results:
        print(f"\n{r.label}")
        if not r.ok:
            print(f"   REJECTED: {r.error}")
            continue
        print(f"   accepted, {r.rows} row(s); attributes in each row: {', '.join(r.keys) or 'none'}")
        for f, vals in r.shown.items():
            print(f"   {f}: {', '.join(vals[:8])}" + (f"   -> {r.order}" if f == show[0] else ""))
    print()
    for line in verdict(results, not a.ascending):
        print("=> " + line)
    return 0


sys.exit(asyncio.run(main()))

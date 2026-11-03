"""Which of these fields does the pod really accept? Finds the one bad name in a list that makes a whole request fail.

    python devtools/field_probe.py ledgersLOV --map LEDGER_FIELD_MAP        # the fields the MCP's list_ledgers asks for
    python devtools/field_probe.py ledgersLOV --fields LedgerName,Name,CurrencyCode
    python devtools/field_probe.py ledgersLOV --tool list_ledgers           # also run the MCP's own typed tool once

It sends small read-only requests (one row each). The output holds field names and error text, no business data.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession, StdioServerParameters                       # noqa: E402
from mcp.client.stdio import stdio_client                                  # noqa: E402

from bridge.config import load_settings                                    # noqa: E402
from bridge.fieldprobe import field_map_keys, probe_fields                 # noqa: E402
from bridge.live.session import mcp_env                                    # noqa: E402
from bridge.mcp_util import ToolError, shorten_error, unwrap               # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("resource")
    ap.add_argument("--fields", default="", help="comma list of field names to test")
    ap.add_argument("--map", default="", help="name of a *_FIELD_MAP in the MCP's field_maps.py, e.g. LEDGER_FIELD_MAP")
    ap.add_argument("--tool", default="", help="also call this typed tool once with no arguments, e.g. list_ledgers")
    a = ap.parse_args()
    s = load_settings()
    if not s.live_ready:
        print("Set ORACLE_FUSION_BASE_URL, ORACLE_USERNAME and ORACLE_PASSWORD in .env first.")
        return 2
    fields = [f.strip() for f in a.fields.split(",") if f.strip()]
    if a.map:
        fields += field_map_keys(s.mcp_dir / "oracle_fusion_mcp" / "oracle" / "field_maps.py", a.map)
    if not fields and not a.tool:
        print("Give --fields, --map or --tool.")
        return 2
    env = {**os.environ, **{k: v for k, v in mcp_env(s).items() if v}}
    params = StdioServerParameters(command="uv", args=["run", "--directory", str(s.mcp_dir.resolve()), "server.py"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), 180)

            async def call(name, args):
                return unwrap(await asyncio.wait_for(session.call_tool(name, args), 120))
            if a.tool:
                try:
                    out = await call(a.tool, {"limit": 1})
                    print(f"Typed tool {a.tool}: OK ({len(out.get('items', out.get('records', []))) if isinstance(out, dict) else '?'} row)")
                except ToolError as e:
                    print(f"Typed tool {a.tool}: FAILED\n   {shorten_error(str(e), 400)}")
            if fields:
                print(f"\nTesting {len(fields)} field(s) on {a.resource}, one at a time...")
                r = await probe_fields(call, a.resource, fields)
                for f, ok, err in r["fields"]:
                    print(f"  {'OK ' if ok else 'BAD'} {f}" + (f"   {err}" if err else ""))
                print(f"\nAll together: {'OK' if r['together_ok'] else 'FAILED  ' + r['together_error']}")
                if r["bad"]:
                    print("\nRejected by this pod: " + ", ".join(r["bad"]) +
                          ".\nA request that includes any of them fails as a whole, which is what the MCP's own tool does.")
                else:
                    print("\nEvery field is accepted on its own. If the request still fails, try the fields in pairs, or ask the pod owner.")
    return 0


sys.exit(asyncio.run(main()))

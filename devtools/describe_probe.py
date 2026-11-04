"""Look inside a Fusion resource's description, straight from the pod, without a model.

    python devtools/describe_probe.py invoices                       # size, structure, and anything about "hold"
    python devtools/describe_probe.py invoices --grep hold,block     # other words
    python devtools/describe_probe.py invoices --children            # list the child resources (compact)
    python devtools/describe_probe.py invoices --child lines         # describe one child only
    python devtools/describe_probe.py workers --hr                   # an HR resource
    python devtools/describe_probe.py invoices --save invoices.json  # keep the full description to read yourself

The description holds field names and types, not business data. It tells you what the bot can actually see:
how big the description is (too big for the bot's 25,000-token limit?), the child resources, and where a word appears.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.logsafe import safe_streams     # noqa: E402
safe_streams()

from mcp import ClientSession, StdioServerParameters                       # noqa: E402
from mcp.client.stdio import stdio_client                                  # noqa: E402

from bridge.config import load_settings                                    # noqa: E402
from bridge.describeprobe import child_names, find_hits, size_report       # noqa: E402
from bridge.live.session import mcp_env                                    # noqa: E402
from bridge.mcp_util import ToolError, unwrap                              # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("resource")
    ap.add_argument("--child", default=None, help="describe only this child resource")
    ap.add_argument("--grep", default="hold", help="comma list of words to look for (default: hold)")
    ap.add_argument("--children", action="store_true", help="list child resources with fusion_find_relationships")
    ap.add_argument("--hr", action="store_true", help="the resource is an HR resource (uses describe_resource)")
    ap.add_argument("--save", default=None, help="write the full description to this file")
    ap.add_argument("--max-hits", type=int, default=40)
    a = ap.parse_args()

    s = load_settings()
    if not s.live_ready:
        print("Set ORACLE_FUSION_BASE_URL, ORACLE_USERNAME and ORACLE_PASSWORD in .env first.")
        return 2
    env = {**os.environ, **{k: v for k, v in mcp_env(s).items() if v}}
    params = StdioServerParameters(command="uv", args=["run", "--directory", str(s.mcp_dir.resolve()), "server.py"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), 180)
            if a.children:
                tool, args = "fusion_find_relationships", {"source_resource": a.resource}
            elif a.hr:
                tool, args = "describe_resource", {"resource": a.resource}
            else:
                tool, args = "fusion_describe_resource", {"resource": a.resource, **({"child": a.child} if a.child else {})}
            print(f"Calling {tool} {json.dumps(args)} ...")
            try:
                payload = unwrap(await asyncio.wait_for(session.call_tool(tool, args), 180))
            except ToolError as e:
                print(f"\nFAILED: {e}")
                return 1
    rep = size_report(payload, s.max_mcp_output_tokens)
    print(f"\nSize: {rep['chars']:,} characters, about {rep['approx_tokens']:,} tokens "
          f"(the bot's limit for one result is {s.max_mcp_output_tokens:,}).")
    print("The bot " + ("can read this in one go." if rep["fits_in_one_result"] else "CANNOT read this whole: it becomes an unreadable file."))
    if rep["top_level"]:
        print("\nTop level:")
        for k, v in rep["top_level"].items():
            print(f"  {k:28} {v['type']:6} {'' if v['items'] is None else str(v['items']) + ' items,'} {v['chars']:,} chars")
    kids = child_names(payload)
    if kids:
        print(f"\nChild resources ({len(kids)}): " + ", ".join(kids[:60]) + (" ..." if len(kids) > 60 else ""))
    words = [w.strip() for w in a.grep.split(",") if w.strip()]
    hits = find_hits(payload, words, a.max_hits)
    print(f"\nWhere {', '.join(words)} appears ({len(hits)} place(s)):")
    for h in hits:
        print("  " + h[:200])
    if not hits:
        print("  nowhere in this description. Try --children, or other words with --grep.")
    if a.save:
        Path(a.save).write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"\nSaved the full description to {a.save}")
    return 0


sys.exit(asyncio.run(main()))

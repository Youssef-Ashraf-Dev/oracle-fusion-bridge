"""Launch the vendor MCP exactly as the bridge does and compare its tool list with the policy.

Run after every bundle upgrade. Any tool not in bridge/live/permissions.py is denied by default;
this tells you which ones, so you can classify them on purpose.
    python scripts/check_mcp.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession, StdioServerParameters           # noqa: E402
from mcp.client.stdio import stdio_client                       # noqa: E402

from bridge.config import load_settings                         # noqa: E402
from bridge.live import permissions as P                        # noqa: E402
from bridge.live.session import mcp_env                         # noqa: E402


async def main() -> int:
    s = load_settings()
    env = dict(os.environ)
    env.update({k: v for k, v in mcp_env(s).items() if v})
    for k in ("ORACLE_FUSION_BASE_URL", "HCM_BASE_URL"):
        env.setdefault(k, "https://example.invalid")          # listing tools makes no Oracle call
    for k in ("ORACLE_USERNAME", "ORACLE_PASSWORD", "HCM_USERNAME", "HCM_PASSWORD"):
        env.setdefault(k, "check")
    env.setdefault("MCP_MODE", "readonly")
    params = StdioServerParameters(command="uv", args=["run", "--directory", str(s.mcp_dir.resolve()), "server.py"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
    new, gone = sorted(names - P.KNOWN), sorted(P.KNOWN - names)
    print(f"{len(names)} tools. mode=readonly environment={env['MCP_ENVIRONMENT']}")
    print("NEW (denied until you classify them):", new or "none")
    print("MISSING from bundle:", gone or "none")
    return 1 if new or gone else 0


sys.exit(asyncio.run(main()))

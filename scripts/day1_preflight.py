"""Day 1, step 1: prove the setup before spending any model credit.

    python scripts/day1_preflight.py

Reads the same environment as the bridge (.env values exported, or set in the shell).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.config import load_settings                                   # noqa: E402
from bridge.preflight import (FAIL, check_anthropic, check_config, check_mcp, check_runtime,  # noqa: E402
                              check_sdk, format_checks)


async def main() -> int:
    s = load_settings()
    checks = check_runtime() + check_config(s) + check_anthropic(s)
    if not any(c.status == FAIL and c.name.startswith(("Required", "Pod guard")) for c in checks):
        print("Starting the MCP (the first run builds its environment and can take a minute)...")
        checks += await check_mcp(s)
        print("Starting the Claude Code engine with your options (no model call is paid for)...")
        checks += await check_sdk(s)
    print("\n" + format_checks(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


sys.exit(asyncio.run(main()))

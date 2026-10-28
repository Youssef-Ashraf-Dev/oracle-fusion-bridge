"""Day 1 preflight: prove the setup works before spending any model credit.

Every check that reaches Anthropic uses a free endpoint, and every check that reaches Fusion goes through
the MCP exactly as the bridge launches it.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

from .config import Settings
from .live import permissions as P
from .live.session import mcp_env
from .mcp_util import ToolError, unwrap

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


def http_get(url: str, headers: dict) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                         # DNS, TLS, proxy
        return 0, f"{type(e).__name__}: {e}"


def check_runtime(which: Callable[[str], Optional[str]] = shutil.which) -> list[Check]:
    ok = sys.version_info >= (3, 11)
    return [Check("Python 3.11 or newer", PASS if ok else FAIL, sys.version.split()[0]),
            Check("uv on PATH (launches the MCP)", PASS if which("uv") else FAIL,
                  which("uv") or "install uv: https://docs.astral.sh/uv/")]


def check_config(s: Settings) -> list[Check]:
    out: list[Check] = []
    missing = [n for n, v in (("ANTHROPIC_API_KEY", s.anthropic_api_key), ("ORACLE_FUSION_BASE_URL", s.oracle_base_url),
                              ("ORACLE_USERNAME", s.oracle_user), ("ORACLE_PASSWORD", s.oracle_password)) if not v]
    out.append(Check("Required settings present", FAIL if missing else PASS,
                     ("missing: " + ", ".join(missing)) if missing else "ANTHROPIC_API_KEY, ORACLE_* set"))
    host = urlparse(s.oracle_base_url).netloc
    out.append(Check("Pod host (must be NON-PRODUCTION)", WARN if host else FAIL,
                     f"{host or 'not set'}  <- confirm this is the non-production pod before continuing"))
    allow = [a.strip().lower() for a in s.oracle_allowed_base_urls.split(",") if a.strip()]
    if not allow:
        out.append(Check("Pod guard ORACLE_ALLOWED_BASE_URLS", WARN, "empty: the MCP will not check the pod URL"))
    elif not any(a in s.oracle_base_url.lower() for a in allow):
        out.append(Check("Pod guard ORACLE_ALLOWED_BASE_URLS", FAIL,
                         "none of the allowed substrings appears in ORACLE_FUSION_BASE_URL; the MCP will refuse to start"))
    else:
        out.append(Check("Pod guard ORACLE_ALLOWED_BASE_URLS", PASS, ", ".join(allow)))
    out.append(Check("MCP_ENVIRONMENT", FAIL if s.mcp_environment.lower() == "prod" else PASS, s.mcp_environment))
    out.append(Check("MCP_PROD_WRITES_ENABLED is off", FAIL if s.mcp_prod_writes_enabled else PASS,
                     "true" if s.mcp_prod_writes_enabled else "false"))
    return out


def check_anthropic(s: Settings, get: Callable[[str, dict], tuple[int, str]] = http_get) -> list[Check]:
    if not s.anthropic_api_key:
        return [Check("Anthropic API key", FAIL, "ANTHROPIC_API_KEY is not set")]
    status, body = get("https://api.anthropic.com/v1/models?limit=1000",
                       {"x-api-key": s.anthropic_api_key, "anthropic-version": "2023-06-01"})
    if status != 200:
        hint = {401: "the key was rejected", 403: "the key is not allowed to list models", 0: "network problem"}.get(status, "")
        return [Check("Anthropic API key", FAIL, f"HTTP {status} {hint} {body[:120]}".strip())]
    try:
        ids = [m["id"] for m in json.loads(body).get("data", [])]
    except Exception:
        return [Check("Anthropic API key", WARN, "key accepted but the model list could not be read")]
    out = [Check("Anthropic API key", PASS, f"accepted; {len(ids)} models visible")]
    out.append(Check(f"Model {s.model} is available", PASS if s.model in ids else FAIL,
                     "found" if s.model in ids else "not in the list; set MODEL to one of: " + ", ".join(ids[:8])))
    return out


async def _call(session, name: str, args: Optional[dict] = None, timeout: float = 120):
    res = await asyncio.wait_for(session.call_tool(name, args or {}), timeout)
    return unwrap(res)


async def check_mcp(s: Settings, extra_env: Optional[dict] = None) -> list[Check]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    out: list[Check] = []
    env = {**os.environ, **{k: v for k, v in mcp_env(s).items() if v}, **(extra_env or {})}
    params = StdioServerParameters(command="uv", args=["run", "--directory", str(s.mcp_dir.resolve()), "server.py"], env=env)
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 180)           # first run builds the uv environment
                names = {t.name for t in (await session.list_tools()).tools}
                new, gone = sorted(names - P.KNOWN), sorted(P.KNOWN - names)
                out.append(Check("MCP starts and lists its tools", PASS if not (new or gone) else WARN,
                                 f"{len(names)} tools" + (f"; NEW (denied until classified): {new}" if new else "")
                                 + (f"; MISSING: {gone}" if gone else "")))
                try:
                    info = await _call(session, "server_info")
                    feat = info.get("features", {})
                    out.append(Check("server_info", PASS if not feat.get("writes_enabled") else WARN,
                                     f"pod_host={info.get('pod_host')} auth={info.get('auth_type')} "
                                     f"writes_enabled={feat.get('writes_enabled')} redaction={info.get('redaction_active')}"))
                except Exception as e:
                    out.append(Check("server_info", FAIL, str(e)[:200]))
                try:
                    await _call(session, "test_oracle_connection")
                    out.append(Check("Fusion login and pod reachable (test_oracle_connection)", PASS, "connected"))
                except (ToolError, asyncio.TimeoutError, Exception) as e:
                    out.append(Check("Fusion login and pod reachable (test_oracle_connection)", FAIL, str(e)[:200] or "timed out"))
                try:
                    res = await _call(session, "fusion_list_resources", timeout=180)
                    avail = [r.get("resource", r) if isinstance(r, dict) else r for r in res.get("resources", [])]
                    bad = res.get("unavailable", [])
                    out.append(Check("Finance resources visible on the pod", PASS if avail else WARN,
                                     f"{len(avail)} available, {len(bad)} unavailable; e.g. {', '.join(map(str, avail[:6]))}"))
                except Exception as e:
                    out.append(Check("Finance resources visible on the pod", WARN, str(e)[:200]))
                try:
                    cap = await _call(session, "get_capabilities")
                    out.append(Check("HR modules reported", PASS, json.dumps(cap)[:160]))
                except Exception as e:
                    out.append(Check("HR modules reported", WARN, str(e)[:200]))
    except Exception as e:
        out.append(Check("MCP starts and lists its tools", FAIL, f"{type(e).__name__}: {str(e)[:200]}"))
    return out


async def check_sdk(s: Settings, timeout: float = 240) -> list[Check]:
    """Start the bundled CLI with the bridge's exact options and read its startup report.

    The key is deliberately invalid, so the model call that follows fails with an authentication error and
    nothing is spent. What it proves: the CLI accepts our options, both MCP servers connect, the model is offered
    exactly the tools we intend, and the planner skill is discovered the way chat would discover it.
    """
    import dataclasses
    import warnings

    from claude_agent_sdk import ClaudeSDKClient, ResultMessage, SystemMessage

    from .live.session import RunContext, build_options
    from .state.db import Database

    async def approve(_plan):
        return True

    out: list[Check] = []
    db = await Database(":memory:").open()
    client = None
    init: dict = {}
    try:
        probe = dataclasses.replace(s, anthropic_api_key="sk-ant-preflight-no-credit")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            opts = build_options(probe, RunContext(probe, db, 0, approve), None)
        client = ClaudeSDKClient(opts)
        await asyncio.wait_for(client.connect(), timeout)
        await client.query("ping")

        async def first_init():
            async for m in client.receive_response():
                if isinstance(m, SystemMessage) and m.subtype == "init" and isinstance(m.data, dict):
                    return m.data
                if isinstance(m, ResultMessage):
                    return {}
            return {}
        init = await asyncio.wait_for(first_init(), timeout)
    except Exception as e:
        out.append(Check("Agent SDK starts the CLI with the bridge's options", FAIL, f"{type(e).__name__}: {str(e)[:200]}"))
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        await db.close()
    if not init:
        return out or [Check("Agent SDK starts the CLI with the bridge's options", FAIL, "no startup report received")]

    out.append(Check("Agent SDK starts the CLI with the bridge's options", PASS,
                     f"Claude Code {init.get('claude_code_version', '?')}, model {init.get('model')}; no credit spent"))
    servers = {m.get("name"): m.get("status") for m in init.get("mcp_servers", [])}
    bad = {k: v for k, v in servers.items() if v != "connected"}
    out.append(Check("Both MCP servers connect (fusion, bridge)", PASS if not bad and len(servers) == 2 else
                     (WARN if all(v == "pending" for v in bad.values()) and bad else FAIL),
                     ", ".join(f"{k}={v}" for k, v in servers.items()) + ("  (pending: the first start builds the MCP environment; rerun)" if bad else "")))
    tools = init.get("tools", [])
    fusion = [t for t in tools if t.startswith(P.PREFIX)]
    other = sorted(t for t in tools if not t.startswith("mcp__"))
    expected = ["Skill"] if s.effective_skill_mode == "native" else []
    out.append(Check("Fusion tools offered to the model", PASS if len(fusion) == len(P.KNOWN) else WARN,
                     f"{len(fusion)} (policy knows {len(P.KNOWN)})"))
    out.append(Check("No built-in tools except what is intended", PASS if other == expected else FAIL,
                     f"built-in tools offered: {other or 'none'}; expected: {expected or 'none'}"))
    if s.effective_skill_mode == "native":
        from .live.skills import skill_name
        name = skill_name(s.planner_skill)
        out.append(Check(f"Planner skill {name} discovered (native, as in chat)", PASS if name in init.get("skills", []) else FAIL,
                         "listed in the CLI's startup report" if name in init.get("skills", []) else "not found under the runtime folder"))
    else:
        out.append(Check("Planner skill", PASS, f"mode {s.effective_skill_mode}: " + (
            "pasted into the system prompt" if s.effective_skill_mode == "inline" else "not used (set PLANNER_SKILL_PATH to load it)")))
    return out


def format_checks(checks: list[Check]) -> str:
    lines = [f"{c.status:4}  {c.name}" + (f"\n      {c.detail}" if c.detail else "") for c in checks]
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    lines.append("")
    lines.append("READY for the Day 1 spike" + (f" ({warns} warning(s) to read)" if warns else "") if not fails
                 else f"NOT READY: {fails} check(s) failed")
    return "\n".join(lines)

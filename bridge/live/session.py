"""Live pipeline: one Agent SDK session per question, the MCP as a local stdio subprocess.

Everything the agent can do goes through can_use_tool (default deny) or the one in-process
bridge tool, propose_query_plan. Approval wait time is excluded from the compute timeout (NFR-07).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow,
                              PermissionResultDeny, ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage, create_sdk_mcp_server, tool)

import warnings

from ..config import Settings
from ..state.db import Database
from . import permissions as P
from .plan import Approver, PlanError, PlanState, parse_plan
from .prompt import build_system_prompt
from .skills import prepare_runtime, skill_name

try:    # By design the discovery and compute tools are pre-approved, so the callback is shadowed for them.
    from claude_agent_sdk import CanUseToolShadowedWarning
    warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)
except ImportError:
    pass

BUILTIN_TOOLS = ["Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
                 "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite", "SlashCommand", "ExitPlanMode"]

StatusFn = Callable[[str], Awaitable[None]]


def mcp_env(s: Settings) -> dict[str, str]:
    """Environment for the vendor MCP. Read-only mode and the pod pin are set here, not left to chance."""
    return {
        "ORACLE_FUSION_BASE_URL": s.oracle_base_url, "ORACLE_USERNAME": s.oracle_user,
        "ORACLE_PASSWORD": s.oracle_password, "ORACLE_FUSION_API_VERSION": s.oracle_api_version,
        "HCM_BASE_URL": s.oracle_base_url, "HCM_USERNAME": s.oracle_user,
        "HCM_PASSWORD": s.oracle_password, "HCM_REST_VERSION": s.oracle_api_version,
        "MCP_MODE": "readonly", "MCP_ENVIRONMENT": s.mcp_environment,
        "ORACLE_ALLOWED_BASE_URLS": s.oracle_allowed_base_urls or s.oracle_base_url,
        "MCP_PROD_WRITES_ENABLED": "true" if s.mcp_prod_writes_enabled else "false",
    }


@dataclass
class LiveResult:
    status: str = "error"            # ok | no_answer | denied | timeout | budget | error
    answer: str = ""                 # the agent's final text, relayed to the user (FR-09)
    session_id: Optional[str] = None
    cost_usd: float = 0.0
    duration_s: float = 0.0
    turns: int = 0
    data_calls: int = 0
    tool_calls: list[dict] = field(default_factory=list)      # [{"name": ..., "input": ...}] in order
    tool_errors: list[str] = field(default_factory=list)      # failures seen in tool results
    usage: dict = field(default_factory=dict)                 # token counts reported by the SDK
    message: str = ""


@dataclass
class RunContext:
    """Everything one question needs. Plain async methods so they can be tested without the SDK."""
    settings: Settings
    db: Database
    user_id: int
    approver: Approver
    state: PlanState = field(default_factory=PlanState)
    approval_wait: float = 0.0
    waiting_since: Optional[float] = None      # set while a plan is awaiting the user
    user_denied: bool = False
    skill: str = ""                            # the one skill the agent may load (native mode)

    async def propose_query_plan(self, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            plan = parse_plan(args, self.settings.allow_hcm_sensitive)
        except PlanError as e:
            return f"Plan rejected: {e}. Fix it and propose again.", True
        self.waiting_since = time.monotonic()
        try:
            ok = await asyncio.wait_for(self.approver(plan), self.settings.approval_timeout_s)
            why = "approved" if ok else "denied"
        except asyncio.TimeoutError:
            ok, why = False, "timeout"
        finally:
            self.approval_wait += time.monotonic() - self.waiting_since
            self.waiting_since = None
        await self.db.record_plan(self.user_id, {"summary": plan.summary, "steps": [s.__dict__ for s in plan.steps]},
                                  why, self.user_id if ok or why == "denied" else None)
        await self.db.audit(self.user_id, f"plan_{why}", plan.render())
        if ok:
            self.state.approved.append(plan)
            return "Plan approved. Run the planned calls now.", False
        self.user_denied = True
        return "The user did not approve the plan. Stop now and do not retry.", True

    def approval_seconds(self) -> float:
        """Total time spent waiting on the user, including a wait still in progress."""
        live = (time.monotonic() - self.waiting_since) if self.waiting_since is not None else 0.0
        return self.approval_wait + live

    async def can_use_tool(self, tool_name: str, tool_input: dict[str, Any], _ctx: Any = None):
        if self.user_denied:
            return PermissionResultDeny(message="The user declined; stop.", interrupt=True)
        if tool_name == "Skill":                           # only the vendor skill, never a built-in one
            if self.skill and any(self.skill in str(v) for v in tool_input.values()):
                return PermissionResultAllow(updated_input=tool_input)
            await self.db.audit(self.user_id, "tool_denied", f"Skill {_short(tool_input)} :: only {self.skill or 'none'} may be loaded")
            return PermissionResultDeny(message=f"Only the {self.skill} skill is available.")
        d = P.classify(tool_name, self.settings.allow_hcm_sensitive)
        if d.verdict == "deny":
            await self.db.audit(self.user_id, "tool_denied", f"{tool_name} {_short(tool_input)} :: {d.reason}")
            return PermissionResultDeny(message=d.reason)
        if d.verdict == "needs_plan" and P.bare(tool_name) in P.GENERIC_READERS and not self.settings.allow_hcm_sensitive:
            hit = P.touches_sensitive_resource(tool_input)
            if hit:
                why = f"{hit} holds pay or identity data, which is not available"
                await self.db.audit(self.user_id, "tool_denied", f"{tool_name} {_short(tool_input)} :: {why}")
                return PermissionResultDeny(message=why)
        if d.verdict == "needs_plan":
            ok, why = self.state.covers(tool_name, tool_input)
            if not ok:
                await self.db.audit(self.user_id, "tool_denied", f"{tool_name} {_short(tool_input)} :: {why}")
                return PermissionResultDeny(message=why)
            self.state.note_call()
            await self.db.audit(self.user_id, "data_call", f"{tool_name} {_short(tool_input)}")
        return PermissionResultAllow(updated_input=tool_input)


def _short(d: Any, n: int = 300) -> str:
    return json.dumps(d, default=str)[:n]


def _result(text: str, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


def build_options(s: Settings, ctx: RunContext, resume: Optional[str]) -> ClaudeAgentOptions:
    @tool("propose_query_plan", "Propose the data calls you need; the user approves once. Required before any data tool.",
          {"type": "object", "required": ["summary", "steps"], "properties": {
              "summary": {"type": "string"},
              "steps": {"type": "array", "items": {"type": "object", "required": ["tool"], "properties": {
                  "tool": {"type": "string"},
                  "resource": {"type": "string", "description": "Only for fusion_query_resource, fusion_get_resource_record, query_resource and get_record. Leave it out for every other tool."},
                  "note": {"type": "string", "description": "Filters, fields and limit, for the user to read."}}}}}})
    async def propose(args):
        return _result(*(await ctx.propose_query_plan(args)))

    bridge = create_sdk_mcp_server(name=P.BRIDGE_SERVER, tools=[propose])
    fusion = {"type": "stdio", "command": "uv", "args": ["run", "--directory", str(s.mcp_dir.resolve()), "server.py"],
              "env": mcp_env(s)}
    mode = s.effective_skill_mode
    name = skill_name(s.planner_skill) if mode != "none" else ""
    kw: dict[str, Any] = {}
    if mode == "native":
        # The same mechanism chat uses: <cwd>/.claude/skills/<name>/SKILL.md, loaded by the Skill tool.
        # `skills=[name]` hides the CLI's built-in skills from the model.
        kw.update(cwd=str(prepare_runtime(s.planner_skill, s.runtime_dir).resolve()), setting_sources=["project"],
                  tools=["Skill"], skills=[name])
        ctx.skill = name
    else:
        kw.update(tools=[], setting_sources=[])
    if s.effort:
        kw["effort"] = s.effort
    return ClaudeAgentOptions(
        model=s.model,
        system_prompt=build_system_prompt(s.planner_skill, mode, name),
        disallowed_tools=BUILTIN_TOOLS,
        allowed_tools=P.sdk_allowed_tools(s.allow_hcm_sensitive),
        mcp_servers={P.SERVER: fusion, P.BRIDGE_SERVER: bridge},
        strict_mcp_config=True,
        permission_mode="default", can_use_tool=ctx.can_use_tool,
        max_budget_usd=s.run_budget_usd, max_turns=s.max_turns,
        env={"ANTHROPIC_API_KEY": s.anthropic_api_key,
             # Chat loads every tool up front. The engine defers tools behind a search tool by default, which
             # needs a built-in tool we do not enable and is unsupported on Haiku models.
             "ENABLE_TOOL_SEARCH": "false",
             # Results over this many tokens are written to a file the agent cannot read.
             "MAX_MCP_OUTPUT_TOKENS": str(s.max_mcp_output_tokens)},
        resume=resume, **kw,
    )


def _tool_result_texts(block: ToolResultBlock) -> str:
    c = block.content
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in c)
    return ""


ClientFactory = Callable[[ClaudeAgentOptions], Any]


class LiveRunner:
    def __init__(self, settings: Settings, db: Database, approver: Approver,
                 client_factory: Optional[ClientFactory] = None, tick_s: float = 1.0):
        self.s, self.db, self.approver = settings, db, approver
        self.factory = client_factory or (lambda opts: ClaudeSDKClient(opts))
        self.tick_s = tick_s
        self.last_ctx: Optional[RunContext] = None

    async def run(self, user_id: int, question: str, resume: Optional[str] = None,
                  on_status: Optional[StatusFn] = None) -> LiveResult:
        ctx = self.last_ctx = RunContext(self.s, self.db, user_id, self.approver)
        out = LiveResult()
        t0 = time.monotonic()
        client = self.factory(build_options(self.s, ctx, resume))
        queue: asyncio.Queue = asyncio.Queue()

        async def pump():
            try:
                await client.connect()
                await client.query(question)
                async for m in client.receive_response():
                    await queue.put(m)
            except Exception as e:                       # surfaced to the consumer below
                await queue.put(e)
            finally:
                await queue.put(None)

        task = asyncio.create_task(pump())
        texts: list[str] = []
        result_text = ""
        last_status = 0.0
        try:
            while True:
                compute = time.monotonic() - t0 - ctx.approval_seconds()
                if compute > self.s.compute_timeout_s:
                    out.status, out.message = "timeout", "The run took too long and was stopped."
                    try:
                        await client.interrupt()
                    except Exception:
                        pass
                    break
                try:
                    m = await asyncio.wait_for(queue.get(), self.tick_s)
                except asyncio.TimeoutError:
                    if on_status and compute - last_status >= 10:
                        last_status = compute
                        await on_status(f"Working... {int(compute)}s")
                    continue
                if m is None:
                    break
                if isinstance(m, Exception):
                    out.status, out.message = "error", f"{type(m).__name__}: {m}"
                    break
                if isinstance(m, AssistantMessage):
                    for b in m.content:
                        if isinstance(b, TextBlock):
                            texts.append(b.text)
                        elif isinstance(b, ToolUseBlock):
                            out.tool_calls.append({"name": P.bare(b.name), "input": _short(b.input, 200)})
                elif isinstance(m, UserMessage) and isinstance(m.content, list):
                    for b in m.content:
                        if isinstance(b, ToolResultBlock):
                            body = _tool_result_texts(b)
                            if b.is_error or '"error"' in body[:400]:
                                out.tool_errors.append(body[:240])
                elif isinstance(m, SystemMessage) and not out.session_id:
                    out.session_id = (m.data or {}).get("session_id") if isinstance(m.data, dict) else None
                elif isinstance(m, ResultMessage):
                    out.cost_usd = float(m.total_cost_usd or 0.0)
                    out.session_id = m.session_id or out.session_id
                    out.turns = m.num_turns
                    out.usage = dict(m.usage or {})
                    result_text = (m.result or "").strip()
                    if m.subtype == "error_max_budget_usd":
                        out.status, out.message = "budget", "The run hit its cost limit."
                    elif m.subtype == "error_max_turns":
                        out.status, out.message = "turns", "The run hit its step limit."
                    elif m.is_error:
                        out.status = "error"
                        out.message = "; ".join(str(e) for e in (m.errors or [])) or "The agent run failed."
        finally:
            task.cancel()
            try:
                await client.disconnect()
            except Exception:
                pass
        out.duration_s = round(time.monotonic() - t0 - ctx.approval_seconds(), 2)     # time working, not time waiting for the tap
        out.data_calls = ctx.state.data_calls
        out.answer = result_text or (texts[-1].strip() if texts else "")
        if out.status == "error" and not out.message:           # no failure signalled: derive the outcome
            out.status = "denied" if ctx.user_denied else ("ok" if out.answer else "no_answer")
        await self.db.audit(user_id, "ask_finished",
                            f"{out.status} cost={out.cost_usd:.4f} turns={out.turns} data_calls={out.data_calls}")
        return out

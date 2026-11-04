"""Live pipeline: one Agent SDK session per question, the MCP as a local stdio subprocess.

Everything the agent can do goes through can_use_tool (default deny) or the one in-process
bridge tool, propose_query_plan. Approval wait time is excluded from the compute timeout (NFR-07).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, PermissionResultAllow,
                              PermissionResultDeny, ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage, create_sdk_mcp_server, tool)

import warnings

from ..config import Settings
from ..mcp_util import payload_error, shorten_error
from ..state.db import Database
from . import permissions as P
from .plan import Approver, PlanError, PlanState, parse_plan
from .prompt import build_system_prompt, load_recipes
from .skills import prepare_runtime, skill_name

try:    # By design the discovery and compute tools are pre-approved, so the callback is shadowed for them.
    from claude_agent_sdk import CanUseToolShadowedWarning
    warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)
except ImportError:
    pass

log = logging.getLogger("bridge.policy")
_CUT = re.compile(r"(?i)(exceeds? (the )?maximum allowed|exceed|too large|too long|truncat|saved (to|in)|has been saved|written to|tool-results)")


def looks_cut(body: str) -> bool:
    """A short result that talks about size or a saved file: the engine replaced a too-large result with a note."""
    if body.lstrip().startswith(("{", "[")):                 # a JSON payload is a real result, whatever words it contains
        return False
    return len(body) < 1200 and bool(_CUT.search(body))


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
    data_attempts: int = 0
    tool_calls: list[dict] = field(default_factory=list)      # [{"name": ..., "input": ...}] in order
    tool_errors: list[str] = field(default_factory=list)      # failures seen in tool results
    usage: dict = field(default_factory=dict)                 # token counts reported by the SDK
    message: str = ""
    error_kind: str = ""             # the engine's own label: billing_error, authentication_failed, rate_limit, ...
    api_status: Optional[int] = None
    engine_lines: list[str] = field(default_factory=list)     # the last lines the engine wrote to stderr
    session_cost_total: float = 0.0  # the engine's running total for the whole conversation (cost_usd is this question only)
    plans: int = 0                   # plans shown to the user in this run
    results_cut: int = 0             # tool results the engine replaced with a note because they were too large


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
    plans_proposed: int = 0
    engine_lines: list = field(default_factory=list)

    async def propose_query_plan(self, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            plan = parse_plan(args, self.settings.allow_hcm_sensitive)
        except PlanError as e:
            return f"Plan rejected: {e}. Fix it and propose again.", True
        self.plans_proposed += 1
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

    async def _deny(self, tool_name: str, tool_input: dict[str, Any], why: str):
        await self.db.audit(self.user_id, "tool_denied", f"{tool_name} {_short(tool_input)} :: {why}")
        log.info("deny user=%s tool=%s reason=%s", self.user_id, P.bare(tool_name), why[:200])
        return PermissionResultDeny(message=why)

    async def can_use_tool(self, tool_name: str, tool_input: dict[str, Any], _ctx: Any = None):
        if self.user_denied:
            return PermissionResultDeny(message="The user declined; stop.", interrupt=True)
        if tool_name == "Skill":                           # only the vendor skill, never a built-in one
            if self.skill and any(self.skill in str(v) for v in tool_input.values()):
                return PermissionResultAllow(updated_input=tool_input)
            await self.db.audit(self.user_id, "tool_denied", f"Skill {_short(tool_input)} :: only {self.skill or 'none'} may be loaded")
            log.info("deny user=%s tool=Skill reason=only %s may be loaded", self.user_id, self.skill or "none")
            return PermissionResultDeny(message=f"Only the {self.skill} skill is available.")
        d = P.classify(tool_name, self.settings.allow_hcm_sensitive)
        if d.verdict == "deny":
            return await self._deny(tool_name, tool_input, d.reason)
        if d.verdict == "needs_plan" and P.bare(tool_name) in P.GENERIC_READERS and not self.settings.allow_hcm_sensitive:
            hit = P.touches_sensitive_resource(tool_input)
            if hit:
                return await self._deny(tool_name, tool_input, f"{hit} holds pay or identity data, which is not available")
        if d.verdict == "needs_plan" and P.workers_or_lookup(tool_name, tool_input):
            return await self._deny(tool_name, tool_input, P.WORKERS_OR_REASON)
        if d.verdict == "needs_plan":
            limit = tool_input.get("limit")
            if isinstance(limit, (int, float)) and limit > self.settings.max_rows_per_call:
                return await self._deny(tool_name, tool_input, f"limit {int(limit)} is too large: use "
                                        f"{self.settings.max_rows_per_call} or less and page with offset, or filter harder")
            ok, why = self.state.covers(tool_name, tool_input)
            if not ok:
                return await self._deny(tool_name, tool_input, why)
            call_id = getattr(_ctx, "tool_use_id", None)
            if call_id:
                self.state.start(call_id)                  # counted when its result arrives, and only if it succeeded
            else:
                self.state.note_call()
            await self.db.audit(self.user_id, "data_call", f"{tool_name} {_short(tool_input)}")
        return PermissionResultAllow(updated_input=tool_input)


def _short(d: Any, n: int = 300) -> str:
    return json.dumps(d, default=str)[:n]


def _result(text: str, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


def build_options(s: Settings, ctx: RunContext, resume: Optional[str], prior_cost: float = 0.0) -> ClaudeAgentOptions:
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
        system_prompt=build_system_prompt(s.planner_skill, mode, name, recipes=load_recipes(s.recipes_path)),
        disallowed_tools=BUILTIN_TOOLS,
        allowed_tools=P.sdk_allowed_tools(s.allow_hcm_sensitive),
        mcp_servers={P.SERVER: fusion, P.BRIDGE_SERVER: bridge},
        strict_mcp_config=True,
        permission_mode="default", can_use_tool=ctx.can_use_tool,
        max_budget_usd=s.run_budget_usd + prior_cost, max_turns=s.max_turns,      # the engine counts the whole conversation
        env={"ANTHROPIC_API_KEY": s.anthropic_api_key,
             # Chat loads every tool up front. The engine defers tools behind a search tool by default, which
             # needs a built-in tool we do not enable and is unsupported on Haiku models.
             "ENABLE_TOOL_SEARCH": "false",
             # Results over this many tokens are written to a file the agent cannot read.
             "MAX_MCP_OUTPUT_TOKENS": str(s.max_mcp_output_tokens),
             # The engine ships a telemetry plugin and update checks. A server that reads company data needs neither.
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
             "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
             # Keep the engine's saved conversations inside the bot's own data folder, not in the user profile.
             "CLAUDE_CONFIG_DIR": str(s.engine_dir.resolve())},
        stderr=_stderr_sink(ctx), resume=resume, **kw,
    )


def _stderr_sink(ctx: "RunContext"):
    """Keep the engine's own error lines: they say why a run failed before any message arrived."""
    elog = logging.getLogger("bridge.engine")

    def sink(line: str) -> None:
        line = line.rstrip()
        if line:
            ctx.engine_lines.append(line[:300])
            del ctx.engine_lines[:-20]
            elog.warning("engine: %s", line[:300])
    return sink


def _tool_result_texts(block: ToolResultBlock) -> str:
    c = block.content
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in c)
    return ""


def activity_label(tool_name: str) -> str:
    """What the user is told while a tool runs, in plain words."""
    if tool_name == "Skill":
        return "Reading the Fusion playbook"
    if tool_name.startswith(P.BRIDGE_PREFIX):
        return "Preparing the plan"
    name = P.bare(tool_name)
    if name in P.DISCOVERY:
        return "Checking what Fusion offers"
    if name in P.COMPUTE:
        return "Calculating"
    return "Reading from Fusion"


ClientFactory = Callable[[ClaudeAgentOptions], Any]


class LiveRunner:
    def __init__(self, settings: Settings, db: Database, approver: Approver,
                 client_factory: Optional[ClientFactory] = None, tick_s: float = 1.0, status_every_s: float = 10.0,
                 disconnect_timeout_s: float = 4.0):
        self.status_every_s = status_every_s
        self.disconnect_timeout_s = disconnect_timeout_s
        self.s, self.db, self.approver = settings, db, approver
        self.factory = client_factory or (lambda opts: ClaudeSDKClient(opts))
        self.tick_s = tick_s
        self.last_ctx: Optional[RunContext] = None

    async def run(self, user_id: int, question: str, resume: Optional[str] = None,
                  on_status: Optional[StatusFn] = None, prior_cost: float = 0.0) -> LiveResult:
        ctx = self.last_ctx = RunContext(self.s, self.db, user_id, self.approver)
        out = LiveResult()
        t0 = time.monotonic()
        client = self.factory(build_options(self.s, ctx, resume, prior_cost))
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
        activity = "Thinking"
        try:
            while True:
                compute = time.monotonic() - t0 - ctx.approval_seconds()
                if compute > self.s.compute_timeout_s:
                    out.status, out.message = "timeout", "The run took too long and was stopped."
                    try:
                        await client.interrupt()
                    except Exception:
                        pass
                    deadline = time.monotonic() + 5            # the engine reports its cost when it stops: wait briefly for it
                    while time.monotonic() < deadline:
                        try:
                            late = await asyncio.wait_for(queue.get(), 0.5)
                        except asyncio.TimeoutError:
                            continue
                        if late is None or isinstance(late, Exception):
                            break
                        if isinstance(late, ResultMessage):
                            out.cost_usd = float(late.total_cost_usd or 0.0)
                            out.turns = late.num_turns
                            out.usage = dict(late.usage or {})
                            out.session_id = late.session_id or out.session_id
                            break
                    break
                try:
                    m = await asyncio.wait_for(queue.get(), self.tick_s)
                except asyncio.TimeoutError:
                    if on_status and compute - last_status >= self.status_every_s:
                        last_status = compute
                        await on_status(f"{activity}... {int(compute)}s")
                    continue
                if m is None:
                    break
                if isinstance(m, Exception):
                    out.status, out.message = "error", f"{type(m).__name__}: {m}"
                    break
                if isinstance(m, AssistantMessage):
                    if getattr(m, "error", None):
                        out.error_kind = str(m.error)
                    for b in m.content:
                        if isinstance(b, TextBlock):
                            texts.append(b.text)
                        elif isinstance(b, ToolUseBlock):
                            out.tool_calls.append({"name": P.bare(b.name), "input": _short(b.input, 200)})
                            activity = activity_label(b.name)
                elif isinstance(m, UserMessage) and isinstance(m.content, list):
                    for b in m.content:
                        if isinstance(b, ToolResultBlock):
                            activity = "Reviewing the results"
                            body = _tool_result_texts(b)
                            err = payload_error(body)
                            if looks_cut(body):                              # counted even when the text also reads as an error
                                out.results_cut += 1
                                err = err or f"result too large for the engine to pass on: {body[:160]}"
                            failed = bool(b.is_error or err)
                            ctx.state.finish(b.tool_use_id, ok=not failed)
                            if failed:
                                out.tool_errors.append(shorten_error(err or body))
                elif isinstance(m, SystemMessage) and not out.session_id:
                    out.session_id = (m.data or {}).get("session_id") if isinstance(m.data, dict) else None
                elif isinstance(m, ResultMessage):
                    out.cost_usd = float(m.total_cost_usd or 0.0)
                    out.session_id = m.session_id or out.session_id
                    out.turns = m.num_turns
                    out.usage = dict(m.usage or {})
                    out.api_status = getattr(m, "api_error_status", None)
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
            try:                                               # stopping the engine must not hold up a shutdown for long
                await asyncio.wait_for(client.disconnect(), self.disconnect_timeout_s)
            except asyncio.TimeoutError:
                log.warning("The engine did not stop within %.0fs; moving on", self.disconnect_timeout_s)
            except Exception:
                pass
        out.duration_s = round(time.monotonic() - t0 - ctx.approval_seconds(), 2)     # time working, not time waiting for the tap
        out.session_cost_total = out.cost_usd                  # what the engine reported: the conversation's running total
        if prior_cost > 0 and out.cost_usd >= prior_cost:      # a resumed conversation: this question is the difference
            out.cost_usd = round(out.cost_usd - prior_cost, 6)
        out.plans = ctx.plans_proposed
        out.engine_lines = list(ctx.engine_lines[-10:])
        out.data_calls = ctx.state.data_calls                  # successful data calls
        out.data_attempts = ctx.state.attempts
        out.answer = result_text or (texts[-1].strip() if texts else "")
        if out.status == "error" and not out.message:           # no failure signalled: derive the outcome
            out.status = "denied" if ctx.user_denied else ("ok" if out.answer else "no_answer")
        await self.db.audit(user_id, "ask_finished",
                            f"{out.status} cost={out.cost_usd:.4f} turns={out.turns} data_calls={out.data_calls}")
        return out

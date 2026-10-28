import asyncio
import dataclasses

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

from bridge.live import permissions as P
from bridge.live.prompt import BRIDGE_RULES, build_system_prompt
from bridge.live.session import BUILTIN_TOOLS, LiveRunner, RunContext, build_options, mcp_env
from bridge.state.db import Database


def result(cost=0.1, session="sess-1", subtype="success", is_error=False, text="Answer.", usage=None):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=4,
                         session_id=session, total_cost_usd=cost, result=text, usage=usage or {})


class FakeSDK:
    def __init__(self, options, script):
        self.options, self.script, self.interrupted = options, script, False

    async def connect(self): pass
    async def query(self, q): self.question = q
    async def interrupt(self): self.interrupted = True
    async def disconnect(self): pass

    async def receive_response(self):
        async for m in self.script(self):
            yield m


async def make(tmp_path, settings, approver, script):
    db = await Database(tmp_path / "t.db").open()
    holder = {}
    def factory(opts):
        holder["client"] = FakeSDK(opts, script)
        return holder["client"]
    return db, LiveRunner(settings, db, approver, factory, tick_s=0.02), holder


async def yes(plan): return True
async def no(plan): return False


async def test_happy_path_plan_then_data_then_answer(tmp_path, settings):
    log = {}
    async def script(c):
        ctx = runner.last_ctx
        log["discovery"] = await c.options.can_use_tool(P.PREFIX + "fusion_list_resources", {}, None)
        log["early"] = await c.options.can_use_tool(P.PREFIX + "fusion_query_resource", {"resource": "invoices"}, None)
        log["plan"] = await ctx.propose_query_plan({"summary": "open invoices", "steps": [
            {"tool": "fusion_query_resource", "resource": "invoices", "note": "status=open"}]})
        log["data"] = await c.options.can_use_tool(P.PREFIX + "fusion_query_resource", {"resource": "invoices"}, None)
        log["other"] = await c.options.can_use_tool(P.PREFIX + "fusion_query_resource", {"resource": "payments"}, None)
        log["write"] = await c.options.can_use_tool(P.PREFIX + "fusion_execute_confirmed_write", {}, None)
        log["bash"] = await c.options.can_use_tool("Bash", {"command": "ls"}, None)
        yield AssistantMessage(content=[ToolUseBlock("t1", "mcp__fusion__fusion_list_resources", {}),
                                        ToolUseBlock("t2", "mcp__fusion__fusion_query_resource", {"resource": "invoices"})], model="m")
        yield result(0.31, text="You have 42 open invoices.", usage={"input_tokens": 1000, "output_tokens": 200})
    db, runner, _ = await make(tmp_path, settings, yes, script)
    res = await runner.run(7, "open AP invoices?")
    assert log["discovery"].behavior == "allow"
    assert log["early"].behavior == "deny" and "propose_query_plan" in log["early"].message
    assert log["plan"][1] is False
    assert log["data"].behavior == "allow"
    assert log["other"].behavior == "deny"
    assert log["write"].behavior == "deny" and log["bash"].behavior == "deny"
    assert res.status == "ok" and res.answer == "You have 42 open invoices."
    assert res.cost_usd == pytest.approx(0.31) and res.session_id == "sess-1" and res.usage["output_tokens"] == 200
    assert [t["name"] for t in res.tool_calls] == ["fusion_list_resources", "fusion_query_resource"]
    assert res.data_calls == 1
    events = [r["event"] for r in await (await db.db.execute("SELECT event FROM audit")).fetchall()]
    assert "plan_approved" in events and "data_call" in events and events.count("tool_denied") == 4   # FR-13
    await db.close()


async def test_denied_plan_stops_everything(tmp_path, settings):
    log = {}
    async def script(c):
        log["plan"] = await runner.last_ctx.propose_query_plan({"summary": "x", "steps": [{"tool": "search_invoices"}]})
        log["after"] = await c.options.can_use_tool(P.PREFIX + "fusion_list_resources", {}, None)
        yield result(text="")
    db, runner, _ = await make(tmp_path, settings, no, script)
    res = await runner.run(7, "q")
    assert log["plan"][1] is True and "did not approve" in log["plan"][0]
    assert log["after"].behavior == "deny" and log["after"].interrupt is True
    assert res.status == "denied" and res.answer == ""
    await db.close()


async def test_plan_with_write_tool_is_rejected_not_asked(tmp_path, settings):
    asked = []
    async def approver(plan): asked.append(plan); return True
    out = {}
    async def script(c):
        out["r"] = await runner.last_ctx.propose_query_plan({"summary": "x", "steps": [{"tool": "fusion_preview_write"}]})
        yield result()
    db, runner, _ = await make(tmp_path, settings, approver, script)
    await runner.run(7, "q")
    assert out["r"][1] is True and "not available" in out["r"][0] and not asked     # FR-07: the user is never asked
    await db.close()


async def test_approval_wait_is_excluded_from_compute_timeout(tmp_path, settings):
    s = dataclasses.replace(settings, compute_timeout_s=1, approval_timeout_s=5)
    async def slow_yes(plan):
        await asyncio.sleep(1.6)                      # longer than the compute budget
        return True
    async def script(c):
        await runner.last_ctx.propose_query_plan({"summary": "x", "steps": [{"tool": "search_invoices"}]})
        yield result(text="Done.")
    db, runner, holder = await make(tmp_path, s, slow_yes, script)
    res = await runner.run(7, "q")
    assert res.status == "ok" and not holder["client"].interrupted
    await db.close()


async def test_compute_timeout_interrupts(tmp_path, settings):
    s = dataclasses.replace(settings, compute_timeout_s=1)
    async def script(c):
        await asyncio.sleep(30)
        yield result()
    db, runner, holder = await make(tmp_path, s, yes, script)
    res = await runner.run(7, "q")
    assert res.status == "timeout" and holder["client"].interrupted
    await db.close()


async def test_approval_timeout_denies(tmp_path, settings):
    s = dataclasses.replace(settings, approval_timeout_s=0.1)
    async def never(plan): await asyncio.sleep(5); return True
    out = {}
    async def script(c):
        out["r"] = await runner.last_ctx.propose_query_plan({"summary": "x", "steps": [{"tool": "search_invoices"}]})
        yield result(text="")
    db, runner, _ = await make(tmp_path, s, never, script)
    res = await runner.run(7, "q")
    assert out["r"][1] is True and res.status == "denied"
    await db.close()


async def test_budget_error_and_no_answer(tmp_path, settings):
    async def s_budget(c): yield result(0.6, subtype="error_max_budget_usd", is_error=True, text="")
    db, runner, _ = await make(tmp_path, settings, yes, s_budget)
    assert (await runner.run(7, "q")).status == "budget"
    async def s_none(c): yield result(text="")
    db2, runner2, _ = await make(tmp_path, settings, yes, s_none)
    assert (await runner2.run(7, "q")).status == "no_answer"
    async def s_boom(c):
        raise RuntimeError("cli died")
        yield
    db3, runner3, _ = await make(tmp_path, settings, yes, s_boom)
    r = await runner3.run(7, "q")
    assert r.status == "error" and "cli died" in r.message
    for d in (db, db2, db3): await d.close()


async def test_answer_falls_back_to_last_assistant_text(tmp_path, settings):
    async def script(c):
        yield AssistantMessage(content=[TextBlock("first"), TextBlock("final words")], model="m")
        yield result(text="")
    db, runner, _ = await make(tmp_path, settings, yes, script)
    res = await runner.run(7, "q")
    assert res.status == "ok" and res.answer == "final words"
    await db.close()


async def test_tool_failures_are_captured_even_when_is_error_is_false(tmp_path, settings):
    async def script(c):
        yield UserMessage(content=[
            ToolResultBlock("t1", [{"type": "text", "text": '{"error": "Error executing tool: [Errno -2] Name or service not known"}'}], False),
            ToolResultBlock("t2", "1 validation error ... Field required", True),
            ToolResultBlock("t3", [{"type": "text", "text": '{"items": [], "count": 0}'}], False)])
        yield result(text="Could not reach Fusion.")
    db, runner, _ = await make(tmp_path, settings, yes, script)
    res = await runner.run(7, "q")
    assert len(res.tool_errors) == 2 and "Name or service not known" in res.tool_errors[0]      # FR-10
    await db.close()


SKILL_MD = "---\nname: \"demo-skill\"\ndescription: \"Use for Fusion\"\n---\n# Demo\nDISCOVER FIRST, ALWAYS."


def configured(settings, tmp_path, **over):
    skill = tmp_path / "demo-skill" / "SKILL.md"
    skill.parent.mkdir(exist_ok=True)
    skill.write_text(SKILL_MD, encoding="utf-8")
    base = dict(oracle_base_url="https://np.example.com", oracle_user="u", oracle_password="p", anthropic_api_key="k",
                mcp_dir=tmp_path, planner_skill=skill, runtime_dir=tmp_path / "rt")
    base.update(over)
    return dataclasses.replace(settings, **base)


async def test_native_options_reproduce_chat_skill_loading(tmp_path, settings):
    db = await Database(tmp_path / "t.db").open()
    s = configured(settings, tmp_path, skill_mode="native")
    ctx = RunContext(s, db, 1, yes)
    opts = build_options(s, ctx, resume="abc")
    assert opts.tools == ["Skill"] and opts.skills == ["demo-skill"] and opts.setting_sources == ["project"]
    assert (tmp_path / "rt" / ".claude" / "skills" / "demo-skill" / "SKILL.md").read_text() == SKILL_MD
    assert str(opts.cwd) == str((tmp_path / "rt").resolve()) and ctx.skill == "demo-skill"
    assert "Skill tool" in opts.system_prompt and "DISCOVER FIRST" not in opts.system_prompt   # body is loaded on demand
    assert opts.resume == "abc" and opts.strict_mcp_config is True and opts.model == s.model
    await db.close()


async def test_inline_and_none_modes_offer_no_builtin_tools(tmp_path, settings):
    db = await Database(tmp_path / "t.db").open()
    inline = build_options(configured(settings, tmp_path, skill_mode="inline"), RunContext(settings, db, 1, yes), None)
    assert inline.tools == [] and inline.setting_sources == [] and "DISCOVER FIRST, ALWAYS." in inline.system_prompt
    none = build_options(configured(settings, tmp_path, skill_mode="none"), RunContext(settings, db, 1, yes), None)
    assert none.tools == [] and "DISCOVER FIRST" not in none.system_prompt and "Skill tool" not in none.system_prompt
    missing = build_options(configured(settings, tmp_path, planner_skill=tmp_path / "nope.md"), RunContext(settings, db, 1, yes), None)
    assert missing.tools == [] and "Skill tool" not in missing.system_prompt        # native without a file falls back to none
    await db.close()


async def test_engine_environment_matches_chat_assumptions(tmp_path, settings):
    db = await Database(tmp_path / "t.db").open()
    s = configured(settings, tmp_path, effort="low", max_mcp_output_tokens=40000)
    opts = build_options(s, RunContext(s, db, 1, yes), None)
    assert opts.env["ENABLE_TOOL_SEARCH"] == "false"           # tools load up front, as in chat; Haiku has no tool search
    assert opts.env["MAX_MCP_OUTPUT_TOKENS"] == "40000" and opts.env["ANTHROPIC_API_KEY"] == "k"
    assert opts.effort == "low"
    assert build_options(configured(settings, tmp_path), RunContext(s, db, 1, yes), None).effort is None
    await db.close()


async def test_sdk_options_lock_the_agent_down(tmp_path, settings):
    db = await Database(tmp_path / "t.db").open()
    for mode in ("native", "inline", "none"):
        s = configured(settings, tmp_path, skill_mode=mode)
        opts = build_options(s, RunContext(s, db, 1, yes), None)
        assert set(BUILTIN_TOOLS) <= set(opts.disallowed_tools) and "Skill" not in BUILTIN_TOOLS
        assert opts.max_budget_usd == s.run_budget_usd and opts.can_use_tool is not None
        assert set(opts.mcp_servers) == {"fusion", "bridge"}
        assert not any(t.removeprefix(P.PREFIX) in P.DATA | P.SENSITIVE | P.WRITE_WORKFLOW for t in opts.allowed_tools)
        assert "Skill" not in opts.allowed_tools                   # the permission callback decides about the skill
        fusion = opts.mcp_servers["fusion"]
        assert fusion["command"] == "uv" and fusion["env"]["MCP_MODE"] == "readonly"
        assert fusion["env"]["MCP_ENVIRONMENT"] == "test" and fusion["env"]["MCP_PROD_WRITES_ENABLED"] == "false"
        assert fusion["env"]["ORACLE_ALLOWED_BASE_URLS"] == "https://np.example.com"
        assert "propose_query_plan" in opts.system_prompt and "submit_dashboard" not in opts.system_prompt
    await db.close()


async def test_only_the_vendor_skill_may_be_loaded(tmp_path, settings):
    db = await Database(tmp_path / "t.db").open()
    ctx = RunContext(settings, db, 1, yes, skill="demo-skill")
    assert (await ctx.can_use_tool("Skill", {"command": "demo-skill"}, None)).behavior == "allow"
    assert (await ctx.can_use_tool("Skill", {"skill": "design"}, None)).behavior == "deny"      # a built-in skill
    assert (await RunContext(settings, db, 1, yes).can_use_tool("Skill", {"command": "demo-skill"}, None)).behavior == "deny"
    await db.close()


def test_prompt_supplies_the_date_and_the_read_only_overrides():
    import datetime as dt
    p = build_system_prompt(None, "none", now=dt.datetime(2026, 11, 3, 9, 5, tzinfo=dt.timezone.utc))
    assert "Tuesday 2026-11-03 09:05 UTC" in p and "last month" in p
    for needle in ("read-only", "never offer to", "action discovery", "cut or saved to a file", "ONE short question"):
        assert needle in p


def test_planner_skill_modes_in_the_prompt(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text(SKILL_MD)
    assert "DISCOVER FIRST, ALWAYS." in build_system_prompt(skill, "inline", "demo-skill")
    native = build_system_prompt(skill, "native", "demo-skill")
    assert "named demo-skill" in native and "load it with the Skill tool" in native and "DISCOVER FIRST" not in native
    assert "Skill" not in build_system_prompt(tmp_path / "missing.md", "native", "x").replace("Skill sections", "")  # no file, no mention


def test_prompt_states_the_rules_the_cheat_sheet_calls_for():
    for needle in ("propose_query_plan", "isError", "fields and a limit", "compute tools", "plain text", "cannot change anything"):
        assert needle in BRIDGE_RULES


def test_mcp_env_never_enables_prod_writes_by_default(settings):
    assert mcp_env(settings)["MCP_PROD_WRITES_ENABLED"] == "false"

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


async def test_only_successful_data_calls_are_counted_and_the_row_cap_is_enforced(tmp_path, settings):
    from types import SimpleNamespace
    log = {}
    async def script(c):
        await runner.last_ctx.propose_query_plan({"summary": "x", "steps": [{"tool": "query_resource", "resource": "absences"}]})
        t = lambda i: SimpleNamespace(tool_use_id=i)
        log["a"] = await c.options.can_use_tool(P.PREFIX + "query_resource", {"resource": "absences", "limit": 50}, t("t1"))
        log["b"] = await c.options.can_use_tool(P.PREFIX + "query_resource", {"resource": "absences", "limit": 50}, t("t2"))
        log["big"] = await c.options.can_use_tool(P.PREFIX + "query_resource", {"resource": "absences", "limit": 5000}, t("t3"))
        yield UserMessage(content=[
            ToolResultBlock("t1", [{"type": "text", "text": '{"error": "orderBy is not valid"}'}], False),   # a failed call
            ToolResultBlock("t2", [{"type": "text", "text": '{"items": [], "count": 0}'}], False)])          # a good one
        yield result(text="Done.")
    db, runner, _ = await make(tmp_path, settings, yes, script)
    res = await runner.run(7, "q")
    assert log["a"].behavior == "allow" and log["b"].behavior == "allow"
    assert log["big"].behavior == "deny" and "limit 5000 is too large" in log["big"].message
    assert res.data_calls == 1 and res.data_attempts == 2 and len(res.tool_errors) == 1
    await db.close()


async def test_the_first_real_run_scenario_now_reaches_the_data_call(tmp_path, settings):
    """A typed tool, a plan step that names a resource, no resource in the call: this was refused three times."""
    log = {}
    async def script(c):
        await runner.last_ctx.propose_query_plan({"summary": "10 latest invoices", "steps": [
            {"tool": "search_invoices", "resource": "invoices", "note": "limit=10"}]})
        log["call"] = await c.options.can_use_tool(P.PREFIX + "search_invoices", {"date_to": "2026-10-28", "limit": 10, "offset": 0}, None)
        yield result(text="Here are the invoices.")
    db, runner, _ = await make(tmp_path, settings, yes, script)
    res = await runner.run(7, "latest invoices")
    assert log["call"].behavior == "allow" and res.data_calls == 1 and res.status == "ok"
    await db.close()


async def test_reported_duration_excludes_the_wait_for_the_tap(tmp_path, settings):
    async def slow_yes(plan):
        await asyncio.sleep(0.6)
        return True
    async def script(c):
        await runner.last_ctx.propose_query_plan({"summary": "x", "steps": [{"tool": "search_invoices"}]})
        yield result(text="Done.")
    db, runner, _ = await make(tmp_path, settings, slow_yes, script)
    res = await runner.run(7, "q")
    assert res.duration_s < 0.4                                    # the 0.6 s wait is not counted as working time
    await db.close()


async def _pay_data_scenario(tmp_path, settings, name):
    log, current = {}, {}
    async def script(c):
        await current["r"].last_ctx.propose_query_plan({"summary": "worker names", "steps": [{"tool": "query_resource", "resource": "workers"}]})
        log["ok"] = await c.options.can_use_tool(P.PREFIX + "query_resource", {"resource": "workers", "fields": ["DisplayName"]}, None)
        log["expand"] = await c.options.can_use_tool(P.PREFIX + "query_resource", {"resource": "workers", "expand": ["salaries"]}, None)
        log["direct"] = await c.options.can_use_tool(P.PREFIX + "query_resource", {"resource": "salaries"}, None)
        yield result(text="Done.")
    db = await Database(tmp_path / f"{name}.db").open()
    runner = LiveRunner(settings, db, yes, lambda opts: FakeSDK(opts, script), tick_s=0.02)
    current["r"] = runner
    await runner.run(7, "names")
    await db.close()
    return log


async def test_expand_or_child_cannot_smuggle_pay_data_past_an_approved_plan(tmp_path, settings):
    log = await _pay_data_scenario(tmp_path, settings, "default")
    assert log["ok"].behavior == "allow"
    assert log["expand"].behavior == "deny" and "pay or identity" in log["expand"].message
    assert log["direct"].behavior == "deny" and "pay or identity" in log["direct"].message


async def test_the_sensitive_switch_lifts_the_resource_block_but_not_the_plan_scope(tmp_path, settings):
    log = await _pay_data_scenario(tmp_path, dataclasses.replace(settings, allow_hcm_sensitive=True), "sensitive")
    assert log["ok"].behavior == "allow" and log["expand"].behavior == "allow"          # expand is not scoped by the plan
    assert log["direct"].behavior == "deny" and "pay or identity" not in log["direct"].message   # salaries is not in the plan


async def test_progress_lines_say_what_is_happening(tmp_path, settings):
    from bridge.live.session import activity_label
    async def script(c):
        yield AssistantMessage(content=[ToolUseBlock("t1", "Skill", {"command": "demo-skill"})], model="m")
        await asyncio.sleep(0.3)
        yield AssistantMessage(content=[ToolUseBlock("t2", P.PREFIX + "fusion_describe_resource", {})], model="m")
        await asyncio.sleep(0.3)
        yield AssistantMessage(content=[ToolUseBlock("t3", P.PREFIX + "query_resource", {"resource": "absences"})], model="m")
        await asyncio.sleep(0.3)
        yield UserMessage(content=[ToolResultBlock("t3", [{"type": "text", "text": "{}"}], False)])
        await asyncio.sleep(0.3)
        yield result(text="Done.")
    seen = []
    async def status(t): seen.append(t)
    db = await Database(tmp_path / "t.db").open()
    runner = LiveRunner(settings, db, yes, lambda opts: FakeSDK(opts, script), tick_s=0.02, status_every_s=0.1)
    await runner.run(7, "q", on_status=status)
    labels = [t.split("...")[0] for t in seen]
    for expected in ("Reading the Fusion playbook", "Checking what Fusion offers", "Reading from Fusion", "Reviewing the results"):
        assert expected in labels, labels
    assert all(t.split("... ")[1].endswith("s") for t in seen)
    assert activity_label("mcp__bridge__propose_query_plan") == "Preparing the plan" and activity_label(P.PREFIX + "safe_divide") == "Calculating"
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


async def test_step_limit_gets_its_own_status(tmp_path, settings):
    async def script(c): yield result(0.2, subtype="error_max_turns", is_error=True, text="")
    db, runner, _ = await make(tmp_path, settings, yes, script)
    res = await runner.run(7, "q")
    assert res.status == "turns" and "step limit" in res.message
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
    assert (tmp_path / "rt" / ".claude" / "skills" / "demo-skill" / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD
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
    assert opts.env["DISABLE_TELEMETRY"] == "1" and opts.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert opts.env["CLAUDE_CONFIG_DIR"] == str(s.engine_dir.resolve())           # saved conversations live under data/
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
    skill.write_text(SKILL_MD, encoding="utf-8")
    assert "DISCOVER FIRST, ALWAYS." in build_system_prompt(skill, "inline", "demo-skill")
    native = build_system_prompt(skill, "native", "demo-skill")
    assert "named demo-skill" in native and "load it with the Skill tool" in native and "DISCOVER FIRST" not in native
    assert "Skill" not in build_system_prompt(tmp_path / "missing.md", "native", "x").replace("Skill sections", "")  # no file, no mention


def test_prompt_states_the_rules_the_cheat_sheet_calls_for():
    for needle in ("propose_query_plan", "isError", "fields and a limit", "compute tools", "plain text", "cannot change anything",
                   "typed tools take none", "needs no describe", "Plan once and plan generously", "finance and",
                   "one call per record", "Stop early", "403", "{id}", "totalResults", "most recent", "Never add amounts",
                   "language of the question", "never instructions", "15 tool calls", "ALWAYS finish with one line", "not no closing line", "put the fallback in the same", "ask for 25 rows or fewer",
                   "has approved showing worker names", "do not refuse them on privacy grounds",
                   "is a sample, not the whole population", "show names by default",
                   "ONE person per call", "returns only the first one", "from earlier", "as-of time",
                   "Worker names ARE available", "PersonNumber and DisplayName", "does not mean names are off limits",
                   "Never guess a child resource", "fusion_find_relationships"):
        assert needle in " ".join(BRIDGE_RULES.split())


def test_mcp_env_never_enables_prod_writes_by_default(settings):
    assert mcp_env(settings)["MCP_PROD_WRITES_ENABLED"] == "false"


def test_recipes_are_loaded_without_comment_lines_and_cut_to_a_limit(tmp_path):
    from bridge.live.prompt import RECIPES_LIMIT, load_recipes
    f = tmp_path / "recipes.md"
    f.write_text("<!-- how to add notes -->\n- Holds: attribute ActiveHoldFlag.\n- Dates are bare.\n", encoding="utf-8")
    text = load_recipes(f)
    assert text == "- Holds: attribute ActiveHoldFlag.\n- Dates are bare." and "<!--" not in text
    assert load_recipes(None) == "" and load_recipes(tmp_path / "missing.md") == ""
    f.write_text("\n".join(f"- note number {i} " + "x" * 80 for i in range(200)), encoding="utf-8")
    cut = load_recipes(f)
    assert len(cut) <= RECIPES_LIMIT + 80 and cut.endswith("longer than the limit)")


def test_recipes_reach_the_system_prompt_under_their_own_heading():
    p = build_system_prompt(None, "none", recipes="- Holds: ActiveHoldFlag.")
    assert "# Notes about this pod" in p and "ActiveHoldFlag" in p and "do not guess" in p
    assert "# Notes about this pod" not in build_system_prompt(None, "none")


def test_the_shipped_recipes_hold_the_verified_hold_field_and_fit_the_limit():
    from pathlib import Path
    from bridge.live.prompt import RECIPES_LIMIT, load_recipes
    text = load_recipes(Path(__file__).resolve().parents[1] / "skill" / "recipes.md")
    assert "ActiveHoldFlag" in text and "invoiceHolds" in text and "<!--" not in text
    assert "list_ledgers" in text and "ledgersLOV" in text and "fails with 400" in text and "Do not use it" in text
    assert len(text) < RECIPES_LIMIT * 0.6                                       # room to grow without bloating every turn


async def test_recipes_are_added_to_the_options_when_a_file_is_configured(tmp_path, settings):
    db = await Database(tmp_path / "t.db").open()
    f = tmp_path / "recipes.md"
    f.write_text("- Holds: ActiveHoldFlag on invoices.", encoding="utf-8")
    s = dataclasses.replace(settings, recipes_path=f)
    assert "ActiveHoldFlag on invoices" in build_options(s, RunContext(s, db, 1, yes), None).system_prompt
    assert "Notes about this pod" not in build_options(settings, RunContext(settings, db, 1, yes), None).system_prompt
    await db.close()


def test_every_recipe_says_how_it_was_checked():
    """A recipe written from a guess did harm once. Each note must end with a parenthesis naming its evidence."""
    import re
    from pathlib import Path
    from bridge.live.prompt import load_recipes
    text = load_recipes(Path(__file__).resolve().parents[1] / "skill" / "recipes.md")
    notes = re.split(r"(?m)^- ", text)[1:]
    assert len(notes) >= 5
    for note in notes:
        assert re.search(r"\((?:Checked|Seen|Measured|Verified|The failing request)[^)]*\)\s*$", note.strip(), re.S), note[:70]


async def test_the_bridge_refuses_an_or_lookup_on_workers_and_accepts_one_person_per_call(tmp_path, settings):
    """The call is refused with the reason, no data call is counted for it, and the one-person calls then go through."""
    from types import SimpleNamespace

    async def script(c):
        ctx = c.options.can_use_tool.__self__
        await ctx.propose_query_plan({"summary": "names", "steps": [{"tool": "query_resource", "resource": "workers"}]})
        calls = [{"resource": "workers", "q": "PersonNumber = '1' or PersonNumber = '2'", "fields": ["PersonNumber", "DisplayName"]},
                 {"resource": "workers", "q": "PersonNumber = '1'", "fields": ["PersonNumber", "DisplayName"]},
                 {"resource": "workers", "q": "PersonNumber = '2'", "fields": ["PersonNumber", "DisplayName"]}]
        for i, args in enumerate(calls):
            verdict = await c.options.can_use_tool(P.PREFIX + "query_resource", args, SimpleNamespace(tool_use_id=f"w{i}"))
            c.verdicts.append(verdict)
            if verdict.behavior == "allow":
                yield UserMessage(content=[ToolResultBlock(f"w{i}", [{"type": "text", "text": '{"items": [{"PersonNumber": "1"}]}'}], False)])
        yield result(0.01, text="Names looked up one by one.")
    verdicts = []
    class Recording(FakeSDK):
        def __init__(self, options, script):
            super().__init__(options, script)
            self.verdicts = verdicts
    db = await Database(tmp_path / "t.db").open()
    res = await LiveRunner(settings, db, yes, lambda o: Recording(o, script), tick_s=0.02).run(1, "names")
    assert res.status == "ok", (res.status, res.message, res.tool_errors)
    assert [v.behavior for v in verdicts] == ["deny", "allow", "allow"]
    assert "ONE person per call" in verdicts[0].message and res.data_calls == 2 and res.data_attempts == 2
    await db.close()


async def test_a_date_range_or_the_word_and_is_refused_inside_a_run_and_a_valid_filter_is_allowed(tmp_path, settings):
    log, current = {}, {}

    async def script(c):
        await current["r"].last_ctx.propose_query_plan({"summary": "sick leave", "steps": [{"tool": "query_resource", "resource": "absences"}]})
        log["range"] = await c.options.can_use_tool(P.PREFIX + "query_resource", {"resource": "absences",
                                                    "q": "startDate >= 2026-09-01;startDate <= 2026-09-30"}, None)
        log["valid"] = await c.options.can_use_tool(P.PREFIX + "query_resource", {"resource": "absences",
                                                    "q": "absenceType LIKE 'Sick%';startDate >= 2026-09-01"}, None)
        log["and"] = await c.options.can_use_tool(P.PREFIX + "query_resource", {"resource": "absences",
                                                  "q": "absenceType LIKE 'Sick%' and startDate >= 2026-09-01"}, None)
        yield result(text="Done.")
    db = await Database(tmp_path / "r.db").open()
    runner = LiveRunner(settings, db, yes, lambda opts: FakeSDK(opts, script), tick_s=0.02)
    current["r"] = runner
    await runner.run(7, "who took sick leave in September")
    await db.close()
    assert log["range"].behavior == "deny" and "ONE condition per attribute" in log["range"].message and "startDate" in log["range"].message
    assert log["valid"].behavior == "allow"
    assert log["and"].behavior == "deny" and "rejects the word 'and'" in log["and"].message and "subtract the second total" in log["and"].message


async def test_a_finance_date_pair_is_refused_inside_a_run_whether_typed_or_generic_and_single_dates_are_allowed(tmp_path, settings):
    """The finance probe showed InvoiceDate >= A;InvoiceDate <= B returning the total of <= B alone, and the MCP's own search_invoices builds that pair."""
    log, current = {}, {}

    async def script(c):
        await current["r"].last_ctx.propose_query_plan({"summary": "invoices in January", "steps": [
            {"tool": "search_invoices"}, {"tool": "fusion_query_resource", "resource": "invoices"}]})
        ask = lambda name, args: c.options.can_use_tool(P.PREFIX + name, args, None)
        log["typed_pair"] = await ask("search_invoices", {"date_from": "2026-01-01", "date_to": "2026-01-31", "limit": 25})
        log["typed_one"] = await ask("search_invoices", {"date_from": "2026-01-01", "limit": 25})
        log["generic_pair"] = await ask("fusion_query_resource", {"resource": "invoices",
                                        "q": "InvoiceDate >= '2026-01-01';InvoiceDate <= '2026-01-31'", "limit": 1, "totalResults": True})
        log["generic_one"] = await ask("fusion_query_resource", {"resource": "invoices", "q": "InvoiceDate >= '2026-01-01'", "limit": 1, "totalResults": True})
        yield result(text="Done.")
    db = await Database(tmp_path / "f.db").open()
    runner = LiveRunner(settings, db, yes, lambda opts: FakeSDK(opts, script), tick_s=0.02)
    current["r"] = runner
    await runner.run(7, "invoices in January")
    await db.close()
    assert log["typed_pair"].behavior == "deny" and "send only ONE of them" in log["typed_pair"].message and "InvoiceDate" in log["typed_pair"].message
    assert log["generic_pair"].behavior == "deny" and "ONE condition per attribute" in log["generic_pair"].message
    assert log["typed_one"].behavior == "allow" and log["generic_one"].behavior == "allow"

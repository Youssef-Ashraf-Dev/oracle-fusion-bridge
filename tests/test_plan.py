import pytest

from bridge.live import permissions as P
from bridge.live.plan import MAX_DATA_CALLS, Plan, PlanError, PlanState, Step, parse_plan

GOOD = {"summary": "open AP invoices", "steps": [
    {"tool": "fusion_query_resource", "resource": "invoices", "note": "status=open"}]}


def test_parse_valid_plan():
    plan = parse_plan(GOOD)
    assert plan.steps == [Step("fusion_query_resource", "invoices", "status=open")]


def test_prefixed_tool_names_accepted():
    plan = parse_plan({"summary": "x", "steps": [{"tool": "mcp__fusion__search_invoices"}]})
    assert plan.steps[0].tool == "search_invoices"


@pytest.mark.parametrize("tool", sorted(P.WRITE_WORKFLOW) + ["Bash", "totally_unknown"])
def test_plan_cannot_include_denied_tools(tool):
    with pytest.raises(PlanError):
        parse_plan({"summary": "x", "steps": [{"tool": tool}]})


def test_plan_rejects_free_tools():
    with pytest.raises(PlanError):
        parse_plan({"summary": "x", "steps": [{"tool": "fusion_list_resources"}]})


def test_plan_requires_summary_and_steps():
    with pytest.raises(PlanError):
        parse_plan({"summary": "", "steps": [{"tool": "search_invoices"}]})
    with pytest.raises(PlanError):
        parse_plan({"summary": "x", "steps": []})


def test_no_plan_blocks_data_calls():
    ok, why = PlanState().covers("mcp__fusion__search_invoices", {})
    assert not ok and "propose_query_plan" in why


def test_call_must_match_plan_step():
    st = PlanState(approved=[parse_plan(GOOD)])
    assert st.covers("mcp__fusion__fusion_query_resource", {"resource": "Invoices"})[0]
    assert not st.covers("mcp__fusion__fusion_query_resource", {"resource": "payments"})[0]
    assert not st.covers("mcp__fusion__search_suppliers", {})[0]


def test_step_without_resource_matches_any_resource():
    st = PlanState(approved=[Plan("x", [Step("search_invoices")])])
    assert st.covers("mcp__fusion__search_invoices", {"supplier": "ACME"})[0]


def test_call_cap():
    st = PlanState(approved=[parse_plan(GOOD)])
    for _ in range(MAX_DATA_CALLS):
        st.note_call()
    ok, why = st.covers("mcp__fusion__fusion_query_resource", {"resource": "invoices"})
    assert not ok and "limit" in why


def test_second_plan_extends_scope():
    st = PlanState(approved=[parse_plan(GOOD)])
    st.approved.append(parse_plan({"summary": "y", "steps": [{"tool": "fusion_query_resource", "resource": "payments"}]}))
    assert st.covers("mcp__fusion__fusion_query_resource", {"resource": "payments"})[0]


def test_bridge_tools_allowed_but_not_unknown_bridge_tools():
    assert P.classify("mcp__bridge__propose_query_plan").verdict == "allow"
    assert P.classify("mcp__bridge__submit_dashboard").verdict == "deny"       # removed with dashboards
    assert P.classify("mcp__bridge__anything_else").verdict == "deny"


def test_typed_tool_call_matches_a_step_that_names_a_resource():
    """The exact failure seen on the first real run: the model wrote resource=invoices on a typed tool."""
    plan = parse_plan({"summary": "10 latest invoices", "steps": [
        {"tool": "search_invoices", "resource": "invoices", "note": "limit=10"}]})
    st = PlanState(approved=[plan])
    ok, why = st.covers("mcp__fusion__search_invoices", {"date_to": "2026-10-28", "limit": 10, "offset": 0})
    assert ok, why


def test_resource_scoping_still_applies_to_generic_readers_with_a_helpful_message():
    st = PlanState(approved=[parse_plan({"summary": "x", "steps": [
        {"tool": "fusion_query_resource", "resource": "invoices"}]})])
    assert st.covers("mcp__fusion__fusion_query_resource", {"resource": "Invoices/"})[0]
    ok, why = st.covers("mcp__fusion__fusion_query_resource", {"resource": "payments"})
    assert not ok and "allows it on invoices" in why and "names the resource" in why
    ok, why = st.covers("mcp__fusion__fusion_query_resource", {})                       # a generic call with no resource
    assert not ok and "no resource" in why


def test_typed_tool_still_needs_its_own_step():
    st = PlanState(approved=[parse_plan({"summary": "x", "steps": [{"tool": "search_invoices", "resource": "invoices"}]})])
    ok, why = st.covers("mcp__fusion__search_suppliers", {})
    assert not ok and "not in the approved plan" in why


def test_generic_reader_set_matches_the_tools_that_really_take_a_resource():
    import json
    from pathlib import Path
    tools = json.loads((Path(__file__).with_name("tools_inspected.json")).read_text())
    assert P.GENERIC_READERS <= {t["name"] for t in tools}


def test_plan_steps_on_pay_or_identity_resources_are_refused_before_the_user_is_asked():
    for res in ("salaries", "individualCompensations", "userAccounts"):
        with pytest.raises(PlanError, match="pay or identity data"):
            parse_plan({"summary": "x", "steps": [{"tool": "query_resource", "resource": res}]})
    assert parse_plan({"summary": "x", "steps": [{"tool": "query_resource", "resource": "workers"}]}).steps[0].resource == "workers"
    assert parse_plan({"summary": "x", "steps": [{"tool": "query_resource", "resource": "salaries"}]}, allow_sensitive=True)

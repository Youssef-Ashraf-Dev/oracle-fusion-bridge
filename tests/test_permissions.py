import json
from pathlib import Path

import pytest

from bridge.live import permissions as P

TOOLS_JSON = Path(__file__).with_name("tools_inspected.json")


def test_every_inspected_tool_is_classified():
    """All 59 tools in the bundle are known: a bundle upgrade that adds one fails here."""
    names = {t["name"] for t in json.loads(TOOLS_JSON.read_text())}
    assert len(names) == 59
    unknown = names - P.KNOWN
    assert not unknown, f"unclassified tools (denied by default, review them): {sorted(unknown)}"


def test_no_stale_names_in_policy():
    names = {t["name"] for t in json.loads(TOOLS_JSON.read_text())}
    assert not (P.KNOWN - names), f"policy names tools the bundle no longer has: {sorted(P.KNOWN - names)}"


@pytest.mark.parametrize("tool", sorted(P.WRITE_WORKFLOW))
def test_every_write_workflow_tool_denied(tool):
    assert P.classify(P.PREFIX + tool).verdict == "deny"
    assert P.classify(P.PREFIX + tool, allow_sensitive=True).verdict == "deny"


def test_all_seven_write_tools_present():
    assert len(P.WRITE_WORKFLOW) == 7


def test_unknown_tool_denied_by_default():
    assert P.classify(P.PREFIX + "fusion_brand_new_tool").verdict == "deny"


@pytest.mark.parametrize("tool", ["Bash", "Read", "Write", "WebFetch", "mcp__other__list_resources"])
def test_non_fusion_tools_denied(tool):
    assert P.classify(tool).verdict == "deny"


def test_tiers():
    assert P.classify(P.PREFIX + "fusion_list_resources").verdict == "allow"
    assert P.classify(P.PREFIX + "safe_divide").verdict == "allow"
    assert P.classify(P.PREFIX + "fusion_query_resource").verdict == "needs_plan"
    assert P.classify(P.PREFIX + "search_invoices").verdict == "needs_plan"


def test_sensitive_gate():
    t = P.PREFIX + "get_current_compensation"
    assert P.classify(t).verdict == "deny"
    assert P.classify(t, allow_sensitive=True).verdict == "needs_plan"


def test_sdk_preapproved_list_has_no_data_or_write_tools():
    pre = set(P.sdk_allowed_tools())
    for t in P.DATA | P.SENSITIVE | P.WRITE_WORKFLOW | P.UNUSED:
        assert P.PREFIX + t not in pre


@pytest.mark.parametrize("call,hit", [
    ({"resource": "salaries"}, "salaries"), ({"resource": "Salaries/"}, "Salaries/"), ({"resource": "individualCompensations"}, "individualCompensations"),
    ({"resource": "workers", "expand": ["salaries"]}, "salaries"), ({"resource": "workers", "expand": "assignments,userAccounts"}, "assignments,userAccounts"),
    ({"resource": "payrollRelationships"}, "payrollRelationships"), ({"resource": "workers", "child": "bank_accounts"}, "bank_accounts"),
    ({"resource": "workers"}, ""), ({"resource": "absences"}, ""), ({"resource": "invoices", "expand": "lines"}, ""), ({}, ""),
])
def test_generic_readers_cannot_reach_pay_or_identity_resources(call, hit):
    assert P.touches_sensitive_resource(call) == hit

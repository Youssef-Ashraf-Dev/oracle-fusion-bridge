import json
from pathlib import Path

import pytest

from bridge.live import permissions as P

TOOLS_JSON = Path(__file__).with_name("tools_inspected.json")


def test_every_inspected_tool_is_classified():
    """All 59 tools in the bundle are known: a bundle upgrade that adds one fails here."""
    names = {t["name"] for t in json.loads(TOOLS_JSON.read_text(encoding="utf-8"))}
    assert len(names) == 59
    unknown = names - P.KNOWN
    assert not unknown, f"unclassified tools (denied by default, review them): {sorted(unknown)}"


def test_no_stale_names_in_policy():
    names = {t["name"] for t in json.loads(TOOLS_JSON.read_text(encoding="utf-8"))}
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


def test_the_refusal_of_profile_tools_says_that_names_are_still_available():
    for tool in ("find_worker", "get_worker_profile", "get_current_compensation"):
        d = P.classify(P.PREFIX + tool)
        assert d.verdict == "deny" and "names are" in d.reason.lower() and "PersonNumber,DisplayName" in d.reason
        assert "Pay, bank, ID-document and user-account data are not available" in d.reason


def test_an_or_lookup_on_workers_is_recognised_but_other_reads_are_not():
    """Ten person numbers joined with 'or' returned one person on this pod: the model must ask for one person per call."""
    pre = P.PREFIX
    w = lambda q, res="workers", tool="query_resource": P.workers_or_lookup(pre + tool, {"resource": res, "q": q})
    assert w("PersonNumber = '1' or PersonNumber = '2'") and w("PersonNumber = 1 OR PersonNumber = 2") and w("a = 1\nor b = 2")
    assert not w("PersonNumber = '1'") and not w("PersonNumber = '1' and AssignmentStatus = 'ACTIVE'")
    assert not w("DisplayName = 'Mohamed or Ali'")                                    # an 'or' inside a quoted value is just text
    assert not w("PersonNumber = '1' or PersonNumber = '2'", res="absences")           # only the resource that was checked
    assert not w("PersonNumber = '1' or PersonNumber = '2'", tool="search_invoices")
    assert not P.workers_or_lookup(pre + "query_resource", {"resource": "workers"})   # no filter at all
    assert not P.workers_or_lookup(pre + "query_resource", {"resource": "workers", "q": None})
    assert "ONE person per call" in P.WORKERS_OR_REASON and "FIRST person" in P.WORKERS_OR_REASON


def test_a_filter_naming_the_same_attribute_twice_is_recognised_on_both_families_but_valid_filters_are_not():
    """Measured on absences (16 versus 4,115 for the same two conditions reversed) and on invoices (InvoiceDate >= A;InvoiceDate <= B gave the total of <= B)."""
    hr, fin = P.PREFIX + "query_resource", P.PREFIX + "fusion_query_resource"
    twice = lambda q, res="absences", tool=hr: P.repeated_filter_attribute(tool, {"resource": res, "q": q})
    assert twice("startDate >= 2026-09-01;startDate <= 2026-09-30") == "startDate"
    assert twice("startDate >= 1900-01-01;absenceType LIKE 'Sick%';StartDate <= 2026-09-30") == "StartDate"      # not adjacent, any capitals
    assert twice("absenceType = 'Sick Leave';absenceType = 'Annual Leave'") == "absenceType"
    assert twice("PersonNumber = 1;PersonNumber = 2", res="workers") == "PersonNumber"                          # every HR resource, not only absences
    assert twice("InvoiceDate >= '2026-01-01';InvoiceDate <= '2026-01-31'", res="invoices", tool=fin) == "InvoiceDate"
    assert twice("InvoiceDate <= 2026-01-31;InvoiceDate >= 2026-01-01", res="invoices", tool=fin) == "InvoiceDate"
    assert twice("absenceType LIKE 'Sick%';startDate >= 2026-09-01") is None                                    # different attributes combine
    assert twice("InvoiceDate >= '2026-01-01';ValidationStatus = 'X'", res="invoices", tool=fin) is None
    assert twice("startDate >= 2026-09-01") is None and twice(None) is None and twice("") is None
    assert twice("startDate <= 2026-11-05") is None                                                              # the one condition 'latest N' sends with the sort
    assert twice("absenceType LIKE 'startDate >= x;startDate <= y'") is None                                    # text inside quotes
    assert P.repeated_filter_attribute(P.PREFIX + "search_suppliers", {"q": "a = 1;a = 2"}) is None            # a tool outside the measured families
    assert P.repeated_filter_attribute(hr, {"resource": "absences"}) is None
    why = P.repeated_filter_reason("startDate")
    assert "ONE condition per attribute" in why and "startDate >= FROM" in why and "ask for the total (limit 1) twice" in why
    assert "measured on absences and on invoices" in why and "THE DAY AFTER TO" in why and "send only ONE of them" not in why


def test_a_typed_search_given_both_date_from_and_date_to_is_recognised_because_it_builds_a_pair_on_one_attribute():
    """search_invoices appends InvoiceDate >= date_from and then InvoiceDate <= date_to: with the pod's last-one-wins rule the lower bound is lost."""
    typed = lambda tool, **kw: P.repeated_filter_attribute(P.PREFIX + tool, kw)
    assert typed("search_invoices", date_from="2026-01-01", date_to="2026-01-31") == "InvoiceDate"
    assert typed("search_payments", date_from="a", date_to="b") == "PaymentDate"
    assert typed("search_ar_invoices", date_from="a", date_to="b") == "CreationDate"
    assert typed("search_receipts", date_from="a", date_to="b") == "ReceiptDate"
    assert typed("search_expense_reports", date_from="a", date_to="b") == "ReportSubmitDate"
    assert typed("search_invoices", date_from="2026-01-01", supplier="google") is None                           # one bound, or another attribute: fine
    assert typed("search_invoices", date_to="2026-01-31") is None and typed("search_invoices") is None
    assert typed("search_invoices", date_to="2026-01-31", q="InvoiceDate >= '2026-01-01'") == "InvoiceDate"     # q and a date parameter overlap
    assert typed("search_invoices", date_from="2026-01-01", q="Supplier LIKE 'G%'") is None
    assert typed("search_invoices", date_from="", date_to=None) is None
    assert "send only ONE of them" in P.repeated_filter_reason("InvoiceDate", typed_search=True)


def test_the_word_and_in_an_absences_filter_is_recognised_because_the_pod_answers_400():
    """The V4 run sent 'absenceType LIKE ... and startDate >= 2025-01-01' three times and spent its calls on 400 errors."""
    pre = P.PREFIX + "query_resource"
    has = lambda q, res="absences", tool=pre: P.absences_and_keyword(tool, {"resource": res, "q": q})
    assert has("absenceType LIKE 'Sick%' and startDate >= 2025-01-01") and has("a = 1 AND b = 2") and has("(a >= 1) and (a <= 2)") and has("a = 1\nand b = 2")
    assert not has("absenceType LIKE 'Sick%';startDate >= 2025-01-01") and not has("startDate >= 2025-01-01") and not has(None) and not has("")
    assert not has("absenceType = 'Sick and Annual'")                                           # inside quotes it is just text
    assert not has("a = 1 and b = 2", res="workers") and not has("a = 1 and b = 2", tool=P.PREFIX + "fusion_query_resource")
    why = P.ABSENCES_AND_REASON
    assert "rejects the word 'and'" in why and "DIFFERENT attributes with ';'" in why and "THE DAY AFTER TO" in why and "subtract the second total" in why
    assert "Do not report a total from a filter that was refused or ignored" in why

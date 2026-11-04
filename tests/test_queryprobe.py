import asyncio

import pytest

from bridge.mcp_util import ToolError
from bridge.queryprobe import build_args, ordering, probe_orderings, verdict

ROWS = [{"personNumber": "1", "startDate": "2026-09-22", "DisplayName": "Secret Name A"},
        {"personNumber": "2", "startDate": "2026-09-14", "DisplayName": "Secret Name B"},
        {"personNumber": "3", "startDate": "2026-09-01", "DisplayName": "Secret Name C"}]


def test_the_hr_and_finance_tools_get_their_own_argument_shapes():
    tool, a = build_args("absences", q="startDate >= 2026-01-01", fields=["personNumber", "startDate"], order_by="startDate:desc", limit=5)
    assert tool == "query_resource" and a == {"resource": "absences", "limit": 5, "q": "startDate >= 2026-01-01",
                                              "fields": ["personNumber", "startDate"], "order_by": "startDate:desc"}
    tool, a = build_args("invoices", fields=["InvoiceId", "InvoiceAmount"], order_by="ignored", limit=1, total=True, finance=True)
    assert tool == "fusion_query_resource" and a == {"resource": "invoices", "limit": 1, "fields": "InvoiceId,InvoiceAmount", "totalResults": True}
    assert "order_by" not in a                                                      # the finance tool has none
    assert build_args("workers", effective_date="2026-01-01", total=True)[1]["total_results"] is True


@pytest.mark.parametrize("values,expected", [
    (["2026-09-22", "2026-09-14", "2026-09-01"], "descending"), (["2026-09-01", "2026-09-14"], "ascending"),
    (["2026-09-14", "2026-09-22", "2026-09-01"], "unordered"), (["2026-09-14", "2026-09-14"], "same value"),
    (["10", "9", "2"], "descending"), (["x"], "n/a"), ([], "n/a")])
def test_the_order_of_what_came_back_is_judged_with_numbers_compared_as_numbers(values, expected):
    assert ordering(values) == expected


def fake_pod(sorting, ignoring, rejecting):
    async def call(tool, args):
        assert tool == "query_resource"
        ob = args.get("order_by")
        if ob in rejecting:
            raise ToolError("Error executing tool: [400] : URL request parameter orderBy with value " + ob + " is not valid. For more information check: https://x")
        rows = list(ROWS)
        if ob in sorting:
            rows = sorted(rows, key=lambda r: r["startDate"], reverse=True)
        elif ob is None or ob in ignoring:
            rows = [ROWS[1], ROWS[2], ROWS[0]]                                      # the pod's natural order
        return {"items": rows}
    return call


def test_which_sort_spellings_work_are_told_apart_from_accepted_but_ignored_and_rejected():
    results = asyncio.run(probe_orderings(fake_pod({"startDate:desc"}, {"-startDate"}, {"startDate desc"}), "absences",
                                          ["startDate:desc", "-startDate", "startDate desc"], ["startDate"]))
    by = {r.label: r for r in results}
    assert by["no order_by (baseline)"].order == "unordered" and by["order_by = startDate:desc"].order == "descending"
    assert by["order_by = -startDate"].ok and by["order_by = -startDate"].order == "unordered"
    assert not by["order_by = startDate desc"].ok and "not valid" in by["order_by = startDate desc"].error and "mozilla" not in by["order_by = startDate desc"].error
    text = " ".join(verdict(results))
    assert "Works: order_by = startDate:desc returned the rows descending" in text
    assert "Accepted but did not sort: order_by = -startDate (unordered)" in text and "Rejected by the pod: order_by = startDate desc" in text


def test_when_nothing_sorts_the_verdict_says_so_and_no_value_beyond_those_asked_for_is_kept():
    results = asyncio.run(probe_orderings(fake_pod(set(), {"a", "b"}, set()), "absences", ["a", "b"], ["startDate"]))
    assert "No spelling sorted the rows" in " ".join(verdict(results))
    assert "Secret Name" not in repr(results)                                      # the names were in the rows; only the shown field is kept
    assert results[0].keys == ["DisplayName", "personNumber", "startDate"] and set(results[0].shown) == {"startDate"}

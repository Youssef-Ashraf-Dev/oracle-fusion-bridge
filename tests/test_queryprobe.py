import asyncio

import pytest

from bridge.mcp_util import ToolError
from bridge.queryprobe import build_args, filter_verdicts, ordering, outside_window, probe_filters, probe_orderings, verdict

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


ABS = [{"personNumber": str(i), "startDate": d, "absenceType": t, "DisplayName": "Secret Name"} for i, (d, t) in enumerate([
    ("2019-10-05", "Unpaid Leave"), ("2021-01-13", "Unpaid Leave"), ("2025-07-20", "Sick Leave"), ("2026-04-01", "Sick Leave"),
    ("2026-08-25", "Annual Leave"), ("2026-09-10", "Sick Leave"), ("2026-09-22", "Sick Leave"), ("2026-09-24", "Annual Leave"), ("2026-11-06", "Annual Leave")])]


def one_condition(clause, rows):
    for op in (">=", "<="):
        if op in clause:
            f, v = clause.split(op)
            return [r for r in rows if (r[f.strip()] >= v.strip() if op == ">=" else r[f.strip()] <= v.strip())]
    f, v = clause.split(" LIKE ")
    return [r for r in rows if r[f.strip()].startswith(v.strip().strip("'").rstrip("%"))]


def pod_like_yours():
    """Behaves as the probe showed on the real pod: one condition filters; with ';' only the LAST condition is applied; 'and' gives a 400."""
    async def call(tool, args):
        q = args.get("q")
        if q and " and " in q.lower():
            raise ToolError("Error executing tool query_resource: [400] : An instance of type class java.sql.Date cannot be created from string startDate <= 2025-12-31")
        rows = one_condition(q.split(";")[-1], ABS) if q else list(ABS)
        return {"items": rows[:args["limit"]], "totalResults": len(rows)}
    return call


def test_a_dropped_condition_is_visible_in_the_totals_and_the_window_count():
    """The September sick-leave question: the bot sent three valid conditions and the pod applied one. Compare spellings and the gap shows."""
    win = ("2026-09-01", "2026-09-30")
    r = asyncio.run(probe_filters(pod_like_yours(), "absences",
                                  ["absenceType LIKE 'Sick%'", "startDate >= 2026-09-01",
                                   "startDate >= 2026-09-01;startDate <= 2026-09-30",
                                   "startDate >= 2026-09-01;startDate <= 2026-09-30;absenceType LIKE 'Sick%'",
                                   "startDate >= 2026-09-01 and startDate <= 2026-09-30", None],
                                  ["startDate"], limit=20, window=win))
    sick, lower, two, three, joined, nothing = r
    assert sick.total == 4 and lower.total == 4 and three.total == sick.total        # the date conditions changed nothing: only the last one was applied
    assert two.total == 8 and two.total != lower.total                               # the lower bound was dropped, the upper kept
    assert not joined.ok and "java.sql.Date" in joined.error                         # 'and' is rejected, with the pod's own words
    assert nothing.total == len(ABS) and nothing.q is None
    assert three.outside == (2, 4) and lower.outside == (1, 4)                       # rows outside September are counted
    text = " ".join(filter_verdicts(r))
    assert "OUTSIDE   startDate >= 2026-09-01;startDate <= 2026-09-30;absenceType LIKE 'Sick%'   (2 of 4 rows read" in text
    assert "REJECTED  startDate >= 2026-09-01 and startDate <= 2026-09-30" in text


def test_the_filter_probe_keeps_only_the_fields_you_asked_to_see():
    r = asyncio.run(probe_filters(pod_like_yours(), "absences", ["startDate >= 2026-09-01"], ["startDate"], limit=3))
    assert set(r[0].shown) == {"startDate"} and "Secret Name" not in repr(r)
    assert outside_window(["2026-09-10T00:00:00", "2026-11-06"], "2026-09-01", "2026-09-30") == (1, 2)
    assert outside_window([], "a", "b") == (0, 0)


def test_the_recipes_teach_one_condition_per_attribute_and_no_longer_teach_the_semicolon():
    from pathlib import Path
    text = " ".join((Path(__file__).resolve().parents[1] / "skill" / "recipes.md").read_text(encoding="utf-8").split())   # line breaks do not matter
    assert "ONE condition per attribute" in text and "collapse silently to the last one" in text and "minus total(X >= the day after TO)" in text
    assert "Filters, HR and finance" in text and "a typed search given both date_from and date_to" in text and "on invoices, 2026-11-05" in text
    assert "join conditions with a semicolon" not in text and "pod keeps only the LAST" not in text
    for good in ("personAbsenceEntryId", "personNumber", "absenceType", "startDate", "endDate", "absenceDispStatus"):
        assert good in text                                                                # the six field names the probe accepted
    assert "AbsenceId, PersonNumber and StartDate give a 400" in text and "one bad name fails the whole call" in text


def test_a_finance_range_is_probed_through_the_finance_tool_with_its_own_argument_shapes():
    """The MCP's own invoice search builds a date range from two InvoiceDate conditions joined with ';'. Does the finance REST keep both? Measure it."""
    seen = []
    INV = [{"InvoiceDate": d, "Secret": "x"} for d in ("2025-12-20", "2026-01-05", "2026-01-20", "2026-02-03")]

    async def finance_pod(tool, args):
        seen.append((tool, args))
        q = args.get("q") or ""
        clauses = q.split(";")
        rows = list(INV)
        for c in clauses:                                                         # a finance pod that keeps every condition
            op = ">=" if ">=" in c else "<="
            v = c.split(op)[1].strip().strip("'")
            rows = [r for r in rows if (r["InvoiceDate"] >= v if op == ">=" else r["InvoiceDate"] <= v)]
        return {"items": rows[:args["limit"]], "totalResults": len(rows)}
    r = asyncio.run(probe_filters(finance_pod, "invoices", ["InvoiceDate >= '2026-01-01';InvoiceDate <= '2026-01-31'"], ["InvoiceDate"],
                                  fields=["InvoiceDate"], limit=5, window=("2026-01-01", "2026-01-31"), finance=True))
    tool, args = seen[0]
    assert tool == "fusion_query_resource" and args["fields"] == "InvoiceDate" and args["totalResults"] is True and "total_results" not in args
    assert r[0].ok and r[0].total == 2 and r[0].outside == (0, 2) and "Secret" not in repr(r)          # both conditions applied: two January invoices
    hr = asyncio.run(probe_filters(pod_like_yours(), "absences", ["startDate >= 2026-09-01"], ["startDate"], limit=3))
    assert hr[0].ok                                                                                       # the HR path is unchanged (finance defaults to False)

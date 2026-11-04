import asyncio
import re

from bridge.lookupprobe import advise, build_q, probe_lookup, rows_of, values_of
from bridge.mcp_util import ToolError

PEOPLE = ["17894", "26608", "21267", "15706", "26768", "14989"]


class FakePod:
    """A pod that knows some people and may drop rows from a long 'or' query: q is parsed the way the real one would see it."""

    def __init__(self, known, per_query_cap=None, reject_unquoted=False, in_unlimited=False):
        self.known, self.cap, self.reject_unquoted, self.in_unlimited, self.calls = set(known), per_query_cap, reject_unquoted, in_unlimited, []

    async def __call__(self, name, args):
        assert name == "query_resource" and args["resource"] == "workers"
        self.calls.append(args)
        if self.reject_unquoted and "= '" not in args["q"] and " IN (" not in args["q"]:
            raise ToolError("Error executing tool: Client error '400 Bad Request' for url 'https://pod.example.com/hcmRestApi/resources/11.13.18.05/workers?q=x' For more information check: https://x")
        is_in = " IN (" in args["q"]
        asked = re.findall(r"[0-9]+", re.search(r"IN \(([^)]*)\)", args["q"]).group(1)) if is_in else re.findall(r"PersonNumber = '?([0-9]+)'?", args["q"])
        hits = [n for n in asked if n in self.known]
        if self.cap and not (is_in and self.in_unlimited):
            hits = hits[:self.cap]
        return {"items": [{"PersonNumber": n, "DisplayName": "Secret Name"} for n in hits], "count": len(hits)}


def run(pod, values=PEOPLE, **kw):
    return asyncio.run(probe_lookup(pod, "workers", "PersonNumber", ["PersonNumber", "DisplayName"], list(values), **kw))


def test_a_long_or_query_that_loses_rows_is_told_apart_from_people_the_resource_does_not_show():
    """The transcript: ten person numbers asked for, one name back. Is it the query, or the people?"""
    lossy = run(FakePod(PEOPLE, per_query_cap=1))
    found = {r.label: len(r.found) for r in lossy}
    assert found == {"all 6 in one query, joined with or (quoted)": 1, "all 6 in one query, joined with or (not quoted)": 1,
                     "all 6 in one query, using IN (...)": 1, "batches of 5": 2, "one at a time": 6}
    text = " ".join(advise(lossy))
    assert "These ways of asking LOSE people, so batch the lookup instead:" in text
    assert "returned 1 of 6" in text and "Only 'one at a time' is complete" in text and "using IN" in text
    assert "One call can fetch everyone with IN" not in text                       # IN is just as lossy on this fake pod
    missing = run(FakePod(PEOPLE[:2]))                                             # the resource knows only two of the six people
    text = " ".join(advise(missing))
    assert "4 of 6 people are not returned by ANY way of asking (21267, 15706, 26768, 14989)" in text and "--as-of" in text
    assert "LOSE people" not in text                                               # a different problem, a different advice
    assert "everyone" in " ".join(advise(run(FakePod(PEOPLE)))).lower()


def test_when_in_works_the_probe_says_one_call_can_fetch_everyone():
    results = run(FakePod(PEOPLE, per_query_cap=1, in_unlimited=True))
    assert len(next(r for r in results if "using IN" in r.label).found) == 6
    text = " ".join(advise(results))
    assert "One call can fetch everyone with IN (...)" in text and "LOSE people" in text      # or still loses people, IN does not


def test_a_rejected_way_of_asking_is_recorded_not_fatal_and_dates_are_passed_on():
    pod = FakePod(PEOPLE, reject_unquoted=True)
    results = run(pod, as_of="2026-04-01")
    unquoted = next(r for r in results if "not quoted" in r.label)
    assert unquoted.errors and "400 Bad Request" in unquoted.errors[0] and "mozilla" not in unquoted.errors[0] and not unquoted.found
    assert all(a.get("effective_date") == "2026-04-01" for a in pod.calls)
    assert len(next(r for r in results if r.label == "one at a time").found) == len(PEOPLE)


def test_batches_and_single_lookups_make_the_expected_number_of_calls_and_never_return_names():
    pod = FakePod(PEOPLE)
    results = run(pod)
    assert sorted(r.calls for r in results) == [1, 1, 1, 2, 6]             # all-in-one (or quoted, or not quoted, IN), batches of 5 (6 people = 2), one at a time
    assert [r.label for r in results][-1] == "one at a time" and results[-1].calls == len(PEOPLE)
    assert "Secret Name" not in repr([(r.label, r.found, r.missing, r.errors) for r in results])       # only person numbers are kept


def test_rows_are_found_wherever_the_server_put_them_and_keys_match_in_any_capitalisation():
    assert rows_of({"items": [{"a": 1}, {"a": 2}]}) == [{"a": 1}, {"a": 2}]
    assert len(rows_of({"data": {"result": {"records": [{"x": 1}, {"x": 2}, {"x": 3}]}}, "links": [{"rel": "self"}]})) == 3
    assert rows_of({"count": 0}) == [] and rows_of(None) == []
    assert values_of([{"personNumber": 5}, {"PERSONNUMBER": "5"}, {"personnumber": "7"}, {"other": 1}], "PersonNumber") == ["5", "7"]


def test_the_query_text_quotes_values_and_escapes_a_quote_inside_one():
    assert build_q("PersonNumber", ["1", "2"]) == "PersonNumber = '1' or PersonNumber = '2'"
    assert build_q("PersonNumber", ["1", "2"], quoted=False) == "PersonNumber = 1 or PersonNumber = 2"
    assert build_q("Name", ["O'Neil"]) == "Name = 'O''Neil'"

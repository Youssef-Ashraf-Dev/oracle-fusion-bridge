import asyncio

import pytest

from bridge.verify import FAIL, PASS, WARN, Asked, Case, Verdict, cases, find_total, numbers_in, render, verify_case


@pytest.mark.parametrize("text,expected", [
    ("There are 4,520 sick leave absences starting in 2025.", {2025, 4520}),
    ("Total: 4.520 invoices", {4520}),
    ("about 1 234 567 rows and 12.5% of them", {1234567, 12}),
    ("1,234.56 EGP and 3,75 USD", {1234, 3}),
    ("\u0639\u062f\u062f \u0627\u0644\u0641\u0648\u0627\u062a\u064a\u0631 \u0661\u066c\u0662\u0663\u0664", {1234}),               # Arabic-Indic digits with the Arabic thousands mark
    ("As of 2025-12-31 08:06 UTC from iaehkf-dev1.fa.ocs.oraclecloud.com", set()),                  # a date, a time and a host are not counts
    ("1. INV-12 100\n2. INV-13 90\nIn total 12 invoices", {100, 90, 12}),                           # list markers and invoice ids are not counts
    ("", set())])
def test_the_numbers_in_an_answer_are_read_the_way_people_write_counts(text, expected):
    assert numbers_in(text) == expected


def test_a_total_is_found_wherever_the_server_put_it_and_a_missing_one_names_the_keys():
    assert find_total({"items": [], "totalResults": 4520}) == 4520 and find_total({"a": {"total_results": "7"}}) == 7
    assert find_total([{"x": 1}, {"total": 3}]) == 3
    with pytest.raises(LookupError) as e:
        find_total({"items": [], "count": 1})                                                      # a row count is not a total
    assert "no total in the response" in str(e.value) and "items" in str(e.value)


def truth_returning(*values):
    it = iter(values)

    async def truth(call):
        return next(it)
    return truth


def case(*values):
    return Case("VX", "How many X? Give me just the number.", "X", truth_returning(*values))


def run(c, answer, status="ok"):
    async def ask(q):
        return Asked(answer, status, 0.012, 9.0)

    async def call(*a):
        raise AssertionError("the truth function was faked")
    return asyncio.run(verify_case(c, call, ask))


def test_a_correct_number_passes_a_wrong_one_fails_and_the_verdict_says_both_numbers():
    ok = run(case(4520, 4520), "There are 4,520 invoices. Pod x.example.com, 2026-11-03 09:05.")
    assert ok.status == PASS and ok.note == "" and ok.expected_before == 4520 and ok.cost_usd == 0.012
    bad = run(case(4520, 4520), "There are 4,250 invoices.")
    assert bad.status == FAIL and "the bot gave 4250; Fusion says 4520" in bad.note          # a transposed digit is caught
    none = run(case(4520, 4520), "I could not count them, but there are many.")
    assert none.status == FAIL and "contains no number; Fusion says 4520" in none.note
    erred = run(case(4520), "", status="timeout")
    assert erred.status == FAIL and "gave no answer (status timeout)" in erred.note


def test_data_that_changes_while_the_bot_answers_is_not_held_against_it():
    v = run(case(4520, 4523), "There are 4,523 invoices.")
    assert v.status == PASS and "the data changed while the bot was answering (4520 before, 4523 after)" in v.note
    assert run(case(4520, 4523), "There are 4,000 invoices.").status == FAIL                 # neither number matches


def test_a_small_number_is_doubtful_only_when_the_answer_holds_other_numbers_too():
    """V2 on the real pod: one ledger, and the bot said one. A lone '1' in the answer is unambiguous."""
    assert run(case(1, 1), "The pod has 1 ledger.").status == PASS
    v = run(case(5, 5), "There are 5 ledgers, 25 rows were read and 3 were skipped.")
    assert v.status == WARN and "other numbers too" in v.note and "coincidence is possible" in v.note


def test_when_fusion_itself_cannot_give_the_truth_the_result_is_a_warning_not_a_guess():
    async def broken(call):
        raise LookupError("no total in the response")

    async def ask(q):
        raise AssertionError("the bot must not be asked when there is nothing to compare with")
    v = asyncio.run(verify_case(Case("V9", "q", "l", broken), lambda *a: None, ask))
    assert v.status == WARN and "could not get the true number from Fusion" in v.note


def test_the_shipped_cases_ask_for_one_number_and_call_the_right_tool_with_the_right_arguments():
    from bridge.live import permissions as P
    cs = cases()
    assert len({c.vid for c in cs}) == len(cs) >= 5 and all("just the number" in c.question for c in cs)
    log = []

    async def call(name, args):
        log.append((name, args))
        return {"items": [], "totalResults": 7}
    for c in cs:
        asyncio.run(c.truth(call))
    fscm = [a for n, a in log if n == "fusion_query_resource"]
    assert {"resource": "invoices", "limit": 1, "totalResults": True} in fscm
    assert {"resource": "ledgersLOV", "limit": 1, "totalResults": True} in fscm
    assert {"resource": "invoices", "q": "InvoiceDate >= '2026-01-01'", "limit": 1, "totalResults": True} in fscm
    assert {"resource": "invoices", "q": "InvoiceDate >= '2026-02-01'", "limit": 1, "totalResults": True} in fscm
    hr = [a for n, a in log if n == "query_resource"]
    assert {"resource": "absences", "q": "startDate >= 2026-09-01", "limit": 1, "total_results": True} in hr
    assert all(a["total_results"] is True and a["limit"] == 1 for a in hr)
    # The pod collapses several conditions on one attribute to the last one: a verifier that sent such a filter would count the wrong thing exactly as
    # the bot does and 'confirm' a wrong number. Use the bridge's own guard on EVERY call the verifier makes, so the two can never disagree.
    for n, a in log:
        assert P.repeated_filter_attribute(P.PREFIX + n, a) is None, (n, a)
    assert all(" and " not in str(a.get("q", "")).lower() for _, a in log)


def test_a_window_is_counted_as_the_difference_of_two_totals_each_with_one_date_condition():
    seen = []

    async def call(name, args):
        seen.append(args["q"])
        return {"totalResults": 7778 if "2025-01-01" in args["q"] else 3258}
    v4 = next(c for c in cases() if c.vid == "V4")
    assert asyncio.run(v4.truth(call)) == 4520                                      # sick leave starting in 2025, the figure the bot gave
    assert seen == ["absenceType LIKE 'Sick%';startDate >= 2025-01-01", "absenceType LIKE 'Sick%';startDate >= 2026-01-01"]


def test_the_report_shows_what_fusion_said_and_what_the_bot_said_without_the_answer():
    c = case(1)
    text = render([(c, Verdict("VX", PASS, 4520, 4523, [4523], "the data changed", 0.012, 9)),
                   (c, Verdict("VX", FAIL, 4520, 4520, [4250, 2025], "the bot gave 4250; Fusion says 4520", 0.02, 11))])
    assert "| VX X | PASS | 4520 then 4523 | 4523 |" in text and "| VX X | FAIL | 4520 | 4250, 2025 |" in text


def test_a_failed_case_keeps_the_bots_answer_in_memory_but_the_report_never_holds_it():
    """Whether 7,778 was said as fact or as 'all sick leave, not 2025' decides how serious a failure is: the answer must be readable on request."""
    v = run(case(4520, 4520), "There are 7,778 sick leave absences in 2025.")
    assert v.status == FAIL and v.answer == "There are 7,778 sick leave absences in 2025."
    assert "7,778 sick leave" not in render([(case(1), v)])
    import subprocess, sys
    from pathlib import Path
    out = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "devtools" / "verify_answers.py"), "--help"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60).stdout
    assert "--show-answers" in out


def test_invoices_in_a_month_are_counted_as_the_difference_of_two_totals_each_with_one_date_condition():
    seen = []

    async def call(name, args):
        seen.append((name, args["q"]))
        return {"totalResults": 8519 if "2026-01-01" in args["q"] else 7301}
    v5 = next(c for c in cases() if c.vid == "V5")
    assert asyncio.run(v5.truth(call)) == 1218                                          # dated from 1 January minus dated from 1 February
    assert seen == [("fusion_query_resource", "InvoiceDate >= '2026-01-01'"), ("fusion_query_resource", "InvoiceDate >= '2026-02-01'")]

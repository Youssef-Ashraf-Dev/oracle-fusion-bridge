from bridge.runall import FAIL, MANUAL, PASS, SKIP, WARN, Stage, aggregate, overall, parse_pytest


def test_pytest_output_is_summarised():
    ok = "........\n215 passed, 3 warnings in 18.47s\n"
    assert parse_pytest(ok) == (215, 0, 0, [])
    mixed = "FAILED tests/test_a.py::test_one - boom\nFAILED tests/test_b.py::test_two\n2 failed, 205 passed, 5 skipped in 9.1s\n"
    assert parse_pytest(mixed) == (205, 2, 5, ["tests/test_a.py::test_one", "tests/test_b.py::test_two"])
    assert parse_pytest("1 error in 0.5s")[1] == 1
    assert parse_pytest("") == (0, 0, 0, [])


def test_overall_verdict_is_the_worst_stage_and_skips_do_not_count():
    assert overall([Stage("a", PASS), Stage("b", SKIP)]) == PASS
    assert overall([Stage("a", PASS), Stage("b", WARN)]) == WARN
    assert overall([Stage("a", WARN), Stage("b", FAIL), Stage("c", PASS)]) == FAIL
    assert aggregate([PASS, PASS]) == PASS and aggregate([PASS, WARN]) == WARN and aggregate([WARN, FAIL]) == FAIL


def test_the_manual_checklist_covers_what_only_a_person_can_check():
    text = " ".join(MANUAL).lower()
    for needle in ("/help", "approve", "follow-up", "deny", "unregistered", "stop the bot", "token"):
        assert needle in text

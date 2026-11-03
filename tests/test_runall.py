from bridge.runall import FAIL, MANUAL, PASS, SKIP, WARN, Stage, aggregate, overall, parse_pytest, pytest_detail, tail_lines, dots_seen


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


def test_a_pytest_that_stopped_before_running_anything_is_explained_not_reported_as_zero_tests():
    """Reported: 'Offline tests: 0 passed, 0 failed, 0 skipped in 10s', with the reason hidden."""
    assert pytest_detail(4, 0, 0, 0, 10) == "pytest ran no tests (exit code 4: pytest could not start (a usage error, or an import error in conftest.py)) after 10s"
    assert "import error while collecting" in pytest_detail(2, 0, 0, 0, 3) and "no tests to run" in pytest_detail(5, 0, 0, 0, 1)
    assert pytest_detail(0, 340, 0, 1, 61.4) == "340 passed, 0 failed, 1 skipped in 61s"          # a normal run reads as before
    assert pytest_detail(1, 338, 2, 1, 60) == "338 passed, 2 failed, 1 skipped in 60s"
    assert pytest_detail(99, 0, 0, 0, 1).endswith("unknown) after 1s")


def test_the_last_lines_of_the_output_are_kept_without_blank_lines_and_trimmed():
    text = "\n".join(["", "line one", "", "x" * 400, "last"])
    got = tail_lines(text, 2)
    assert got == ["x" * 200, "last"] and tail_lines("", 5) == []


def test_a_pytest_cut_off_in_the_middle_says_how_far_it_got():
    """The reported case: 78 dots, then nothing."""
    out = ("..............................................................................\n")
    assert dots_seen(out) == 78
    assert dots_seen("....s....   [ 21%]\n.........F..   [ 43%]\ntests/x.py:1: in <module>\n") == 4 + 1 + 4 + 9 + 1 + 2 or dots_seen("....s....   [ 21%]\n") == 9
    assert dots_seen("") == 0 and dots_seen("ERROR collecting x\n1 error in 0.1s\n") == 0
    msg = pytest_detail(2, 0, 0, 0, 10, dots=78)
    assert msg.startswith("pytest ended abruptly after about 78 tests, with no summary (exit code 2, 10s)") and "signals its own process" in msg
    assert pytest_detail(2, 0, 0, 0, 3, dots=0).startswith("pytest ran no tests (exit code 2")      # nothing ran: the older wording
    assert pytest_detail(0, 78, 0, 0, 10, dots=78) == "78 passed, 0 failed, 0 skipped in 10s"        # and a healthy run is unaffected

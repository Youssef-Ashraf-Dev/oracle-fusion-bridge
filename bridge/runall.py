"""Pieces of the one-command test run: stage results, pytest output parsing and the overall verdict."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


@dataclass
class Stage:
    name: str
    status: str
    detail: str = ""
    lines: list[str] = field(default_factory=list)       # extra lines shown under the stage (failures, notes)


def parse_pytest(text: str) -> tuple[int, int, int, list[str]]:
    """(passed, failed, skipped, names of failed tests) from pytest's final output."""
    def n(word: str) -> int:
        m = re.search(rf"(\d+) {word}", text)
        return int(m.group(1)) if m else 0
    failed_names = re.findall(r"^FAILED (\S+)", text, re.M)
    return n("passed"), n("failed") + n("error"), n("skipped"), failed_names


_EXIT = {1: "some tests failed", 2: "pytest was interrupted (usually an import error while collecting tests)",
         3: "pytest crashed internally", 4: "pytest could not start (a usage error, or an import error in conftest.py)",
         5: "pytest found no tests to run"}


def dots_seen(text: str) -> int:
    """How many tests had reported a result (a dot, F, E, s, x or X) before the output ended."""
    n = 0
    for line in text.splitlines():
        body = re.sub(r"\s*\[\s*\d+%\]\s*$", "", line).strip()
        if body and re.fullmatch(r"[.FEsxX]+", body):
            n += len(body)
    return n


def pytest_detail(returncode: int, passed: int, failed: int, skipped: int, seconds: float, dots: int = 0) -> str:
    """One line for the stage: the counts when there are any, and the reason when pytest stopped without a summary."""
    if passed == 0 and failed == 0 and returncode != 0:
        if dots > 0:
            return (f"pytest ended abruptly after about {dots} tests, with no summary (exit code {returncode}, {seconds:.0f}s): "
                    "the process was stopped from outside, for example by a test that signals its own process")
        return f"pytest ran no tests (exit code {returncode}: {_EXIT.get(returncode, 'unknown')}) after {seconds:.0f}s"
    return f"{passed} passed, {failed} failed, {skipped} skipped in {seconds:.0f}s"


def tail_lines(text: str, n: int = 14) -> list[str]:
    return [ln.rstrip()[:200] for ln in text.splitlines() if ln.strip()][-n:]


def overall(stages: list[Stage]) -> str:
    if any(s.status == FAIL for s in stages):
        return FAIL
    return WARN if any(s.status == WARN for s in stages) else PASS


def aggregate(statuses: list[str]) -> str:
    return FAIL if FAIL in statuses else (WARN if WARN in statuses else PASS)


MANUAL = ["In Telegram, from your registered account: /help answers; a question shows a plan with Approve and Deny.",
          "Tap Approve: an answer arrives with a cost line. Ask a follow-up: it continues without a new plan.",
          "Deny a plan: 'Cancelled'. Type 'yes' while a plan waits: it counts as the tap.",
          "From an unregistered account: no reply, and the bot console prints 'Ignored ... not registered'.",
          "Stop the bot (Ctrl+C), start it, send a follow-up: the conversation continues.",
          "Check the console for any line that shows a token or a key (there should be none)."]

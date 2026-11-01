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

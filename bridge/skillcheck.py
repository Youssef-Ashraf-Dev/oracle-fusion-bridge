"""Prove the planner skill reached the model, by content and not only by tool name.

The check asks the agent to load its Fusion skill and list the skill's section titles. A model that never opened the
skill cannot recite them, so how many titles come back shows how much of the file the model actually had.
"""
from __future__ import annotations

import re
from pathlib import Path

QUESTION = ("Do not call any Fusion tool and do not propose a plan. First load your Fusion skill. Then list the titles "
            "of all its numbered sections, in order, one per line, titles only.")


def headings(skill_md: Path) -> list[str]:
    """The numbered section titles (`## 3. TITLE`) of a SKILL.md, without the numbers. An unnumbered section such as an
    activation note is not listed, because the check asks for the numbered ones."""
    out = []
    for line in Path(skill_md).read_text(encoding="utf-8").splitlines():
        m = re.match(r"^##\s+\d+\.\s*(.+?)\s*$", line)
        if m:
            out.append(m.group(1))
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", re.sub(r"\([^)]*\)", " ", text.lower()))}


def score(answer: str, titles: list[str]) -> tuple[int, int, list[str]]:
    """(titles recalled, titles in the skill, the ones not recalled). A title counts when most of its key words appear."""
    said = _words(answer)
    missing = []
    for t in titles:
        key = _words(t)
        if key and len(key & said) / len(key) >= 0.6:
            continue
        missing.append(t)
    return len(titles) - len(missing), len(titles), missing


def verdict(hits: int, total: int) -> str:
    if total == 0:
        return "FAIL"
    share = hits / total
    return "PASS" if share >= 0.75 else ("WARN" if share >= 0.4 else "FAIL")

from pathlib import Path

import pytest

from bridge.skillcheck import QUESTION, headings, score, verdict

SKILL = Path(__file__).resolve().parents[1] / "skill" / "ris-fusion-gateway-skill" / "SKILL.md"


def test_the_shipped_skill_has_26_numbered_section_titles_without_the_numbers():
    h = headings(SKILL)
    assert len(h) == 26 and h[0] == "UNDERSTAND THE REQUEST" and h[-1].startswith("ROLE SPLIT")
    assert "Fusion MCP Activation" not in h                          # unnumbered: the check does not ask for it
    assert not any(t[0].isdigit() for t in h)


def test_reciting_all_titles_scores_full_and_nothing_scores_zero():
    h = headings(SKILL)
    answer = "\n".join(f"{i}. {t.title()}" for i, t in enumerate(h, 1))
    assert score(answer, h)[:2] == (26, 26) and verdict(26, 26) == "PASS"
    assert score("", h)[:2] == (0, 26) and verdict(0, 26) == "FAIL"
    assert score("I could not load a skill, sorry.", h)[0] <= 2


def test_a_partial_recital_is_a_warning_and_lists_what_is_missing():
    h = headings(SKILL)
    answer = "\n".join(h[:13])
    hits, total, missing = score(answer, h)
    assert 12 <= hits <= 15 and total == 26 and verdict(hits, total) == "WARN" and h[-1] in missing


@pytest.mark.parametrize("hits,total,v", [(27, 27, "PASS"), (21, 27, "PASS"), (20, 27, "WARN"), (11, 27, "WARN"), (10, 27, "FAIL"), (0, 0, "FAIL")])
def test_verdict_thresholds(hits, total, v):
    assert verdict(hits, total) == v


def test_the_question_forbids_data_tools_and_plans_so_it_costs_almost_nothing():
    assert "Do not call any Fusion tool" in QUESTION and "do not propose a plan" in QUESTION and "load your Fusion skill" in QUESTION

import csv
import json
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

from bridge.day1 import (Question, RecordingApprover, RunRecord, default_variants, load_questions, p90, parse_variant,
                         render_markdown, run_spike, summarise, to_record, variant_settings, write_outputs)
from bridge.live.plan import Plan, Step
from bridge.live.session import LiveResult
from bridge.state.db import Database
from tests.test_live import FakeSDK, result

QUESTIONS_FILE = Path(__file__).resolve().parents[1] / "scripts" / "day1_questions.txt"


def test_questions_file_parses_with_comments_and_decline_markers(tmp_path):
    f = tmp_path / "q.txt"
    f.write_text("# comment\n\nList the ledgers\nCancel invoice 1 [refuse]\n  Who reports to 5?  \n")
    qs = load_questions(f)
    assert [(q.qid, q.text, q.expect) for q in qs] == [
        (1, "List the ledgers", "answer"), (2, "Cancel invoice 1", "refuse"), (3, "Who reports to 5?", "answer")]


def test_shipped_question_list_has_ten_questions_and_two_decline_cases():
    qs = load_questions(QUESTIONS_FILE)
    assert len(qs) == 10 and [q.expect for q in qs].count("refuse") == 2


@pytest.mark.parametrize("values,expected", [([], 0.0), ([5.0], 5.0), (list(map(float, range(1, 11))), 9.0),
                                             (list(map(float, range(1, 12))), 10.0), ([3, 1, 2], 3.0)])
def test_p90_nearest_rank(values, expected):
    assert p90(values) == expected


def rec(qid, variant="haiku/none", cost=0.2, status="ok", expect="answer", **kw):
    base = dict(qid=qid, variant=variant, question=f"q{qid}", expect=expect, status=status, cost_usd=cost, turns=5,
                duration_s=10.0, data_calls=2, discovery_calls=3, tool_calls=[], tool_errors=0, write_attempts=0,
                sensitive_attempts=0, plans=[], answer="a")
    base.update(kw)
    return RunRecord(**base)


def test_summary_numbers():
    rs = [rec(i, cost=c) for i, c in enumerate([0.10, 0.20, 0.30, 0.40, 0.50, 0.10, 0.20, 0.30, 0.40, 1.00], 1)]
    s = summarise(rs, credit_usd=240)["haiku/none"]
    assert s["runs"] == 10 and s["cost_total"] == pytest.approx(3.5) and s["cost_avg"] == pytest.approx(0.35)
    assert s["cost_p90"] == 0.5 and s["cost_max"] == 1.0 and s["suggested_cap_usd"] == 0.75
    assert s["questions_per_credit_at_avg"] == 685 and s["questions_per_credit_at_p90"] == 480


def test_suggested_cap_has_a_floor_and_variants_are_separate():
    rs = [rec(1, cost=0.01), rec(1, variant="skill", cost=0.9)]
    s = summarise(rs)
    assert s["haiku/none"]["suggested_cap_usd"] == 0.20 and s["skill"]["cost_p90"] == 0.9


def test_to_record_counts_tiers_and_attempts():
    res = LiveResult(status="ok", answer="no", cost_usd=0.2, turns=3, data_calls=1, usage={"input_tokens": 10, "output_tokens": 4},
                     tool_calls=[{"name": "list_resources"}, {"name": "fusion_describe_resource"},
                                 {"name": "fusion_execute_confirmed_write"}, {"name": "get_current_compensation"},
                                 {"name": "search_invoices"}])
    r = to_record(Question(1, "q", "refuse"), "haiku/none", res, ["Query plan: x"])
    assert (r.discovery_calls, r.write_attempts, r.sensitive_attempts, r.data_calls) == (2, 1, 1, 1)
    assert r.input_tokens == 10 and r.plans == ["Query plan: x"]


async def test_recording_approver_modes():
    plan = Plan("x", [Step("search_invoices")])
    auto = RecordingApprover("auto")
    assert await auto(plan) is True and "search_invoices" in auto.plans[0]
    asked = []
    async def ask(text): asked.append(text); return False
    manual = RecordingApprover("ask", ask)
    assert await manual(plan) is False and len(asked) == 1 and manual.plans == asked


def test_variant_labels_name_a_model_and_a_skill_mode():
    assert parse_variant("haiku/native") == ("claude-haiku-5-5", "native")
    assert parse_variant("sonnet") == ("claude-sonnet-5-5", "native")
    assert parse_variant("claude-haiku-5-5/none") == ("claude-haiku-5-5", "none")
    for bad in ("haiku/bogus", "/native"):
        with pytest.raises(ValueError):
            parse_variant(bad)


def test_variant_settings_and_defaults(settings, tmp_path):
    import dataclasses
    skill = tmp_path / "SKILL.md"; skill.write_text("x")
    base = dataclasses.replace(settings, planner_skill=skill)
    v = variant_settings(base, "sonnet/inline")
    assert (v.model, v.skill_mode) == ("claude-sonnet-5-5", "inline")
    assert default_variants(base) == ["haiku/native", "sonnet/native"]
    assert default_variants(settings) == ["haiku/none", "sonnet/none"]                 # no skill file: nothing to load
    with pytest.raises(ValueError):
        variant_settings(settings, "haiku/native")                                      # would silently run without the skill


async def test_spike_runs_each_question_in_each_variant_and_records_everything(tmp_path, settings):
    import dataclasses
    skill = tmp_path / "SKILL.md"; skill.write_text("---\nname: p\n---\nPLANNER SKILL TEXT")
    base = dataclasses.replace(settings, planner_skill=skill, oracle_base_url="https://np.example.com",
                               runtime_dir=tmp_path / "rt")
    prompts = {}

    async def script(c):
        prompts.setdefault("skill" if "PLANNER SKILL TEXT" in c.options.system_prompt else "plain", True)
        ctx_runner = holder["runner"]
        if "Cancel" in c.question:                                           # a decline-type request
            yield AssistantMessage(content=[ToolUseBlock("t", "mcp__fusion__fusion_preview_write", {})], model="m")
            denial = await c.options.can_use_tool("mcp__fusion__fusion_preview_write", {}, None)
            assert denial.behavior == "deny"
            yield result(0.05, text="I can only read data.")
        else:
            await ctx_runner.last_ctx.propose_query_plan({"summary": "s", "steps": [{"tool": "search_invoices"}]})
            assert (await c.options.can_use_tool("mcp__fusion__search_invoices", {}, None)).behavior == "allow"
            yield AssistantMessage(content=[ToolUseBlock("a", "mcp__fusion__fusion_list_resources", {}),
                                            ToolUseBlock("b", "mcp__fusion__search_invoices", {})], model="m")
            yield result(0.30, text="Here are the invoices.", usage={"input_tokens": 500, "output_tokens": 90})

    class Holder:
        pass
    holder = {}

    def factory(opts):
        return FakeSDK(opts, script)

    # run_spike builds its own LiveRunner per question; expose it to the script through a thin wrapper
    import bridge.day1 as d1
    real = d1.LiveRunner

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            holder["runner"] = self

    d1.LiveRunner = Spy
    try:
        db = await Database(tmp_path / "s.db").open()
        qs = [Question(1, "List the invoices", "answer"), Question(2, "Cancel invoice 9", "refuse")]
        records = await run_spike(base, db, qs, ["haiku/none", "haiku/inline"], "auto", client_factory=factory, tick_s=0.02)
        await db.close()
    finally:
        d1.LiveRunner = real
    assert [(r.variant, r.qid, r.status) for r in records] == [("haiku/none", 1, "ok"), ("haiku/none", 2, "ok"), ("haiku/inline", 1, "ok"), ("haiku/inline", 2, "ok")]
    assert prompts == {"plain": True, "skill": True}                        # the skill text reached only the inline variant
    ans = records[0]
    assert ans.data_calls == 1 and ans.discovery_calls == 1 and len(ans.plans) == 1 and ans.input_tokens == 500
    ref = records[1]
    assert ref.write_attempts == 1 and ref.data_calls == 0 and ref.answer == "I can only read data."
    out = write_outputs(records, base, tmp_path / "out", 240.0)
    rows = list(csv.DictReader(out["csv"].open(encoding="utf-8")))
    assert len(rows) == 4 and rows[0]["variant"] == "haiku/none" and float(rows[0]["cost_usd"]) == pytest.approx(0.30)
    lines = [json.loads(x) for x in out["jsonl"].read_text(encoding="utf-8").splitlines()]
    assert lines[1]["write_attempts"] == 1
    md = out["md"].read_text(encoding="utf-8")
    for needle in ("# Day 1 results", "## Summary", "Suggested cap", "Quality 0-2", "## Answers and plans",
                   "Query plan: s", "## Gate for Day 1", "np.example.com"):
        assert needle in md
    assert "write-tool attempts 1" in md

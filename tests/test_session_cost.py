"""The engine reports a resumed conversation's running total. One question's cost is the difference."""
import dataclasses
import sqlite3

import pytest

from bridge.live.session import LiveResult, LiveRunner, RunContext, build_options
from bridge.state.db import Database
from tests.test_app_telegram import REAL_ID, Sink, bridge as make_bridge
from tests.test_live import FakeSDK, result, yes


async def run_with(tmp_path, settings, total, prior, name="c"):
    async def script(c):
        yield result(total, text="Answer.")
    db = await Database(tmp_path / f"{name}.db").open()
    res = await LiveRunner(settings, db, yes, lambda o: FakeSDK(o, script), tick_s=0.02).run(1, "q", resume=REAL_ID if prior else None,
                                                                                            prior_cost=prior)
    await db.close()
    return res


async def test_a_follow_up_costs_the_difference_not_the_running_total(tmp_path, settings):
    """The audit log showed $0.0235, $0.0248, $0.0259 for a question and two one-turn follow-ups."""
    res = await run_with(tmp_path, settings, total=0.0262, prior=0.0248)
    assert res.cost_usd == pytest.approx(0.0014) and res.session_cost_total == pytest.approx(0.0262)


async def test_a_first_question_costs_what_the_engine_reports(tmp_path, settings):
    res = await run_with(tmp_path, settings, total=0.0235, prior=0.0)
    assert res.cost_usd == pytest.approx(0.0235) and res.session_cost_total == pytest.approx(0.0235)


async def test_a_total_below_the_prior_is_taken_as_this_questions_own_cost(tmp_path, settings):
    """If the engine ever reports per-question costs, nothing is subtracted."""
    res = await run_with(tmp_path, settings, total=0.0010, prior=0.0248)
    assert res.cost_usd == pytest.approx(0.0010)


async def test_the_per_question_cost_cap_is_widened_by_what_the_conversation_already_cost(tmp_path, settings):
    db = await Database(tmp_path / "t.db").open()
    s = dataclasses.replace(settings, run_budget_usd=0.60)
    assert build_options(s, RunContext(s, db, 1, yes), None).max_budget_usd == pytest.approx(0.60)
    assert build_options(s, RunContext(s, db, 1, yes), REAL_ID, prior_cost=0.41).max_budget_usd == pytest.approx(1.01)
    await db.close()


async def test_the_bridge_hands_the_conversations_cost_in_and_keeps_the_new_total(tmp_path, settings):
    first = LiveResult(status="ok", answer="A.", cost_usd=0.0235, session_cost_total=0.0235, session_id=REAL_ID, duration_s=5)
    second = LiveResult(status="ok", answer="B.", cost_usd=0.0013, session_cost_total=0.0248, session_id=REAL_ID, duration_s=2)
    b, db, runner = await make_bridge(tmp_path, settings, results=[first, second])
    sink = Sink()
    await b.handle("ask", "first", sink.channel())
    assert runner.priors == [0.0] and (await db.get_chat(50))["session_cost"] == pytest.approx(0.0235)
    await b.handle("ask", "follow-up", sink.channel())
    assert runner.priors[1] == pytest.approx(0.0235) and (await db.get_chat(50))["session_cost"] == pytest.approx(0.0248)
    assert sink.texts[-1].endswith("(No new Fusion read \u00b7 $0.001 \u00b7 2s)")     # the footer shows this question's cost
    spent = await db.spend_since(0)
    assert spent == pytest.approx(0.0235 + 0.0013)                                    # /usage adds questions, not running totals
    await db.close()


async def test_a_new_conversation_starts_the_cost_from_zero(tmp_path, settings):
    res = LiveResult(status="ok", answer="A.", cost_usd=0.01, session_cost_total=0.01, session_id=REAL_ID, duration_s=1)
    b, db, runner = await make_bridge(tmp_path, settings, res=res, session_idle_minutes=120)
    await db.set_live_session(50, REAL_ID, 0.5)
    await db.db.execute("UPDATE chats SET updated = updated - 10800 WHERE chat_id = 50")        # idle for three hours
    await db.db.commit()
    await b.handle("ask", "again", Sink().channel())
    assert runner.priors[-1] == 0.0                                                    # an expired conversation is a new one
    await b.handle("new", "", Sink().channel())
    assert (await db.get_chat(50))["live_session_id"] is None
    await db.close()


async def test_a_database_from_before_the_running_cost_was_kept_is_migrated(tmp_path):
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript("CREATE TABLE chats(chat_id INTEGER PRIMARY KEY, live_session_id TEXT, updated REAL);"
                      "INSERT INTO chats VALUES(9, 'old-session', 1.0);")
    con.commit(); con.close()
    db = await Database(p).open()
    assert (await db.get_chat(9))["live_session_id"] == "old-session" and (await db.get_chat(9))["session_cost"] == 0.0
    await db.set_live_session(9, REAL_ID, 0.02)
    assert (await db.get_chat(9))["session_cost"] == pytest.approx(0.02)
    await db.close()

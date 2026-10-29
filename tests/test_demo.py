import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from bridge.app import Bridge, Channel
from bridge.config import Settings
from bridge.demo import BANNER, DemoRunner, make_answer
from bridge.main import make_runner_factory, missing_settings
from bridge.state.db import Database
from bridge.telegram.handlers import COMMANDS, command_menu
from bridge.telegram.ids import senders


async def yes(plan): return True
async def no(plan): return False


async def runner(tmp_path, settings, approver=yes, **kw):
    db = await Database(tmp_path / "d.db").open()
    s = dataclasses.replace(settings, demo_mode=True)
    kw = {"pause_s": 0.0, **kw}
    return DemoRunner(s, db, approver, **kw), db


async def test_demo_goes_through_the_real_plan_approval_and_audit(tmp_path, settings):
    r, db = await runner(tmp_path, settings)
    res = await r.run(5, "list open invoices")
    assert "canned" in BANNER and "do not depend on your question" in BANNER
    assert res.status == "ok" and BANNER in res.answer and "list open invoices" in res.answer and res.session_id.startswith("demo-")
    assert res.cost_usd == 0.0 and res.data_calls == 1
    events = [x["event"] for x in await (await db.db.execute("SELECT event FROM audit")).fetchall()]
    assert "plan_approved" in events and "ask_finished" in events
    plan = await (await db.db.execute("SELECT decision FROM plans")).fetchone()
    assert plan["decision"] == "approved"
    await db.close()


async def test_demo_deny_and_approval_timeout(tmp_path, settings):
    r, db = await runner(tmp_path, settings, approver=no)
    assert (await r.run(5, "q")).status == "denied"
    async def never(plan): await asyncio.sleep(5); return True
    r2, db2 = await runner(tmp_path, dataclasses.replace(settings, approval_timeout_s=0.1), approver=never)
    assert (await r2.run(5, "q")).status == "denied"
    await db.close(); await db2.close()


async def test_demo_follow_up_resumes_the_same_session(tmp_path, settings):
    r, db = await runner(tmp_path, settings)
    first = await r.run(5, "first")
    second = await r.run(5, "and last month?", resume=first.session_id)
    assert second.session_id == first.session_id and "continues your earlier conversation" in second.answer
    assert "continues" not in first.answer
    await db.close()


async def test_demo_error_and_slow_keywords(tmp_path, settings):
    r, db = await runner(tmp_path, settings)
    assert (await r.run(5, "trigger an error please")).status == "error"
    seen = []
    async def status(t): seen.append(t)
    slow, db2 = await runner(tmp_path, settings, slow_s=0.25, status_every_s=0.05)
    res = await slow.run(5, "a slow one", on_status=status)
    assert res.status == "ok" and len(seen) >= 3 and seen[0].startswith("Working...")
    await db.close(); await db2.close()


def test_long_keyword_exceeds_one_telegram_message():
    assert len(make_answer("a long list please", False)) > 10_000
    assert len(make_answer("short", False)) < 800


async def test_demo_through_the_bridge_splits_a_long_answer(tmp_path, settings):
    r, db = await runner(tmp_path, settings)
    await db.add_user(5)
    s = dataclasses.replace(settings, demo_mode=True, send_pace_s=0.0)
    bridge = Bridge(s, db, lambda approver: DemoRunner(s, db, approver, pause_s=0.0))
    texts = []
    async def send(t): texts.append(t)
    async def status(t): texts.append("STATUS " + t)
    await bridge.handle("ask", "a long list", Channel(5, 50, send, yes, status))
    pieces = [t for t in texts if not t.startswith(("Working on it", "STATUS"))]
    assert len(pieces) >= 3 and all(len(p) <= 4000 for p in pieces)
    assert "$0.00" in pieces[-1] and pieces[-1].endswith("s)")
    assert BANNER in pieces[0]
    await db.close()


def test_demo_mode_needs_no_oracle_settings_and_no_model_key():
    base = Settings(telegram_token="1:x")
    assert base.live_ready is False and missing_settings(base) == ["ANTHROPIC_API_KEY"]
    demo = dataclasses.replace(base, demo_mode=True)
    assert demo.live_ready is True and missing_settings(demo) == []
    assert missing_settings(Settings(demo_mode=True)) == ["TELEGRAM_BOT_TOKEN"]


async def test_runner_factory_picks_the_demo_runner_only_in_demo_mode(tmp_path, settings):
    from bridge.live.session import LiveRunner
    db = await Database(tmp_path / "f.db").open()
    assert isinstance(make_runner_factory(dataclasses.replace(settings, demo_mode=True), db)(yes), DemoRunner)
    assert isinstance(make_runner_factory(settings, db)(yes), LiveRunner)
    await db.close()


def test_command_menu_lists_the_commands_the_bot_handles():
    menu = command_menu()
    assert [c.command for c in menu] == [n for n, _ in COMMANDS] == ["ask", "new", "usage", "help"]
    assert all(c.description and len(c.description) <= 256 for c in menu)           # Telegram's limit


def upd(uid, first="A", last=None, username=None, is_bot=False, chat="private", edited=False):
    msg = SimpleNamespace(from_user=SimpleNamespace(id=uid, first_name=first, last_name=last, username=username, is_bot=is_bot),
                          chat=SimpleNamespace(type=chat))
    return SimpleNamespace(message=None if edited else msg, edited_message=msg if edited else None)


def test_senders_lists_unique_private_humans_only():
    got = senders([upd(5, "Sam", "Lee", "sam"), upd(5, "Sam"), upd(6, "Bo"), upd(7, is_bot=True),
                   upd(8, chat="group"), SimpleNamespace(message=None, edited_message=None), upd(9, "Ed", edited=True)])
    assert [g["id"] for g in got] == [5, 6, 9]
    assert got[0] == {"id": 5, "name": "Sam Lee", "username": "@sam"} and got[1]["username"] == ""

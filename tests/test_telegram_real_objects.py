"""The real Telegram handlers, driven with real python-telegram-bot objects. Only the network is faked.

This is the closest to a live bot that can run without a token: commands, plain text, the Approve and Deny buttons,
the requester-only rule, message splitting and the registry all go through the library's own routing.
"""
import asyncio
import dataclasses
import json
import time

import pytest
from telegram import Update
from telegram.request import BaseRequest

from bridge.app import Bridge
from bridge.demo import BANNER, DemoRunner
from bridge.state.db import Database
from bridge.telegram.handlers import ASK_LINE, build_application


class FakeRequest(BaseRequest):
    """Records every Bot API call and answers with minimal valid JSON."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self._mid = 1000

    async def initialize(self): pass
    async def shutdown(self): pass

    @property
    def read_timeout(self): return 5.0

    async def do_request(self, url, method, request_data=None, *a, **k):
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append((name, params))
        if name == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Bridge", "username": "bridge_bot"}
        elif name in ("sendMessage", "editMessageText"):
            self._mid += 1
            result = {"message_id": params.get("message_id", self._mid), "date": int(time.time()),
                      "chat": {"id": int(params["chat_id"]) if "chat_id" in params else 1, "type": "private"},
                      "from": {"id": 1, "is_bot": True, "first_name": "Bridge", "username": "bridge_bot"},
                      "text": params.get("text", "")}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def sent(self, name="sendMessage"):
        return [p for n, p in self.calls if n == name]


def msg(app, uid, text, uid_seq=[0]):
    uid_seq[0] += 1
    m = {"message_id": 10 + uid_seq[0], "date": int(time.time()), "chat": {"id": uid, "type": "private"},
         "from": {"id": uid, "is_bot": False, "first_name": "T"}, "text": text}
    if text.startswith("/"):
        m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.de_json({"update_id": 500 + uid_seq[0], "message": m}, app.bot)


def tap(app, uid, data, message_text="plan"):
    return Update.de_json({"update_id": 900, "callback_query": {
        "id": "cb1", "chat_instance": "ci", "from": {"id": uid, "is_bot": False, "first_name": "T"}, "data": data,
        "message": {"message_id": 77, "date": int(time.time()), "chat": {"id": uid, "type": "private"}, "text": message_text}}}, app.bot)


async def setup(tmp_path, settings, registered=(5,), approval_timeout_s=3):
    db = await Database(tmp_path / "r.db").open()
    for u in registered:
        await db.add_user(u)
    s = dataclasses.replace(settings, demo_mode=True, send_pace_s=0.0, approval_timeout_s=approval_timeout_s)
    bridge = Bridge(s, db, lambda ap: DemoRunner(s, db, ap, pause_s=0.0))
    req = FakeRequest()
    app = build_application("1:fake-token", bridge, request=req)
    await app.initialize()
    return app, req, db


def button_data(req):
    for p in req.sent():
        if "reply_markup" in p:
            kb = p["reply_markup"]["inline_keyboard"][0]
            return {b["text"]: b["callback_data"] for b in kb}
    return {}


async def wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_unregistered_user_gets_no_reply_at_all(tmp_path, settings):
    app, req, db = await setup(tmp_path, settings)
    await app.process_update(msg(app, 999, "/ask list invoices"))
    await app.process_update(msg(app, 999, "hello"))
    await app.process_update(msg(app, 999, "/help"))
    assert req.sent() == []                                                         # FR-01, with real routing
    await app.shutdown(); await db.close()


async def test_ask_with_buttons_end_to_end(tmp_path, settings):
    app, req, db = await setup(tmp_path, settings)
    run = asyncio.create_task(app.process_update(msg(app, 5, "/ask list open invoices")))
    assert await wait_for(lambda: button_data(req))
    buttons = button_data(req)
    assert set(buttons) == {"Approve", "Deny"}
    plan_msg = next(p for p in req.sent() if "reply_markup" in p)
    assert "DEMO plan" in plan_msg["text"] and "list open invoices" in plan_msg["text"] and "always the same" in plan_msg["text"]
    assert plan_msg["text"].endswith(ASK_LINE) and "only reads data" in plan_msg["text"] and "type yes or no" in plan_msg["text"]
    await app.process_update(tap(app, 7, buttons["Approve"]))                       # a different person: refused
    assert any("Only the requester" in str(p.get("text", "")) for p in req.sent("answerCallbackQuery")) or \
        req.sent("answerCallbackQuery")[-1].get("show_alert")
    assert not run.done()
    await app.process_update(tap(app, 5, buttons["Approve"], plan_msg["text"]))
    await asyncio.wait_for(run, 5)
    texts = [p["text"] for p in req.sent()]
    assert texts[0].startswith("Working on it")
    assert any(BANNER in t for t in texts) and texts[-1].endswith("s)") and "$0.00" in texts[-1]
    assert req.sent("editMessageText")[-1]["text"].endswith("Approved.")            # the plan message is updated
    await app.shutdown(); await db.close()


async def test_deny_cancels_and_reads_nothing(tmp_path, settings):
    app, req, db = await setup(tmp_path, settings)
    run = asyncio.create_task(app.process_update(msg(app, 5, "/ask anything")))
    assert await wait_for(lambda: button_data(req))
    await app.process_update(tap(app, 5, button_data(req)["Deny"]))
    await asyncio.wait_for(run, 5)
    assert "wasn't approved" in req.sent()[-1]["text"] and not any(BANNER in p["text"] for p in req.sent())
    await app.shutdown(); await db.close()


async def test_plain_text_counts_as_a_question_and_commands_answer(tmp_path, settings):
    app, req, db = await setup(tmp_path, settings)
    run = asyncio.create_task(app.process_update(msg(app, 5, "what is open?")))
    assert await wait_for(lambda: button_data(req))
    await app.process_update(tap(app, 5, button_data(req)["Approve"]))
    await asyncio.wait_for(run, 5)
    assert any("what is open?" in p["text"] and BANNER in p["text"] for p in req.sent())
    await app.process_update(msg(app, 5, "/usage"))
    await app.process_update(msg(app, 5, "/help"))
    await app.process_update(msg(app, 5, "/new"))
    texts = [p["text"] for p in req.sent()]
    assert any("Your questions today: 1 of 100" in t for t in texts)
    assert any("I can only read" in t for t in texts) and any("Started fresh" in t for t in texts)
    await app.shutdown(); await db.close()


async def test_long_answer_arrives_in_pieces(tmp_path, settings):
    app, req, db = await setup(tmp_path, settings)
    run = asyncio.create_task(app.process_update(msg(app, 5, "/ask a long list")))
    assert await wait_for(lambda: button_data(req))
    await app.process_update(tap(app, 5, button_data(req)["Approve"]))
    await asyncio.wait_for(run, 5)
    pieces = [p["text"] for p in req.sent() if not p["text"].startswith(("Working on it", "DEMO plan", "Query plan"))]
    assert len(pieces) >= 3 and all(len(t) <= 4000 for t in pieces)
    await app.shutdown(); await db.close()


async def test_second_question_while_one_is_running_is_refused(tmp_path, settings):
    app, req, db = await setup(tmp_path, settings)
    first = asyncio.create_task(app.process_update(msg(app, 5, "/ask first")))
    assert await wait_for(lambda: button_data(req))
    await app.process_update(msg(app, 5, "/ask second"))
    assert any("already running" in p["text"] for p in req.sent())
    await app.process_update(tap(app, 5, button_data(req)["Deny"]))
    await asyncio.wait_for(first, 5)
    await app.shutdown(); await db.close()


async def test_silence_past_the_approval_limit_ends_the_run_and_expires_the_buttons(tmp_path, settings):
    app, req, db = await setup(tmp_path, settings, approval_timeout_s=0.2)
    await asyncio.wait_for(app.process_update(msg(app, 5, "/ask nobody taps")), 5)
    assert "wasn't approved" in req.sent()[-1]["text"]
    assert req.sent("editMessageText")[-1]["text"].endswith("Expired.")
    stale = button_data(req)["Approve"]
    await app.process_update(tap(app, 5, stale))                                   # a late tap on an old button
    assert req.sent("answerCallbackQuery")[-1].get("show_alert")
    await app.shutdown(); await db.close()


async def test_typing_yes_answers_a_waiting_plan_like_the_button(tmp_path, settings):
    app, req, db = await setup(tmp_path, settings)
    run = asyncio.create_task(app.process_update(msg(app, 5, "/ask list open invoices")))
    assert await wait_for(lambda: button_data(req))
    await app.process_update(msg(app, 5, "Yes"))                                  # typed, not tapped
    await asyncio.wait_for(run, 5)
    texts = [p["text"] for p in req.sent()]
    assert any(BANNER in t for t in texts) and not any("already running" in t for t in texts)
    assert req.sent("editMessageText")[-1]["text"].endswith("Approved.")
    await app.shutdown(); await db.close()


async def test_typing_no_denies_and_other_text_is_still_refused_while_waiting(tmp_path, settings):
    app, req, db = await setup(tmp_path, settings)
    run = asyncio.create_task(app.process_update(msg(app, 5, "/ask first")))
    assert await wait_for(lambda: button_data(req))
    await app.process_update(msg(app, 5, "what about last month?"))                # a real question: not an answer
    assert any("already running" in p["text"] for p in req.sent())
    await app.process_update(msg(app, 5, "no"))
    await asyncio.wait_for(run, 5)
    assert "wasn't approved" in req.sent()[-1]["text"]
    await app.shutdown(); await db.close()


async def test_yes_from_someone_else_does_not_answer_my_plan(tmp_path, settings):
    app, req, db = await setup(tmp_path, settings, registered=(5, 6))
    run = asyncio.create_task(app.process_update(msg(app, 5, "/ask first")))
    assert await wait_for(lambda: button_data(req))
    other = asyncio.create_task(app.process_update(msg(app, 6, "yes")))           # user 6 has nothing waiting: it is a question
    await asyncio.sleep(0.2)
    assert not run.done()                                                          # user 5's plan was not answered by user 6
    await app.process_update(msg(app, 5, "ok"))
    await asyncio.wait_for(run, 5)
    other.cancel()
    await asyncio.gather(other, return_exceptions=True)
    await app.shutdown(); await db.close()


async def test_an_unhandled_error_is_logged_and_the_user_is_told_once(tmp_path, settings, caplog):
    import logging
    app, req, db = await setup(tmp_path, settings)
    with caplog.at_level(logging.ERROR, logger="bridge.telegram"):
        await app.process_error(msg(app, 5, "/help"), RuntimeError("boom"))
    assert req.sent()[-1]["text"] == "Something went wrong. It has been logged."
    assert any("Unhandled error in a Telegram handler" in r.getMessage() for r in caplog.records)
    await app.shutdown(); await db.close()


async def test_incoming_messages_are_logged_without_their_text(tmp_path, settings, caplog):
    import logging
    app, req, db = await setup(tmp_path, settings)
    with caplog.at_level(logging.INFO, logger="bridge.telegram"):
        await app.process_update(msg(app, 5, "/usage"))
        run = asyncio.create_task(app.process_update(msg(app, 5, "what are the open invoices for acme?")))
        assert await wait_for(lambda: button_data(req))
        await app.process_update(msg(app, 5, "yes"))
        await asyncio.wait_for(run, 5)
    lines = [r.getMessage() for r in caplog.records]
    assert any(l.startswith("message user=5 command=usage") for l in lines)
    assert any(l.startswith("message user=5 command=ask chars=") for l in lines)
    assert any(l.startswith("message user=5 command=plan-answer") for l in lines)
    assert not any("acme" in l for l in lines)
    await app.shutdown(); await db.close()

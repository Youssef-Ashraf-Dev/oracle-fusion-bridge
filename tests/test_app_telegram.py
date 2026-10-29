import asyncio
import dataclasses
import datetime as dt
from types import SimpleNamespace

import pytest

from bridge.app import Bridge, Channel, cycle_start
from bridge.live.plan import Plan, Step
from bridge.live.session import LiveResult
from bridge.state.db import Database
from bridge.telegram import handlers as H


class Sink:
    def __init__(self):
        self.texts = []

    def channel(self, user=5, chat=50, approve=None):
        async def text(t): self.texts.append(t)
        async def status(t): self.texts.append("STATUS " + t)
        async def yes(plan): return True
        return Channel(user, chat, text, approve or yes, status)


class FakeRunner:
    """Stands in for LiveRunner: returns a scripted LiveResult and records how it was called."""
    def __init__(self, res, delay=0.0):
        self.res, self.delay, self.calls = res, delay, []

    def factory(self, approver):
        outer = self

        class R:
            async def run(self, user_id, question, resume=None, on_status=None):
                outer.calls.append((user_id, question, resume))
                await asyncio.sleep(outer.delay)
                return outer.res
        return R()


async def bridge(tmp_path, settings, res=None, delay=0.0, **over):
    db = await Database(tmp_path / "b.db").open()
    await db.add_user(5, "sam")
    cfg = dict(oracle_base_url="https://np.example.com", oracle_user="u", oracle_password="p", send_pace_s=0.0)
    cfg.update(over)
    s = dataclasses.replace(settings, **cfg)
    runner = FakeRunner(res or LiveResult(status="ok", answer="42 open invoices.", cost_usd=0.31, duration_s=12,
                                          session_id="s9"), delay)
    return Bridge(s, db, runner.factory), db, runner


async def test_unregistered_user_gets_nothing_and_is_logged(tmp_path, settings):
    b, db, runner = await bridge(tmp_path, settings)
    sink = Sink()
    await b.handle("ask", "open invoices?", sink.channel(user=999))
    assert sink.texts == [] and runner.calls == []                                   # FR-01
    row = await (await db.db.execute("SELECT event FROM audit")).fetchone()
    assert row["event"] == "rejected_user"
    await db.close()


async def test_answer_is_delivered_as_text_with_footer_and_session_saved(tmp_path, settings):
    b, db, runner = await bridge(tmp_path, settings)
    sink = Sink()
    await b.handle("ask", "open invoices?", sink.channel())
    assert sink.texts[-1] == "42 open invoices.\n\n($0.31, 12s)"                      # FR-09
    assert (await db.get_chat(50))["live_session_id"] == "s9"
    await b.handle("ask", "and last month?", sink.channel())                         # FR-03: follow-up resumes
    assert runner.calls[1] == (5, "and last month?", "s9")
    await b.handle("new", "", sink.channel())
    await b.handle("ask", "fresh", sink.channel())
    assert runner.calls[2][2] is None
    await db.close()


async def test_long_answer_arrives_in_pieces_under_the_limit(tmp_path, settings):
    long = "\n".join(f"Invoice {i}: ACME Ltd, EGP {i}00.00" for i in range(500))
    assert len(long) > 10_000
    b, db, _ = await bridge(tmp_path, settings, res=LiveResult(status="ok", answer=long, cost_usd=0.5, duration_s=30))
    sink = Sink()
    await b.handle("ask", "all invoices", sink.channel())
    answer_pieces = [t for t in sink.texts if not t.startswith(("Working on it", "STATUS"))]
    assert len(answer_pieces) >= 3 and all(len(p) <= 4000 for p in answer_pieces)
    assert answer_pieces[-1].endswith("($0.50, 30s)")
    await db.close()


async def test_pieces_are_paced(tmp_path, settings):
    long = "\n".join("x" * 100 for _ in range(120))
    b, db, _ = await bridge(tmp_path, settings, res=LiveResult(status="ok", answer=long), send_pace_s=0.05)
    sink = Sink()
    t0 = asyncio.get_running_loop().time()
    await b.handle("ask", "q", sink.channel())
    assert asyncio.get_running_loop().time() - t0 >= 0.05                           # NFR-11
    await db.close()


async def test_commands_and_empty_question(tmp_path, settings):
    b, db, runner = await bridge(tmp_path, settings)
    sink = Sink()
    await b.handle("help", "", sink.channel())
    await b.handle("ask", "   ", sink.channel())
    assert "I can only read" in sink.texts[0] and "Add your question" in sink.texts[1] and runner.calls == []
    await db.close()


async def test_one_run_per_user_and_global_concurrency(tmp_path, settings):
    b, db, runner = await bridge(tmp_path, settings, delay=0.3, max_concurrent_runs=1)
    await db.add_user(6)
    s1, s2, s3 = Sink(), Sink(), Sink()
    t1 = asyncio.create_task(b.handle("ask", "a", s1.channel(user=5, chat=50)))
    await asyncio.sleep(0.05)
    await b.handle("ask", "again", s2.channel(user=5, chat=50))                     # same user while running
    assert any("already running" in t for t in s2.texts)
    t3 = asyncio.create_task(b.handle("ask", "b", s3.channel(user=6, chat=60)))
    await asyncio.sleep(0.05)
    assert len(runner.calls) == 1                                                  # user 6 waits for the one slot
    await asyncio.gather(t1, t3)
    assert len(runner.calls) == 2
    await db.close()


async def test_daily_quota(tmp_path, settings):
    b, db, _ = await bridge(tmp_path, settings, user_daily_runs=1)
    sink = Sink()
    await b.handle("ask", "a", sink.channel())
    await b.handle("ask", "b", sink.channel())
    assert any("Daily limit" in t for t in sink.texts)
    await db.close()


async def test_refuses_cleanly_when_live_is_not_configured(tmp_path, settings):
    b, db, runner = await bridge(tmp_path, settings, oracle_password="")
    sink = Sink()
    await b.handle("ask", "q", sink.channel())
    assert sink.texts == ["Live queries aren't configured on this bridge yet."] and runner.calls == []   # FR-14
    await db.close()


@pytest.mark.parametrize("res,needles", [
    (LiveResult(status="denied"), ["wasn't approved"]),
    (LiveResult(status="timeout"), ["too long"]),
    (LiveResult(status="budget"), ["cost limit"]),
    (LiveResult(status="turns"), ["more steps", "narrower"]),
    (LiveResult(status="no_answer"), ["rephrasing"]),
    (LiveResult(status="error", message="CLIConnectionError: secret path /home/x"), ["has been logged"]),
    (LiveResult(status="no_answer", tool_errors=["Error executing tool: [Errno -2] Name or service not known"]),
     ["could not be reached"]),
])
async def test_failures_become_one_plain_message(tmp_path, settings, res, needles):
    b, db, _ = await bridge(tmp_path, settings, res=res)
    sink = Sink()
    await b.handle("ask", "q", sink.channel())
    final = sink.texts[-1]
    assert all(n in final for n in needles) and "secret path" not in final           # FR-10
    await db.close()


async def test_usage_and_cycle_math(tmp_path, settings):
    b, db, _ = await bridge(tmp_path, settings)
    await db.record_run(5, "ask", "ok", 1.5, 3)
    sink = Sink()
    await b.handle("usage", "", sink.channel())
    assert "of 25" in sink.texts[0] and "$1.50 of $240" in sink.texts[0]
    mid = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
    early = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
    assert dt.datetime.fromtimestamp(cycle_start(mid, 7), dt.timezone.utc).date() == dt.date(2026, 11, 7)
    assert dt.datetime.fromtimestamp(cycle_start(early, 7), dt.timezone.utc).date() == dt.date(2026, 10, 7)
    await db.close()


# ---- Telegram glue ---------------------------------------------------------------

class FakeBot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw):
        m = SimpleNamespace(text=text, edits=[], kw=kw)
        async def edit(t, **k): m.edits.append(t)
        m.edit_text = edit
        self.sent.append(m)
        return m


def fake_app():
    return SimpleNamespace(bot=FakeBot(), bot_data={})


def cb(app, pid, choice, user):
    answers, edits = [], []
    async def answer(*a, **k): answers.append((a, k))
    async def edit(t, **k): edits.append(t)
    q = SimpleNamespace(data=f"plan:{pid}:{choice}", from_user=SimpleNamespace(id=user), answer=answer,
                        edit_message_text=edit, message=SimpleNamespace(text="Query plan: x\n\nApprove these data calls?"))
    return SimpleNamespace(callback_query=q), SimpleNamespace(application=app), answers, edits


async def test_approver_only_requester_can_answer():
    app = fake_app()
    ap = H.TelegramApprover(app, 50, 5)
    task = asyncio.create_task(ap(Plan("open invoices", [Step("search_invoices")])))
    await asyncio.sleep(0.02)
    pid = next(iter(H._pending(app)))
    assert app.bot.sent[0].kw["reply_markup"] is not None and "search_invoices" in app.bot.sent[0].text
    upd, ctx, answers, _ = cb(app, pid, "y", user=777)                              # someone else taps Approve
    await H.on_callback(upd, ctx)
    assert not task.done() and answers[0][1].get("show_alert")
    upd, ctx, _, edits = cb(app, pid, "y", user=5)                                  # the requester
    await H.on_callback(upd, ctx)
    assert await task is True and edits[0].endswith("Approved.") and H._pending(app) == {}


async def test_approver_deny_expiry_and_stale_button():
    app = fake_app()
    ap = H.TelegramApprover(app, 50, 5)
    t = asyncio.create_task(ap(Plan("x", [Step("search_invoices")])))
    await asyncio.sleep(0.02)
    upd, ctx, _, _ = cb(app, next(iter(H._pending(app))), "n", user=5)
    await H.on_callback(upd, ctx)
    assert await t is False
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ap(Plan("x", [Step("search_invoices")])), 0.05)
    assert H._pending(app) == {} and app.bot.sent[-1].edits[-1].endswith("Expired.")
    upd, ctx, answers, _ = cb(app, "gone", "y", user=5)                             # stale button
    await H.on_callback(upd, ctx)
    assert answers[0][1].get("show_alert")


def test_application_registers_the_commands_and_plain_text(settings):
    app = H.build_application("123456:ABC-DEF", Bridge(settings, None, lambda a: None))
    names = {c for h in app.handlers[0] for c in getattr(h, "commands", [])}
    assert {"start", "help", "new", "usage", "ask"} <= names and "report" not in names

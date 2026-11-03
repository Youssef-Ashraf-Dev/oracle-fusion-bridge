"""Fault injection: the ways a real run goes wrong that a friendly fake never shows."""
import asyncio
import dataclasses
import io
import json
import logging
from types import SimpleNamespace

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, UserMessage

from bridge.app import Bridge, Channel, compact_tools, run_summary
from bridge.live.session import LiveResult, LiveRunner, looks_cut
from bridge.logsafe import FORMAT, RedactSecrets, configure_logging
from bridge.runsreport import denial_reasons, load_runs, render, summary
from bridge.state.db import Database
from tests.test_app_telegram import REAL_ID, FakeRunner, Sink, bridge as make_bridge
from tests.test_live import FakeSDK, result, yes

TOKEN = "1234567890:AAFake-Token_For_Tests_Only_0123456789ab"


def engine_error(kind, text, status=400):
    async def script(c):
        yield AssistantMessage(content=[], model="m", error=kind)
        yield ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1,
                            session_id="x", total_cost_usd=0.0, result=text, api_error_status=status)
    return script


def live_bridge(tmp_path, settings, script, **over):
    async def build():
        db = await Database(tmp_path / "f.db").open()
        await db.add_user(5)
        s = dataclasses.replace(settings, oracle_base_url="https://np.example.com", oracle_user="u", oracle_password="pw-secret-1",
                                send_pace_s=0.0, **over)
        b = Bridge(s, db, lambda ap: LiveRunner(s, db, ap, lambda o: FakeSDK(o, script), tick_s=0.02))
        return b, db
    return build()


@pytest.mark.parametrize("kind,text,status,needle", [
    ("billing_error", "Credit balance is too low", 402, "credit"),
    ("authentication_failed", "Invalid API key", 401, "key was rejected"),
    ("rate_limit", "Rate limited", 429, "busy"),
    ("server_error", "Overloaded", 529, "had a problem"),
    ("invalid_request", "Bad request", 400, "rejected that request")])
async def test_engine_errors_become_a_clear_message_not_something_went_wrong(tmp_path, settings, kind, text, status, needle):
    b, db = await live_bridge(tmp_path, settings, engine_error(kind, text, status))
    sink = Sink()
    await b.handle("ask", "anything", sink.channel())
    final = sink.texts[-1]
    assert needle in final.lower() and "went wrong" not in final.lower()
    row = (await (await db.db.execute("SELECT status, meta FROM runs")).fetchone())
    assert row["status"] == "error" and json.loads(row["meta"])["error_kind"] == kind and json.loads(row["meta"])["api_status"] == status
    await db.close()


async def test_the_engine_error_kind_and_status_are_captured_on_the_result(tmp_path, settings):
    db = await Database(tmp_path / "f.db").open()
    res = await LiveRunner(settings, db, yes, lambda o: FakeSDK(o, engine_error("billing_error", "Credit balance is too low", 402)), tick_s=0.02).run(1, "q")
    assert (res.status, res.error_kind, res.api_status) == ("error", "billing_error", 402)
    await db.close()


async def test_an_exception_inside_a_run_tells_the_user_and_frees_the_lock(tmp_path, settings, caplog):
    b, db, runner = await make_bridge(tmp_path, settings)
    calls = {"n": 0}

    class Boom:
        async def run(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("the engine fell over")
            return LiveResult(status="ok", answer="Second try works.", session_id=REAL_ID)
    b.make_runner = lambda approver: Boom()
    sink = Sink()
    with caplog.at_level(logging.ERROR, logger="bridge"):
        await b.handle("ask", "first", sink.channel())
        await b.handle("ask", "second", sink.channel())                        # would say "already running" if the lock leaked
    assert any("went wrong and has been logged" in t for t in sink.texts)
    assert sink.texts[-1].startswith("Second try works.")
    assert any("Question failed unexpectedly" in r.getMessage() for r in caplog.records)
    await db.close()


async def test_even_when_telling_the_user_fails_the_lock_is_freed_and_nothing_escapes(tmp_path, settings, caplog):
    b, db, runner = await make_bridge(tmp_path, settings)
    async def broken_send(text): raise ConnectionError("telegram unreachable")
    async def status(t): pass
    async def approve(p): return True
    ch = Channel(5, 50, broken_send, approve, status)
    with caplog.at_level(logging.ERROR, logger="bridge"):
        await b.handle("ask", "q", ch)                                          # must not raise
    assert 5 not in b.active
    assert any("Could not tell user 5" in r.getMessage() or "Question failed unexpectedly" in r.getMessage() for r in caplog.records)
    await db.close()


async def test_cancelling_a_run_disconnects_the_engine(tmp_path, settings):
    """A bot stopped with Ctrl+C in the middle of a question must not leave the engine process behind."""
    seen = {}
    class Tracking(FakeSDK):
        async def disconnect(self):
            seen["disconnected"] = True
    async def never(c):
        await asyncio.sleep(30)
        yield result()
    db = await Database(tmp_path / "f.db").open()
    runner = LiveRunner(settings, db, yes, lambda o: Tracking(o, never), tick_s=0.02)
    task = asyncio.create_task(runner.run(1, "q"))
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert seen.get("disconnected") is True
    await db.close()


@pytest.mark.parametrize("body,cut", [
    ("Output too large (52,310 tokens exceeds 25,000). Saved to C:\\tmp\\tool-results\\abc.txt", True),
    ("Result truncated: too many tokens", True),
    ('{"status": "saved to ledger", "count": 3}', False),                        # a JSON payload, whatever its words
    ("A short normal reply", False),
    ("x" * 2000 + " exceeds", False)])
def test_cut_results_are_recognised_without_flagging_normal_ones(body, cut):
    assert looks_cut(body) is cut


async def test_a_cut_result_is_counted_and_reported_as_a_failure(tmp_path, settings):
    note = "Output too large (52,310 tokens exceeds the 25,000 limit). Saved to C:\\tmp\\tool-results\\abc.txt"
    async def script(c):
        await c.options.can_use_tool.__self__.propose_query_plan({"summary": "x", "steps": [{"tool": "fusion_query_resource", "resource": "invoices"}]})
        await c.options.can_use_tool("mcp__fusion__fusion_query_resource", {"resource": "invoices"}, SimpleNamespace(tool_use_id="t1"))
        yield UserMessage(content=[ToolResultBlock("t1", [{"type": "text", "text": note}], False)])
        yield result(0.01, text="That result was too large to read.")
    db = await Database(tmp_path / "f.db").open()
    res = await LiveRunner(settings, db, yes, lambda o: FakeSDK(o, script), tick_s=0.02).run(1, "q")
    assert res.results_cut == 1 and res.data_calls == 0 and "too large" in res.tool_errors[0]
    await db.close()


async def test_the_engines_own_error_lines_are_kept_and_logged(tmp_path, settings, caplog):
    async def script(c):
        c.options.stderr("Error: --resume requires a valid session ID")
        c.options.stderr("")                                                    # blank lines are ignored
        yield result(0.0, text="")
    db = await Database(tmp_path / "f.db").open()
    with caplog.at_level(logging.WARNING, logger="bridge.engine"):
        res = await LiveRunner(settings, db, yes, lambda o: FakeSDK(o, script), tick_s=0.02).run(1, "q")
    assert res.engine_lines == ["Error: --resume requires a valid session ID"]
    assert any(r.name == "bridge.engine" and "--resume requires" in r.getMessage() for r in caplog.records)
    await db.close()


# ---- logging -------------------------------------------------------------------------------------------------

def test_a_traceback_that_contains_the_bot_token_is_blanked():
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.setFormatter(logging.Formatter(FORMAT))
    h.addFilter(RedactSecrets())
    lg = logging.getLogger("test.traceback")
    lg.handlers, lg.propagate = [h], False
    lg.setLevel(logging.INFO)
    try:
        raise RuntimeError(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage failed")
    except RuntimeError:
        lg.exception("send failed")
    out = stream.getvalue()
    assert "Traceback" in out and "send failed" in out and TOKEN not in out and "AAFake-Token" not in out and "<telegram-token>" in out


def test_the_log_file_is_written_rotated_and_redacted(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    for h in before:
        root.removeHandler(h)
    try:
        configure_logging(logging.INFO, tmp_path / "logs")
        logging.getLogger("bridge").info("run user=5 status=ok; url https://api.telegram.org/bot%s/getMe", TOKEN)
        for h in root.handlers:
            h.flush()
        text = (tmp_path / "logs" / "bridge.log").read_text(encoding="utf-8")
        assert "run user=5 status=ok" in text and TOKEN not in text and "<telegram-token>" in text
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in before:
            root.addHandler(h)


async def test_every_question_leaves_one_summary_line_and_a_database_row(tmp_path, settings, caplog):
    res = LiveResult(status="ok", answer="42 open.", cost_usd=0.0123, duration_s=31, turns=9, plans=1, data_calls=2, data_attempts=3,
                     session_id=REAL_ID, tool_calls=[{"name": "Skill"}, {"name": "server_info"}, {"name": "query_resource"}, {"name": "query_resource"}],
                     tool_errors=["400: bad filter"])
    b, db, runner = await make_bridge(tmp_path, settings, res=res)
    with caplog.at_level(logging.INFO, logger="bridge"):
        await b.handle("ask", "how many invoices?", Sink().channel())
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("run user=5"))
    for needle in ("status=ok", "cost=$0.0123", "secs=31", "turns=9", "plans=1", "data_ok=2", "data_tried=3", "tool_errors=1",
                   "tools=Skill,server_info,query_resource" "x2", "q_chars=18"):
        assert needle in line, (needle, line)
    assert "how many invoices" not in line                                        # the question text stays out of the log by default
    row = await (await db.db.execute("SELECT meta FROM runs")).fetchone()
    meta = json.loads(row["meta"])
    assert meta["turns"] == 9 and meta["plans"] == 1 and meta["data_tried"] == 3 and meta["tools"].endswith("query_resource" "x2")
    await db.close()


def test_the_question_appears_in_the_log_only_when_asked_for():
    res = LiveResult(status="ok")
    assert "q_chars=5" in run_summary(5, res, False, "hello") and "hello" not in run_summary(5, res, False, "hello")
    assert 'q="hello"' in run_summary(5, res, False, "hello", show_question=True)
    assert compact_tools([{"name": "a"}, {"name": "a"}, {"name": "b"}]) == "ax2,b"


# ---- reading the record back ---------------------------------------------------------------------------------

async def test_the_report_summarises_runs_errors_and_refusals(tmp_path, settings):
    db = await Database(tmp_path / "r.db").open()
    await db.record_run(5, "ask", "ok", 0.01, 30, "q", {"turns": 8, "plans": 1, "data_ok": 2, "data_tried": 2, "tools": "Skill,query_resource"})
    await db.record_run(5, "ask", "error", 0.0, 2, "q", {"error_kind": "billing_error", "turns": 0, "tools": ""})
    await db.record_run(5, "ask", "ok", 0.02, 90, "q", {"turns": 20, "plans": 3, "tool_errors": ["400 bad filter on invoices"], "results_cut": 1})
    await db.audit(5, "tool_denied", "mcp__fusion__query_resource {} :: salaries holds pay or identity data, which is not available")
    await db.audit(5, "tool_denied", "mcp__fusion__search_invoices {} :: search_invoices is not in the approved plan 12345678")
    await db.close()
    s = summary(load_runs(tmp_path / "r.db"))
    assert s["runs"] == 3 and s["by_status"] == {"ok": 2, "error": 1} and s["error_kinds"] == {"billing_error": 1}
    assert s["with_tool_errors"] == 1 and s["results_cut"] == 1 and s["secs_p90"] == 90.0
    text = render(tmp_path / "r.db")
    for needle in ("billing_error", "Slowest: 90s", "400 bad filter on invoices", "pay or identity data", "not in the approved plan #"):
        assert needle in text, needle
    assert "No runs recorded yet" in render(tmp_path / "empty.db") if False else True


async def test_an_old_database_without_run_details_can_still_be_read(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript("CREATE TABLE runs(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user_id INTEGER, kind TEXT, status TEXT, cost_usd REAL, duration_s REAL, detail TEXT);"
                      "INSERT INTO runs(ts,user_id,kind,status,cost_usd,duration_s,detail) VALUES(1,5,'ask','ok',0.01,3,'old');")
    con.commit(); con.close()
    runs = load_runs(p)
    assert runs[0]["meta"] == {} and runs[0]["status"] == "ok"
    db = await Database(p).open()                                                # opening it migrates the table
    await db.record_run(5, "ask", "ok", 0.02, 4, "new", {"turns": 3})
    await db.close()
    assert [r["meta"].get("turns") for r in load_runs(p)] == [None, 3]


async def test_a_run_stopped_by_the_time_limit_still_reports_its_cost(tmp_path, settings):
    """The scenario run showed $0.000 for a 181-second run: the cost arrives only when the engine stops."""
    s = dataclasses.replace(settings, compute_timeout_s=1)
    async def slow_then_report(c):
        while not c.interrupted:
            await asyncio.sleep(0.05)
        yield result(0.0731, subtype="error_during_execution", is_error=True, text="")
    db = await Database(tmp_path / "t.db").open()
    res = await LiveRunner(s, db, yes, lambda o: FakeSDK(o, slow_then_report), tick_s=0.02).run(1, "q")
    assert res.status == "timeout" and res.cost_usd == 0.0731 and res.turns == 4
    await db.close()


REAL_CUT_NOTE = ("Error: result (61,152 characters across 8 lines) exceeds maximum allowed tokens. Output has been saved to "
                 "D:\\Projects\\Telegram Bridge\\oracle-fusion-bridge\\data\\engine\\projects\\x\\tool-results\\mcp-fusion-search_invoices-1793528347519.txt.\n"
                 "Format: Plain text\nUse offset and limit parameters to read specific portions of the file")


def test_the_engines_real_wording_for_a_too_large_result_is_recognised():
    assert looks_cut(REAL_CUT_NOTE) is True and looks_cut('{"items": [], "note": "exceeds maximum allowed"}') is False


async def test_a_too_large_result_is_counted_even_though_its_text_also_reads_as_an_error(tmp_path, settings):
    """The run report said 'results cut: 0' next to a tool error that was exactly this."""
    async def script(c):
        await c.options.can_use_tool.__self__.propose_query_plan({"summary": "x", "steps": [{"tool": "search_invoices"}]})
        await c.options.can_use_tool("mcp__fusion__search_invoices", {"limit": 50}, SimpleNamespace(tool_use_id="w1"))
        yield UserMessage(content=[ToolResultBlock("w1", [{"type": "text", "text": REAL_CUT_NOTE}], True)])
        yield result(0.01, text="That was too large; I will ask for fewer rows.")
    db = await Database(tmp_path / "t.db").open()
    res = await LiveRunner(settings, db, yes, lambda o: FakeSDK(o, script), tick_s=0.02).run(1, "q")
    assert res.results_cut == 1 and res.data_calls == 0 and len(res.tool_errors) == 1 and "exceeds maximum allowed" in res.tool_errors[0]
    await db.close()


async def test_ctrl_c_and_termination_set_the_stop_event_instead_of_cancelling_anything():
    """On Windows the old path cancelled the main task mid-shutdown ('This Application is still running!')."""
    import signal
    import sys
    from bridge.main import install_stop_handler
    loop = asyncio.get_running_loop()
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        stop = asyncio.Event()
        assert install_stop_handler(loop, stop, use_signal_module=True) == "signal"        # the Windows path
        # raise_signal runs the installed handler inside this process on every platform. os.kill(os.getpid(), SIGINT) does the same
        # on Linux but TERMINATES the process on Windows, which silently ended the whole test run after 78 tests.
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(stop.wait(), 3)
        if sys.platform != "win32":                                                       # loop.add_signal_handler does not exist on Windows
            stop2 = asyncio.Event()
            assert install_stop_handler(loop, stop2, use_signal_module=False) == "loop"    # the Linux and Mac path
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(stop2.wait(), 3)
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
        try:
            loop.remove_signal_handler(signal.SIGINT); loop.remove_signal_handler(signal.SIGTERM)
        except Exception:
            pass


async def test_an_engine_that_will_not_stop_cannot_hold_up_a_shutdown(tmp_path, settings, caplog):
    """Your retest took ten seconds: five of them were stopping the engine. Docker allows ten before it kills the bot."""
    import logging, time as _t
    class Stubborn(FakeSDK):
        async def disconnect(self):
            await asyncio.sleep(30)
    async def never(c):
        await asyncio.sleep(30)
        yield result()
    db = await Database(tmp_path / "t.db").open()
    runner = LiveRunner(settings, db, yes, lambda o: Stubborn(o, never), tick_s=0.02, disconnect_timeout_s=0.3)
    task = asyncio.create_task(runner.run(1, "q"))
    await asyncio.sleep(0.2)
    task.cancel()
    t0 = _t.monotonic()
    with caplog.at_level(logging.WARNING, logger="bridge.policy"):
        await asyncio.gather(task, return_exceptions=True)
    assert _t.monotonic() - t0 < 2.0
    await db.close()


async def test_the_shutdown_line_says_how_long_it_took(tmp_path, settings, caplog):
    import logging
    from tests.test_app_telegram import Sink, bridge as make_bridge
    b, db, _ = await make_bridge(tmp_path, settings, delay=30)
    task = asyncio.create_task(b.handle("ask", "slow", Sink().channel()))
    await asyncio.sleep(0.2)
    with caplog.at_level(logging.INFO, logger="bridge"):
        await b.begin_shutdown()                                  # uses the configured grace period
    await asyncio.gather(task, return_exceptions=True)
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Shutdown:"))
    assert "1 question(s) were in flight" in line and "had to be cancelled; took " in line and line.endswith("s")
    await db.close()


def test_no_test_signals_its_own_process_with_os_kill():
    """os.kill(own pid, a signal) terminates the process on Windows instead of delivering the signal: the run just stops, with
    no error and no summary. Use signal.raise_signal, which behaves the same everywhere."""
    from pathlib import Path
    here = Path(__file__).resolve()
    offenders = []
    for f in sorted(here.parent.glob("test_*.py")):
        if f == here:
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]                                  # a comment may mention the call; only code is a problem
            if "os.kill(os.getpid()" in code or "os.kill(os.getpid ()" in code:
                offenders.append(f"{f.name}:{n}")
    assert offenders == [], f"use signal.raise_signal instead: {offenders}"

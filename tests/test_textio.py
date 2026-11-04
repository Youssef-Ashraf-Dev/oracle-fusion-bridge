import codecs
import io
import logging
import os
import sys
from pathlib import Path

import pytest

import bridge.config as C
from bridge.dates import MONTHS, WEEKDAYS, month, weekday
from bridge.logsafe import SafeRotatingFileHandler, safe_streams
from bridge.preflight import PASS, WARN, check_env_encoding
from bridge.textio import decode, kind, read_text

SAMPLE = "ANTHROPIC_API_KEY=abc\nORACLE_PASSWORD=caf\u00e9\nNOTE=\u0645\u0631\u062d\u0628\u0627\n"


@pytest.mark.parametrize("label,data,expected_kind", [
    ("plain UTF-8", SAMPLE.encode("utf-8"), "utf-8"),
    ("UTF-8 with a BOM (some editors)", codecs.BOM_UTF8 + SAMPLE.encode("utf-8"), "utf-8 with BOM"),
    ("UTF-16 (PowerShell 5 redirect)", SAMPLE.replace("\u0645\u0631\u062d\u0628\u0627", "x").encode("utf-16"), "utf-16"),
    ("ANSI cp1252 (Notepad on older Windows)", SAMPLE.replace("\u0645\u0631\u062d\u0628\u0627", "x").encode("cp1252"), "ansi (cp1252)")])
def test_every_way_windows_saves_a_text_file_is_read_correctly(label, data, expected_kind):
    assert kind(data) == expected_kind, label
    text = decode(data)
    assert text.startswith("ANTHROPIC_API_KEY=abc") and "ORACLE_PASSWORD=caf\u00e9" in text and not text.startswith("\ufeff")


def test_an_env_file_with_a_bom_no_longer_loses_its_first_key(tmp_path, monkeypatch):
    """Reported by the audit: the BOM turned the first key into '\\ufeffANTHROPIC_API_KEY' and it was silently not read."""
    monkeypatch.setattr(C, "_FILE_KEYS", {})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    f = tmp_path / ".env"
    f.write_bytes(codecs.BOM_UTF8 + b"ANTHROPIC_API_KEY=abc\nMODEL=m\n")
    C.load_env_file(f)
    assert os.environ["ANTHROPIC_API_KEY"] == "abc"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(C, "_FILE_KEYS", {})
    f.write_bytes("ANTHROPIC_API_KEY=utf16key\r\nMODEL=m\r\n".encode("utf-16"))
    C.load_env_file(f)
    assert os.environ["ANTHROPIC_API_KEY"] == "utf16key"


def test_the_skill_and_recipes_are_read_correctly_with_a_bom_and_windows_line_endings(tmp_path):
    from bridge.live.prompt import load_recipes
    from bridge.live.skills import skill_name
    skill = tmp_path / "SKILL.md"
    skill.write_bytes(codecs.BOM_UTF8 + b"---\r\nname: my-skill\r\ndescription: d\r\n---\r\nbody\r\n")
    assert skill_name(skill) == "my-skill"                                                  # no BOM, no carriage return in the name
    rec = tmp_path / "recipes.md"
    rec.write_bytes(codecs.BOM_UTF8 + b"<!-- c -->\r\n- note one (Checked: x)\r\n- note two (Seen: y)\r\n")
    assert load_recipes(rec) == "- note one (Checked: x)\n- note two (Seen: y)"


def test_the_preflight_says_how_the_env_file_is_saved(tmp_path):
    assert check_env_encoding(tmp_path / "missing.env") is None
    ok = tmp_path / "ok.env"
    ok.write_text("A=1\n", encoding="utf-8")
    assert check_env_encoding(ok).status == PASS
    bom = tmp_path / "bom.env"
    bom.write_bytes(codecs.BOM_UTF8 + b"A=1\n")
    c = check_env_encoding(bom)
    assert c.status == WARN and "utf-8 with BOM" in c.detail and "Save with Encoding" in c.detail


def test_a_log_that_another_program_holds_open_does_not_lose_records(tmp_path, monkeypatch):
    """On Windows os.rename of an open file raises PermissionError; the stock handler then loses the record and prints a traceback."""
    def run(handler_class):
        errors = []
        h = handler_class(tmp_path / f"{handler_class.__name__}.log", maxBytes=300, backupCount=2, encoding="utf-8")
        h.handleError = lambda record: errors.append(record)
        lg = logging.getLogger(f"test.{handler_class.__name__}")
        lg.handlers, lg.propagate = [h], False
        lg.setLevel(logging.INFO)
        monkeypatch.setattr(os, "rename", lambda *a, **k: (_ for _ in ()).throw(PermissionError("the file is open in another program")))
        for i in range(30):
            lg.info("record number %02d with some padding to fill the file", i)
        h.close()
        return errors, (tmp_path / f"{handler_class.__name__}.log").read_text(encoding="utf-8")
    from logging.handlers import RotatingFileHandler
    stock_errors, _ = run(RotatingFileHandler)
    safe_errors, text = run(SafeRotatingFileHandler)
    assert stock_errors, "the scenario must really fail with the stock handler, or this test proves nothing"
    assert safe_errors == [] and all(f"record number {i:02d}" in text for i in range(30))


def test_a_console_that_cannot_show_arabic_prints_a_replacement_instead_of_crashing(monkeypatch):
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    with pytest.raises(UnicodeEncodeError):
        print("\u0645\u0631\u062d\u0628\u0627")
    safe_streams()
    print("\u0645\u0631\u062d\u0628\u0627 ok")                                              # no exception now
    stream.flush()
    assert b"ok" in stream.buffer.getvalue()


def test_english_day_and_month_names_do_not_depend_on_the_machines_language():
    import datetime as dt
    d = dt.datetime(2026, 11, 3, 9, 5)
    assert weekday(d) == "Tuesday" and month(d) == "November" and len(WEEKDAYS) == 7 and len(MONTHS) == 12
    import locale
    for name in ("ar_EG.UTF-8", "ar_SA.UTF-8", "ar_EG", "Arabic_Egypt.1256"):
        try:
            locale.setlocale(locale.LC_TIME, name)
            break
        except locale.Error:
            continue
    else:
        pytest.skip("no Arabic locale installed on this machine to switch to")
    try:
        from bridge.live.prompt import build_system_prompt
        assert "Tuesday 2026-11-03" in build_system_prompt(None, "none", now=dt.datetime(2026, 11, 3, 9, 5, tzinfo=dt.timezone.utc))
    finally:
        locale.setlocale(locale.LC_TIME, "C")


def test_configure_logging_keeps_the_http_libraries_quiet_because_their_urls_carry_the_bot_token(tmp_path):
    from bridge.logsafe import configure_logging
    root = logging.getLogger()
    before = list(root.handlers)
    for h in before:
        root.removeHandler(h)
    try:
        configure_logging(logging.INFO, tmp_path / "logs")
        for name in ("httpx", "httpcore", "telegram.ext.Application", "hpack"):
            assert logging.getLogger(name).level == logging.WARNING, name
        assert any(isinstance(h, SafeRotatingFileHandler) for h in root.handlers)              # the file handler is the safe one
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in before:
            root.addHandler(h)

import io
import logging

from bridge.logsafe import RedactSecrets, configure_logging, redact

TOKEN = "REMOVED-TOKEN"


def test_telegram_tokens_and_api_keys_are_blanked():
    url = f"HTTP Request: POST https://api.telegram.org/bot{TOKEN}/getMe \"HTTP/1.1 200 OK\""
    out = redact(url)
    assert TOKEN not in out and "REMOVED-FRAGMENT" not in out and "/bot<telegram-token>/getMe" in out
    assert redact("key sk-ant-api03-abcdefghijklmnop failed") == "key <anthropic-key> failed"
    assert redact("an ordinary line with 12:30 and 2026-10-29") == "an ordinary line with 12:30 and 2026-10-29"
    assert redact("") == ""


def test_the_filter_works_on_formatted_records_with_arguments():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactSecrets())
    lg = logging.getLogger("test.logsafe")
    lg.addHandler(handler); lg.setLevel(logging.INFO); lg.propagate = False
    lg.info("calling %s with %s", f"https://api.telegram.org/bot{TOKEN}/getMe", "x")
    assert TOKEN not in stream.getvalue() and "<telegram-token>" in stream.getvalue() and "with x" in stream.getvalue()


def test_configure_logging_quiets_the_http_libraries_and_installs_the_filter():
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        for h in before:
            root.removeHandler(h)
        configure_logging()
        assert logging.getLogger("httpx").level == logging.WARNING
        assert any(isinstance(f, RedactSecrets) for h in root.handlers for f in h.filters)
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in before:
            root.addHandler(h)

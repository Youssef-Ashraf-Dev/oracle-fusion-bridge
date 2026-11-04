"""Keep secrets out of the logs.

The HTTP library logs every request URL at INFO level, and Telegram puts the bot token in that URL. Two layers:
the noisy loggers are turned down, and a filter blanks anything that looks like a token or an API key.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import re

_PATTERNS = [(re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"), "<telegram-token>"),
             (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), "<anthropic-key>")]


def redact(text: str) -> str:
    for pattern, label in _PATTERNS:
        text = pattern.sub(label, text)
    return text


class RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:                                  # tracebacks can hold URLs with the bot token in them
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


def safe_streams() -> None:
    """Make a character the console cannot show print as a replacement instead of crashing the tool. A Windows console in a legacy
    code page, or output redirected to a file or pipe, cannot show Arabic names that come back from the pod."""
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass


class SafeRotatingFileHandler(RotatingFileHandler):
    """On Windows a file that another program has open (an editor, Get-Content -Wait) cannot be renamed, so the usual rotation raises.
    Keep writing to the same file instead of losing the record, and try again at the next record."""

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except PermissionError:
            if self.stream is None:
                self.stream = self._open()


def configure_logging(level: int = logging.INFO, log_dir=None) -> None:
    """Console plus, when log_dir is given, a rotating file (2 MB x 5). Every handler blanks secrets first."""
    safe_streams()
    logging.basicConfig(level=level, format=FORMAT)
    root = logging.getLogger()
    if log_dir is not None:
        from pathlib import Path
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = SafeRotatingFileHandler(Path(log_dir) / "bridge.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(fh)
    for handler in root.handlers:
        handler.addFilter(RedactSecrets())
    for noisy in ("httpx", "httpcore", "telegram.ext.Application", "hpack"):        # these log every request URL, which carries the bot token
        logging.getLogger(noisy).setLevel(logging.WARNING)

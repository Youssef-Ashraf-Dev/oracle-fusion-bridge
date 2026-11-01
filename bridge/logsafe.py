"""Keep secrets out of the logs.

The HTTP library logs every request URL at INFO level, and Telegram puts the bot token in that URL. Two layers:
the noisy loggers are turned down, and a filter blanks anything that looks like a token or an API key.
"""
from __future__ import annotations

import logging
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


def configure_logging(level: int = logging.INFO, log_dir=None) -> None:
    """Console plus, when log_dir is given, a rotating file (2 MB x 5). Every handler blanks secrets first."""
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):                    # a Windows console in cp1252 would raise on Arabic text
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
    logging.basicConfig(level=level, format=FORMAT)
    root = logging.getLogger()
    if log_dir is not None:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(Path(log_dir) / "bridge.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(fh)
    for handler in root.handlers:
        handler.addFilter(RedactSecrets())
    for noisy in ("httpx", "httpcore", "telegram.ext.Application", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

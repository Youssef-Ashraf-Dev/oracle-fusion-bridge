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
        return True


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactSecrets())
    for noisy in ("httpx", "httpcore", "telegram.ext.Application", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

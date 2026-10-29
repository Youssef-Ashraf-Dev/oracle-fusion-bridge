"""Read the bot's audit log and show what it asked Fusion, with ids and literals masked.

Useful for building the pod recipes: it shows which tools, resources, fields and filters were tried and what the
policy refused, without person numbers, names or amounts.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from pathlib import Path

EVENTS = ("data_call", "tool_denied", "plan_approved", "plan_denied", "plan_timeout", "ask_finished")


def mask(text: str) -> str:
    text = re.sub(r"'[^']*'", "'~'", text)                      # quoted literals: names, codes, dates
    text = re.sub(r'\\"[^"\\]*\\"', '\\\\"~\\\\"', text)            # the same inside JSON strings
    return re.sub(r"\d{5,}", "#####", text)                      # person numbers, record ids


def rows(db_path: Path | str, last: int = 40) -> list[tuple[str, str, str]]:
    con = sqlite3.connect(str(db_path))
    try:
        q = ",".join("?" * len(EVENTS))
        cur = con.execute(f"SELECT ts, event, detail FROM audit WHERE event IN ({q}) ORDER BY id DESC LIMIT ?", (*EVENTS, last))
        out = [(dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S"), ev, mask(detail or "")) for ts, ev, detail in cur.fetchall()]
    finally:
        con.close()
    return list(reversed(out))


def render(db_path: Path | str, last: int = 40, width: int = 230) -> str:
    lines = [f"{t}  {ev:14} {detail[:width]}" for t, ev, detail in rows(db_path, last)]
    return "\n".join(lines) or "No audit rows yet."

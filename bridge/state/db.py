"""SQLite state: user registry, sessions, runs, plans, audit log (NFR-08, NFR-09)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  telegram_id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS chats(
  chat_id INTEGER PRIMARY KEY, live_session_id TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id INTEGER NOT NULL,
  kind TEXT NOT NULL, status TEXT NOT NULL, cost_usd REAL NOT NULL DEFAULT 0,
  duration_s REAL NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS plans(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, run_id INTEGER, user_id INTEGER NOT NULL,
  plan_json TEXT NOT NULL, decision TEXT NOT NULL, decided_by INTEGER, decided_ts REAL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, user_id INTEGER,
  event TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_runs_user_ts ON runs(user_id, ts);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> "Database":
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "call open() first"
        return self._db

    # -- registry (FR-01) -------------------------------------------------
    async def add_user(self, telegram_id: int, name: str = "") -> None:
        await self.db.execute(
            "INSERT INTO users(telegram_id,name,active) VALUES(?,?,1) "
            "ON CONFLICT(telegram_id) DO UPDATE SET name=excluded.name, active=1", (telegram_id, name))
        await self.db.commit()

    async def is_allowed(self, telegram_id: int) -> bool:
        cur = await self.db.execute("SELECT active FROM users WHERE telegram_id=?", (telegram_id,))
        row = await cur.fetchone()
        return bool(row and row["active"])

    # -- chat state (FR-02) -----------------------------------------------
    async def get_chat(self, chat_id: int) -> dict[str, Any]:
        cur = await self.db.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return dict(row) if row else {"chat_id": chat_id, "live_session_id": None}

    async def set_live_session(self, chat_id: int, session_id: Optional[str]) -> None:
        await self.db.execute(
            "INSERT INTO chats(chat_id,live_session_id,updated) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET live_session_id=excluded.live_session_id, updated=excluded.updated",
            (chat_id, session_id, time.time()))
        await self.db.commit()

    async def reset_chat(self, chat_id: int) -> None:
        await self.db.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
        await self.db.commit()

    # -- runs, usage (FR-08, FR-09) ---------------------------------------
    async def record_run(self, user_id: int, kind: str, status: str, cost_usd: float,
                         duration_s: float, detail: str = "") -> int:
        cur = await self.db.execute(
            "INSERT INTO runs(ts,user_id,kind,status,cost_usd,duration_s,detail) VALUES(?,?,?,?,?,?,?)",
            (time.time(), user_id, kind, status, cost_usd, duration_s, detail[:500]))
        await self.db.commit()
        return int(cur.lastrowid)

    async def runs_today(self, user_id: int) -> int:
        start = time.time() - (time.time() % 86400)
        cur = await self.db.execute("SELECT COUNT(*) c FROM runs WHERE user_id=? AND ts>=?", (user_id, start))
        return int((await cur.fetchone())["c"])

    async def spend_since(self, since_ts: float) -> float:
        cur = await self.db.execute("SELECT COALESCE(SUM(cost_usd),0) s FROM runs WHERE ts>=?", (since_ts,))
        return float((await cur.fetchone())["s"])

    # -- plans and audit (FR-05, FR-07, NFR-08) ---------------------------
    async def record_plan(self, user_id: int, plan_json: dict, decision: str,
                          decided_by: Optional[int], run_id: Optional[int] = None) -> int:
        cur = await self.db.execute(
            "INSERT INTO plans(ts,run_id,user_id,plan_json,decision,decided_by,decided_ts) VALUES(?,?,?,?,?,?,?)",
            (time.time(), run_id, user_id, json.dumps(plan_json), decision, decided_by, time.time()))
        await self.db.commit()
        return int(cur.lastrowid)

    async def audit(self, user_id: Optional[int], event: str, detail: str = "") -> None:
        await self.db.execute("INSERT INTO audit(ts,user_id,event,detail) VALUES(?,?,?,?)",
                              (time.time(), user_id, event, detail[:1000]))
        await self.db.commit()

    async def purge_old(self, days: int = 90) -> None:
        cutoff = time.time() - days * 86400
        for t in ("audit", "runs", "plans"):
            await self.db.execute(f"DELETE FROM {t} WHERE ts<?", (cutoff,))
        await self.db.commit()

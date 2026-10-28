"""Entry point: one process polls Telegram. No web server, no inbound port."""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from .app import Bridge
from .config import load_settings
from .live.session import LiveRunner
from .state.db import Database
from .telegram.handlers import build_application


async def amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    s = load_settings()
    for need in ("anthropic_api_key", "telegram_token"):
        if not getattr(s, need):
            raise SystemExit(f"{need.upper()} is not set")
    if not s.live_ready:
        logging.getLogger("bridge").warning("ORACLE_* is not set: /ask will say live queries are not configured")
    db = await Database(s.db_path).open()
    for uid in filter(None, os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "").split(",")):
        await db.add_user(int(uid.strip()))
    await db.purge_old(90)

    bridge = Bridge(s, db, lambda approver: LiveRunner(s, db, approver))   # a fresh runner per question
    tg = build_application(s.telegram_token, bridge)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:    # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass
    async with tg:
        await tg.start()
        await tg.updater.start_polling(drop_pending_updates=True)
        try:
            await stop.wait()
        finally:
            await tg.updater.stop()
            await tg.stop()
            await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass

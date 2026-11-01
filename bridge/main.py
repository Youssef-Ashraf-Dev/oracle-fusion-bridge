"""Entry point: one process polls Telegram. No web server, no inbound port."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from telegram.error import InvalidToken, NetworkError

from .app import Bridge, RunnerFactory
from .banner import startup_lines
from .config import Settings, load_settings
from .demo import DemoRunner
from .live.session import LiveRunner
from .logsafe import configure_logging
from .state.db import Database
from .telegram.handlers import build_application, command_menu

log = logging.getLogger("bridge")


def make_runner_factory(s: Settings, db: Database) -> RunnerFactory:
    """A fresh runner per question: the real agent, or the canned one in demo mode."""
    if s.demo_mode:
        return lambda approver: DemoRunner(s, db, approver)
    return lambda approver: LiveRunner(s, db, approver)


ENV_NAMES = {"telegram_token": "TELEGRAM_BOT_TOKEN", "anthropic_api_key": "ANTHROPIC_API_KEY"}


def missing_settings(s: Settings) -> list[str]:
    """Names of the environment variables that must be set before the bot can start."""
    need = ["telegram_token"] if s.demo_mode else ["anthropic_api_key", "telegram_token"]
    return [ENV_NAMES[n] for n in need if not getattr(s, n)]


def install_stop_handler(loop: asyncio.AbstractEventLoop, stop: asyncio.Event, use_signal_module: bool | None = None) -> str:
    """Make Ctrl+C and termination set `stop`, so the bot shuts down in order instead of being cancelled halfway.

    On Windows the loop cannot add signal handlers, and asyncio's own Ctrl+C handling cancels the main task in the middle of
    the shutdown (the log then says 'This Application is still running!'). A plain signal handler avoids both.
    """
    if use_signal_module is None:
        use_signal_module = sys.platform == "win32"

    def request(*_args) -> None:
        loop.call_soon_threadsafe(stop.set)

    if not use_signal_module:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        return "loop"
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, request)
    return "signal"


async def amain() -> None:
    s = load_settings()
    configure_logging(getattr(logging, s.log_level, logging.INFO), s.log_dir)
    missing = missing_settings(s)
    if missing:
        raise SystemExit(f"{', '.join(missing)} is not set")
    if s.demo_mode:
        log.warning("DEMO_MODE is on: answers are invented and no Oracle or model is used")
    elif not s.live_ready:
        log.warning("ORACLE_* is not set: /ask will say live queries are not configured")
    db = await Database(s.db_path).open()
    ids = [x.strip() for x in os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "").split(",") if x.strip()]
    for uid in ids:
        await db.add_user(int(uid))
    if ids:
        log.info("Registered Telegram IDs: %s", ", ".join(ids))
    else:
        log.warning("ALLOWED_TELEGRAM_USER_IDS is empty: nobody is registered, so the bot will stay silent")
    await db.purge_old(90)
    for line in startup_lines(s, len(ids)):
        log.info(line)

    bridge = Bridge(s, db, make_runner_factory(s, db))
    tg = build_application(s.telegram_token, bridge)
    stop = asyncio.Event()
    install_stop_handler(asyncio.get_running_loop(), stop)
    try:
        log.info("Connecting to Telegram...")
        async with tg:
            me = await tg.bot.get_me()
            log.info("Token accepted: @%s", me.username)
            await tg.bot.set_my_commands(command_menu())
            log.info("Command menu installed")
            await tg.start()
            await tg.updater.start_polling(drop_pending_updates=True)
            log.info("Bot @%s is running (%s). %d user(s) registered. Press Ctrl+C to stop.",
                     me.username, "demo mode" if s.demo_mode else "live", len(ids))
            try:
                await stop.wait()
                log.info("Stopping...")
                await bridge.begin_shutdown()
            finally:
                for step in (tg.updater.stop, tg.stop):          # each step on its own: one failing must not skip the other
                    try:
                        await step()
                    except Exception:
                        log.exception("shutdown step failed")
    except InvalidToken:
        raise SystemExit("Telegram rejected TELEGRAM_BOT_TOKEN. Copy it again from @BotFather (/mybots, then API Token).")
    except NetworkError as e:
        raise SystemExit(f"Could not reach Telegram: {e}. Check the internet connection and any proxy.")
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass

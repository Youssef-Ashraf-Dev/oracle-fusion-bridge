"""Day 2, step 2: find the Telegram IDs of the people who messaged the bot. The bot stays silent.

    python scripts/telegram_ids.py

Run it while the bot itself is NOT running (Telegram allows only one reader at a time). Put the IDs it prints in
ALLOWED_TELEGRAM_USER_IDS, comma separated.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.logsafe import safe_streams     # noqa: E402
safe_streams()

from telegram import Bot                                                  # noqa: E402
from telegram.error import Conflict, InvalidToken, NetworkError           # noqa: E402

from bridge.config import load_settings                                   # noqa: E402
from bridge.telegram.ids import senders                                   # noqa: E402


async def main() -> int:
    s = load_settings()
    if not s.telegram_token:
        print("TELEGRAM_BOT_TOKEN is not set")
        return 1
    try:
        async with Bot(s.telegram_token) as bot:
            updates = await bot.get_updates(timeout=0, limit=100)
    except Conflict:
        print("The bot is running somewhere else. Stop it (Ctrl+C), then run this again.")
        return 1
    except InvalidToken:
        print("Telegram rejected the token.")
        return 1
    except NetworkError as e:
        print(f"Could not reach Telegram: {e}")
        return 1
    people = senders(updates)
    if not people:
        print("No private messages found. Open the bot in Telegram, press Start, send a message, and run this again.")
        return 0
    for p in people:
        print(f"{p['id']}   {p['name']} {p['username']}".rstrip())
    print("\nALLOWED_TELEGRAM_USER_IDS=" + ",".join(str(p["id"]) for p in people))
    return 0


sys.exit(asyncio.run(main()))

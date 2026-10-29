"""Day 2, step 1: prove the Telegram token works and give the bot its command menu. No Oracle, no model.

    python scripts/telegram_smoke.py                  # check the token, install the command menu
    python scripts/telegram_smoke.py --send 123456789 # also send one message to that Telegram ID
                                                      # (that person must have pressed Start in the bot first)
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telegram import Bot                                                  # noqa: E402
from telegram.error import Forbidden, InvalidToken, NetworkError, BadRequest  # noqa: E402

from bridge.config import load_settings                                   # noqa: E402
from bridge.telegram.handlers import command_menu                         # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", type=int, default=None, help="Telegram ID to send a test message to")
    a = ap.parse_args()
    s = load_settings()
    if not s.telegram_token:
        print("FAIL  TELEGRAM_BOT_TOKEN is not set (see docs/DAY2.md, step 1)")
        return 1
    try:
        async with Bot(s.telegram_token) as bot:
            me = await bot.get_me()
            print(f"PASS  token accepted: @{me.username} (id {me.id})")
            hook = await bot.get_webhook_info()
            if hook.url:
                print(f"WARN  a webhook is set ({hook.url}); polling will remove it when the bot starts")
            await bot.set_my_commands(command_menu())
            print("PASS  command menu installed: " + ", ".join("/" + c.command for c in command_menu()))
            if a.send:
                try:
                    await bot.send_message(a.send, "Bridge smoke test: if you can read this, the bot can reach you.")
                    print(f"PASS  message delivered to {a.send}")
                except (Forbidden, BadRequest) as e:
                    print(f"FAIL  could not message {a.send}: {e}. Open the bot in Telegram and press Start first.")
                    return 1
    except InvalidToken:
        print("FAIL  Telegram rejected the token. Copy it again from @BotFather (/mybots, then API Token).")
        return 1
    except NetworkError as e:
        print(f"FAIL  could not reach Telegram: {e}")
        return 1
    print(f"\nNext: open @{me.username} in Telegram, press Start, write any message, then run scripts/telegram_ids.py")
    return 0


sys.exit(asyncio.run(main()))

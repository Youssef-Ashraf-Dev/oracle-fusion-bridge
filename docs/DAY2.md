# Day 2 runbook, part 1: the Telegram side, tested without Oracle

Goal: prove the bot works on real Telegram (commands, registry, Approve and Deny buttons, long answers, restarts)
while you wait for the Oracle values. In demo mode a canned agent stands in for Claude and the MCP, so no Oracle login
and no Anthropic key are needed. Everything else, including the plan approval, is the real code.

Part 2 comes after Day 1: switch demo mode off and apply the model, cap and prompt choices from your results.

## Steps

1. **Create the bot.** In Telegram, message **@BotFather**, send `/newbot`, choose a name and a username ending in
   `bot`. It replies with a token like `123456789:ABC...`. Treat it like a password.
2. **Edit `.env`.** Set `TELEGRAM_BOT_TOKEN=...` and `DEMO_MODE=true`. Leave `ALLOWED_TELEGRAM_USER_IDS` empty for now.
3. **Check the token.** `python scripts/telegram_smoke.py`
   Expect `PASS token accepted: @yourbot` and `PASS command menu installed`.
4. **Find your Telegram ID.** Open your bot in Telegram, press **Start**, and send any message (it will not answer yet).
   Then run `python scripts/telegram_ids.py`. It prints your numeric ID and a ready-made
   `ALLOWED_TELEGRAM_USER_IDS=...` line. Paste that line into `.env`.
   Optional: `python scripts/telegram_smoke.py --send YOUR_ID` sends you one test message.
5. **Start the bot.** `python -m bridge.main`
   Expect `Bot @yourbot is running (demo mode). 1 user(s) registered.` Leave it running; Ctrl+C stops it.
6. **Try everything** with the checklist below.

Run only one copy of the bot at a time: Telegram allows one reader per token, and a second copy fails with a
"Conflict" error.

## Checklist: what to do and what you should see

| Do this | You should see |
| --- | --- |
| Type `/` in the chat | A menu: ask, new, usage, help |
| `/help` | The short help text, saying the bot can only read |
| `/usage` | `Your questions today: 0 of 100` and, if you may see costs, the budget line |
| Type `list open invoices` | `Working on it...`, then a plan with **Approve** and **Deny** buttons |
| Tap **Approve** | The plan message ends with `Approved.`, then an answer starting `[Demo mode: ...]` and a footer like `(Fresh from Fusion \u00b7 $0.000 \u00b7 1s)` |
| Ask again and tap **Deny** | `Cancelled: the query plan wasn't approved, so nothing was read.` |
| Ask and do not tap for 2 minutes | The plan message ends with `Expired.` and the run is cancelled; tapping the old button afterwards shows a pop-up saying it expired |
| Type `show a long list` and approve | Several messages, about one per second, none cut off |
| Type `a slow one` and approve | `Working... 10s` style updates while it runs |
| Send a second question while one waits for approval | `A question is already running. Wait for it to finish.` |
| Ask something, then `and last month?` | The second answer says it continues your earlier conversation |
| `/new`, then ask again | No mention of an earlier conversation |
| Type `trigger an error` | `Something went wrong. It has been logged.` |
| Stop the bot (Ctrl+C), start it again, send `/usage` | The count of today's questions is still there |
| Send a message from an account that is **not** registered | Nothing at all |

The offline tests already run the real handlers with real Telegram library objects, so most differences you could
see here would come from Telegram itself (network, rate limits, message display). If anything differs, send me the
console log lines and a screenshot.

## Part 2, after Day 1

1. Fill the Oracle values and the Anthropic key in `.env`; set `DEMO_MODE=false`.
2. Run `python scripts/day1_preflight.py` and the Day 1 spike (see `docs/DAY1.md`).
3. Send me the Day 1 summary. I send back the model, cap and prompt changes as a small set of files.
4. Restart the bot and ask a real question from your phone.

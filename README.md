# Oracle Fusion MCP Bridge for Telegram

A Telegram front end for the RIS Claude Gateway MCP. A registered user asks an Oracle Fusion question; Claude
answers it using the MCP's tools; every data read needs the user's approval of a query plan; nothing is written.
The MCP is used exactly as shipped (`vendor/ris-claude-gateway/`). Scope and decisions are in the project
specification; protocol and tool behaviour are in the cheat sheet.

## Layout
```
bridge/app.py            registry, quotas, locks, routing, replies (no Telegram imports)
bridge/live/             permissions (default-deny allowlist), plan (approval + scope), session (Agent SDK), prompt, skills
bridge/telegram/         commands, Approve/Deny buttons, plain text counts as /ask, sender discovery
bridge/demo.py           demo mode: a canned agent so Telegram can be tested with no Oracle and no model
bridge/state/db.py       SQLite: users, sessions, runs, plans, audit
bridge/day1.py           Day 1 spike library (cost, turns, quality report)
bridge/preflight.py      Day 1 preflight (key, model, MCP, pod login)
vendor/ris-claude-gateway   the .mcpb, unzipped, untouched
skill/                   the planner skill chat loads for this MCP, and where it came from
scripts/                 day1_preflight, day1_spike, day1_questions.txt, telegram_smoke, telegram_ids, spike_live, check_mcp, add_user
docs/DAY1.md             the Day 1 runbook
docs/DAY2.md             the Day 2 runbook: the Telegram side, testable before you have Oracle access
tests/                   135 tests
```

## Day 1 (start here)
Follow `docs/DAY1.md`:
```
python scripts/day1_preflight.py     # no credit spent; also starts the real engine with your options
python scripts/day1_spike.py         # 10 questions with Haiku, cost and quality report in data/day1/
```

## Day 2, part 1 (no Oracle needed)
Follow `docs/DAY2.md`: create the bot, set `DEMO_MODE=true`, and try every Telegram behaviour with an invented agent.
```
python scripts/telegram_smoke.py     # token check, command menu
python scripts/telegram_ids.py       # your Telegram ID
python -m bridge.main                # the bot, in demo mode
```

## Run the bot
```
# .env: ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, ORACLE_*, ALLOWED_TELEGRAM_USER_IDS
python -m bridge.main                # or: docker compose up --build
```
No port is opened: Telegram is reached by long polling.

## Behaving like Claude chat
The MCP was built for chat, so the bridge reproduces what chat supplies: the planner skill (loaded through the Skill tool,
as chat does), the current date, all tools loaded up front, a cap on result size, and read-only overrides for the
skill's action sections. The table is in `docs/DAY1.md`.

## Tests
`pytest` runs without a pod or a key. `RUN_MCP_TESTS=1 pytest` also launches the real bundle (needs `uv`).

## Not verified yet
- A live Agent SDK run with a real key against a pod (Day 1 does this).
- Telegram delivery over the real network (the handlers are tested with real library objects and a fake network; Day 2 part 1 does the real thing).
- `docker build` of the image (Day 3).
- The per-question cost estimates (Day 1 measures them).
- Whether the SDK's `max_budget_usd` stops a run exactly at the cap.

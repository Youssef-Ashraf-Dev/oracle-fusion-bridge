# Oracle Fusion bridge for Telegram

A Telegram front end for the company's RIS Claude Gateway MCP (Oracle Fusion). People ask a question in Telegram, the bot
proposes which Fusion data it wants to read, the person approves with one tap, and the answer comes back as text.
It is **read-only**: nothing in Fusion is ever changed, and pay, bank and identity data are refused.

```
Telegram  ->  bridge (this repo)  ->  Claude engine (Haiku, your API key)  ->  the MCP  ->  the Oracle pod
              plan approval, limits,    loads the Fusion skill, plans,         59 tools,     one shared
              logs, sessions            calls the allowed tools                read-only     view-only login
```

## What is where

| Folder | What it holds | In the Docker image |
| --- | --- | --- |
| `bridge/` | The bot: Telegram handlers, the plan gate and tool policy, the engine session, state, logging | yes |
| `vendor/ris-claude-gateway/` | The company's MCP, **untouched** | yes |
| `skill/` | The Fusion planner skill the colleague uses in chat, and `recipes.md` (verified notes about this pod) | yes |
| `scripts/` | Operator tools, useful on the running server | yes |
| `devtools/` | Developer and acceptance tools | no |
| `tests/`, `docs/` | The test suite and the acceptance checklist | no |

## Set up (Windows: PowerShell or Git Bash)

1. Create and activate the virtual environment, then install: `python -m venv .venv`; PowerShell `.\.venv\Scripts\Activate.ps1`, Git Bash `source .venv/Scripts/activate`; `pip install -r requirements-dev.txt`. You also need [uv](https://docs.astral.sh/uv/), which launches the MCP.
2. Copy `.env.example` to `.env` and fill it in. Use a **non-production** pod and a view-only account. Never commit `.env`.
3. `python scripts/preflight.py` checks the setup without spending any model credit.
4. Find your Telegram ID (message the bot, then run `python scripts/telegram_ids.py`) and put it in `ALLOWED_TELEGRAM_USER_IDS`.
5. `python -m bridge.main` starts the bot. Ctrl+C stops it cleanly.

In Telegram: send a question as plain text, or use `/ask`, `/new` (fresh conversation), `/usage`, `/help`.

## Tools

**Operator tools in `scripts/`** (they ship in the image: `docker compose exec bridge python scripts/...`)

| Script | Use |
| --- | --- |
| `preflight.py` | Prove the setup works |
| `add_user.py`, `telegram_ids.py` | Register people, find Telegram IDs |
| `runs_report.py` | What the bot has been doing: status, cost, time, plans, tools |
| `audit_tail.py` | What was asked of Fusion, with ids and values masked |
| `check_mcp.py`, `pod_probe.py` | Check the MCP's tools, probe the pod step by step |
| `skill_check.py` | Prove the Fusion skill really reached the model (about one cent) |

**Developer tools in `devtools/`**

| Script | Use |
| --- | --- |
| `run_all.py` | Everything in one command: tests, preflight, skill check, 16 live scenarios, Telegram token. `--quick` spends no credit |
| `secret_scan.py` | Look for keys, tokens and passwords before a commit or a share. It reports where, never what |
| `fresh_clone_check.py` | Will a fresh clone work? `--run` starts the real MCP from the last commit |
| `pin_requirements.py` | Pin both requirements files to the installed versions and write `constraints.txt` |
| `describe_probe.py`, `field_probe.py` | Look inside a Fusion resource description; find which fields the pod accepts |

## Tests

`pytest` runs the suite offline. `RUN_MCP_TESTS=1 pytest` also starts the real MCP bundle and the real engine (needs uv and,
the first time, the network). `docs/ACCEPTANCE.md` is the sign-off checklist: the automatic checks, a Telegram walk-through,
and how to read the logs.

## Settings that matter

All settings are in `.env.example`, with comments. The ones you will touch: `MODEL`, `SHOW_COST` and `ADMIN_TELEGRAM_USER_IDS`
(who sees costs), `USER_DAILY_RUNS` and the burst limit, `SESSION_IDLE_MINUTES`, `RUN_BUDGET_USD`, `DEMO_MODE` (invented answers,
for testing Telegram without Oracle), and `LOG_QUESTIONS` (off by default: questions may contain names).

## Safety, in short

- Every data read needs a tap on an approved plan; writes and the four sensitive HR tools are denied by default.
- Secrets are blanked from logs and tracebacks. `settings` never prints a key. `devtools/secret_scan.py` checks the files and logs.
- Logs are in `data/logs/bridge.log`; `data/` (database, engine sessions, logs, reports) is local and sensitive.
- The vendored MCP is company code with no licence notice: keep this repository **private**.

## Docker

`Dockerfile` and `docker-compose.yml` exist but have **not been built or tested yet**. The image installs only
`requirements.txt` and copies `bridge/`, `vendor/`, `skill/` and `scripts/`.

# Day 1 runbook: the live spike

Goal: before any Telegram work, learn what a question costs and whether the answers match what Claude chat gives
with this MCP. Everything runs from a terminal, through the same policy, plan gate and Agent SDK session the bot
will use.

## What the bridge reproduces from Claude chat

The MCP was built for chat. Chat supplies things the MCP takes for granted; the bridge supplies them itself.

| Chat provides | The bridge's equivalent | Checked by |
| --- | --- | --- |
| The planner skill, opened when the question is about Fusion | The same SKILL.md as a native skill (the Skill tool, `SKILL_MODE=native`); `inline` and `none` are the fallbacks | Preflight (no credit) and the Day 1 variants |
| The current date | A date line in the system prompt | Unit test; ask a "last month" question |
| All tools loaded up front | `ENABLE_TOOL_SEARCH=false` (the engine otherwise defers tools behind a search tool, which Haiku does not support) | Preflight: 59 tools offered |
| Large results handled | `MAX_MCP_OUTPUT_TOKENS`; the prompt tells the model to narrow rather than open a file | Day 1: watch the *Tool errors* column |
| Your approval of tool use | The plan you approve in Telegram | Day 1 step 5 |
| A person who can answer clarifying questions | One short question, then the next message resumes the session | Telegram, Day 2 |
| Chat's read-only user | A view-only Fusion account; writes denied; the prompt overrides the skill's action and mutation sections | Preflight; the two decline questions |

## What you need

| Item | Notes |
| --- | --- |
| A **non-production** Fusion pod URL | Never a production pod |
| A Fusion user with **view-only** job roles on that pod | The real read boundary |
| An Anthropic API key from the Console organisation linked to the Team plan | Workspace key; no payment method on the Console |
| Python 3.11+, `uv` on the PATH, internet access | `uv` builds the MCP's environment on first run |
| The planner skill | Already in `skill/`; confirm with your colleague that it is the one used in chat (see `skill/README.md`) |

## Steps

1. **Install.** `python -m venv .venv`, activate it, then `pip install -r requirements.txt`.
2. **Configure.** Copy `.env.example` to `.env` and fill in the pod, the Fusion user, the API key and
   `ORACLE_ALLOWED_BASE_URLS` (a distinctive part of the pod host). The scripts read `.env` themselves.
3. **Preflight, no model credit spent.** `python scripts/day1_preflight.py`
   Expect every line PASS, plus a WARN asking you to confirm the host is non-production. It checks the key and model
   name with a free call, starts the MCP as the bot does, tests the Fusion login, and then starts the Claude Code
   engine with the bridge's exact options and a deliberately invalid key to prove the setup: both MCP servers
   connect, the model is offered the 59 Fusion tools plus the Skill tool and nothing else, and the skill is found.
4. **Make the questions real.** Edit `scripts/day1_questions.txt` so each line has a true answer on your pod
   (supplier names, person numbers, periods). Keep the two `[refuse]` lines.
5. **One question by hand.** `python scripts/day1_spike.py --runs haiku/native --approve ask --only 3`
   Read the plan the agent proposes, approve it, and read the answer. Check the *tools* list in the result: the
   first call should be the Skill tool loading the planner skill.
6. **The full run.** `python scripts/day1_spike.py`
   By default this runs every question with Haiku, loading the skill natively as chat does. To compare how the skill
   should reach the model, add `--runs haiku/native,haiku/inline,haiku/none`. Use `--effort low|medium|high` to trade
   cost for care. Plans are auto-approved and printed; `--approve ask` approves them yourself. Each run stops at the
   per-run cap.
7. **Grade.** Open `data/day1/day1_results.md`, fill the *Quality 0-2* column, compare the variants (if you ran
   more than one), and pick the skill mode and the per-run cap (start from *Suggested cap*, then set `RUN_BUDGET_USD`).

Cost of the default run: 10 questions with one variant = 10 runs. Per-question costs are unmeasured; a few percent of
the $240 monthly credit is the expected order. A sample report is in `docs/SAMPLE_day1_results.md` (invented numbers).

## The Day 1 gate

- [ ] Cost per question measured, and the per-run cap chosen.
- [ ] Haiku accepted: no answerable question scores Quality 0, and plans stay tight (one or two plans per question).
- [ ] A skill mode chosen (native, inline or none) from the comparison.
- [ ] Answers accepted for phone reading: short, plain text, pod and as-of line present.
- [ ] Both decline-type questions were declined and no data was read for them.
- [ ] Tool failures understood (the *Tool errors* column).

## What to bring back

The Summary table and your Quality scores. The full `.md` contains data read from the pod, so share it only if the
data owner agrees.

## Preflight failures

| Line | Meaning | Fix |
| --- | --- | --- |
| `uv on PATH` | The MCP cannot start | Install uv: https://docs.astral.sh/uv/ |
| `Required settings present` | Empty value | Fill `.env`. The MCP exits at startup without a pod URL and credentials |
| `Pod guard ORACLE_ALLOWED_BASE_URLS` FAIL | The allowed text is not inside the pod URL | Use a distinctive part of the pod host |
| `Anthropic API key` HTTP 401 | Wrong or revoked key | Create a key in the linked Console organisation |
| `Model ... is available` FAIL | The model name is not on your account | Set `MODEL` to a name from the list the check prints |
| `MCP starts and lists its tools` FAIL | The MCP exited | Run `uv run --directory vendor/ris-claude-gateway server.py` by hand and read stderr |
| `Fusion login and pod reachable` FAIL | Wrong URL, login, VPN or firewall | Open the pod URL in a browser from this machine; check the user and roles |
| `Both MCP servers connect` WARN (pending) | The first start is still building the MCP environment | Run the preflight again |
| `No built-in tools except what is intended` FAIL | The engine offers a tool we did not allow | Do not continue; send me the line |
| `Planner skill ... discovered` FAIL | The skill folder was not found | Check `PLANNER_SKILL_PATH` and `RUNTIME_DIR`, and that the skill file has a `name:` |

More on how the MCP behaves and fails: *Mentor's Cheat Sheet: Using the RIS Claude Gateway MCP in a Telegram Bridge*.

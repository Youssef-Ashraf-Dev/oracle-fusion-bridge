"""Settings, read once from the environment. Secrets never leave the host."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]      # the project folder: .env, data/, vendor/ and skill/ are found here, wherever the bot is started


def _abs(p: Path | str) -> Path:
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def _b(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # repr=False: a failed test, a traceback or a debug print shows the settings object, and it must never show these
    anthropic_api_key: str = field(default="", repr=False)
    telegram_token: str = field(default="", repr=False)

    model: str = "claude-haiku-5-5"              # one model plans and answers
    effort: Optional[str] = None                  # low | medium | high | xhigh | max; None = the engine's default

    mcp_dir: Path = Path("./vendor/ris-claude-gateway")
    planner_skill: Optional[Path] = None          # the vendor planner skill (SKILL.md) Claude chat would load
    skill_mode: str = "native"                    # native: the Skill tool, as in chat | inline: in the system prompt | none
    runtime_dir: Path = Path("./data/runtime")    # working directory the CLI runs in; holds .claude/skills/
    recipes_path: Optional[Path] = None           # verified notes about this pod, added to the system prompt
    log_dir: Path = Path("./data/logs")           # rotating log file; secrets are blanked before anything is written
    log_level: str = "INFO"
    log_questions: bool = False                   # log the first words of each question (off: questions may hold names)
    engine_dir: Path = Path("./data/engine")      # where the engine keeps its saved conversations (sensitive: they hold answers)
    shutdown_grace_s: float = 3.0                 # on shutdown, how long running questions get to finish before they are cancelled
    session_idle_minutes: int = 120               # a conversation idle longer than this starts fresh; 0 = never
    max_mcp_output_tokens: int = 50000            # the engine's cap on one tool result; larger results become unreadable files
    db_path: Path = Path("./data/bridge.db")

    oracle_base_url: str = ""
    oracle_user: str = field(default="", repr=False)
    oracle_password: str = field(default="", repr=False)
    oracle_api_version: str = "11.13.18.05"
    mcp_environment: str = "test"
    oracle_allowed_base_urls: str = ""
    mcp_prod_writes_enabled: bool = False

    run_budget_usd: float = 0.60                  # per question, passed to the Agent SDK; measured costs are $0.005 to $0.03
    max_turns: int = 30
    max_rows_per_call: int = 200                  # a data call asking for more rows than this is refused
    monthly_credit_usd: float = 240.0
    credit_cycle_day: int = 7
    user_daily_runs: int = 100                    # 0 = no daily limit; the monthly credit and the burst limit are the real guards
    burst_runs: int = 20                          # at most this many questions per user in burst_minutes: catches loops and misuse
    burst_minutes: int = 10
    show_cost: str = "all"                        # who sees costs in footers and /usage: all | admin | none
    admin_ids: frozenset = frozenset()            # Telegram IDs that see costs when show_cost is admin
    allow_hcm_sensitive: bool = False

    demo_mode: bool = False                       # a canned agent: test Telegram with no Oracle and no model

    max_concurrent_runs: int = 2
    compute_timeout_s: int = 180
    approval_timeout_s: int = 120

    message_limit: int = 4000                     # Telegram allows 4096 characters; keep headroom
    send_pace_s: float = 1.0                      # about one message per second per chat

    def can_see_cost(self, user_id: int) -> bool:
        if self.show_cost == "none":
            return False
        return self.show_cost != "admin" or user_id in self.admin_ids

    @property
    def effective_skill_mode(self) -> str:
        """native and inline both need a readable SKILL.md; without one the bridge runs with none."""
        if self.skill_mode in ("native", "inline") and self.planner_skill and Path(self.planner_skill).is_file():
            return self.skill_mode
        return "none"

    @property
    def live_ready(self) -> bool:
        """The vendor MCP exits at startup without a pod URL and credentials, so /ask needs all three."""
        return self.demo_mode or bool(self.oracle_base_url and self.oracle_user and self.oracle_password)


_FILE_KEYS: dict[str, int] = {}          # setting name -> the .env line it was read from, for the settings the file supplied


def _parse_env_file(path: Path) -> list[tuple[int, str, str]]:
    """(line number, key, value) for every KEY=VALUE line, in file order."""
    out: list[tuple[int, str, str]] = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:                                  # trailing comment on an unquoted value
            val = val.split(" #", 1)[0].rstrip()
        if key:
            out.append((n, key, val))
    return out


def load_env_file(path: str | Path = ".env") -> int:
    """Read KEY=VALUE lines into the environment without overriding values already set. Returns how many were added.

    A tiny loader so Windows users need no shell exports. Real environment variables always win, and when a key
    appears twice in the file the FIRST line wins. `origin()` and `env_conflicts()` report what actually happened.
    """
    p = Path(path)
    if not p.is_file():
        return 0
    added = 0
    for n, key, val in _parse_env_file(p):
        if key not in os.environ:
            os.environ[key] = val
            _FILE_KEYS[key] = n
            added += 1
    return added


def origin(name: str, path: str | Path = ".env") -> str:
    """Where the value in use for `name` came from, and whether that hides what .env says."""
    if name in _FILE_KEYS:
        return f".env line {_FILE_KEYS[name]}"
    if name in os.environ:
        p = Path(path)
        in_file = [v for _, k, v in _parse_env_file(p) if k == name] if p.is_file() else []
        if in_file and in_file[0] == os.environ[name]:
            return "your shell environment (same value as .env, so nothing is overridden; reopen the terminal after editing .env)"
        if in_file:
            return "your shell environment (it OVERRIDES the different value in .env)"
        return "your shell environment (.env does not set it)"
    return "not set"


def env_conflicts(path: str | Path = ".env", names: tuple[str, ...] = ()) -> list[str]:
    """Plain-language warnings about settings that are not what the .env file appears to say."""
    p = Path(path)
    if not p.is_file():
        return []
    by_key: dict[str, list[tuple[int, str]]] = {}
    for n, key, val in _parse_env_file(p):
        by_key.setdefault(key, []).append((n, val))
    out: list[str] = []
    for key, entries in by_key.items():
        if names and key not in names:
            continue
        if len(entries) > 1 and len({v for _, v in entries}) > 1:
            lines = ", ".join(str(n) for n, _ in entries)
            out.append(f"{key} appears on lines {lines} with different values; the first one wins. Delete the extra lines.")
        used = os.environ.get(key)
        if key not in _FILE_KEYS and used is not None and used != entries[0][1]:
            out.append(f"{key} is also set in your shell with a different value, and the shell wins over .env.")
    return out


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    if env_file:
        load_env_file(_abs(env_file))
    e = os.environ.get
    rp = e("RECIPES_PATH", "").strip()
    recipes = _abs(rp) if rp else (ROOT / "skill" / "recipes.md" if (ROOT / "skill" / "recipes.md").is_file() else None)
    skill = e("PLANNER_SKILL_PATH", "").strip()
    if not skill and (ROOT / "skill" / "ris-fusion-gateway-skill" / "SKILL.md").is_file():
        skill = str(ROOT / "skill" / "ris-fusion-gateway-skill" / "SKILL.md")      # the copy shipped with the project
    return Settings(
        anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
        telegram_token=e("TELEGRAM_BOT_TOKEN", ""),
        model=e("MODEL", "claude-haiku-5-5"),
        effort=e("EFFORT") or None,
        skill_mode=e("SKILL_MODE", "native").strip().lower(),
        runtime_dir=_abs(e("RUNTIME_DIR", "./data/runtime")),
        engine_dir=_abs(e("ENGINE_DIR", "./data/engine")),
        log_dir=_abs(e("LOG_DIR", "./data/logs")),
        log_level=e("LOG_LEVEL", "INFO").upper(),
        log_questions=_b("LOG_QUESTIONS"),
        recipes_path=recipes,
        session_idle_minutes=int(e("SESSION_IDLE_MINUTES", "120")),
        shutdown_grace_s=float(e("SHUTDOWN_GRACE_S", "3")),
        max_mcp_output_tokens=int(e("MAX_MCP_OUTPUT_TOKENS", "50000")),
        mcp_dir=_abs(e("MCP_DIR", "./vendor/ris-claude-gateway")),
        planner_skill=_abs(skill) if skill else None,
        db_path=_abs(e("DB_PATH", "./data/bridge.db")),
        oracle_base_url=e("ORACLE_FUSION_BASE_URL", ""),
        oracle_user=e("ORACLE_USERNAME", ""),
        oracle_password=e("ORACLE_PASSWORD", ""),
        oracle_api_version=e("ORACLE_FUSION_API_VERSION", "11.13.18.05"),
        mcp_environment=e("MCP_ENVIRONMENT", "test"),
        oracle_allowed_base_urls=e("ORACLE_ALLOWED_BASE_URLS", ""),
        mcp_prod_writes_enabled=_b("MCP_PROD_WRITES_ENABLED"),
        run_budget_usd=float(e("RUN_BUDGET_USD", "0.60")),
        max_turns=int(e("MAX_TURNS", "30")),
        max_rows_per_call=int(e("MAX_ROWS_PER_CALL", "200")),
        monthly_credit_usd=float(e("MONTHLY_CREDIT_USD", "240")),
        credit_cycle_day=int(e("CREDIT_CYCLE_DAY", "7")),
        user_daily_runs=int(e("USER_DAILY_RUNS", "100")),
        burst_runs=int(e("USER_BURST_RUNS", "20")),
        burst_minutes=int(e("USER_BURST_MINUTES", "10")),
        show_cost=e("SHOW_COST", "all").strip().lower(),
        admin_ids=frozenset(int(x) for x in e("ADMIN_TELEGRAM_USER_IDS", "").replace(" ", "").split(",") if x.isdigit()),
        allow_hcm_sensitive=_b("ALLOW_HCM_SENSITIVE"),
        demo_mode=_b("DEMO_MODE"),
    )

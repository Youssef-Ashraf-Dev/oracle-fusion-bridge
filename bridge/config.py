"""Settings, read once from the environment. Secrets never leave the host."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _b(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str = ""
    telegram_token: str = ""

    model: str = "claude-haiku-5-5"              # one model plans and answers
    effort: Optional[str] = None                  # low | medium | high | xhigh | max; None = the engine's default

    mcp_dir: Path = Path("./vendor/ris-claude-gateway")
    planner_skill: Optional[Path] = None          # the vendor planner skill (SKILL.md) Claude chat would load
    skill_mode: str = "native"                    # native: the Skill tool, as in chat | inline: in the system prompt | none
    runtime_dir: Path = Path("./data/runtime")    # working directory the CLI runs in; holds .claude/skills/
    max_mcp_output_tokens: int = 25000            # the engine's cap on one tool result; larger results become unreadable files
    db_path: Path = Path("./data/bridge.db")

    oracle_base_url: str = ""
    oracle_user: str = ""
    oracle_password: str = ""
    oracle_api_version: str = "11.13.18.05"
    mcp_environment: str = "test"
    oracle_allowed_base_urls: str = ""
    mcp_prod_writes_enabled: bool = False

    run_budget_usd: float = 0.60                  # per question, passed to the Agent SDK; Day 1 tunes it
    max_turns: int = 30
    monthly_credit_usd: float = 240.0
    credit_cycle_day: int = 7
    user_daily_runs: int = 25
    allow_hcm_sensitive: bool = False

    demo_mode: bool = False                       # a canned agent: test Telegram with no Oracle and no model

    max_concurrent_runs: int = 2
    compute_timeout_s: int = 180
    approval_timeout_s: int = 120

    message_limit: int = 4000                     # Telegram allows 4096 characters; keep headroom
    send_pace_s: float = 1.0                      # about one message per second per chat

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


def origin(name: str) -> str:
    """Where the value in use for `name` came from."""
    if name in _FILE_KEYS:
        return f".env line {_FILE_KEYS[name]}"
    if name in os.environ:
        return "your shell environment (it overrides .env)"
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
        load_env_file(env_file)
    e = os.environ.get
    skill = e("PLANNER_SKILL_PATH", "").strip()
    if not skill and Path("./skill/ris-fusion-gateway-skill/SKILL.md").is_file():
        skill = "./skill/ris-fusion-gateway-skill/SKILL.md"            # the copy shipped with the project
    return Settings(
        anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
        telegram_token=e("TELEGRAM_BOT_TOKEN", ""),
        model=e("MODEL", "claude-haiku-5-5"),
        effort=e("EFFORT") or None,
        skill_mode=e("SKILL_MODE", "native").strip().lower(),
        runtime_dir=Path(e("RUNTIME_DIR", "./data/runtime")),
        max_mcp_output_tokens=int(e("MAX_MCP_OUTPUT_TOKENS", "25000")),
        mcp_dir=Path(e("MCP_DIR", "./vendor/ris-claude-gateway")),
        planner_skill=Path(skill) if skill else None,
        db_path=Path(e("DB_PATH", "./data/bridge.db")),
        oracle_base_url=e("ORACLE_FUSION_BASE_URL", ""),
        oracle_user=e("ORACLE_USERNAME", ""),
        oracle_password=e("ORACLE_PASSWORD", ""),
        oracle_api_version=e("ORACLE_FUSION_API_VERSION", "11.13.18.05"),
        mcp_environment=e("MCP_ENVIRONMENT", "test"),
        oracle_allowed_base_urls=e("ORACLE_ALLOWED_BASE_URLS", ""),
        mcp_prod_writes_enabled=_b("MCP_PROD_WRITES_ENABLED"),
        run_budget_usd=float(e("RUN_BUDGET_USD", "0.60")),
        max_turns=int(e("MAX_TURNS", "30")),
        monthly_credit_usd=float(e("MONTHLY_CREDIT_USD", "240")),
        credit_cycle_day=int(e("CREDIT_CYCLE_DAY", "7")),
        user_daily_runs=int(e("USER_DAILY_RUNS", "25")),
        allow_hcm_sensitive=_b("ALLOW_HCM_SENSITIVE"),
        demo_mode=_b("DEMO_MODE"),
    )

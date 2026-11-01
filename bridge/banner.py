"""What the bot says about itself when it starts: versions, mode and limits, never secrets."""
from __future__ import annotations

import platform
import sys
from urllib.parse import urlparse

from . import __version__
from .config import Settings
from .live.plan import MAX_ATTEMPTS, MAX_DATA_CALLS
from .live.prompt import load_recipes
from .live.skills import skill_name


def _version(pkg: str) -> str:
    try:
        from importlib.metadata import version
        return version(pkg)
    except Exception:
        return "unknown"


def startup_lines(s: Settings, registered: int) -> list[str]:
    mode = s.effective_skill_mode
    skill = f"{mode} ({skill_name(s.planner_skill)})" if mode != "none" else "none"
    recipes = load_recipes(s.recipes_path)
    host = urlparse(s.oracle_base_url).netloc or "not set"
    return [
        f"oracle-fusion-bridge {__version__} | python {sys.version.split()[0]} on {platform.system()} | "
        f"claude-agent-sdk {_version('claude-agent-sdk')} | python-telegram-bot {_version('python-telegram-bot')}",
        f"mode: {'DEMO (invented answers)' if s.demo_mode else 'live'} | pod: {host} | model: {s.model} | effort: {s.effort or 'default'} | "
        f"skill: {skill} | recipes: {len(recipes)} characters",
        f"limits: ${s.run_budget_usd:.2f} per run, {s.max_turns} turns, {MAX_DATA_CALLS} data calls ({MAX_ATTEMPTS} attempts), "
        f"{s.max_rows_per_call} rows per call, {s.compute_timeout_s}s compute, {s.approval_timeout_s}s to approve, "
        f"{s.user_daily_runs or 'unlimited'} questions per user per day (burst {s.burst_runs} in {s.burst_minutes} min), "
        f"conversation idle limit {s.session_idle_minutes} min, costs shown to: {s.show_cost}",
        f"users: {registered} registered | log file: {s.log_dir / 'bridge.log'} | questions in the log: {'yes' if s.log_questions else 'no'}",
    ]

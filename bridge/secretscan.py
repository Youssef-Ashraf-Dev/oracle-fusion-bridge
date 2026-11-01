"""Look for secrets in the files that would be committed or shared, and report where, never what."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

PATTERNS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    "Telegram bot token": re.compile(r"(?<!\d)\d{8,12}:[A-Za-z0-9_-]{30,}"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
SECRET_NAMES = ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "ORACLE_PASSWORD")
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}
NO_PATTERN_DIRS = {"tests", "vendor"}   # fake tokens in tests and example text in vendor code are expected; real values are still checked


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str                         # what it is: never the value itself


def known_secrets(env: Mapping[str, str], env_file: Path | None = None) -> dict[str, str]:
    """The current real values of the secret settings, from the environment and the .env file. Short values are ignored."""
    values: dict[str, str] = {}
    if env_file is not None and Path(env_file).is_file():
        for raw in Path(env_file).read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, val = raw.partition("=")
            if key.strip() in SECRET_NAMES:
                values[key.strip()] = val.strip().strip("\"'")
    for k in SECRET_NAMES:
        if env.get(k):
            values[k] = env[k]
    return {k: v for k, v in values.items() if len(v) >= 6}


def scan_text(text: str, secrets: Mapping[str, str], patterns: bool = True) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        if patterns:
            hits += [(n, kind) for kind, rx in PATTERNS.items() if rx.search(line)]
        hits += [(n, f"the current value of {name}") for name, val in secrets.items() if val in line]
    return hits


def candidate_files(root: Path) -> list[Path]:
    """Files git would commit (tracked, or untracked and not ignored). Without git: everything outside the usual skip folders."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root, capture_output=True,
                             text=True, timeout=30, check=True).stdout
        return [root / p for p in out.splitlines() if p and (root / p).is_file()]
    except Exception:
        found = []
        for p in root.rglob("*"):
            if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS) and p.name != ".env":
                found.append(p)
        return found


def scan_files(root: Path, files: Iterable[Path], secrets: Mapping[str, str], max_bytes: int = 2_000_000) -> list[Finding]:
    findings: list[Finding] = []
    for f in files:
        try:
            if f.stat().st_size > max_bytes:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = f.relative_to(root)
        patterns = rel.parts[0] not in NO_PATTERN_DIRS
        findings += [Finding(rel.as_posix(), n, kind) for n, kind in scan_text(text, secrets, patterns)]
    return findings


def local_data_files(root: Path) -> list[Path]:
    """Files that stay on this machine (logs, run records, reports). A secret here is not in git, but it is on disk."""
    out: list[Path] = []
    for sub in ("data/logs", "data/runall", "data/day1"):
        d = root / sub
        if d.is_dir():
            out += [p for p in d.rglob("*") if p.is_file() and p.suffix not in (".db", ".db-wal", ".db-shm")]
    return out

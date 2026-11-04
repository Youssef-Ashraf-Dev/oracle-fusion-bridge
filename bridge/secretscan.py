"""Look for secrets in the files that would be committed or shared, and report where, never what."""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .repocheck import NotAGitRepo, _git
from .textio import read_text

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
        for raw in read_text(env_file).splitlines():
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
                             text=True, timeout=30, check=True, encoding="utf-8", errors="replace").stdout
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
    for sub in ("data/logs", "data/runall"):
        d = root / sub
        if d.is_dir():
            out += [p for p in d.rglob("*") if p.is_file() and p.suffix not in (".db", ".db-wal", ".db-shm")]
    return out


# ---- the whole history: every commit, not just the files as they are today ------------------------------------------

# the same shapes written for git grep -E (no look-behind), applied to every file except tests and vendor
GIT_SHAPES = {
    "Anthropic API key": r"sk-ant-[A-Za-z0-9_-]{16,}",
    "Telegram bot token": r"[0-9]{8,12}:[A-Za-z0-9_-]{30,}",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}


@dataclass(frozen=True)
class HistoryFinding:
    commit: str           # first 8 characters of the commit id
    path: str
    kind: str             # what it is: never the value


def revisions(root: Path) -> list[str]:
    return _git(root, "rev-list", "--all").split()


def _grep_revs(root: Path, revs: list[str], args: list[str], pathspec: list[str]) -> list[tuple[str, str]]:
    """(commit, path) for every file in `revs` that matches. git grep exits 1 when nothing matches, which is not an error."""
    r = subprocess.run(["git", "grep", "-I", "-l", *args, *revs, "--", *pathspec], cwd=root, capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
    if r.returncode not in (0, 1):
        raise NotAGitRepo(f"git grep failed: {(r.stderr or '').strip()[:160]}")
    out = []
    for line in r.stdout.splitlines():
        rev, _, path = line.partition(":")
        if rev and path:
            out.append((rev[:8], path))
    return out


def scan_history(root: Path, secrets: Mapping[str, str], also: Iterable[str] = (), chunk: int = 25) -> list[HistoryFinding]:
    """Search every commit of every branch for key and token shapes, for the exact current secret values, and for extra words.

    Reports where (commit and path) and what kind, never the text itself. Windows limits a command line, so commits go in chunks.
    """
    revs = revisions(root)
    found: list[HistoryFinding] = []
    with tempfile.TemporaryDirectory() as tmp:
        sfile = Path(tmp) / "secrets.txt"
        sfile.write_text("\n".join(secrets.values()) + "\n", encoding="utf-8")
        afile = Path(tmp) / "also.txt"
        words = [w for w in also if w]
        afile.write_text("\n".join(words) + "\n", encoding="utf-8")
        for i in range(0, len(revs), chunk):
            part = revs[i:i + chunk]
            for kind, rx in GIT_SHAPES.items():
                found += [HistoryFinding(c, p, kind) for c, p in _grep_revs(root, part, ["-E", "-e", rx], [".", ":(exclude)tests", ":(exclude)vendor"])]
            if secrets:
                names = ", ".join(secrets)
                found += [HistoryFinding(c, p, f"a current secret value ({names})") for c, p in _grep_revs(root, part, ["-F", "-f", str(sfile)], ["."])]
            if words:
                found += [HistoryFinding(c, p, "an extra word you asked for") for c, p in _grep_revs(root, part, ["-i", "-F", "-f", str(afile)], ["."])]
    return sorted(set(found), key=lambda f: (f.kind, f.path, f.commit))


def identities(root: Path) -> list[str]:
    """Every name and email that appears as an author or committer in any commit."""
    out = _git(root, "log", "--all", "--format=%an <%ae>%n%cn <%ce>")
    return sorted({ln.strip() for ln in out.splitlines() if ln.strip()})

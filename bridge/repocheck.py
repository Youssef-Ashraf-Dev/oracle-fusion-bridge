"""Will a fresh clone of this repository work? Finds files that exist here but that git would not carry over."""
from __future__ import annotations

import subprocess
from pathlib import Path

LOCAL_ONLY_PARTS = {".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", ".git", "audit"}
LOCAL_ONLY_NAMES = {".env"}


class NotAGitRepo(RuntimeError):
    pass


def _git(root: Path, *args: str, stdin: str | None = None) -> str:
    try:
        return subprocess.run(["git", *args], cwd=root, input=stdin, capture_output=True, text=True, timeout=60, check=True).stdout
    except FileNotFoundError as e:
        raise NotAGitRepo("git is not installed or not on the PATH") from e
    except subprocess.CalledProcessError as e:
        raise NotAGitRepo(f"{root} is not a git repository, or git failed: {(e.stderr or '').strip()[:160]}") from e


def ignored_but_present(root: Path) -> list[tuple[str, str]]:
    """(path, rule) for files on disk that git ignores and that are not the usual local-only things.

    The project's own data/ folder, virtual environments, caches, audit logs and .env are expected to stay local. Anything else
    that is ignored, such as a package's own data/ folder hidden by an unanchored rule, will be missing from a clone.
    """
    listed = [p for p in _git(root, "ls-files", "--others", "--ignored", "--exclude-standard").splitlines() if p]
    candidates = [p for p in listed
                  if not (set(Path(p).parts) & LOCAL_ONLY_PARTS) and Path(p).name not in LOCAL_ONLY_NAMES and Path(p).parts[0] != "data"]
    if not candidates:
        return []
    rules = {}
    for line in _git(root, "check-ignore", "-v", "--stdin", stdin="\n".join(candidates)).splitlines():
        info, _, path = line.partition("\t")
        rules[path] = info.split(":", 2)[2] if info.count(":") >= 2 else info
    return [(p, rules.get(p, "?")) for p in candidates]

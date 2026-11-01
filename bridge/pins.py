"""Pin the dependencies to exactly what is installed, and check that the installed versions still match the pins."""
from __future__ import annotations

import json
import re
from importlib import metadata
from typing import Iterable, Mapping

SKIP_IN_CONSTRAINTS = {"pip", "setuptools", "wheel", "distribute"}
_LINE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<extras>\[[^\]]*\])?\s*(?P<spec>[<>=!~][^#;]*)?(?P<rest>\s*(?:;[^#]*)?(?:#.*)?)$")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirements(text: str) -> list[tuple[str, str]]:
    """(name, version spec) for each requirement line. Comments, blank lines and options such as -r are skipped."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        m = _LINE.match(line)
        if m:
            out.append((m["name"], (m["spec"] or "").strip()))
    return out


def installed_versions() -> dict[str, str]:
    return {norm(d.metadata["Name"]): d.version for d in metadata.distributions() if d.metadata["Name"]}


def pin_requirements(text: str, versions: Mapping[str, str]) -> tuple[str, list[str]]:
    """Rewrite each requirement line as name==installed version, keeping extras, markers, comments and order.
    Returns the new text and the names that are not installed (their lines are left as they were)."""
    missing: list[str] = []
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        m = _LINE.match(line) if line and not line.startswith(("#", "-")) else None
        if not m:
            lines.append(raw)
            continue
        v = versions.get(norm(m["name"]))
        if v is None:
            missing.append(m["name"])
            lines.append(raw)
            continue
        lines.append(f"{m['name']}{m['extras'] or ''}=={v}{(' ' + m['rest'].strip()) if m['rest'].strip() else ''}")
    return "\n".join(lines) + "\n", missing


def _is_local(dist) -> bool:
    """An editable install or a folder on disk: it has no version a clean machine could download."""
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        raw = None
    if not raw:
        return False
    try:
        info = json.loads(raw)
    except ValueError:
        return True
    return bool(info.get("dir_info")) or str(info.get("url", "")).startswith("file:")


def constraints_text(dists: Iterable) -> str:
    """Every installed package at its exact version, one per line. Used with `pip install -c`, which only restricts
    versions and never forces a package to be installed, so Windows-only entries are harmless on Linux."""
    seen: dict[str, str] = {}
    for d in dists:
        name = d.metadata["Name"]
        if not name or norm(name) in SKIP_IN_CONSTRAINTS or norm(name) in seen or _is_local(d):
            continue
        seen[norm(name)] = f"{name}=={d.version}"
    header = ("# Exact versions installed when this file was made (python scripts/pin_requirements.py --write).\n"
              "# Use with: pip install -r requirements.txt -c constraints.txt\n")
    return header + "\n".join(seen[k] for k in sorted(seen)) + "\n"


def check_pins(req_text: str, installed: Mapping[str, str]) -> list[str]:
    """What is wrong: lines that are not pinned with ==, packages not installed, installed versions that differ from the pin."""
    problems = []
    for name, spec in requirements(req_text):
        if not spec.startswith("=="):
            problems.append(f"{name}: not pinned ({spec or 'any version'})")
            continue
        want = spec[2:].strip()
        have = installed.get(norm(name))
        if have is None:
            problems.append(f"{name}: pinned {want} but not installed")
        elif have != want:
            problems.append(f"{name}: pinned {want}, installed {have}")
    return problems

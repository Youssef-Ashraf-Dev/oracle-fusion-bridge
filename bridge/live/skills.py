"""Reproduce how Claude chat loads a skill, for the Agent SDK.

In chat a skill is a folder with SKILL.md; the model sees its name and description and loads the body when the
description matches. The Agent SDK does the same from <cwd>/.claude/skills/<name>/SKILL.md when setting_sources
includes "project" and the Skill tool is available. This module builds that folder from the vendor's SKILL.md.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..textio import read_text


def skill_name(skill_md: Path) -> str:
    """The `name:` from the SKILL.md front matter, falling back to the containing folder's name."""
    text = read_text(skill_md)
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if m:
        n = re.search(r"^name:\s*[\"']?([A-Za-z0-9._-]+)[\"']?\s*$", m.group(1), re.M)
        if n:
            return n.group(1)
    return Path(skill_md).resolve().parent.name


def skill_body(skill_md: Path) -> str:
    text = read_text(skill_md)
    return text.split("---", 2)[-1].strip() if text.startswith("---") else text


def valid_skill_name(name: str) -> bool:
    """The convention for skill names: lowercase letters, digits and single hyphens, at most 64 characters."""
    return bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)) and len(name) <= 64


def prepare_runtime(skill_md: Path, runtime_dir: Path) -> Path:
    """Create <runtime_dir>/.claude/skills/<name>/SKILL.md (a copy) and return runtime_dir.

    The folder holds nothing else, so loading project settings from it cannot pull in anything unexpected. Folders left
    by an earlier name or an earlier file are removed: a renamed skill must not leave its old self behind.
    """
    name = skill_name(skill_md)
    skills_dir = Path(runtime_dir) / ".claude" / "skills"
    if skills_dir.is_dir():
        for child in skills_dir.iterdir():
            if child.is_dir() and child.name != name:
                shutil.rmtree(child, ignore_errors=True)
    target = skills_dir / name
    target.mkdir(parents=True, exist_ok=True)
    dest = target / "SKILL.md"
    want = engine_bytes(skill_md)
    if not dest.exists() or dest.read_bytes() != want:
        dest.write_bytes(want)
    return Path(runtime_dir)


def engine_bytes(skill_md: Path) -> bytes:
    """The skill as the engine should read it: Unix line endings and no byte-order mark. A Git checkout on Windows can turn the file into
    CRLF (this project's .gitattributes asks Git to normalise text files), and the file must begin with the '---' of its header."""
    data = Path(skill_md).read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.replace(b"\r\n", b"\n")

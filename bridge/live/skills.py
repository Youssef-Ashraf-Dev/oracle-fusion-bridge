"""Reproduce how Claude chat loads a skill, for the Agent SDK.

In chat a skill is a folder with SKILL.md; the model sees its name and description and loads the body when the
description matches. The Agent SDK does the same from <cwd>/.claude/skills/<name>/SKILL.md when setting_sources
includes "project" and the Skill tool is available. This module builds that folder from the vendor's SKILL.md.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path


def skill_name(skill_md: Path) -> str:
    """The `name:` from the SKILL.md front matter, falling back to the containing folder's name."""
    text = Path(skill_md).read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if m:
        n = re.search(r"^name:\s*[\"']?([A-Za-z0-9._-]+)[\"']?\s*$", m.group(1), re.M)
        if n:
            return n.group(1)
    return Path(skill_md).resolve().parent.name


def skill_body(skill_md: Path) -> str:
    text = Path(skill_md).read_text(encoding="utf-8")
    return text.split("---", 2)[-1].strip() if text.startswith("---") else text


def prepare_runtime(skill_md: Path, runtime_dir: Path) -> Path:
    """Create <runtime_dir>/.claude/skills/<name>/SKILL.md (a copy) and return runtime_dir.

    The folder holds nothing else, so loading project settings from it cannot pull in anything unexpected.
    """
    name = skill_name(skill_md)
    target = Path(runtime_dir) / ".claude" / "skills" / name
    target.mkdir(parents=True, exist_ok=True)
    dest = target / "SKILL.md"
    if not dest.exists() or dest.read_bytes() != Path(skill_md).read_bytes():
        shutil.copyfile(skill_md, dest)
    return Path(runtime_dir)

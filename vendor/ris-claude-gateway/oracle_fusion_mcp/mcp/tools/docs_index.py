"""Docs-derived candidate names for discovery suggestions (read-only data).

Phase 1: docs-assisted candidate discovery. This module loads a
hand-reviewed snapshot (``docs_catalog.json``: resource names plus
documentation links — no attribute schemas, no required fields, no
policy data) and answers one question: which docs-listed names exist
for an API family.

Hard rules (do not relax without review):
- No network I/O in this module. Ever. The snapshot is baked at build
  time; staleness fails safe (a removed name costs one live 404).
- Family-scoped: ``docs_candidate_names("fscm")`` never returns HCM
  names and vice versa. Phase 1 ships FSCM entries only.
- Advisory only: membership here must never gate, authorize, validate,
  or resolve anything. It feeds suggestion pools, nothing else.
- Loader never raises: missing file, malformed JSON, wrong shapes,
  malformed or duplicate entries all degrade to skipping (or {}).
"""

import json
import os
from typing import Any, Dict, Set

_CATALOG_FILENAME = "docs_catalog.json"
_cache: Dict[str, Dict[str, Any]] = {}


def _catalog_path() -> str:
    return os.path.join(os.path.dirname(__file__), _CATALOG_FILENAME)


def load_docs_catalog(path: Any = None) -> Dict[str, Any]:
    """Load the docs snapshot. Never raises; {} on any problem."""
    key = str(path) if path else _catalog_path()
    if key in _cache:
        return _cache[key]
    try:
        with open(path or _catalog_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        _cache[key] = {}
        return {}
    if not isinstance(raw, dict):
        _cache[key] = {}
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, list):
        _cache[key] = {}
        return {}
    clean = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("resource")
        family = entry.get("family")
        if not name or not isinstance(name, str) or not name.strip():
            continue
        if family not in ("fscm", "hcm"):
            continue
        name = name.strip()
        if name in seen:
            continue  # duplicates: first wins, deterministic
        seen.add(name)
        clean.append(entry)
    out = dict(raw)
    out["entries"] = clean
    _cache[key] = out
    return out


def docs_candidate_names(family: str) -> Set[str]:
    """Docs-listed resource names for one API family. Never raises."""
    if family not in ("fscm", "hcm"):
        return set()
    try:
        catalog = load_docs_catalog()
    except Exception:
        return set()
    try:
        return {e["resource"] for e in catalog.get("entries", [])
                if isinstance(e, dict) and e.get("family") == family
                and isinstance(e.get("resource"), str)}
    except Exception:
        return set()


def docs_entry_for(resource: Any, family: str) -> Dict[str, Any]:
    """Provenance record for one name (doc_url etc.), or {} if unlisted."""
    if not isinstance(resource, str) or family not in ("fscm", "hcm"):
        return {}
    try:
        for entry in load_docs_catalog().get("entries", []):
            if isinstance(entry, dict) and entry.get("family") == family \
                    and entry.get("resource") == resource:
                return dict(entry)
    except Exception:
        pass
    return {}

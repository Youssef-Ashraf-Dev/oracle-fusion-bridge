"""ADF ``q=`` filter helpers: validate a raw filter against a known attribute
set, and build a filter string from structured conditions.

Validation is best-effort: it extracts the attributes a filter references and
rejects any that aren't in the resource's schema, so the model gets an
actionable error before an Oracle 400. It does not fully parse ADF grammar.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import FilterError

# Identifier immediately followed by a comparison operator -> a filter attribute.
_OPERATORS = r"(?:=|!=|<>|>=|<=|>|<|\bLIKE\b|\bIN\b|\bBETWEEN\b|\bIS\b)"
_ATTR_RE = re.compile(rf"([A-Za-z_][A-Za-z0-9_]*)\s*{_OPERATORS}", re.IGNORECASE)

# Reserved words that can precede an operator but are not attributes.
_RESERVED = {"and", "or", "not"}

_BUILD_OPS = {"=", "!=", ">", ">=", "<", "<=", "LIKE"}


def extract_attributes(q: str) -> set[str]:
    """Return the attribute identifiers referenced on the left of operators."""
    found = {m.group(1) for m in _ATTR_RE.finditer(q)}
    return {a for a in found if a.lower() not in _RESERVED}


def validate_q(q: str, allowed: set[str]) -> None:
    """Raise :class:`FilterError` if ``q`` references attributes not in ``allowed``."""
    if not allowed:
        return  # no schema to validate against — skip
    unknown = {a for a in extract_attributes(q) if a not in allowed}
    if unknown:
        sample = ", ".join(sorted(allowed)[:25])
        raise FilterError(
            f"Filter references unknown attribute(s): {', '.join(sorted(unknown))}. "
            f"Valid filterable attributes include: {sample}"
            + (" ..." if len(allowed) > 25 else "")
        )


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def build_q(conditions: list[dict[str, Any]]) -> str:
    """Build an ADF ``q`` string from ``[{attr, op, value}]`` conditions (AND-joined)."""
    parts: list[str] = []
    for cond in conditions:
        attr = cond["attr"]
        op = cond.get("op", "=").upper() if cond.get("op", "=") in {"LIKE"} else cond.get("op", "=")
        if op not in _BUILD_OPS:
            raise FilterError(f"Unsupported operator {op!r}. Allowed: {sorted(_BUILD_OPS)}")
        parts.append(f"{attr} {op} {_format_value(cond['value'])}")
    return " and ".join(parts)

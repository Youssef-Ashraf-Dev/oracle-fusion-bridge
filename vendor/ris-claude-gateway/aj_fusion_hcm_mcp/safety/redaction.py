"""PII redaction for HCM responses.

Masks sensitive fields by default (national IDs, salary, date of birth, etc.).
Unmasking requires ``features.sensitive_fields_enabled`` and is audit-logged by
the caller. Matching is by normalized field-name substring, so variants like
``AnnualSalary`` or ``NationalIdentifierNumber`` are caught (DESIGN.md §7).
"""

from __future__ import annotations

import re
from typing import Any

# Normalized (lowercased, alphanumerics-only) substrings that mark a field PII.
DEFAULT_SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "nationalid",
    "nationalidentifier",
    "ssn",
    "socialsecurity",
    "salary",
    "dateofbirth",
    "birthdate",
    "passport",
    "taxpayer",
    "bankaccount",
    "iban",
    "visapermit",
)

_MASK = "***REDACTED***"
_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _normalize(name: str) -> str:
    return _NON_ALNUM.sub("", name.lower())


class Redactor:
    def __init__(self, enabled: bool = True, keywords: tuple[str, ...] | None = None) -> None:
        self.enabled = enabled
        self._keywords = tuple(_normalize(k) for k in (keywords or DEFAULT_SENSITIVE_KEYWORDS))

    def is_sensitive(self, field_name: str) -> bool:
        norm = _normalize(field_name)
        return any(kw in norm for kw in self._keywords)

    def redact(self, obj: Any) -> Any:
        """Return a copy of ``obj`` with sensitive field values masked."""
        if not self.enabled:
            return obj
        if isinstance(obj, dict):
            return {
                k: (_MASK if self.is_sensitive(k) else self.redact(v)) for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self.redact(v) for v in obj]
        return obj

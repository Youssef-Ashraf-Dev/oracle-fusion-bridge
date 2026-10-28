"""Oracle write-failure classifier (read-only analysis, never executes).

When a Fusion mutation fails, this module parses the Oracle error text for
FIELD-LEVEL guidance: which fields are missing (must be added), which were
refused (must be omitted), or whether the failure is policy/auth (no field
change will help). Every suggestion names the exact curated table edit —
proposed for human approval, never auto-applied.

Known-evidence patterns (extend only from verified live errors):
- AP-810667 "You must provide a valid value for the Supplier,
  Supplier Site attribute." -> missing runtime-required fields.
- POZ-2130496 "You must provide a value for either the X or XCode
  attribute." -> missing runtime-required fields (display names).
- POZ-2130495 "You can't provide a value for the X attribute." /
  "Attribute X in view object Y cannot be set." -> refused fields
  (omit-table candidates).
- AP-810730 voucher / delete blocks -> policy (no field suggestion).
- HTTP 401/403 -> auth (no field suggestion).
"""

import re
from typing import Any, Dict, List, Optional

_APPROVAL_NOTE = (
    "Proposed — requires human approval; never auto-applied. "
    "Re-run fusion_create_requirements after any table change."
)

# "You must provide a [valid] value for [either] the A[, B] [or C] attribute(s)."
_MUST_PROVIDE_RE = re.compile(
    r"[Yy]ou must provide a (?:valid )?value for (?:either )?the "
    r"(.+?)(?:\s+attributes?\b|[.;])",
    re.IGNORECASE,
)

# "Attribute X in view object Y cannot be set." / "You can't provide a
# value for the X attribute."
_CANNOT_SET_RE = re.compile(
    r"[Aa]ttribute (\w+) in view object \w+ cannot be set", re.IGNORECASE)
_CANNOT_PROVIDE_RE = re.compile(
    r"[Yy]ou can'?t provide a value for the (\w+) attribute", re.IGNORECASE)

# Generic fallback: "X is required" / "X is mandatory" / "X cannot be null".
_GENERIC_REQUIRED_RE = re.compile(
    r"(\b[A-Z][A-Za-z0-9_]*\b)\s+(?:is required|is mandatory|cannot be null)",
    re.IGNORECASE)

_POLICY_MARKERS = (
    "voucher",
    "cannot be deleted",
    "cancel ",
    "approval",
    "period is closed",
    "accounting date",
    "insufficient privilege",
    "not authorized",
)


def _split_field_list(text: str) -> List[str]:
    """Split "A, B or C" style Oracle field enumerations into names.

    Oracle often names display labels with spaces ("Supplier Site") while
    the attribute is spaceless ("SupplierSite"): both the raw part (when
    identifier-shaped) and its spaceless join are kept as candidates.
    """
    parts = re.split(r",|\bor\b| and ", text)
    names = []
    for part in parts:
        candidate = part.strip().strip("'\"")
        if re.match(r"^[A-Za-z][A-Za-z0-9_]*$", candidate):
            if candidate not in names:
                names.append(candidate)
            continue
        joined = candidate.replace(" ", "")
        if re.match(r"^[A-Za-z][A-Za-z0-9_]*$", joined) \
                and joined not in names:
            names.append(joined)
    return names


def classify_write_error(error_text: Any, *, system: str, resource: str,
                         scope: str, operation: str,
                         action: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Classify a failed mutation's error text. Never raises.

    Returns None when the error carries no field-level guidance (auth
    failures, transport errors, unrecognized shapes).
    """
    if not isinstance(error_text, str) or not error_text.strip():
        return None
    text = error_text.strip()
    lowered = text.lower()
    if "[401]" in text or "[403]" in text \
            or "unauthorized" in lowered or "forbidden" in lowered:
        return {"category": "auth", "fields": [], "suggestion": None,
                "confidence": "high",
                "note": "Authorization failure: no field change will help. "
                        "Check the integration user's roles."}
    refused: List[str] = []
    for pattern in (_CANNOT_SET_RE, _CANNOT_PROVIDE_RE):
        match = pattern.search(text)
        if match and match.group(1) not in refused:
            refused.append(match.group(1))
    if refused:
        return {"category": "refused_fields", "fields": refused,
                "suggestion": _omit_suggestion(system, resource, operation,
                                              refused),
                "confidence": "high",
                "note": "Fusion refuses any caller-supplied value for these "
                        "fields. " + _APPROVAL_NOTE}
    match = _MUST_PROVIDE_RE.search(text.replace("\n", " "))
    if match:
        fields = _split_field_list(match.group(1))
        fields = [f for f in fields if f.lower() not in
                  ("attribute", "attributes")]
        if fields:
            return {"category": "missing_fields", "fields": fields,
                    "suggestion": _runtime_suggestion(
                        system, resource, operation, fields),
                    "confidence": "high",
                    "note": "Fusion demands these fields at runtime. "
                            + _APPROVAL_NOTE}
    match = _GENERIC_REQUIRED_RE.search(text)
    if match:
        return {"category": "missing_fields", "fields": [match.group(1)],
                "suggestion": _runtime_suggestion(
                    system, resource, operation, [match.group(1)]),
                "confidence": "low",
                "note": "Generic requiredness pattern; verify against live "
                        "metadata before curating. " + _APPROVAL_NOTE}
    if any(marker in lowered for marker in _POLICY_MARKERS):
        return {"category": "policy", "fields": [], "suggestion": None,
                "confidence": "medium",
                "note": "Business-policy refusal: no field change will help."}
    return None


def _omit_suggestion(system: str, resource: str, operation: str,
                     fields: List[str]) -> Dict[str, Any]:
    contract_op = "create" if operation in ("create", "upsert") else operation
    return {"table": "FSCM_CREATE_OMIT_FIELDS" if system == "FSCM"
            else "HCM_SERVER_OWNED_CREATE_FIELDS",
            "key": [system, resource, contract_op],
            "add": sorted(fields)}


def _runtime_suggestion(system: str, resource: str, operation: str,
                        fields: List[str]) -> Dict[str, Any]:
    contract_op = "create" if operation in ("create", "upsert") else operation
    return {"table": "FSCM_RUNTIME_REQUIRED_CREATE_FIELDS"
            if system == "FSCM" else "HCM_CREATE_CALLER_INPUT_OVERRIDES",
            "key": [system, resource, contract_op],
            "add": sorted(fields)}

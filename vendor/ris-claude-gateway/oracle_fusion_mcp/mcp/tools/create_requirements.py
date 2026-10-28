"""Read-only create-requirements discovery (never executes anything).

Answers "what must the caller supply to create X?" from the SAME contract
machinery the write validator enforces, so requirements can never drift
from enforcement. Intended flow: call ``fusion_create_requirements``
FIRST when a human wants to create something, ask the human for every
``required`` field value, never send ``never_send`` fields, then call
``fusion_preview_write`` with the complete values.

Only live ``/describe`` metadata (plus the curated semantic tables) is
consulted. No network I/O happens in this module.
"""

from typing import Any, Dict, List

from .dynamic_planner import (
    HCM_AUDIT_COLUMNS,
    HCM_CREATE_CALLER_INPUT_OVERRIDES,
    HCM_HIGH_RISK_FIELDS,
    HCM_SERVER_OWNED_CREATE_FIELDS,
)
from .policy import get_table_entry as _policy_entry
from .fscm_create import (
    FSCM_CREATE_OMIT_FIELDS,
    FSCM_RUNTIME_REQUIRED_CREATE_FIELDS,
    discover_create_contract,
)

_ASK_FIRST_NOTE = (
    "Ask the human for every `required` field value before previewing. "
    "Never send `never_send` fields. Then call fusion_preview_write "
    "with the complete values."
)


def _plain_name(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) \
        and not value.strip().startswith("/") and ".." not in value \
        and "://" not in value


def describe_create_requirements(*, system: str, resource: str,
                                 operation: str = "create",
                                 fscm_raw: Any = None,
                                 hcm_attrs: Any = None) -> Dict[str, Any]:
    """Return the caller-supplied field sets for creating a resource.

    ``fscm_raw`` is live FSCM ``/describe`` output; ``hcm_attrs`` is a
    live HCM attribute list. Either may be omitted, in which case the
    resource is reported unresolved instead of guessed.
    """
    if system not in ("FSCM", "HCM") or not _plain_name(resource):
        return {"resolved": False, "system": system, "resource": resource,
                "operation": operation, "reasons": [
                    "system must be HCM or FSCM and resource a plain name"]}
    if system == "FSCM" and operation not in ("create", "upsert"):
        return {"resolved": False, "system": system, "resource": resource,
                "operation": operation, "reasons": [
                    "requirements are modeled for create/upsert only"]}
    if system == "HCM" and operation != "create":
        return {"resolved": False, "system": system, "resource": resource,
                "operation": operation, "reasons": [
                    "requirements are modeled for create only"]}
    if system == "FSCM":
        return _fscm_requirements(resource, operation, fscm_raw)
    return _hcm_requirements(resource, hcm_attrs)


def _fscm_requirements(resource: str, operation: str,
                       fscm_raw: Any) -> Dict[str, Any]:
    contract = discover_create_contract(fscm_raw, resource) \
        if isinstance(fscm_raw, dict) else {"resolved": False}
    if not contract.get("resolved"):
        return {"resolved": False, "system": "FSCM", "resource": resource,
                "operation": operation, "reasons": contract.get(
                    "reasons", ["describe metadata unavailable"])}
    required: List[Dict[str, Any]] = []
    for f in contract.get("mandatory_fields", []):
        if f.get("caller_supplied"):
            required.append({"name": f["name"], "type": f.get("type"),
                             "source": "metadata-mandatory"})
    runtime_required = _policy_entry(
        "FSCM_RUNTIME_REQUIRED_CREATE_FIELDS", ("FSCM", resource, "create"),
        FSCM_RUNTIME_REQUIRED_CREATE_FIELDS.get(
            ("FSCM", resource, "create"), frozenset()))
    for name in sorted(runtime_required):
        if name not in {r["name"] for r in required}:
            required.append({"name": name, "type": None,
                             "source": "runtime-required"})
    server_generated = set(contract.get("server_generated_fields", []))
    read_only = set(contract.get("read_only_fields", []))
    optional = [{"name": f["name"], "type": f.get("type")}
                for f in contract.get("optional_fields", [])
                if f["name"] not in read_only
                and f["name"] not in server_generated]
    never_send = []
    for name in sorted(server_generated):
        never_send.append({"name": name, "reason": "server-generated"})
    for name in sorted(read_only):
        if name not in server_generated:
            never_send.append({"name": name,
                               "reason": "non-settable (allowChanges=never "
                                         "or curated omit)"})
    return {"resolved": True, "system": "FSCM", "resource": resource,
            "operation": operation, "required": required,
            "optional": optional, "never_send": never_send,
            "note": _ASK_FIRST_NOTE}


def _hcm_requirements(resource: str, hcm_attrs: Any) -> Dict[str, Any]:
    if not isinstance(hcm_attrs, list) or not hcm_attrs:
        return {"resolved": False, "system": "HCM", "resource": resource,
                "operation": "create",
                "reasons": ["HCM attribute metadata unavailable"]}
    by_name = {a.get("name"): a for a in hcm_attrs if a.get("name")}
    overrides = _policy_entry(
        "HCM_CREATE_CALLER_INPUT_OVERRIDES", ("HCM", resource, "create"),
        HCM_CREATE_CALLER_INPUT_OVERRIDES.get(
            ("HCM", resource, "create"), frozenset()))
    server_owned = _policy_entry(
        "HCM_SERVER_OWNED_CREATE_FIELDS", ("HCM", resource, "create"),
        HCM_SERVER_OWNED_CREATE_FIELDS.get(
            ("HCM", resource, "create"), frozenset()))
    required: List[Dict[str, Any]] = []
    skipped_locked: List[str] = []
    for name, spec in by_name.items():
        if not spec.get("mandatory"):
            continue
        if name in server_owned:
            continue
        if name in HCM_AUDIT_COLUMNS:
            continue
        if spec.get("updatable") is False and name not in overrides:
            # Never required, never accepted here: only the live POST
            # can arbitrate caller-settable locked fields.
            skipped_locked.append(name)
            continue
        required.append({"name": name, "type": spec.get("type"),
                         "source": "metadata-mandatory"})
    optional = [{"name": name, "type": spec.get("type")}
                for name, spec in by_name.items()
                if not spec.get("mandatory")
                and (spec.get("updatable") is not False
                     or name in overrides)
                and name not in server_owned
                and name not in HCM_AUDIT_COLUMNS]
    never_send = [{"name": name, "reason": "server-owned"}
                  for name in sorted(server_owned) if name in by_name]
    never_send.extend(
        {"name": name, "reason": "server-owned (audit column)"}
        for name in sorted(HCM_AUDIT_COLUMNS) if name in by_name)
    never_send.extend(
        {"name": name,
         "reason": "locked at create; POST arbitrates (supplying fails)"}
        for name in sorted(skipped_locked))
    return {"resolved": True, "system": "HCM", "resource": resource,
            "operation": "create", "required": required,
            "optional": optional, "never_send": never_send,
            "note": _ASK_FIRST_NOTE}

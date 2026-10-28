"""Controlled curated-action abstraction — dry-run only, no HTTP.

Pilot scope is a small explicit allowlist ( draft submit + five purchase
order item actions). This module performs no network I/O: it validates a
proposed action against (a) the curated allowlist and (b) live describe
metadata parsed by :mod:`action_metadata`. There is deliberately no way to
supply an endpoint, URL, or HTTP method — those come from the allowlist and
are cross-checked against the metadata (fail closed).

Execution (POST) is not implemented anywhere in this module.
"""

import re
import uuid
from typing import Any, Callable, Dict, List, Optional

from .action_metadata import business_actions

# The curated operations this abstraction will ever validate. Adding an
# entry is a deliberate future code change, never a caller decision.
# Deliberately excluded (later phases at most): cancel, freeze, unfreeze,
# communicate, duplicate, renumberPO, acknowledge, all child actions.
# Role clarification (P1/P2): this list is legacy compatibility and
# specialized-policy data for the curated preview lane ONLY. It is not
# the capability universe: absence here never means "Fusion does not
# support this action". The dynamic lane (fusion_preview_write) resolves
# actions exclusively from live describe metadata; is_allowlisted() must
# never become a dynamic execution gate.
ALLOWLIST: List[Dict[str, str]] = [
    {"resource": "draftPurchaseOrders", "scope": "item", "action": "submit", "method": "POST"},
    {"resource": "purchaseOrders", "scope": "item", "action": "submit", "method": "POST"},
    {"resource": "purchaseOrders", "scope": "item", "action": "close", "method": "POST"},
    {"resource": "purchaseOrders", "scope": "item", "action": "hold", "method": "POST"},
    {"resource": "purchaseOrders", "scope": "item", "action": "removeHold", "method": "POST"},
    {"resource": "purchaseOrders", "scope": "item", "action": "reopen", "method": "POST"},
]


def is_allowlisted(resource: str, scope: str, action: str, method: str = "POST") -> bool:
    """Exact-membership check against the curated allowlist."""
    return any(
        e["resource"] == resource
        and e["scope"] == scope
        and e["action"] == action
        and e["method"] == method
        for e in ALLOWLIST
    )


# Per-action post-write verification contract. ``observe`` names GET-visible
# fields re-read after execution; ``ready`` False means the executor must
# refuse even a fully validated request (hold semantics have no observable
# hold-state field in purchaseOrders GET responses).
VERIFICATION: Dict[str, Dict[str, Any]] = {
    "draftPurchaseOrders/item/submit": {
        "observe": ["Status", "StatusCode"],
        "ready": True,
        "note": "proven live: Incomplete -> Pending Approval",
    },
    "purchaseOrders/item/submit": {
        "observe": ["Status", "StatusCode"],
        "ready": True,
        "note": "same re-read pattern as draft submit; never executed",
    },
    "purchaseOrders/item/close": {
        "observe": ["Status", "StatusCode"],
        "ready": True,
        "note": "close flips lifecycle status; never executed",
    },
    "purchaseOrders/item/hold": {
        "observe": ["Status", "StatusCode", "LastUpdateDate"],
        "ready": False,
        "note": "NOT READY: no hold-state field in PO GET responses",
    },
    "purchaseOrders/item/removeHold": {
        "observe": ["Status", "StatusCode", "LastUpdateDate"],
        "ready": False,
        "note": "NOT READY: no hold-state field in PO GET responses",
    },
    "purchaseOrders/item/reopen": {
        "observe": ["Status", "StatusCode"],
        "ready": True,
        "note": "reopen flips lifecycle status; never executed",
    },
}


def verification_rule(resource: str, scope: str, action: str) -> Dict[str, Any]:
    """Return the verification contract, defaulting to NOT READY fail-closed."""
    return VERIFICATION.get(
        "%s/%s/%s" % (resource, scope, action),
        {"observe": [], "ready": False, "note": "NOT READY: no verification contract"},
    )

_RECORD_ID_RE = re.compile(r"^[0-9]{1,32}$")

# Lifecycle/state fields surfaced by GET-before-write when present.
STATE_FIELDS: Dict[str, tuple] = {
    "draftPurchaseOrders": (
        "POHeaderId",
        "OrderNumber",
        "Status",
        "StatusCode",
        "ProcurementBU",
        "Buyer",
        "DocumentStyle",
        "Revision",
        "FundsStatus",
        "CreationDate",
        "LastUpdateDate",
    ),
    "purchaseOrders": (
        "POHeaderId",
        "OrderNumber",
        "Status",
        "StatusCode",
        "FrozenFlag",
        "ProcurementBU",
        "Buyer",
        "DocumentStyle",
        "Revision",
        "FundsStatus",
        "Ordered",
        "Total",
        "CreationDate",
        "LastUpdateDate",
    ),
}

# Backwards-compatible name used by earlier tests and the draft preview path.
DRAFT_STATE_FIELDS = STATE_FIELDS["draftPurchaseOrders"]


class ControlledActionError(ValueError):
    """Raised when a proposed action fails allowlist or metadata validation."""


def _type_matches(declared_type: Any, value: Any) -> bool:
    """Strict ADF parameter type check (fail closed on unknown types)."""
    if declared_type == "boolean":
        return isinstance(value, bool)
    if declared_type == "string":
        return isinstance(value, str)
    if declared_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared_type in ("date", "datetime"):
        return isinstance(value, str)
    if declared_type == "array":
        return isinstance(value, list)
    if declared_type == "object":
        return isinstance(value, dict)
    return False


def _find_item_action(parsed: Dict[str, Any], action: str) -> Optional[Dict[str, Any]]:
    """Locate ``action`` in the parsed describe ``item`` node (exact name)."""
    item = parsed.get("item") or {}
    for entry in item.get("actions") or []:
        if isinstance(entry, dict) and entry.get("action") == action:
            return entry
    return None


def validate_controlled_preview(
    parsed_describe: Dict[str, Any],
    resource: str,
    scope: str,
    record_id: Any,
    action: str,
    parameters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate a curated-action dry-run request. No I/O, no execution.

    Checks, in order: curated-allowlist membership (resource/scope/action),
    record-ID shape, parameter container shape, metadata presence of the
    action at item scope with the allowlisted method, parameter allowlisting
    against the metadata declaration, strict type checks, mandatory presence.
    Returns a validated bundle consumed by :func:`build_dry_run_preview`.
    """
    allowed = next(
        (
            e
            for e in ALLOWLIST
            if e["resource"] == resource
            and e["scope"] == scope
            and e["action"] == action
        ),
        None,
    )
    if allowed is None:
        known = sorted("%s/%s/%s" % (e["resource"], e["scope"], e["action"]) for e in ALLOWLIST)
        raise ControlledActionError(
            "not allowlisted: %s/%s/%s is not permitted (curated: %s)"
            % (resource, scope, action, ", ".join(known))
        )
    if not isinstance(record_id, (str, int)) or not _RECORD_ID_RE.match(str(record_id)):
        raise ControlledActionError(
            "invalid record_id %r: numeric PO header id required" % (record_id,)
        )
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise ControlledActionError("parameters must be an object")

    entry = _find_item_action(parsed_describe, action)
    if entry is None:
        raise ControlledActionError(
            "action %r not present in live describe metadata: failing closed" % (action,)
        )
    if entry.get("method") != allowed["method"]:
        raise ControlledActionError(
            "metadata method %r != allowlisted %r: failing closed"
            % (entry.get("method"), allowed["method"])
        )
    if entry.get("kind") != "business":
        raise ControlledActionError("action %r is not a business action" % (action,))

    declared = {p.get("name"): p for p in (entry.get("parameters") or []) if p.get("name")}
    unknown = sorted(k for k in parameters if k not in declared)
    if unknown:
        raise ControlledActionError("unknown parameters rejected: %s" % ", ".join(unknown))
    for name, spec in declared.items():
        if spec.get("mandatory") and name not in parameters:
            raise ControlledActionError("missing mandatory parameter: %s" % name)
    for name, value in parameters.items():
        if not _type_matches(declared[name].get("type"), value):
            raise ControlledActionError(
                "parameter %r expects %s, got %s"
                % (name, declared[name].get("type"), type(value).__name__)
            )
    # Variant-selector parameters (e.g. closeAction) are never defaulted:
    # the same explicitness rule as the dynamic planner applies here.
    from .dynamic_planner import validate_variant_params as _validate_variants
    variant_status, variant_reasons = _validate_variants(
        resource, action, parameters)
    if variant_status == "blocked":
        raise ControlledActionError(
            "; ".join(variant_reasons) + " (HIGH risk: blocked)")
    if variant_status == "missing":
        raise ControlledActionError("; ".join(variant_reasons))

    rid = str(record_id)
    return {
        "resource": allowed["resource"],
        "scope": allowed["scope"],
        "action": allowed["action"],
        "method": allowed["method"],
        "record_id": rid,
        "endpoint": "%s/%s/action/%s" % (allowed["resource"], rid, allowed["action"]),
        "parameters": dict(parameters),
        "metadata_entry": entry,
    }


# Backwards-compatible entry point used by the draft-submit preview path.
validate_submit_preview = validate_controlled_preview


def build_dry_run_preview(validated: Dict[str, Any]) -> Dict[str, Any]:
    """Render the dry-run preview. Pure formatting — never executes."""
    rule = verification_rule(
        validated["resource"], validated["scope"], validated["action"])
    return {
        "mode": "dry_run",
        "preview_id": uuid.uuid4().hex,
        "resource": validated["resource"],
        "scope": validated["scope"],
        "action": validated["action"],
        "method": validated["method"],
        "record_id": validated["record_id"],
        "endpoint": validated["endpoint"],
        "parameters": validated["parameters"],
        "validation": {
            "metadata_verified": True,
            "parameters_valid": True,
            "allowlisted": True,
        },
        "would_execute": True,
        "confirmation_required": True,
        "execution_ready": rule["ready"],
        "readiness_note": rule["note"],
        "note": "preview only: nothing was sent. To execute after explicit user "
                "authorization, call fusion_execute_confirmed_write with the "
                "confirmation_id",
    }


async def fetch_record_before_write(fetch_fn: Callable[..., Any], resource: str,
                                    record_id: Any) -> Dict[str, Any]:
    """Generic GET-before-write helper. Uses only the injected read callable.

    Exactly one ``(endpoint, params)`` read call is made against
    ``{resource}/{record_id}``; no other HTTP verbs are reachable here.
    """
    if not isinstance(record_id, (str, int)) or not _RECORD_ID_RE.match(str(record_id)):
        raise ControlledActionError(
            "invalid record_id %r: numeric header id required" % (record_id,)
        )
    rid = str(record_id)
    record = await fetch_fn("%s/%s" % (resource, rid), {"onlyData": "true"})
    if not isinstance(record, dict):
        raise ControlledActionError("unexpected record shape for %s" % rid)
    fields = STATE_FIELDS.get(resource, DRAFT_STATE_FIELDS)
    state = {k: record[k] for k in fields if k in record}
    return {
        "record_id": rid,
        "endpoint": "%s/%s" % (resource, rid),
        "state": state,
        "fetched": True,
    }


async def fetch_draft_before_write(fetch_fn: Callable[..., Any], record_id: Any) -> Dict[str, Any]:
    """Draft-specific GET-before-write (backwards-compatible wrapper)."""
    return await fetch_record_before_write(fetch_fn, "draftPurchaseOrders", record_id)

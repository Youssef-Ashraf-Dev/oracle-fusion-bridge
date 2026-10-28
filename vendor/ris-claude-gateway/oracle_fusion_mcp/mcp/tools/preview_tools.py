"""Dry-run preview for the pilot controlled action (GET-only, never executes).

``preview_draft_po_submit`` walks the future write path up to — but never
including — the POST: allowlist check, live metadata validation, and a
GET-before-write record fetch. It is registered as a READ tool on purpose:
every network call it makes is a GET through the existing safe wrapper.
"""

from typing import Any, Dict

from oracle_fusion_mcp._types import Request
from ...auth.entra import ValidatedUser
from ...oracle.client import OracleClient
from ._helpers import _text_result
from .action_metadata import parse_describe
from .audit_log import AuditSink, LoggingAuditSink, build_event
from .confirmations import issue_confirmation
from .dynamic_workflow import FSCM_STATE_FIELDS
from .dynamic_workflow import WorkflowRegistry
from .dynamic_workflow import register_legacy_preview
from .endpoint_discovery import canonical_fingerprint
from .endpoint_discovery import _relative_template as _metadata_template
from .controlled_actions import (
    ControlledActionError,
    build_dry_run_preview,
    fetch_record_before_write,
    is_allowlisted,
    validate_controlled_preview,
)

# Swappable audit backend for previews (default: stdlib logging, no files).
AUDIT_SINK: AuditSink = LoggingAuditSink()


def _workflow_registry() -> WorkflowRegistry:
    """Shared public-confirmation registry (single process-wide instance)."""
    from . import dynamic_workflow as _dw
    return _dw.REGISTRY


def set_audit_sink(sink: AuditSink) -> None:
    """Replace the preview audit backend (tests use an in-memory sink)."""
    global AUDIT_SINK
    AUDIT_SINK = sink

TOOLS = [
    {
        "name": "preview_draft_po_submit",
        "description": "Dry-run preview of draft PO submit (read-only, never executes). Validates draftPurchaseOrders/item/submit against live describe metadata plus an allowlist, fetches the draft record via GET, and returns what WOULD be sent plus an opaque confirmation_id for the unified one-time workflow. No POST is performed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string", "description": "Draft PO header id, numeric string e.g. 300000011218642"},
                "parameters": {"type": "object", "description": "Optional submit flags: validateBeforeSubmitFlag, bypassApprovalsFlag, defaultTaxAttributesFlag, requestFundsOverrideFlag (booleans). Default {}.", "default": {}},
            },
            "required": ["record_id"],
        },
    },
    {
        "name": "fusion_preview_controlled_action",
        "description": "Dry-run preview of a Fusion item action (read-only, never executes). Routed through the dynamic lane: validates against live describe metadata (no curated allowlist), fetches the record via GET, returns preview + opaque confirmation_id for the unified one-time workflow. Invented actions fail closed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Curated resource: draftPurchaseOrders or purchaseOrders"},
                "record_id": {"type": "string", "description": "Numeric header id string"},
                "action": {"type": "string", "description": "Curated action, e.g. submit, close, hold, removeHold, reopen"},
                "parameters": {"type": "object", "description": "Action parameters as declared by Fusion metadata. Default {}.", "default": {}},
            },
            "required": ["resource", "record_id", "action"],
        },
    },
]


async def _run_preview(oracle_client: OracleClient, resource: str,
                     record_id: Any, action: str,
                     parameters: Any) -> Dict[str, Any]:
    """Shared GET-only preview core: describe GET, validate, record GET."""
    if not is_allowlisted(resource, "item", action):
        return _text_result({
            "mode": "dry_run",
            "action": action,
            "would_execute": False,
            "error": "not allowlisted: %s/item/%s is not a curated action"
                     % (resource, action),
        })
    try:
        # Single read-only GET: live metadata (child data rides along).
        data = await oracle_client._fetch("%s/describe" % resource, {})
        parsed = parse_describe(resource, data)
        validated = validate_controlled_preview(
            parsed,
            resource=resource,
            scope="item",
            record_id=record_id,
            action=action,
            parameters=parameters,
        )
    except ControlledActionError as exc:
        return _text_result({
            "mode": "dry_run",
            "action": "submit",
            "would_execute": False,
            "error": str(exc),
        })
    try:
        # Single read-only GET: current record state before any future write.
        before = await fetch_record_before_write(
            oracle_client._fetch, validated["resource"], validated["record_id"])
    except ControlledActionError as exc:
        preview = build_dry_run_preview(validated)
        preview["would_execute"] = False
        preview["record_state"] = None
        preview["error"] = "GET-before-write failed: %s" % exc
        return _text_result(preview)
    except Exception as exc:
        preview = build_dry_run_preview(validated)
        preview["would_execute"] = False
        preview["record_state"] = None
        preview["error"] = "GET-before-write failed: %s" % str(exc)[:300]
        return _text_result(preview)
    preview = build_dry_run_preview(validated)
    preview["record_state"] = before["state"]
    # Template-faithful endpoint: prefer the metadata-link template fused
    # into the validated action entry; fall back to the canonical pattern
    # only when the entry carries no usable link. Provenance is bound so
    # the unified executor can verify it against fresh metadata.
    entry = validated.get("metadata_entry") if isinstance(validated, dict) else None
    template = _metadata_template(entry.get("href")) if isinstance(entry, dict) else None
    if template:
        preview["endpoint"] = template.replace("{id}", validated["record_id"]).replace(
            "{key}", validated["record_id"])
        preview["template_source"] = "metadata-link"
    else:
        preview["template_source"] = "canonical-pattern"
    # Bind the exact metadata this preview was resolved from, using the
    # same fingerprint rule as the dynamic planner (parsed item scope
    # node): the unified executor re-resolves fresh and fails closed on
    # any metadata drift.
    preview["metadata_fingerprint"] = canonical_fingerprint(
        {"resource": validated["resource"], "scope": "item",
         "node": parsed["item"]})
    if validated["resource"] not in ("draftPurchaseOrders", "purchaseOrders"):
        preview["would_execute"] = False
        preview["error"] = "no state binding defined for this resource"
        return _text_result(preview)
    preview["bound_state"] = {
        k: before["state"][k] for k in FSCM_STATE_FIELDS if k in before["state"]
    }
    if not preview["bound_state"]:
        preview["would_execute"] = False
        preview["error"] = "record state has no bindable safety fields"
        return _text_result(preview)
    try:
        from .dynamic_workflow import _to_dynamic_preview
        dynamic_preview = _to_dynamic_preview(preview)
        issued = issue_confirmation(dynamic_preview)
        token = issued["confirmation_token"]
    except Exception as exc:
        preview["would_execute"] = False
        preview["error"] = "confirmation issuance failed: %s" % exc
        return _text_result(preview)
    try:
        preview["confirmation_id"] = register_legacy_preview(
            preview, token, registry=_workflow_registry())
    except Exception as exc:
        preview["would_execute"] = False
        preview["error"] = "confirmation registration failed: %s" % exc
        return _text_result(preview)
    try:
        AUDIT_SINK.emit(build_event(
            "preview", resource=preview["resource"], scope=preview["scope"],
            action=preview["action"], record_id=preview["record_id"],
            parameters=preview["parameters"], mode="readonly",
            result="dry_run", confirmation="issued",
            confirmation_token=token))
    except Exception as exc:
        preview["would_execute"] = False
        preview["error"] = "audit unavailable: %s" % exc
        return _text_result(preview)
    return _text_result(preview)


async def handle_preview_draft_po_submit(
    request: Request,
    session: ValidatedUser,
    params: Dict[str, Any],
    oracle_client: OracleClient,
) -> Dict[str, Any]:
    # Backwards-compatible draft-submit entry point (behavior preserved).
    return await _run_preview(
        oracle_client,
        "draftPurchaseOrders",
        params.get("record_id", ""),
        "submit",
        params.get("parameters") or {},
    )


async def handle_preview_controlled_action(
    request: Request,
    session: ValidatedUser,
    params: Dict[str, Any],
    oracle_client: OracleClient,
) -> Dict[str, Any]:
    """Compatibility entry point routed through the dynamic lane (P1).

    The legacy allowlist check is intentionally NOT applied here: any
    action with legitimate live Fusion describe metadata resolves via
    the dynamic planner (risk classification, verification strategy,
    and metadata fingerprint included), while invented actions fail
    closed with no transport. Traversal guards are preserved.
    """
    _validate_preview_params(params)
    resource = params["resource"].strip()
    record_id = params.get("record_id", "")
    action = params["action"].strip()
    fields = params.get("parameters") or {}
    if not isinstance(fields, dict):
        raise ValueError("parameters must be an object")
    from .dynamic_workflow import preview_operation

    async def _describe(res: str) -> Dict[str, Any]:
        return await oracle_client._fetch("%s/describe" % res, {})

    async def _record(res: str, rid: Any) -> Any:
        # Fetch errors propagate to the NOT_ELIGIBLE handler below,
        # mirroring the legacy "GET-before-write failed" refusal.
        return await oracle_client._fetch(
            "%s/%s" % (res, rid), {"onlyData": "true"})

    try:
        return await preview_operation(
            system="FSCM", resource=resource, scope="item",
            operation="business_action", record_id=record_id or None,
            action=action, fields=dict(fields),
            fscm_describe_fn=_describe, record_fetch_fn=_record)
    except Exception as exc:
        return {"status": "NOT_ELIGIBLE", "resource": resource,
                "reasons": ["curated preview failed: %s" % exc],
                "would_execute": False}


def _validate_preview_params(params: Dict[str, Any]) -> None:
    """Reject absolute URLs, traversal, and non-string resource/action."""
    for field in ("resource", "action"):
        value = params.get(field, "")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("%s is required" % field)
        text = value.strip()
        if text.startswith("/"):
            raise ValueError("%s must not start with '/'" % field)
        if ".." in text:
            raise ValueError("%s must not contain '..'" % field)
        lowered = text.lower()
        if lowered.startswith("http://") or lowered.startswith("https://") \
                or "://" in text:
            raise ValueError("%s must be a plain name" % field)


HANDLERS = {
    "preview_draft_po_submit": handle_preview_draft_po_submit,
    "fusion_preview_controlled_action": handle_preview_controlled_action,
}

"""HCM controlled preview registration (READ-only, never executes).

Exposes ``fusion_preview_hcm_absence_update``: validates the proposed
absence comments change against live describe metadata, pre-reads the
record via GET (capturing objectVersionNumber), and returns a dry-run
preview bound to a single-use confirmation token. Every network call is a
GET; execution stays internal and unregistered.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from oracle_fusion_mcp.mcp.tools.audit_log import (
    AuditSink,
    LoggingAuditSink,
    build_event,
)
from oracle_fusion_mcp.mcp.tools.confirmations import issue_confirmation
from oracle_fusion_mcp.mcp.tools.dynamic_workflow import (
    WorkflowRegistry,
    register_legacy_preview,
)
from oracle_fusion_mcp.mcp.tools.endpoint_discovery import (
    canonical_fingerprint,
)
from oracle_fusion_mcp.mcp.tools.hcm_controlled import (
    RESOURCE,
    VERSION_FIELD,
    ControlledActionError,
    build_hcm_absence_preview,
    validate_hcm_absence_preview,
)

from ..context import ServerContext

AUDIT_SINK: AuditSink = LoggingAuditSink()


def set_hcm_audit_sink(sink: AuditSink) -> None:
    """Replace the preview audit backend (tests use an in-memory sink)."""
    global AUDIT_SINK
    AUDIT_SINK = sink


def _version_of(record: Any) -> Any:
    if isinstance(record, dict):
        if record.get(VERSION_FIELD) is not None:
            return record[VERSION_FIELD]
        return record.get("ObjectVersionNumber")
    return None


def _workflow_registry() -> WorkflowRegistry:
    """Shared public-confirmation registry (single process-wide instance)."""
    from oracle_fusion_mcp.mcp.tools import dynamic_workflow as _dw
    return _dw.REGISTRY


async def preview_absence_update(ctx: ServerContext, record_id: str,
                                 comments: Any) -> dict[str, Any]:
    """Shared GET-only preview core, callable without FastMCP (tests)."""
    if not isinstance(record_id, str) or not record_id.strip():
        return {"mode": "dry_run", "would_execute": False,
                "error": "record_id is required"}
    summary = await ctx.catalog.describe(RESOURCE)
    record = await ctx.client.get_record(RESOURCE, record_id.strip(), redact=False)
    if not isinstance(record, dict):
        return {"mode": "dry_run", "would_execute": False,
                "error": "record not found"}
    version = _version_of(record)
    try:
        validated = validate_hcm_absence_preview(
            summary.get("attributes"), RESOURCE, record_id.strip(),
            {"comments": comments}, version)
    except ControlledActionError as exc:
        return {"mode": "dry_run", "would_execute": False, "error": str(exc)}
    preview = build_hcm_absence_preview(validated, record)
    # Bind the exact attribute metadata this preview was resolved from,
    # using the same fingerprint rule as the dynamic planner: the unified
    # executor re-resolves fresh and fails closed on any metadata drift.
    preview["metadata_fingerprint"] = canonical_fingerprint(
        {"resource": RESOURCE, "attrs": summary.get("attributes")})
    preview["record_state"] = {
        key: record.get(key)
        for key in ("personAbsenceEntryId", "personNumber", "absenceType",
                    "absenceStatusCd", "approvalStatusCd", "startDate",
                    "endDate", "comments", "objectVersionNumber")
        if isinstance(record, dict) and key in record
    }
    try:
        from oracle_fusion_mcp.mcp.tools.dynamic_workflow import _to_dynamic_preview
        dynamic_preview = _to_dynamic_preview(preview)
        issued = issue_confirmation(dynamic_preview)
        token = issued["confirmation_token"]
    except Exception as exc:
        preview["would_execute"] = False
        preview["error"] = "confirmation issuance failed: %s" % exc
        return preview
    try:
        preview["confirmation_id"] = register_legacy_preview(
            preview, token, registry=_workflow_registry())
    except Exception as exc:
        preview["would_execute"] = False
        preview["error"] = "confirmation registration failed: %s" % exc
        return preview
    try:
        AUDIT_SINK.emit(build_event(
            "preview", resource=preview["resource"], scope=preview["scope"],
            action=preview["operation"], record_id=preview["record_id"],
            parameters=preview["parameters"], mode="readonly",
            result="dry_run", confirmation="issued",
            confirmation_token=token,
            version=preview.get("version")))
    except Exception as exc:
        preview["would_execute"] = False
        preview["error"] = "audit unavailable: %s" % exc
        return preview
    return preview


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool()
    async def fusion_preview_hcm_absence_update(
        record_id: str,
        comments: Any = None,
    ) -> dict[str, Any]:
        """Dry-run preview of an HCM absence comments update (never executes).

        Validates against live absence metadata, pre-reads the record via
        GET (binding objectVersionNumber), and returns what WOULD be sent
        with a single-use confirmation handle. No PATCH is performed.
        """
        return await preview_absence_update(ctx, record_id, comments)

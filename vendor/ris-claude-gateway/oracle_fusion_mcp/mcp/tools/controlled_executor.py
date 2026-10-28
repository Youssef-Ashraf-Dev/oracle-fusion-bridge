"""Controlled executor boundary for curated actions (NOT registered as a tool).

Pipeline: validate request -> consume confirmation (single-use, atomic) ->
explicit write-mode check -> verification-readiness check ->
pre-execution audit (fail closed) -> mutation via the existing client
transport -> GET-after-write verification -> result audit.

Only curated-allowlist operations can pass validation, the endpoint and body
are derived internally (never caller-supplied), and the client's own
mutation guard remains the final gate. Unit tests drive this module with
mocked clients only.
"""

from typing import Any, Dict

from .audit_log import AuditSink, build_event
from .confirmations import ConfirmationError, ConfirmationStore
from .controlled_actions import (
    ControlledActionError,
    fetch_record_before_write,
    is_allowlisted,
    verification_rule,
)


def _preview_binding(preview: Dict[str, Any]) -> Dict[str, Any]:
    """Extract and sanity-check the fields a preview must carry."""
    if not isinstance(preview, dict) or preview.get("mode") != "dry_run":
        raise ControlledActionError("executor requires a dry-run preview")
    for field in ("resource", "scope", "action", "record_id", "preview_id"):
        if not preview.get(field):
            raise ControlledActionError("preview missing %r" % field)
    parameters = preview.get("parameters")
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise ControlledActionError("preview parameters must be an object")
    allowed = is_allowlisted(
        preview["resource"], preview["scope"], preview["action"],
        preview.get("method", "POST"),
    )
    if not allowed:
        raise ControlledActionError("preview is not the allowlisted operation")
    expected_endpoint = "%s/%s/action/%s" % (
        preview["resource"], preview["record_id"], preview["action"])
    if preview.get("endpoint") != expected_endpoint:
        raise ControlledActionError("preview endpoint mismatch: failing closed")
    return {
        "resource": preview["resource"],
        "scope": preview["scope"],
        "action": preview["action"],
        "record_id": str(preview["record_id"]),
        "parameters": dict(parameters),
        "endpoint": expected_endpoint,
    }


async def execute_controlled(*, preview: Dict[str, Any], confirmation_token: Any,
                             store: ConfirmationStore, client: Any,
                             audit: AuditSink) -> Dict[str, Any]:
    """Run the pipeline for one curated action. Raises without mutating on failure."""
    binding = _preview_binding(preview)
    resource = binding["resource"]
    scope = binding["scope"]
    action = binding["action"]
    try:
        store.consume(confirmation_token, preview)
        consumed = True
    except ConfirmationError as exc:
        audit.emit(build_event(
            "execute", resource=str(preview.get("resource", "")),
            scope=str(preview.get("scope", "")),
            action=str(preview.get("action", "")),
            record_id=str(preview.get("record_id", "")),
            parameters=preview.get("parameters"), mode=_client_mode(client),
            result="blocked", confirmation="rejected", error=exc))
        raise ControlledActionError("confirmation rejected: %s" % exc)

    if _client_mode(client) != "full":
        audit.emit(build_event(
            "execute", resource=binding["resource"], scope=binding["scope"],
            action=binding["action"], record_id=binding["record_id"],
            parameters=binding["parameters"], mode=_client_mode(client),
            result="blocked", confirmation="consumed",
            confirmation_token=confirmation_token,
            error="write mode not enabled"))
        raise ControlledActionError(
            "write mode not enabled: refusing execution in mode %r"
            % _client_mode(client))

    rule = verification_rule(
        binding["resource"], binding["scope"], binding["action"])
    if not rule.get("ready"):
        audit.emit(build_event(
            "execute", resource=binding["resource"], scope=binding["scope"],
            action=binding["action"], record_id=binding["record_id"],
            parameters=binding["parameters"], mode=_client_mode(client),
            result="blocked", confirmation="consumed",
            confirmation_token=confirmation_token,
            error="not ready for live execution: %s" % rule.get("note")))
        raise ControlledActionError(
            "action not ready for live execution: %s" % rule.get("note"))

    try:
        audit.emit(build_event(
            "execute", resource=binding["resource"], scope=binding["scope"],
            action=binding["action"], record_id=binding["record_id"],
            parameters=binding["parameters"], mode=_client_mode(client),
            result="attempt", confirmation="consumed",
            confirmation_token=confirmation_token))
    except Exception as exc:
        # Audit backend failure fails closed: no transport is attempted.
        raise ControlledActionError("audit unavailable, refusing write: %s" % exc)

    try:
        post_result = await client._post_action(
            binding["endpoint"], dict(binding["parameters"]))
    except Exception as exc:
        audit.emit(build_event(
            "execute", resource=binding["resource"], scope=binding["scope"],
            action=binding["action"], record_id=binding["record_id"],
            parameters=binding["parameters"], mode=_client_mode(client),
            result="failed", confirmation="consumed",
            confirmation_token=confirmation_token, error=exc))
        return {"executed": False, "post_status": "failed",
                "resource": binding["resource"], "record_id": binding["record_id"],
                "verification": {"status": "failed", "state": None},
                "error": str(exc)[:300]}

    try:
        before = await fetch_record_before_write(
            client._fetch, binding["resource"], binding["record_id"])
        rule = verification_rule(
            binding["resource"], binding["scope"], binding["action"])
        observed = {k: before["state"].get(k) for k in rule.get("observe", [])}
        verification: Dict[str, Any] = {
            "status": "verified",
            "state": before["state"],
            "observed": observed,
        }
    except Exception as exc:
        verification = {"status": "unverified",
                        "state": None,
                        "note": "POST accepted but re-read failed: %s" % str(exc)[:200]}
    audit.emit(build_event(
        "execute", resource=binding["resource"], scope=binding["scope"],
        action=binding["action"], record_id=binding["record_id"],
        parameters=binding["parameters"], mode=_client_mode(client),
        result="committed", confirmation="consumed",
        confirmation_token=confirmation_token))
    return {"executed": True, "post_status": "accepted",
            "resource": binding["resource"], "record_id": binding["record_id"],
            "post_result": post_result, "verification": verification,
            "confirmation_consumed": consumed}


def _client_mode(client: Any) -> str:
    return str(getattr(getattr(client, "settings", None), "MCP_MODE", ""))


# Backwards-compatible entry point used by existing tests.
execute_submit = execute_controlled

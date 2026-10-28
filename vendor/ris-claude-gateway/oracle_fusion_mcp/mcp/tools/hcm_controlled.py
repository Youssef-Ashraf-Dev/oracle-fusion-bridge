"""HCM controlled mutation model — validation/preview/executor, no HTTP here.

Pilot scope is exactly one operation::

    HCM absences / item / update (PATCH), field allowlist: comments (+ the
    mandatory objectVersionNumber concurrency carrier, never user-editable
    beyond the observed value).

All network access is injected (metadata attrs, record fetch, PATCH sender),
so this module is fully testable offline. Live wiring passes the HCM
catalog/client callables; tests pass fakes. Nothing here contacts Fusion.
"""

from typing import Any, Callable, Dict, List, Optional

from .audit_log import AuditSink, build_event
from .confirmations import ConfirmationError, ConfirmationStore
from .controlled_actions import ControlledActionError

SYSTEM = "HCM"
RESOURCE = "absences"
SCOPE = "item"
OPERATION = "update"
METHOD = "PATCH"

HCM_ALLOWLIST: List[Dict[str, str]] = [
    {"system": SYSTEM, "resource": RESOURCE, "scope": SCOPE,
     "operation": OPERATION, "method": METHOD},
]

# The only caller-writable absence field approved for the pilot. Everything
# else the endpoint accepts stays rejected until explicitly curated.
FIELD_ALLOWLIST: Dict[str, Dict[str, Any]] = {
    "comments": {"type": "string", "maxLength": 2000},
}

VERSION_FIELD = "objectVersionNumber"

# GET-visible state surfaced by pre-read/verification when present.
ABSENCE_STATE_FIELDS = (
    "personAbsenceEntryId",
    "personNumber",
    "absenceType",
    "absenceStatusCd",
    "approvalStatusCd",
    "absenceDispStatus",
    "startDate",
    "endDate",
    "comments",
    "objectVersionNumber",
)

VERIFICATION_OBSERVE = ["comments", "objectVersionNumber"]


def _type_matches(declared_type: Any, value: Any) -> bool:
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


def _attr_map(metadata_attrs: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(metadata_attrs, list):
        raise ControlledActionError("metadata attributes unavailable: failing closed")
    out: Dict[str, Dict[str, Any]] = {}
    for attr in metadata_attrs:
        if isinstance(attr, dict) and attr.get("name"):
            out[attr["name"]] = attr
    if not out:
        raise ControlledActionError("metadata attributes unavailable: failing closed")
    return out


def validate_hcm_absence_preview(
    metadata_attrs: Any,
    resource: str,
    record_id: Any,
    changes: Optional[Dict[str, Any]] = None,
    version: Any = None,
) -> Dict[str, Any]:
    """Validate an HCM absence-update dry-run request. No I/O, no execution.

    Checks: curated resource/operation, numeric record id, non-empty changes
    object restricted to the field allowlist AND live metadata (updatable,
    type, maxLength), mandatory version present and integer, version field
    itself not caller-writable.
    """
    if resource != RESOURCE:
        raise ControlledActionError(
            "not allowlisted: only HCM resource %r is permitted" % RESOURCE)
    import re as _re
    if not isinstance(record_id, (str, int)) or not _re.match(r"^[0-9]{1,32}$", str(record_id)):
        raise ControlledActionError(
            "invalid record_id %r: numeric absence entry id required" % (record_id,))
    if not isinstance(changes, dict) or not changes:
        raise ControlledActionError("changes must be a non-empty object")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ControlledActionError(
            "objectVersionNumber is required and must be an integer")
    attrs = _attr_map(metadata_attrs)
    if VERSION_FIELD not in attrs:
        raise ControlledActionError(
            "metadata lacks %r: cannot bind concurrency, failing closed" % VERSION_FIELD)
    for name, value in changes.items():
        if name not in FIELD_ALLOWLIST:
            raise ControlledActionError("field not approved for pilot: %r" % name)
        spec = attrs.get(name)
        if spec is None:
            raise ControlledActionError("unknown field %r: failing closed" % name)
        if spec.get("updatable") is False:
            raise ControlledActionError("read-only field rejected: %r" % name)
        declared_type = FIELD_ALLOWLIST[name].get("type", spec.get("type"))
        if value is None:
            if spec.get("mandatory"):
                raise ControlledActionError("mandatory field cannot be nulled: %r" % name)
            continue
        if not _type_matches(declared_type, value):
            raise ControlledActionError(
                "field %r expects %s, got %s"
                % (name, declared_type, type(value).__name__))
        max_length = FIELD_ALLOWLIST[name].get("maxLength", spec.get("maxLength"))
        if isinstance(value, str) and max_length is not None:
            try:
                limit = int(max_length)
            except (TypeError, ValueError):
                limit = None
            if limit is not None and len(value) > limit:
                raise ControlledActionError(
                    "field %r exceeds maxLength %s" % (name, max_length))
    rid = str(record_id)
    return {
        "system": SYSTEM,
        "resource": RESOURCE,
        "scope": SCOPE,
        "operation": OPERATION,
        "method": METHOD,
        "record_id": rid,
        "endpoint": "%s/%s" % (RESOURCE, rid),
        "changes": dict(changes),
        "version": version,
        "metadata_fields": sorted(attrs.keys()),
    }


def build_hcm_absence_preview(validated: Dict[str, Any],
                              current: Dict[str, Any]) -> Dict[str, Any]:
    """Render the HCM absence dry-run preview. Pure formatting."""
    import uuid as _uuid
    return {
        "mode": "dry_run",
        "preview_id": _uuid.uuid4().hex,
        "system": validated["system"],
        "resource": validated["resource"],
        "scope": validated["scope"],
        "operation": validated["operation"],
        "action": validated["operation"],
        "method": validated["method"],
        "record_id": validated["record_id"],
        "endpoint": validated["endpoint"],
        "parameters": dict(validated["changes"]),
        "current_values": {k: current.get(k) for k in ("comments", VERSION_FIELD)
                           if isinstance(current, dict)},
        "version": validated["version"],
        "validation": {
            "metadata_verified": True,
            "parameters_valid": True,
            "allowlisted": True,
        },
        "would_execute": True,
        "confirmation_required": True,
        "execution_ready": True,
        "readiness_note": "comments-only absence update with version binding",
        "verification": {
            "strategy": "get_after_write",
            "observe": list(VERIFICATION_OBSERVE),
        },
        "note": "preview only: nothing was sent. To execute after explicit user "
                "authorization, call fusion_execute_confirmed_write with the "
                "confirmation_id",
    }


def _preview_binding(preview: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(preview, dict) or preview.get("mode") != "dry_run":
        raise ControlledActionError("executor requires a dry-run preview")
    for field in ("system", "resource", "scope", "operation", "record_id",
                  "preview_id", "version"):
        if preview.get(field) in (None, ""):
            raise ControlledActionError("preview missing %r" % field)
    if (preview["system"] != SYSTEM or preview["resource"] != RESOURCE
            or preview["scope"] != SCOPE or preview["operation"] != OPERATION
            or preview.get("method", METHOD) != METHOD):
        raise ControlledActionError("preview is not the allowlisted HCM operation")
    parameters = preview.get("parameters")
    if not isinstance(parameters, dict):
        raise ControlledActionError("preview parameters must be an object")
    return {
        "system": preview["system"],
        "resource": preview["resource"],
        "record_id": str(preview["record_id"]),
        "parameters": dict(parameters),
        "version": preview["version"],
        "endpoint": "%s/%s" % (preview["resource"], preview["record_id"]),
    }


async def execute_hcm_controlled(*, preview: Dict[str, Any],
                                 confirmation_token: Any,
                                 store: ConfirmationStore, mode: str,
                                 fetch_fn: Callable[..., Any],
                                 update_fn: Callable[..., Any],
                                 audit: AuditSink) -> Dict[str, Any]:
    """Four-stage HCM pipeline: validate, consume, pre-audit, PATCH, verify.

    ``fetch_fn(record_id)`` and ``update_fn(record_id, payload)`` are
    injected; live wiring passes the HCM client with retries disabled for
    the mutation. Raises without mutating on any failure.
    """
    binding = _preview_binding(preview)
    try:
        store.consume(confirmation_token, preview)
    except ConfirmationError as exc:
        audit.emit(build_event(
            "execute", resource=binding["resource"], scope=SCOPE,
            action=OPERATION, record_id=binding["record_id"],
            parameters=binding["parameters"], mode=str(mode),
            result="blocked", confirmation="rejected",
            version=binding["version"], error=exc))
        raise ControlledActionError("confirmation rejected: %s" % exc)
    if mode != "full":
        audit.emit(build_event(
            "execute", resource=binding["resource"], scope=SCOPE,
            action=OPERATION, record_id=binding["record_id"],
            parameters=binding["parameters"], mode=str(mode),
            result="blocked", confirmation="consumed",
            confirmation_token=confirmation_token,
            version=binding["version"], error="write mode not enabled"))
        raise ControlledActionError("write mode not enabled: refusing execution")
    try:
        audit.emit(build_event(
            "execute", resource=binding["resource"], scope=SCOPE,
            action=OPERATION, record_id=binding["record_id"],
            parameters=binding["parameters"], mode=str(mode),
            result="attempt", confirmation="consumed",
            confirmation_token=confirmation_token,
            version=binding["version"]))
    except Exception as exc:
        raise ControlledActionError("audit unavailable, refusing write: %s" % exc)
    try:
        current = await fetch_fn(binding["record_id"])
    except Exception as exc:
        audit.emit(build_event(
            "execute", resource=binding["resource"], scope=SCOPE,
            action=OPERATION, record_id=binding["record_id"],
            parameters=binding["parameters"], mode=str(mode),
            result="blocked", confirmation="consumed",
            confirmation_token=confirmation_token,
            version=binding["version"], error="pre-read failed: %s" % exc))
        raise ControlledActionError("pre-execution re-read failed: %s" % exc)
    live_version = current.get(VERSION_FIELD, current.get("ObjectVersionNumber"))
    if live_version != binding["version"]:
        audit.emit(build_event(
            "execute", resource=binding["resource"], scope=SCOPE,
            action=OPERATION, record_id=binding["record_id"],
            parameters=binding["parameters"], mode=str(mode),
            result="blocked", confirmation="consumed",
            confirmation_token=confirmation_token,
            version=binding["version"],
            error="stale version: bound %r, live %r" % (binding["version"], live_version)))
        raise ControlledActionError(
            "stale objectVersionNumber: preview bound %r but live is %r"
            % (binding["version"], live_version))
    payload = dict(binding["parameters"])
    payload[VERSION_FIELD] = binding["version"]
    try:
        post_result = await update_fn(binding["record_id"], payload)
    except Exception as exc:
        audit.emit(build_event(
            "execute", resource=binding["resource"], scope=SCOPE,
            action=OPERATION, record_id=binding["record_id"],
            parameters=binding["parameters"], mode=str(mode),
            result="failed", confirmation="consumed",
            confirmation_token=confirmation_token,
            version=binding["version"], error=exc))
        return {"executed": False, "patch_status": "failed",
                "resource": binding["resource"], "record_id": binding["record_id"],
                "verification": {"status": "failed", "state": None},
                "error": str(exc)[:300]}
    try:
        after = await fetch_fn(binding["record_id"])
        mismatched = [k for k, v in binding["parameters"].items()
                      if after.get(k) != v]
        new_version = after.get(VERSION_FIELD, after.get("ObjectVersionNumber"))
        if mismatched:
            verification: Dict[str, Any] = {
                "status": "unverified", "state": None,
                "note": "after-values differ for: %s" % ",".join(mismatched)}
        else:
            verification = {"status": "verified",
                            "state": {k: after.get(k)
                                      for k in VERIFICATION_OBSERVE if k in after},
                            "new_version": new_version}
    except Exception as exc:
        verification = {"status": "unverified", "state": None,
                        "note": "PATCH accepted but re-read failed: %s" % str(exc)[:200]}
    audit.emit(build_event(
        "execute", resource=binding["resource"], scope=SCOPE,
        action=OPERATION, record_id=binding["record_id"],
        parameters=binding["parameters"], mode=str(mode),
        result="committed", confirmation="consumed",
        confirmation_token=confirmation_token,
        version=binding["version"]))
    return {"executed": True, "patch_status": "accepted",
            "resource": binding["resource"], "record_id": binding["record_id"],
            "patch_result": post_result, "verification": verification}

"""Claude-facing dynamic write workflow core (preview + one-time execution).

Two public operations (wired to MCP tools in server.py; this module itself
registers nothing):

- ``preview_operation(...)`` — read-only. Resolves via planner, builds an
  executor-compatible preview, mints a single-use token, and returns a
  PUBLIC response carrying an opaque ``confirmation_id``. The real token
  never leaves this module.
- ``execute_confirmation(confirmation_id, ...)`` — accepts ONLY the public
  id. Everything else (resource, method, path, params, version) comes from
  the stored preview and is re-validated by ``dynamic_executor``.

Extra keyword arguments can never influence either operation: both functions
take fixed signatures (unknown kwargs raise TypeError), and the MCP layer
strips unknown fields before they arrive.
"""

import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .action_metadata import parse_describe
from .endpoint_discovery import _resource_node
from .audit_log import AuditSink, LoggingAuditSink, build_event
from .confirmations import ConfirmationError, ConfirmationStore
from .confirmations import CONFIRMATION_STORE as TOKEN_STORE
from .controlled_actions import ControlledActionError
from .dynamic_executor import execute_dynamic_planned
from .dynamic_planner import (
    ELIGIBLE,
    HIGH_RISK_ELIGIBLE,
    PlanError,
    plan_dynamic_write,
)

PREVIEW_TTL_SECONDS = 300

# Safety-relevant record state bound at preview time for FSCM item writes and
# re-checked immediately before execution (STALE_PREVIEW on any difference).
# Status joins the family from live evidence: every record response observed
# across pods carries lifecycle status alongside (or instead of) StatusCode.
# Fields here must be write-observable (change on mutation) — never volatile
# per-read values — so a mismatch always means real concurrent change.
FSCM_STATE_FIELDS = ("StatusCode", "Status", "Revision", "FundsStatusCode",
                     "FrozenFlag", "LastUpdateDate")

AUDIT_SINK: AuditSink = LoggingAuditSink()


def set_workflow_audit_sink(sink: AuditSink) -> None:
    """Replace the workflow audit backend (tests use an in-memory sink)."""
    global AUDIT_SINK
    AUDIT_SINK = sink


class WorkflowError(ValueError):
    """Unknown/expired/used confirmation, or invalid preview request."""


class WorkflowRegistry:
    """Maps public confirmation IDs to (token, preview) pairs.

    The public ID is opaque and short-lived; the real confirmation token is
    only ever handed to ``dynamic_executor`` together with the stored
    preview. Claiming an ID atomically removes it, so at most one execution
    can ever proceed per preview. Process-local (same documented limitation
    as the token store itself).
    """

    def __init__(self, ttl_seconds: int = PREVIEW_TTL_SECONDS,
                 time_fn=None) -> None:
        self.ttl_seconds = ttl_seconds
        self._time = time_fn or time.time
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}

    def register(self, token: str, preview: Dict[str, Any],
                 ttl_seconds: Optional[int] = None) -> str:
        """Publish a token+preview under a fresh public ID. Returns the ID."""
        if not token or not isinstance(preview, dict):
            raise WorkflowError("cannot register an empty confirmation")
        confirmation_id = secrets.token_urlsafe(16)
        lifetime = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        with self._lock:
            self._entries[confirmation_id] = {
                "token": token,
                "preview": preview,
                "expires_at": self._time() + lifetime,
            }
        return confirmation_id

    def claim(self, confirmation_id: Any) -> Dict[str, Any]:
        """Atomically pop an entry for execution. Unknown/expired -> error."""
        if not isinstance(confirmation_id, str) or not confirmation_id:
            raise WorkflowError("missing or malformed confirmation_id")
        with self._lock:
            entry = self._entries.pop(confirmation_id, None)
            if entry is None:
                raise WorkflowError("unknown confirmation (never issued, "
                                    "expired, or already consumed)")
            if self._time() > entry["expires_at"]:
                raise WorkflowError("confirmation expired")
            return entry

    def outstanding(self) -> int:
        with self._lock:
            now = self._time()
            return sum(1 for e in self._entries.values() if now <= e["expires_at"])


# Single process-wide token store, SHARED with the curated preview paths
# (which issue via confirmations.issue_confirmation). One store means a
# confirmation minted by ANY preview lane is consumable by the unified
# executor — separate instances previously made legacy confirmations
# silently unexecutable.
REGISTRY = WorkflowRegistry()


def _suggest_names(want: Any, names: Any, limit: int = 5) -> List[str]:
    """Closest live resource names for an unknown resource (never raises).

    Substring hits first (covers prefix guesses like fixedAssets vs
    fixedAssetBooksLOV), then difflib close matches. Empty when nothing
    is close — the caller reports the miss without suggestions.
    """
    import difflib as _difflib
    if not isinstance(want, str) or not want.strip():
        return []
    pool = sorted({str(n) for n in (names or []) if n})
    if not pool:
        return []
    needle = want.strip().lower()
    hits = [n for n in pool if needle in n.lower()]
    for cand in _difflib.get_close_matches(want, pool, n=limit, cutoff=0.5):
        if cand not in hits:
            hits.append(cand)
    return hits[:limit]


async def _catalog_names(fetcher: Any) -> List[str]:
    try:
        names = await fetcher()
    except Exception:
        return []
    if isinstance(names, (set, frozenset, tuple)):
        names = sorted(names)
    if isinstance(names, dict):
        names = list(names.keys())
    if not isinstance(names, list):
        return []
    return [n for n in names if isinstance(n, str) and n][:500]


def _http_status(exc: Any) -> Any:
    """HTTP status carried by a describe failure, or None.

    FSCM raises httpx.HTTPStatusError (``.response.status_code``); HCM
    raises HcmApiError (``.status``). Anything else (timeouts, network,
    5xx wrappers without status) yields None and is treated as
    indeterminate — never as evidence of absence.
    """
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if isinstance(code, int):
        return code
    code = getattr(exc, "status", None)
    if isinstance(code, int):
        return code
    return None


async def _miss_suggestions(resource: Any, catalog_fn: Any,
                            docs_fn: Any) -> Any:
    """Suggestion pool for a missed describe: live catalog ∪ docs names.

    Returns ``(suggestions, sources)`` where sources names the consulted
    non-empty pools ("live-catalog", "docs-snapshot"). No network beyond
    what the fetchers do themselves (both are cached upstream); never
    probes candidates — suggestions are text for an explicit follow-up
    describe, never automatic fan-out.
    """
    live = await _catalog_names(catalog_fn) \
        if catalog_fn is not None else []
    docs = await _catalog_names(docs_fn) \
        if docs_fn is not None else []
    merged = list(dict.fromkeys(
        [n for n in list(live) + [d for d in docs if d not in live]]))
    suggestions = _suggest_names(resource, merged)
    sources = []
    if live:
        sources.append("live-catalog")
    if docs:
        sources.append("docs-snapshot")
    return suggestions, sources


def _unknown_resource(system: str, resource: Any, detail: Any,
                      suggestions: Any, verdict: str = "not_found",
                      sources: Any = None) -> Dict[str, Any]:
    """Refuse an unresolvable resource name without implying absence.

    Verdicts: "not_found" (HTTP 404 — existing wording preserved),
    "unauthorized" (401/403 — present-but-forbidden, no guessing),
    "indeterminate" (transport/5xx/unknown — explicitly not evidence of
    absence), "unusable" (200 without a usable metadata node).
    """
    if verdict == "unauthorized":
        reasons = [detail]
        suggestions = []
    elif verdict in ("indeterminate", "unusable"):
        reasons = [detail]
        if suggestions:
            reasons.append("did you mean: %s?" % ", ".join(suggestions))
        else:
            reasons.append(
                "list live resources/relationships before guessing names")
    else:
        reasons = ["unknown resource %r (%s)" % (resource, detail)]
        if suggestions:
            reasons.append("did you mean: %s?" % ", ".join(suggestions))
        else:
            reasons.append(
                "list live resources/relationships before guessing names")
    state = {"not_found": "NOT_FOUND", "unauthorized": "UNAUTHORIZED",
             "indeterminate": "INDETERMINATE",
             "unusable": "METADATA_UNUSABLE"}.get(verdict, "NOT_FOUND")
    return {"status": "NOT_RESOLVED", "resource": resource,
            "reasons": reasons, "would_execute": False,
            "discovery": {"state": state,
                          "candidates": list(suggestions or []),
                          "suggestions_from": list(sources or [])}}


async def preview_operation(*, system: str, resource: str, scope: str = "item",
                            operation: str, record_id: Any = None,
                            action: Optional[str] = None,
                            fields: Optional[Dict[str, Any]] = None,
                            fscm_describe_fn: Optional[Callable[..., Any]] = None,
                            hcm_attrs_fn: Optional[Callable[..., Any]] = None,
                             hcm_children_fn: Optional[Callable[..., Any]] = None,
                             fscm_catalog_fn: Optional[Callable[..., Any]] = None,
                             hcm_catalog_fn: Optional[Callable[..., Any]] = None,
                             docs_names_fn: Optional[Callable[..., Any]] = None,
                             record_fetch_fn: Optional[Callable[..., Any]] = None,
                            store: ConfirmationStore = TOKEN_STORE,
                            registry: WorkflowRegistry = REGISTRY,
                            audit: AuditSink = AUDIT_SINK,
                            ttl_seconds: Optional[int] = None,
                            api_version: Any = None,
                            child_path: Any = None,
                            child_record_id: Any = None,
                            environment: Any = None,
                            value_limits: Any = None,
                            prod_writes_enabled: bool = True) -> Dict[str, Any]:
    """Build a read-only preview + public confirmation ID. Never mutates.

    ``environment`` (dev/test/prod) is stamped into the preview for
    confirmation binding and audit. Unknown environments fail closed;
    production writes additionally require ``prod_writes_enabled``.
    """
    fields = dict(fields or {})
    env = str(environment or "dev").lower()
    if env not in ("dev", "test", "prod"):
        return {"status": "NOT_ELIGIBLE",
                "error": "unknown deployment environment %r: failing closed"
                         % (environment,),
                "would_execute": False}
    if env == "prod" and not prod_writes_enabled:
        return {"status": "NOT_ELIGIBLE",
                "resource": resource,
                "reasons": ["production writes are disabled by configuration"],
                "would_execute": False}
    try:
        state: Any = None
        if system == "FSCM":
            if fscm_describe_fn is None:
                raise PlanError("FSCM metadata fetcher unavailable")
            try:
                raw = await fscm_describe_fn(resource)
                parsed = parse_describe(resource, raw)
            except Exception as exc:
                status = _http_status(exc)
                if status in (401, 403):
                    return _unknown_resource(
                        system, resource,
                        "resource %r is present-but-forbidden (HTTP %s): "
                        "access issue, not a naming issue; do not guess "
                        "alternatives" % (resource, status),
                        [], verdict="unauthorized")
                suggestions, sources = await _miss_suggestions(
                    resource, fscm_catalog_fn, docs_names_fn)
                if status == 404:
                    return _unknown_resource(
                        system, resource, "describe failed: %s" % exc,
                        suggestions, verdict="not_found",
                        sources=sources)
                return _unknown_resource(
                    system, resource,
                    "live verification inconclusive for %r "
                    "(describe failed: %s): cannot confirm or deny; not "
                    "evidence of absence" % (resource, exc),
                    suggestions, verdict="indeterminate",
                    sources=sources)
            if _resource_node(raw, resource) is None:
                suggestions, sources = await _miss_suggestions(
                    resource, fscm_catalog_fn, docs_names_fn)
                return _unknown_resource(
                    system, resource,
                    "live describe returned no usable metadata for %r; "
                    "not evidence of absence" % (resource,),
                    suggestions, verdict="unusable", sources=sources)
            planned = plan_dynamic_write(
                system=system, resource=resource, scope=scope,
                operation=operation, record_id=record_id, fields=fields,
                action=action, fscm_parsed=parsed, fscm_raw=raw,
                api_version=api_version, child_path=child_path,
                child_record_id=child_record_id, value_limits=value_limits)
            state: Any = None
            if scope == "item" and record_fetch_fn is not None:
                state = await record_fetch_fn(resource, record_id)
            elif scope == "child_item" and record_fetch_fn is not None:
                # Read the nested target itself (parent key + child key);
                # the executor re-reads this exact endpoint pre-consume.
                trail = (child_path or "").strip("/").replace("/", "/child/")
                state = await record_fetch_fn(
                    "%s/%s/child/%s" % (resource, record_id, trail),
                    child_record_id)
        elif system == "HCM":
            if hcm_attrs_fn is None:
                raise PlanError("HCM metadata fetcher unavailable")
            try:
                attrs = await hcm_attrs_fn(resource)
            except Exception as exc:
                status = _http_status(exc)
                if status in (401, 403):
                    return _unknown_resource(
                        system, resource,
                        "resource %r is present-but-forbidden (HTTP %s): "
                        "access issue, not a naming issue; do not guess "
                        "alternatives" % (resource, status),
                        [], verdict="unauthorized")
                # HCM suggestions come from the live HCM catalog only:
                # FSCM/FARCA docs names must never leak across families.
                suggestions, sources = await _miss_suggestions(
                    resource, hcm_catalog_fn, None)
                if status == 404:
                    return _unknown_resource(
                        system, resource, "describe failed: %s" % exc,
                        suggestions, verdict="not_found",
                        sources=sources)
                return _unknown_resource(
                    system, resource,
                    "live verification inconclusive for %r "
                    "(describe failed: %s): cannot confirm or deny; not "
                    "evidence of absence" % (resource, exc),
                    suggestions, verdict="indeterminate",
                    sources=sources)
            children = await hcm_children_fn(resource) \
                if hcm_children_fn is not None else None
            version: Any = None
            if scope == "item":
                if record_fetch_fn is None:
                    return _refused(
                        system, resource, scope, operation, record_id,
                        "record read is required to bind HCM version")
                state = await record_fetch_fn(resource, record_id)
                version = _hcm_version(state)
            planned = plan_dynamic_write(
                system=system, resource=resource, scope=scope,
                operation=operation, record_id=record_id, fields=fields,
                action=action, hcm_attrs=attrs, hcm_children=children,
                version=version,
                api_version=api_version, child_path=child_path,
                child_record_id=child_record_id, value_limits=value_limits)
        else:
            raise PlanError("system must be HCM or FSCM")
    except PlanError as exc:
        return {"status": "NOT_ELIGIBLE", "error": str(exc),
                "would_execute": False}
    if planned.get("eligibility") not in (ELIGIBLE, HIGH_RISK_ELIGIBLE):
        return {"status": planned.get("eligibility", "NOT_ELIGIBLE"),
                "resource": resource, "reasons": planned.get("reasons", []),
                "would_execute": False}
    preview = _executor_preview(planned)
    # Stamp the normalized deployment environment for confirmation
    # binding and audit. Legacy previews without one bind None.
    preview["environment"] = env
    if system == "FSCM" and scope in ("item", "child_item") \
            and isinstance(state, dict):
        # Bind the safety-relevant snapshot; the executor re-reads and
        # compares it immediately before consuming the confirmation.
        # For nested targets the same stable field family applies.
        preview["bound_state"] = {
            k: state[k] for k in FSCM_STATE_FIELDS if k in state
        }
        if not preview["bound_state"]:
            return {"status": "NOT_ELIGIBLE",
                    "resource": resource,
                    "reasons": ["record state has no bindable safety fields"],
                    "would_execute": False}
    if system == "HCM" and scope == "item" and isinstance(state, dict):
        version = _hcm_version(state)
        if version is not None:
            preview["bound_state"] = {"objectVersionNumber": version}
        else:
            return {"status": "NOT_ELIGIBLE",
                    "resource": resource,
                    "reasons": ["record state has no bindable version field"],
                    "would_execute": False}
    try:
        token = store.issue(preview)["confirmation_token"]
    except Exception as exc:
        return {"status": "NOT_ELIGIBLE",
                "error": "confirmation issuance failed: %s" % exc,
                "would_execute": False}
    confirmation_id = registry.register(token, preview,
                                        ttl_seconds=ttl_seconds)
    try:
        audit.emit(build_event(
            "preview", resource=preview["resource"], scope=preview["scope"],
            action=preview.get("action", preview.get("operation", "")),
            record_id=str(preview.get("record_id", "")),
            parameters=preview.get("parameters"), mode="readonly",
            result="dry_run", confirmation="issued",
            version=preview.get("version")))
    except Exception as exc:
        return {"status": "NOT_ELIGIBLE",
                "error": "audit unavailable: %s" % exc,
                "would_execute": False}
    response_warnings = list(planned.get("warnings", []) or [])
    if env == "prod":
        response_warnings.append(
            "Production environment: verify the business need twice.")
    return {
        "status": "AWAITING_USER_AUTHORIZATION",
        "confirmation_id": confirmation_id,
        "preview_id": preview["preview_id"],
        "system": preview["system"],
        "resource": preview["resource"],
        "scope": preview["scope"],
        "operation": preview.get("operation"),
        "record_id": preview["record_id"],
        "method": preview["method"],
        "resolved_target": preview["path"],
        "risk": preview.get("risk"),
        "warnings": response_warnings,
        "current_state": _public_state(system, state),
        "bound_state": preview.get("bound_state"),
        "proposed_change": preview.get("parameters"),
        "expected_result": _expected_result(preview),
        "verification": preview.get("verification"),
        "expires_in_seconds": ttl_seconds if ttl_seconds is not None
        else registry.ttl_seconds,
    }


async def execute_confirmation(*, confirmation_id: Any,
                               registry: WorkflowRegistry = REGISTRY,
                               store: ConfirmationStore = TOKEN_STORE,
                               audit: AuditSink = AUDIT_SINK,
                               fscm_client_factory=None,
                               hcm_client_factory=None,
                               fscm_describe_fn=None,
                               hcm_attrs_fn=None,
                               hcm_children_fn=None,
                               environment: Any = None) -> Dict[str, Any]:
    """Execute exactly the operation bound to a public confirmation ID.

    The ONLY caller-controlled input is ``confirmation_id``. Everything else
    comes from the stored preview and is re-validated inside
    ``dynamic_executor`` (fresh metadata, binding, mode, audit, verify).
    """
    try:
        claimed = registry.claim(confirmation_id)
    except WorkflowError as exc:
        return {"executed": False, "error": str(exc)}
    preview, token = claimed["preview"], claimed["token"]
    try:
        if preview.get("system") == "FSCM":
            if fscm_client_factory is None or fscm_describe_fn is None:
                raise ControlledActionError("FSCM execution plumbing missing")
            return await execute_dynamic_planned(
                preview=preview, confirmation_token=token, store=store,
                audit=audit, fscm_client=fscm_client_factory(),
                fscm_describe_fn=fscm_describe_fn,
                environment=environment)
        if preview.get("system") == "HCM":
            if hcm_client_factory is None or hcm_attrs_fn is None:
                raise ControlledActionError("HCM execution plumbing missing")
            return await execute_dynamic_planned(
                preview=preview, confirmation_token=token, store=store,
                audit=audit, hcm_client=hcm_client_factory(),
                hcm_attrs_fn=hcm_attrs_fn,
                hcm_children_fn=hcm_children_fn,
                environment=environment)
    except ControlledActionError as exc:
        return {"executed": False, "error": str(exc)}
    raise ControlledActionError("unknown system in stored preview")


def _executor_preview(planned: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a planner output into the preview the executor consumes."""
    return {
        "mode": "dry_run",
        "system": planned["system"],
        "resource": planned["resource"],
        "scope": planned["scope"],
        "operation": planned.get("operation") or planned.get("action") or "",
        "action": planned.get("action") or planned.get("operation") or "",
        "method": planned["method"],
        "path": planned["path"],
        "template_source": planned.get("template_source"),
        "api_version": planned.get("api_version"),
        "metadata_fingerprint": planned.get("metadata_fingerprint"),
        "record_id": planned["record_id"],
        "child_path": planned.get("child_path"),
        "child_record_id": planned.get("child_record_id"),
        "primary_key": planned.get("primary_key"),
        "parameters": dict(planned.get("parameters") or {}),
        "version": planned.get("version"),
        "risk": planned.get("risk"),
        "eligibility": planned.get("eligibility"),
        "preview_id": planned["preview_id"],
        "verification": planned.get("verification"),
    }


def _hcm_version(state: Any) -> Any:
    if isinstance(state, dict):
        value = state.get("objectVersionNumber",
                          state.get("ObjectVersionNumber"))
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _public_state(system: str, state: Any) -> Any:
    if not isinstance(state, dict):
        return None
    if system == "HCM":
        keys = ("personAbsenceEntryId", "personNumber", "absenceType",
                "absenceStatusCd", "comments", "objectVersionNumber")
    else:
        keys = ("POHeaderId", "OrderNumber", "Status", "StatusCode",
                "Supplier", "Buyer")
    return {k: state.get(k) for k in keys if k in state}


def _expected_result(preview: Dict[str, Any]) -> str:
    """Human-readable outcome summary naming the actual target operation."""
    system = preview.get("system")
    operation = preview.get("operation") or preview.get("action") or ""
    resource = preview.get("resource")
    record_id = preview.get("record_id")
    if operation == "create":
        return ("new %s record created and re-read; supplied fields must "
                "equal observed values" % (resource,))
    if operation == "update":
        if system == "HCM":
            return ("%s %s updated and re-read; supplied fields must equal "
                    "observed values and objectVersionNumber must advance"
                    % (resource, record_id))
        return ("%s %s updated and re-read; supplied fields must equal "
                "observed values" % (resource, record_id))
    if operation == "delete":
        return ("%s %s deleted; target must be absent on re-read"
                % (resource, record_id))
    verification = preview.get("verification") or {}
    if operation == "business_action":
        from .dynamic_planner import contract_for
        contract = contract_for(preview.get("system", "FSCM"), resource,
                                preview.get("scope"), preview.get("action"))
        if contract and contract.get("predicate"):
            pred = contract["predicate"]
            expected = pred.get("equals", pred.get("in"))
            return ("action accepted by Fusion; VERIFIED only if the "
                    "evidenced postcondition (%s = %s) holds on the "
                    "independent re-read" % (pred.get("field"), expected))
        if contract and contract.get("hook"):
            return ("action accepted by Fusion; a specialized hook will "
                    "evaluate the authoritative postcondition")
        return ("action accepted by Fusion and target re-read; no "
                "action-specific success predicate exists, so the outcome "
                "will be reported as acknowledged-unverified, not verified")
    return ("action accepted by Fusion and target re-read; observed state "
            "reported for review")


def _refused(system, resource, scope, operation, record_id, reason):
    return {"status": "NOT_ELIGIBLE", "system": system, "resource": resource,
            "scope": scope, "operation": operation, "record_id": record_id,
            "reasons": [reason], "would_execute": False}


def _to_dynamic_preview(preview: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt a curated legacy preview to dynamic-executor shape.

    Only previews that already passed their curated validator are accepted,
    and only for operations with a dynamic executor shape:
      * FSCM curated (draftPurchaseOrders/purchaseOrders item actions) that
        are allowlisted AND readiness-approved;
      * HCM curated absence update.
    Anything else raises WorkflowError — legacy output never becomes
    executable merely by passing through this adapter.
    """
    if not isinstance(preview, dict) or preview.get("mode") != "dry_run":
        raise WorkflowError("adapter requires a curated dry-run preview")
    if not preview.get("would_execute"):
        raise WorkflowError("adapter requires an executable curated preview")
    resource = preview.get("resource")
    scope = preview.get("scope")
    record_id = preview.get("record_id")
    parameters = preview.get("parameters")
    if not isinstance(parameters, dict):
        raise WorkflowError("adapter requires object parameters")
    if preview.get("system", "FSCM") == "HCM" or preview.get("operation") == "update":
        if not (resource == "absences" and scope == "item"
                and preview.get("operation") == "update"
                and preview.get("method") == "PATCH"):
            raise WorkflowError("no dynamic shape for this HCM preview")
        if preview.get("execution_ready") is not True:
            raise WorkflowError("HCM preview is not readiness-approved")
        return {
            "mode": "dry_run",
            "system": "HCM",
            "resource": resource,
            "scope": scope,
            "operation": "update",
            "action": "update",
            "method": "PATCH",
            "path": "%s/%s" % (resource, record_id),
            "template_source": "canonical-pattern",
            "api_version": preview.get("api_version"),
            "metadata_fingerprint": preview.get("metadata_fingerprint"),
            "record_id": record_id,
            "parameters": dict(parameters),
            "version": preview.get("version"),
            "risk": "LOW",
            "eligibility": ELIGIBLE,
            "preview_id": preview.get("preview_id"),
            "bound_state": dict(preview.get("bound_state") or {}),
            "verification": {
                "strategy": "get_equality",
                "observe": sorted(parameters.keys()) + ["objectVersionNumber"],
            },
        }
    action = preview.get("action")
    method = preview.get("method")
    endpoint = preview.get("endpoint")
    if not (resource and scope == "item" and action and method == "POST"
            and isinstance(endpoint, str) and endpoint):
        raise WorkflowError("no dynamic shape for this FSCM preview")
    expected = "%s/%s/action/%s" % (resource, record_id, action)
    if endpoint != expected:
        raise WorkflowError("curated endpoint does not match derived pattern")
    if preview.get("execution_ready") is not True:
        raise WorkflowError("preview is not readiness-approved for execution")
    return {
        "mode": "dry_run",
        "system": "FSCM",
        "resource": resource,
        "scope": scope,
        "operation": "business_action",
        "action": action,
        "method": "POST",
        "path": endpoint,
        "template_source": preview.get("template_source") or "canonical-pattern",
        "api_version": preview.get("api_version"),
        "metadata_fingerprint": preview.get("metadata_fingerprint"),
        "record_id": record_id,
        "parameters": dict(parameters),
        "version": None,
        "risk": "MEDIUM",
        "eligibility": ELIGIBLE,
        "preview_id": preview.get("preview_id"),
        "bound_state": dict(preview.get("bound_state") or {}),
        "verification": {"strategy": "get_after_action",
                         "observe": ["Status", "StatusCode"]},
    }


def register_legacy_preview(preview: Dict[str, Any], token: str, *,
                            registry: WorkflowRegistry | None = None) -> str:
    """Publish a curated preview under a public confirmation ID.

    Returns the opaque ID; the raw token is never exposed to callers.
    Raises WorkflowError when the preview has no dynamic executor shape,
    which keeps NOT READY operations (e.g. hold) non-executable.
    """
    if not token:
        raise WorkflowError("cannot register an empty confirmation")
    target = registry if registry is not None else REGISTRY
    return target.register(token, _to_dynamic_preview(preview))

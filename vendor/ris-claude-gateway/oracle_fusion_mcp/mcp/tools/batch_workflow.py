"""Batch confirmed writes: one authorization for an ordered operation list.

A batch is N fully-resolved single-operation previews bound under ONE
single-use token. Rules, all fail-closed:

- Formation: every item must plan ELIGIBLE or HIGH_RISK_ELIGIBLE with
  valid fields, else no batch forms at all (per-item reasons reported).
- Review: the batch preview carries the COMPLETE itemized table
  (method/path/parameters/risk/verification per item) — the human
  reviews every operation before authorizing.
- Freshness: every item is re-resolved and state-checked immediately
  before the commit loop; any drift aborts before the token is consumed.
- Commit loop: exactly one transport per item, in order, no retries.
  The FIRST transport or verification failure STOPS the loop: the
  committed prefix stands, the suffix is untouched, and the result
  names the exact resume index. The token is single-use regardless.
- Scope: dynamic-lane single-operation shapes only (anything
  plan_dynamic_write resolves). Legacy curated previews are out of scope.

Read-only until the commit loop; the loop itself performs exactly the
transports the items authorize — nothing else, never twice.
"""

import uuid
from typing import Any, Callable, Dict, List, Optional

from .action_metadata import parse_describe
from .audit_log import AuditSink, build_event
from .confirmations import ConfirmationError, ConfirmationStore
from .controlled_actions import ControlledActionError
from .dynamic_executor import (
    _audit_result,
    _execute_fscm,
    _execute_hcm,
    _fscm_mode,
    _hcm_mode,
    _require_fresh_match,
    check_live_state,
    fresh_plan_for,
)
from .dynamic_planner import (
    ELIGIBLE,
    HIGH_RISK_ELIGIBLE,
    NOT_ELIGIBLE,
    PlanError,
    plan_dynamic_write,
)
from .dynamic_workflow import FSCM_STATE_FIELDS, WorkflowRegistry


async def preview_batch(*, operations: Any,
                        fscm_describe_fn: Optional[Callable[..., Any]] = None,
                        hcm_attrs_fn: Optional[Callable[..., Any]] = None,
                        hcm_children_fn: Optional[Callable[..., Any]] = None,
                        record_fetch_fn: Optional[Callable[..., Any]] = None,
                        hcm_record_fetch_fn: Optional[Callable[..., Any]] = None,
                        store: ConfirmationStore,
                        registry: WorkflowRegistry,
                        audit: AuditSink,
                        api_version: Any = None,
                        environment: Any = None,
                        value_limits: Any = None,
                        prod_writes_enabled: bool = True,
                        max_items: int = 50,
                        ttl_seconds: Optional[int] = None,
                        ) -> Dict[str, Any]:
    """Validate N operations and mint one batch confirmation. Read-only."""
    fields_env = str(environment or "dev").lower()
    if fields_env not in ("dev", "test", "prod"):
        return {"status": "NOT_ELIGIBLE",
                "error": "unknown deployment environment: failing closed",
                "would_execute": False}
    if fields_env == "prod" and not prod_writes_enabled:
        return {"status": "NOT_ELIGIBLE", "reasons": [
            "production writes are disabled by configuration"],
            "would_execute": False}
    if not isinstance(operations, list) or not operations:
        return {"status": "NOT_ELIGIBLE",
                "error": "batch requires a non-empty operations list",
                "would_execute": False}
    try:
        limit = int(max_items)
    except (TypeError, ValueError):
        limit = 50
    if len(operations) > limit:
        return {"status": "NOT_ELIGIBLE",
                "error": "batch of %d exceeds the limit of %d"
                         % (len(operations), limit),
                "would_execute": False}
    describe_cache: Dict[Any, Any] = {}
    items: List[Dict[str, Any]] = []
    item_reports: List[Dict[str, Any]] = []
    highest_risk = "LOW"
    risk_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "UNKNOWN": 3}
    for index, raw in enumerate(operations):
        report = await _plan_batch_item(
            index, raw, fscm_describe_fn=fscm_describe_fn,
            hcm_attrs_fn=hcm_attrs_fn, hcm_children_fn=hcm_children_fn,
            record_fetch_fn=record_fetch_fn,
            hcm_record_fetch_fn=hcm_record_fetch_fn,
            describe_cache=describe_cache, api_version=api_version,
            environment=fields_env, value_limits=value_limits)
        item_reports.append(report)
        if not report.get("ok"):
            continue
        items.append(report["preview"])
        rank = risk_rank.get(report["preview"].get("risk"), 3)
        if rank > risk_rank.get(highest_risk, 0):
            highest_risk = report["preview"].get("risk")
    failures = [r for r in item_reports if not r.get("ok")]
    if failures:
        return {"status": "NOT_ELIGIBLE", "would_execute": False,
                "reasons": ["batch refused: %d of %d operations unplannable"
                            % (len(failures), len(operations))],
                "items": item_reports}
    batch_preview = {
        "mode": "dry_run_batch",
        "preview_id": uuid.uuid4().hex,
        "count": len(items),
        "risk": highest_risk,
        "eligibility": HIGH_RISK_ELIGIBLE if highest_risk == "HIGH"
        else ELIGIBLE,
        "environment": fields_env,
        "items": items,
    }
    try:
        token = store.issue_batch(batch_preview,
                                  ttl_seconds=ttl_seconds)["confirmation_token"]
    except Exception as exc:
        return {"status": "NOT_ELIGIBLE",
                "error": "confirmation issuance failed: %s" % exc,
                "would_execute": False}
    confirmation_id = registry.register(token, batch_preview,
                                        ttl_seconds=ttl_seconds)
    try:
        audit.emit(build_event(
            "preview", resource="batch:%d" % len(items), scope="batch",
            action="batch", record_id="",
            parameters={"count": len(items), "risk": highest_risk},
            mode="readonly", result="dry_run", confirmation="issued",
            version=None))
    except Exception as exc:
        return {"status": "NOT_ELIGIBLE",
                "error": "audit unavailable: %s" % exc,
                "would_execute": False}
    return {
        "status": "AWAITING_USER_AUTHORIZATION",
        "confirmation_id": confirmation_id,
        "preview_id": batch_preview["preview_id"],
        "count": len(items),
        "risk": highest_risk,
        "eligibility": batch_preview["eligibility"],
        "items": [_public_item(item) for item in items],
        "verification": "per-item strategies apply at execution",
        "expires_in_seconds": ttl_seconds if ttl_seconds is not None
        else registry.ttl_seconds,
    }


def _public_item(preview: Dict[str, Any]) -> Dict[str, Any]:
    """The human-reviewable row: everything bound, nothing secret."""
    return {
        "system": preview.get("system"),
        "resource": preview.get("resource"),
        "scope": preview.get("scope"),
        "operation": preview.get("operation"),
        "action": preview.get("action"),
        "record_id": preview.get("record_id"),
        "child_path": preview.get("child_path"),
        "child_record_id": preview.get("child_record_id"),
        "method": preview.get("method"),
        "path": preview.get("path"),
        "template_source": preview.get("template_source"),
        "parameters": dict(preview.get("parameters") or {}),
        "risk": preview.get("risk"),
        "eligibility": preview.get("eligibility"),
        "verification": preview.get("verification"),
    }


async def _plan_batch_item(index: int, raw: Any, *,
                           fscm_describe_fn: Optional[Callable] = None,
                           hcm_attrs_fn: Optional[Callable] = None,
                           hcm_children_fn: Optional[Callable] = None,
                           record_fetch_fn: Optional[Callable] = None,
                           hcm_record_fetch_fn: Optional[Callable] = None,
                           describe_cache: Optional[Dict] = None,
                           api_version: Any = None,
                           environment: str = "dev",
                           value_limits: Any = None) -> Dict[str, Any]:
    """Plan + bind one batch row. Never executes."""
    base = {"index": index, "ok": False}
    if not isinstance(raw, dict):
        return dict(base, status="NOT_ELIGIBLE",
                    reasons=["item %d is not an object" % index])
    system = raw.get("system")
    resource = raw.get("resource")
    scope = raw.get("scope", "item")
    operation = raw.get("operation")
    record_id = raw.get("record_id")
    action = raw.get("action")
    fields = dict(raw.get("fields") or raw.get("parameters") or {})
    child_path = raw.get("child_path")
    child_record_id = raw.get("child_record_id")
    version = raw.get("version")
    try:
        if system == "FSCM":
            if fscm_describe_fn is None:
                raise PlanError("FSCM metadata fetcher unavailable")
            cache_key = (system, resource)
            if describe_cache is not None and cache_key in describe_cache:
                describe_raw, parsed = describe_cache[cache_key]
            else:
                describe_raw = await fscm_describe_fn(resource)
                parsed = parse_describe(resource, describe_raw)
                if describe_cache is not None:
                    describe_cache[cache_key] = (describe_raw, parsed)
            planned = plan_dynamic_write(
                system=system, resource=resource, scope=scope,
                operation=operation, record_id=record_id, fields=fields,
                action=action, fscm_parsed=parsed, fscm_raw=describe_raw,
                api_version=api_version, child_path=child_path,
                child_record_id=child_record_id,
                value_limits=value_limits)
            state = None
            if scope == "item" and record_fetch_fn is not None:
                state = await record_fetch_fn(resource, record_id)
            elif scope == "child_item" and record_fetch_fn is not None:
                trail = (child_path or "").strip("/").replace("/", "/child/")
                state = await record_fetch_fn(
                    "%s/%s/child/%s" % (resource, record_id, trail),
                    child_record_id)
        elif system == "HCM":
            if hcm_attrs_fn is None:
                raise PlanError("HCM metadata fetcher unavailable")
            attrs = await hcm_attrs_fn(resource)
            children = await hcm_children_fn(resource) \
                if hcm_children_fn is not None else None
            if scope == "item":
                version_fetcher = hcm_record_fetch_fn or record_fetch_fn
                if version_fetcher is None:
                    return dict(base, status="NOT_ELIGIBLE", reasons=[
                        "record read is required to bind HCM version"])
                state = await version_fetcher(resource, record_id)
                version = _hcm_version(state)
            else:
                state = None
            planned = plan_dynamic_write(
                system=system, resource=resource, scope=scope,
                operation=operation, record_id=record_id, fields=fields,
                action=action, hcm_attrs=attrs, hcm_children=children,
                version=version,
                api_version=api_version, child_path=child_path,
                child_record_id=child_record_id,
                value_limits=value_limits)
        else:
            raise PlanError("system must be HCM or FSCM")
    except PlanError as exc:
        return dict(base, status="NOT_ELIGIBLE", reasons=[str(exc)])
    if planned.get("eligibility") not in (ELIGIBLE, HIGH_RISK_ELIGIBLE):
        return dict(base, status=planned.get("eligibility", "NOT_ELIGIBLE"),
                    reasons=planned.get("reasons", []))
    item_preview = {
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
        "environment": environment,
        "parameters": dict(planned.get("parameters") or {}),
        "version": planned.get("version"),
        "risk": planned.get("risk"),
        "eligibility": planned.get("eligibility"),
        "preview_id": planned["preview_id"],
        "verification": planned.get("verification"),
    }
    if system == "FSCM" and scope in ("item", "child_item") \
            and isinstance(state, dict):
        item_preview["bound_state"] = {
            k: state[k] for k in FSCM_STATE_FIELDS if k in state
        }
        if not item_preview["bound_state"]:
            return dict(base, status="NOT_ELIGIBLE", reasons=[
                "record state has no bindable safety fields"])
    if system == "HCM" and scope == "item" and isinstance(state, dict):
        bound_version = _hcm_version(state)
        if bound_version is not None:
            item_preview["bound_state"] = {"objectVersionNumber": bound_version}
            item_preview["version"] = bound_version
        else:
            return dict(base, status="NOT_ELIGIBLE", reasons=[
                "record state has no bindable version field"])
    return dict(base, ok=True, preview=item_preview,
                status=item_preview["eligibility"])


def _hcm_version(state: Any) -> Any:
    if isinstance(state, dict):
        if state.get("objectVersionNumber") is not None:
            return state["objectVersionNumber"]
        return state.get("ObjectVersionNumber")
    return None


async def execute_batch(*, confirmation_id: Any,
                        registry: WorkflowRegistry,
                        store: ConfirmationStore,
                        audit: AuditSink,
                        fscm_client_factory=None,
                        hcm_client_factory=None,
                        fscm_describe_fn=None,
                        hcm_attrs_fn=None,
                        hcm_children_fn=None,
                        environment: Any = None) -> Dict[str, Any]:
    """Execute one authorized batch, stopping at the first failure."""
    try:
        claimed = registry.claim(confirmation_id)
    except Exception as exc:
        return {"executed": False, "error": str(exc)}
    batch_preview, token = claimed["preview"], claimed["token"]
    if batch_preview.get("environment") is not None \
            and batch_preview.get("environment") != environment:
        return {"executed": False,
                "error": "batch environment %r does not match execution "
                         "environment %r: failing closed"
                         % (batch_preview.get("environment"), environment)}
    items = batch_preview.get("items") or []
    if not items:
        return {"executed": False, "error": "batch has no items"}
    results: List[Dict[str, Any]] = []
    try:
        # Phase 1 (read-only): re-resolve + state-check every item before
        # consuming anything.
        for index, item in enumerate(items):
            fresh = await fresh_plan_for(
                item, fscm_describe_fn=fscm_describe_fn,
                hcm_attrs_fn=hcm_attrs_fn,
                hcm_children_fn=hcm_children_fn)
            _require_fresh_match(item, fresh)
            system = item.get("system")
            if system == "FSCM":
                await check_live_state(
                    item, fscm_client=(fscm_client_factory()
                                       if fscm_client_factory else None),
                    hcm_client=None)
            elif system == "HCM":
                await check_live_state(
                    item, fscm_client=None,
                    hcm_client=(hcm_client_factory()
                                if hcm_client_factory else None))
            else:
                raise ControlledActionError(
                    "unknown system %r" % (system,))
        try:
            store.consume_batch(token, batch_preview)
            batch_consumed = True
        except ConfirmationError as exc:
            _audit_batch(audit, batch_preview, "blocked",
                         "confirmation rejected: %s" % exc, None,
                         environment)
            raise ControlledActionError("confirmation rejected: %s" % exc)
        try:
            audit.emit(build_event(
                "execute_batch", resource="batch:%d" % len(items),
                scope="batch", action="batch", record_id="",
                parameters={"count": len(items)}, mode="full",
                result="attempt", confirmation="consumed",
                confirmation_token=token, version=None))
        except Exception as exc:
            raise ControlledActionError(
                "audit unavailable, refusing write: %s" % exc)
        # Phase 2: commit loop — exactly one transport per item, in order.
        for index, item in enumerate(items):
            result = await _execute_batch_item(
                item, audit=audit, confirmation_token=token,
                fscm_client_factory=fscm_client_factory,
                hcm_client_factory=hcm_client_factory)
            results.append({"index": index, "result": result})
            if not result.get("executed"):
                break
    except ControlledActionError as exc:
        return {"executed": False, "error": str(exc),
                "completed": [r for r in results],
                "resume_index": len(results)}
    _audit_batch(audit, batch_preview, "committed", None,
                 batch_consumed, environment)
    fully_executed = bool(results) and all(
        r.get("result", {}).get("executed") for r in results)
    return {"executed": fully_executed, "results": results,
            "completed": [r for r in results],
            "resume_index": len(results)}


async def _execute_batch_item(item: Dict[str, Any], *,
                              audit: AuditSink,
                              confirmation_token: Any,
                              fscm_client_factory=None,
                              hcm_client_factory=None) -> Dict[str, Any]:
    """Run one bound item through the shared single-operation executors."""
    system = item.get("system")
    if system == "FSCM":
        if fscm_client_factory is None:
            raise ControlledActionError("FSCM execution plumbing missing")
        client = fscm_client_factory()
        return await _execute_fscm(client, item, audit, _mode_of(client),
                                   confirmation_token)
    if system == "HCM":
        if hcm_client_factory is None:
            raise ControlledActionError("HCM execution plumbing missing")
        client = hcm_client_factory()
        return await _execute_hcm(client, item, audit, _mode_of(client),
                                  confirmation_token)
    raise ControlledActionError("unknown system %r" % (system,))


def _mode_of(client: Any) -> str:
    settings = getattr(client, "settings", None)
    mode = getattr(settings, "MCP_MODE", None)
    if mode:
        return str(mode)
    return str(getattr(client, "_mode", ""))


def _audit_batch(audit: AuditSink, batch_preview: Dict[str, Any],
                 result: str, error: Any, consumed: Any,
                 environment: Any) -> None:
    audit.emit(build_event(
        "execute_batch", resource="batch:%d" % len(batch_preview.get("items", [])),
        scope="batch", action="batch", record_id="",
        parameters={"count": len(batch_preview.get("items", [])),
                    "environment": environment}, mode="full",
        result=result, confirmation="consumed" if consumed else "rejected",
        confirmation_token=None, version=None, error=error))

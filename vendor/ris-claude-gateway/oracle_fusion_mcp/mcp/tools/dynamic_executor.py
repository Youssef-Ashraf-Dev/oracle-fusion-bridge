"""Dynamic one-time controlled executor (internal only — NEVER an MCP tool).

Accepts ONLY ``dynamic_planner`` preview outputs with
``ELIGIBLE_FOR_ONE_TIME_AUTHORIZATION``. Pipeline:

  preview shape/eligibility check
  -> live metadata re-resolution (injected fetchers, no client writes)
  -> fresh-plan equality (method/path/params/risk/version/record)
  -> HCM version re-read (fail closed on staleness, no retry, no new token)
  -> single-use confirmation consume
  -> explicit full-mode check (per-system client)
  -> pre-write audit (fail closed)
  -> exactly ONE mutation transport, no retries
  -> independent GET verification
  -> final audit

Any deviation fails closed WITHOUT consuming the confirmation whenever the
failure is detected before the consume step, and without a second transport
attempt in all cases. This module performs no registration and implements
no transport of its own: FSCM goes through ``OracleClient._post_action``,
HCM through ``HcmClient.update(..., retries=0)``.
"""

from typing import Any, Callable, Dict, Optional

from .action_metadata import parse_describe
from .audit_log import AuditSink, build_event
from .confirmations import ConfirmationError, ConfirmationStore
from .controlled_actions import ControlledActionError
from .oracle_errors import classify_write_error
from .dynamic_planner import (
    ELIGIBLE,
    EXECUTABLE_SHAPES,
    HIGH_RISK_ELIGIBLE,
    NOT_ELIGIBLE,
    NOT_RESOLVED,
    NOT_READY,
    UNSUPPORTED,
    contract_for,
    plan_dynamic_write,
)

_TERMINAL_REJECTIONS = frozenset({NOT_ELIGIBLE, NOT_RESOLVED, NOT_READY, UNSUPPORTED})

# Hook registry: maps contract "hook" names to async verifiers. Adding a
# new complex verification = one table entry + one hook function — no
# executor edit, no capability-list change. Hooks run only for item-scope
# business actions whose pre-hook verification is accepted or
# acknowledged_unverified (transport-level success); they independently
# assert the authoritative postcondition and may upgrade to verified or
# downgrade to unverified.
_VERIFICATION_HOOKS: Dict[str, Any] = {}


def _register_hooks() -> None:
    _VERIFICATION_HOOKS.clear()
    _VERIFICATION_HOOKS["po_close"] = _verify_po_close
    _VERIFICATION_HOOKS["invoice_distributions"] = _verify_invoice_distributions


def _predicate_met(predicate: Any, observed: Dict[str, Any]) -> bool:
    """Evaluate a recorded predicate against the re-read observation.

    Supports {"field", "equals"} and {"field", "in"}. Missing field or
    unrecognized shape fails closed (not met) — a predicate we cannot
    evaluate is never claimed as passed.
    """
    if not isinstance(predicate, dict):
        return False
    field = predicate.get("field")
    if not isinstance(field, str) or field not in observed:
        return False
    value = observed[field]
    if "equals" in predicate:
        expected = predicate["equals"]
        return value == expected or str(value).lower() == str(expected).lower()
    if "in" in predicate:
        allowed = predicate["in"]
        if not isinstance(allowed, (list, tuple, set)):
            return False
        return value in allowed or any(str(value).lower() == str(a).lower() for a in allowed)
    return False


async def execute_dynamic_planned(*, preview: Dict[str, Any],
                                  confirmation_token: Any,
                                  store: ConfirmationStore,
                                  audit: AuditSink,
                                  fscm_client: Any = None,
                                  hcm_client: Any = None,
                                   fscm_describe_fn: Optional[Callable[..., Any]] = None,
                                   hcm_attrs_fn: Optional[Callable[..., Any]] = None,
                                   hcm_children_fn: Optional[Callable[..., Any]] = None,
                                   environment: Any = None,
                                   ) -> Dict[str, Any]:
    """Execute one planner-authorized operation. Raises without mutating on failure."""
    _check_preview_shape(preview)
    if preview.get("eligibility") not in (ELIGIBLE, HIGH_RISK_ELIGIBLE):
        raise ControlledActionError(
            "preview eligibility %r is not executable" % preview.get("eligibility"))
    if preview.get("environment") is not None \
            and preview.get("environment") != environment:
        raise ControlledActionError(
            "preview environment %r does not match execution environment %r: "
            "failing closed" % (preview.get("environment"), environment))
    system = preview["system"]

    if system == "FSCM" and (fscm_client is None or fscm_describe_fn is None):
        raise ControlledActionError("FSCM execution plumbing missing")
    if system == "HCM" and (hcm_client is None or hcm_attrs_fn is None):
        raise ControlledActionError("HCM execution plumbing missing")
    fresh = await fresh_plan_for(
        preview, fscm_describe_fn=fscm_describe_fn,
        hcm_attrs_fn=hcm_attrs_fn, hcm_children_fn=hcm_children_fn)
    _require_fresh_match(preview, fresh)
    await check_live_state(
        preview, fscm_client=fscm_client, hcm_client=hcm_client)
    if system == "FSCM":
        client, mode = fscm_client, _fscm_mode(fscm_client)
    elif system == "HCM":
        client, mode = hcm_client, _hcm_mode(hcm_client)
    else:
        raise ControlledActionError("unknown system %r" % (system,))

    try:
        store.consume(confirmation_token, preview)
    except ConfirmationError as exc:
        _audit_blocked(audit, preview, mode, "confirmation rejected: %s" % exc)
        raise ControlledActionError("confirmation rejected: %s" % exc)
    if mode != "full":
        _audit_blocked(audit, preview, mode, "write mode not enabled")
        raise ControlledActionError("write mode not enabled: refusing execution")
    try:
        _audit_attempt(audit, preview, mode, confirmation_token)
    except Exception as exc:
        raise ControlledActionError("audit unavailable, refusing write: %s" % exc)

    if system == "FSCM":
        return await _execute_fscm(client, preview, audit, mode, confirmation_token)
    return await _execute_hcm(client, preview, audit, mode, confirmation_token)


async def fresh_plan_for(preview: Dict[str, Any], *,
                         fscm_describe_fn: Optional[Callable[..., Any]] = None,
                         hcm_attrs_fn: Optional[Callable[..., Any]] = None,
                         hcm_children_fn: Optional[Callable[..., Any]] = None,
                         ) -> Dict[str, Any]:
    """Re-resolve one operation from live metadata (no state checks).

    Shared by single and batch execution so both re-validate against
    the same fresh truth.
    """
    system = preview.get("system")
    if system == "FSCM":
        if fscm_describe_fn is None:
            raise ControlledActionError("FSCM execution plumbing missing")
        fresh_raw = await fscm_describe_fn(preview["resource"])
        fresh_parsed = parse_describe(preview["resource"], fresh_raw)
        return plan_dynamic_write(
            system=system, resource=preview["resource"], scope=preview["scope"],
            operation=preview["operation"], record_id=preview["record_id"],
            fields=dict(preview.get("parameters") or {}),
            action=preview.get("action"), fscm_parsed=fresh_parsed,
            fscm_raw=fresh_raw, api_version=preview.get("api_version"),
            child_path=preview.get("child_path"),
            child_record_id=preview.get("child_record_id"))
    if system == "HCM":
        if hcm_attrs_fn is None:
            raise ControlledActionError("HCM execution plumbing missing")
        fresh_attrs = await hcm_attrs_fn(preview["resource"])
        fresh_children = await hcm_children_fn(preview["resource"]) \
            if hcm_children_fn is not None else None
        return plan_dynamic_write(
            system=system, resource=preview["resource"], scope=preview["scope"],
            operation=preview["operation"], record_id=preview["record_id"],
            fields=dict(preview.get("parameters") or {}),
            action=preview.get("action"), hcm_attrs=fresh_attrs,
            hcm_children=fresh_children,
            version=preview.get("version"),
            api_version=preview.get("api_version"),
            child_path=preview.get("child_path"),
            child_record_id=preview.get("child_record_id"))
    raise ControlledActionError("unknown system %r" % (system,))


async def check_live_state(preview: Dict[str, Any], *,
                           fscm_client: Any = None,
                           hcm_client: Any = None) -> None:
    """Pre-consume freshness: state/version re-read per scope and system."""
    system = preview.get("system")
    if system == "FSCM":
        if preview.get("scope") in ("item", "child", "child_item") or (
                preview.get("scope") == "collection"
                and preview.get("operation") == "business_action"):
            if fscm_client is None:
                raise ControlledActionError(
                    "FSCM execution plumbing missing")
            await _require_fresh_state(fscm_client, preview)
        return
    if system == "HCM":
        if hcm_client is None:
            raise ControlledActionError("HCM execution plumbing missing")
        await _require_fresh_version(hcm_client, preview)
        return
    raise ControlledActionError("unknown system %r" % (system,))


def _check_preview_shape(preview: Dict[str, Any]) -> None:
    if not isinstance(preview, dict) or preview.get("mode") != "dry_run":
        raise ControlledActionError("executor requires a dynamic dry-run preview")
    for field in ("system", "resource", "scope", "operation",
                  "preview_id", "method", "path", "parameters"):
        if preview.get(field) in (None, ""):
            raise ControlledActionError("preview missing %r" % field)
    if preview.get("scope") != "collection" and not preview.get("record_id"):
        raise ControlledActionError("preview missing 'record_id'")
    if preview.get("scope") == "child_item" \
            and not preview.get("child_record_id"):
        raise ControlledActionError("preview missing 'child_record_id'")
    if not isinstance(preview.get("parameters"), dict):
        raise ControlledActionError("preview parameters must be an object")
    if preview.get("eligibility") in _TERMINAL_REJECTIONS:
        raise ControlledActionError(
            "preview eligibility %r is not executable" % preview.get("eligibility"))


def _require_fresh_match(preview: Dict[str, Any], fresh: Dict[str, Any]) -> None:
    """The re-resolved operation must equal the authorized preview exactly.

    Beyond method/path/params, the metadata fingerprint, endpoint-template
    provenance, and API version must also match: any metadata drift or
    binding substitution since preview fails closed.
    """
    if fresh.get("eligibility") not in (ELIGIBLE, HIGH_RISK_ELIGIBLE):
        raise ControlledActionError(
            "re-resolution no longer eligible (%s): %s"
            % (fresh.get("eligibility"), "; ".join(fresh.get("reasons", []))))
    for field in ("method", "path", "risk"):
        if fresh.get(field) != preview.get(field):
            raise ControlledActionError(
                "re-resolved %s differs from preview: failing closed" % field)
    for field in ("api_version", "template_source", "metadata_fingerprint"):
        if fresh.get(field) != preview.get(field):
            raise ControlledActionError(
                "re-resolved %s differs from preview: failing closed" % field)
    if (fresh.get("parameters") or {}) != (preview.get("parameters") or {}):
        raise ControlledActionError(
            "re-resolved parameters differ from preview: failing closed")
    if str(fresh.get("record_id")) != str(preview.get("record_id")):
        raise ControlledActionError(
            "re-resolved record differs from preview: failing closed")
    if (fresh.get("version") is not None or preview.get("version") is not None) \
            and fresh.get("version") != preview.get("version"):
        raise ControlledActionError(
            "re-resolved version differs from preview: failing closed")


async def _require_fresh_state(fscm_client: Any, preview: Dict[str, Any]) -> None:
    """Re-read the bound target and compare the snapshot pre-consume.

    Item scope re-reads ``resource/record_id``; child_item scope re-reads
    the resolved nested endpoint (preview path); child (collection) scope
    re-reads the parent for existence. Any difference (or unreadable
    record) fails closed with STALE_PREVIEW: the confirmation is left
    untouched and no transport is attempted.
    """
    scope = preview.get("scope")
    if scope == "child":
        try:
            parent = await fscm_client._fetch(
                "%s/%s" % (preview["resource"], preview["record_id"]),
                {"onlyData": "true"})
        except Exception as exc:
            raise ControlledActionError(
                "pre-execution parent re-read failed: %s" % exc)
        if not isinstance(parent, dict) or not parent:
            raise ControlledActionError(
                "pre-execution parent record missing: failing closed")
        return
    if scope == "collection":
        # Collection actions address their target through bound parameters:
        # the target must exist before anything is consumed.
        located = _collection_action_target(preview)
        if located is None:
            raise ControlledActionError(
                "no verifiable target in preview: failing closed")
        try:
            data = await fscm_client._fetch(located[0], located[1])
        except Exception as exc:
            raise ControlledActionError(
                "pre-execution target read failed: %s" % exc)
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            raise ControlledActionError(
                "STALE_PREVIEW: verification target not found")
        return
    bound = preview.get("bound_state") or {}
    if not bound:
        raise ControlledActionError(
            "no bound state in preview: failing closed")
    if scope == "child_item" and preview.get("operation") == "business_action":
        # Re-read the nested record itself, not the action endpoint.
        target = (preview.get("path") or "").split("/action/")[0] or None
    elif scope == "child_item":
        target = preview.get("path")
    else:
        target = "%s/%s" % (preview["resource"], preview["record_id"])
    if not target:
        raise ControlledActionError(
            "no state target in preview: failing closed")
    try:
        current = await fscm_client._fetch(target, {"onlyData": "true"})
    except Exception as exc:
        raise ControlledActionError("pre-execution re-read failed: %s" % exc)
    if not isinstance(current, dict):
        raise ControlledActionError("pre-execution re-read failed: bad shape")
    for key, value in bound.items():
        if key not in current or current[key] != value:
            raise ControlledActionError(
                "STALE_PREVIEW: bound %s=%r but live is %r"
                % (key, value, current.get(key)))


async def _require_fresh_version(hcm_client: Any, preview: Dict[str, Any]) -> None:
    """Re-read the HCM record and compare objectVersionNumber pre-consume.

    Skipped when the preview binds no version (creates have no prior
    version to check). Any mismatch fails closed with STALE_PREVIEW.
    """
    if preview.get("version") is None:
        return
    try:
        current = await hcm_client.get_record(
            preview["resource"], str(preview["record_id"]), redact=False)
    except Exception as exc:
        raise ControlledActionError("pre-execution re-read failed: %s" % exc)
    live = current.get("objectVersionNumber", current.get("ObjectVersionNumber")) \
        if isinstance(current, dict) else None
    if live != preview.get("version"):
        raise ControlledActionError(
            "STALE_PREVIEW: bound version %r but live is %r"
            % (preview.get("version"), live))


def _fscm_mode(client: Any) -> str:
    return str(getattr(getattr(client, "settings", None), "MCP_MODE", ""))


def _hcm_mode(client: Any) -> str:
    return str(getattr(client, "_mode", ""))


def _audit_blocked(audit: AuditSink, preview: Dict[str, Any],
                   mode: str, error: Any) -> None:
    audit.emit(build_event(
        "execute", resource=str(preview.get("resource", "")),
        scope=str(preview.get("scope", "")), action=str(preview.get("action", "")),
        record_id=str(preview.get("record_id", "")),
        parameters=preview.get("parameters"), mode=mode,
        result="blocked", confirmation="rejected",
        version=preview.get("version"), error=error))


def _audit_attempt(audit: AuditSink, preview: Dict[str, Any], mode: str,
                   confirmation_token: Any) -> None:
    audit.emit(build_event(
        "execute", resource=str(preview.get("resource", "")),
        scope=str(preview.get("scope", "")),
        action=str(preview.get("action", preview.get("operation", ""))),
        record_id=str(preview.get("record_id", "")),
        parameters=preview.get("parameters"), mode=mode,
        result="attempt", confirmation="consumed",
        confirmation_token=confirmation_token,
        version=preview.get("version")))


async def _execute_fscm(client: Any, preview: Dict[str, Any], audit: AuditSink,
                        mode: str, confirmation_token: Any) -> Dict[str, Any]:
    """Generic FSCM dispatch on the resolved operation kind.

    Collection create/upsert -> single POST; item/child update -> PATCH;
    item/child delete -> DELETE; item/child business_action -> template-
    faithful action POST. Collection-scope actions are not dispatched
    (their verification target cannot be derived from the path; P4).
    """
    scope = preview.get("scope")
    operation = preview.get("operation")
    if ("FSCM", scope, operation) not in EXECUTABLE_SHAPES:
        raise ControlledActionError(
            "no FSCM executor for scope %r operation %r" % (scope, operation))
    if scope == "collection" and operation in ("create", "upsert"):
        return await _execute_fscm_create(client, preview, audit, mode, confirmation_token)
    if scope == "child" and operation in ("create", "upsert"):
        return await _execute_fscm_create(client, preview, audit, mode, confirmation_token)
    if operation == "update":
        return await _execute_fscm_update(client, preview, audit, mode, confirmation_token)
    if operation == "delete":
        return await _execute_fscm_delete(client, preview, audit, mode, confirmation_token)
    if operation == "business_action":
        return await _execute_fscm_action(client, preview, audit, mode, confirmation_token)
    raise ControlledActionError(
        "no FSCM executor for scope %r operation %r" % (scope, operation))


async def _resolved_endpoint(preview: Dict[str, Any]) -> str:
    """Return the verified concrete endpoint, rejecting placeholders."""
    endpoint = preview.get("path")
    if not isinstance(endpoint, str) or not endpoint:
        raise ControlledActionError("preview has no resolved endpoint")
    if ("{" in endpoint) or ("}" in endpoint):
        raise ControlledActionError(
            "preview endpoint has unresolved template placeholders: failing closed")
    return endpoint


def _collection_action_target(preview: Dict[str, Any]) -> Any:
    """Locate a collection-action verification target from bound parameters.

    Returns (endpoint, params) for a collection GET filtered to the single
    target record, or None when the preview carries no derivable target.
    The ADF filter value is single-quote escaped; only exact-match ``=``
    filters are ever built here.
    """
    strategy = preview.get("verification") or {}
    target = strategy.get("target") or {}
    query_field = target.get("query_field")
    id_value = target.get("id_value")
    if not query_field or not isinstance(query_field, str):
        return None
    if id_value is None or (isinstance(id_value, str) and not id_value):
        return None
    resource = preview.get("resource")
    if not resource:
        return None
    value = str(id_value).replace("'", "''")
    return (resource, {"q": "%s = '%s'" % (query_field, value),
                       "onlyData": "true",
                       "limit": "25"})


async def _execute_fscm_action(client: Any, preview: Dict[str, Any],
                               audit: AuditSink, mode: str,
                               confirmation_token: Any) -> Dict[str, Any]:
    # Template-faithful action endpoint: the preview path was resolved from
    # Fusion metadata (link template or documented convention) and verified
    # against a fresh re-resolution. It is used verbatim — never rebuilt
    # from a canonical pattern.
    endpoint = await _resolved_endpoint(preview)
    try:
        post_result = await client._post_action(
            endpoint, dict(preview.get("parameters") or {}))
    except Exception as exc:
        _audit_result(audit, preview, mode, confirmation_token, "failed", exc)
        return {"executed": False, "transport": "POST",
                "resource": preview["resource"], "record_id": preview["record_id"],
                "verification": {"status": "failed", "state": None},
                "error": str(exc)[:300],
                "field_report": classify_write_error(
                    str(exc), system=preview.get("system"),
                    resource=preview.get("resource"),
                    scope=preview.get("scope"),
                    operation=preview.get("operation"),
                    action=preview.get("action"))}
    target = endpoint.split("/action/")[0] if "/action/" in endpoint else \
        "%s/%s" % (preview["resource"], preview["record_id"])
    strategy = preview.get("verification") or {}
    # Contract-first outcome model (P3): an explicit evidence-backed
    # verification contract enhances the outcome (verified / hook-proven),
    # but its ABSENCE never changes executability — it yields
    # acknowledged_unverified. strategy["observe"] remains the field source
    # (contract-preferred upstream, legacy-table fallback).
    contract = contract_for(
        preview.get("system", "FSCM"), preview.get("resource"),
        preview.get("scope"), preview.get("action"))
    predicate = contract.get("predicate") if isinstance(contract, dict) else None
    observe = strategy.get("observe") or (
        list(contract["observe"])
        if isinstance(contract, dict) and contract.get("observe")
        else [])
    verification: Dict[str, Any] = {}
    if preview.get("scope") == "collection":
        # Record-less action endpoint: locate the target through the bound
        # identifying parameters and read it back from the collection.
        located = _collection_action_target(preview)
        if located is None:
            verification = {"status": "unverified", "state": None,
                            "note": "POST accepted but verification target "
                                    "cannot be derived from parameters",
                            "post_result": post_result}
        else:
            try:
                data = await client._fetch(located[0], located[1])
                items = data.get("items", []) \
                    if isinstance(data, dict) else []
                if not items or not isinstance(items[0], dict):
                    verification = {
                        "status": "unverified", "state": None,
                        "note": "POST accepted but target not found",
                        "post_result": post_result}
                else:
                    after = items[0]
                    query_field = ((strategy.get("target") or {})
                                   .get("query_field"))
                    keys = ([query_field] if query_field else []) + observe
                    observed = {k: after.get(k) for k in keys if k in after}
                    if predicate is not None:
                        met = _predicate_met(predicate, observed)
                        verification = {
                            "status": "verified" if met else "unverified",
                            "state": observed, "post_result": post_result,
                            **({} if met else {"note": "predicate not met"})}
                    elif isinstance(contract, dict) and contract.get("hook"):
                        verification = {"status": "accepted",
                                        "state": observed,
                                        "post_result": post_result}
                    else:
                        verification = {
                            "status": "acknowledged_unverified",
                            "state": observed, "post_result": post_result,
                            "note": "transport accepted and target re-read; "
                                    "no action-specific success predicate "
                                    "exists, so the action outcome is "
                                    "reported, not verified"}
            except Exception as exc:
                verification = {"status": "unverified", "state": None,
                                "note": "POST accepted but re-read failed: %s"
                                        % str(exc)[:200],
                                "post_result": post_result}
    if not verification:
        try:
            after = await client._fetch(target, {"onlyData": "true"})
            observed = {k: after.get(k) for k in observe if k in after}
            if predicate is not None:
                met = _predicate_met(predicate, observed)
                verification = {
                    "status": "verified" if met else "unverified",
                    "state": observed, "post_result": post_result,
                    **({} if met else {"note": "predicate not met"})}
            elif isinstance(contract, dict) and contract.get("hook"):
                verification = {"status": "accepted", "state": observed,
                                "post_result": post_result}
            else:
                # No policy-selected fields to observe (or observe-only
                # contract): the POST was accepted and the target was
                # re-read, but no authoritative semantic postcondition
                # exists, so success of the requested action is NOT claimed.
                verification = {
                    "status": "acknowledged_unverified", "state": observed,
                    "post_result": post_result,
                    "note": "transport accepted and target re-read; no "
                            "action-specific success predicate exists, so "
                            "the action outcome is reported, not verified"}
        except Exception as exc:
            verification = {"status": "unverified", "state": None,
                            "note": "POST accepted but re-read failed: %s"
                                    % str(exc)[:200],
                            "post_result": post_result}
    hook_name = contract.get("hook") if isinstance(contract, dict) else None
    if hook_name and preview.get("scope") == "item":
        if not _VERIFICATION_HOOKS:
            _register_hooks()
        hook = _VERIFICATION_HOOKS.get(hook_name)
        if (hook is not None
                and verification.get("status") in (
                    "accepted", "acknowledged_unverified")):
            verification = await hook(
                client, preview, post_result, verification)
    _audit_result(audit, preview, mode, confirmation_token, "committed", None)
    return {"executed": True, "transport": "POST",
            "resource": preview["resource"], "record_id": preview["record_id"],
            "verification": verification}


async def _verify_po_close(client: Any, preview: Dict[str, Any],
                           post_result: Any,
                           verification: Dict[str, Any]) -> Dict[str, Any]:
    """Verify a purchaseOrders close: header CLOSED + observed line states.

    Close rolls down to lines/schedules per Oracle's documented action
    semantics, so verification reads the header plus expanded lines. The
    header reaching StatusCode CLOSED is the authoritative postcondition for
    closeAction="close" (partial variants do not promise it, which is why
    they require explicit selection upstream). Line states are reported but
    do not gate the verdict beyond the header, and are marked unverified
    when the expand path is unavailable.
    """
    rid = preview["record_id"]
    try:
        data = await client._fetch(
            "purchaseOrders",
            {"q": "POHeaderId = %s" % rid, "expand": "lines",
             "limit": "1", "onlyData": "true"})
        items = data.get("items") or []
        header = items[0] if items else {}
    except Exception as exc:
        return {"status": "unverified", "state": verification.get("state"),
                "post_result": post_result,
                "note": "close accepted but verification read failed: %s"
                        % str(exc)[:200]}
    raw_lines = header.get("lines")
    if isinstance(raw_lines, dict):
        raw_lines = raw_lines.get("items", [])
    if not isinstance(raw_lines, list):
        raw_lines = []
    line_states = _extract_line_states(raw_lines)
    schedules = await _extract_schedule_states(client, preview, raw_lines)
    state = {"Status": header.get("Status"),
             "StatusCode": header.get("StatusCode"),
             "lines": line_states,
             "schedules": schedules}
    if header.get("StatusCode") == "CLOSED":
        return {"status": "verified", "state": state,
                "post_result": post_result}
    return {"status": "unverified", "state": state,
            "post_result": post_result,
            "note": "header StatusCode is %r, expected 'CLOSED'"
                    % header.get("StatusCode")}


async def _verify_invoice_distributions(client: Any, preview: Dict[str, Any],
                                          post_result: Any,
                                          verification: Dict[str, Any]
                                          ) -> Dict[str, Any]:
    """Verify an invoices generateDistributions: every line carries one.

    Distributions live in the invoiceLines child collection, not on the
    header, so the generic header re-read cannot evidence this action.
    The hook reads the lines and counts distributions per line (tolerating
    dict- or list-shaped child payloads). Verified only when every line
    has at least one distribution with account coding; anything else is
    reported as unverified with counts — never failed, never assumed.
    """
    rid = preview["record_id"]
    try:
        data = await client._fetch(
            "invoices/%s/child/invoiceLines" % rid,
            {"onlyData": "true", "limit": "100"})
    except Exception as exc:
        return {"status": "unverified", "state": verification.get("state"),
                "post_result": post_result,
                "note": "distributions accepted but lines read failed: %s"
                        % str(exc)[:200]}
    items = data.get("items", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        items = []
    total = 0
    distributed = 0
    for line in items:
        if not isinstance(line, dict):
            continue
        total += 1
        dists = line.get("invoiceDistributions")
        if dists is None:
            dists = line.get("distributions")
        if isinstance(dists, dict):
            dists = dists.get("items", [])
        if not isinstance(dists, list):
            continue
        for dist in dists:
            if isinstance(dist, dict) and (
                    dist.get("DistributionCombination")
                    or dist.get("CodeCombination")
                    or dist.get("SegmentCombination")):
                distributed += 1
                break
    state = {"lines": total, "distributed_lines": distributed,
             "header": verification.get("state")}
    if total > 0 and distributed == total:
        return {"status": "verified", "state": state,
                "post_result": post_result}
    return {"status": "unverified", "state": state,
            "post_result": post_result,
            "note": "%d of %d lines carry distributions"
                    % (distributed, total)}


async def _execute_fscm_create(client: Any, preview: Dict[str, Any],
                               audit: AuditSink, mode: str,
                               confirmation_token: Any) -> Dict[str, Any]:
    """Execute exactly one POST to create a resource, then GET-verify.

    Works for top-level and nested collections alike: the endpoint is the
    verified preview path, and the created identity is discovered via the
    bound primary key first, then the known Fusion key candidates.
    """
    endpoint = await _resolved_endpoint(preview)
    body = dict(preview.get("parameters") or {})
    try:
        post_result = await client._post(endpoint, body)
    except Exception as exc:
        _audit_result(audit, preview, mode, confirmation_token, "failed", exc)
        return {"executed": False, "transport": "POST",
                "resource": preview["resource"], "record_id": None,
                "verification": {"status": "failed", "state": None},
                "error": str(exc)[:300],
                "field_report": classify_write_error(
                    str(exc), system=preview.get("system"),
                    resource=preview.get("resource"),
                    scope=preview.get("scope"),
                    operation=preview.get("operation"),
                    action=preview.get("action"))}
    created_id = _discover_created_id(post_result, preview.get("primary_key"))
    if created_id is None:
        _audit_result(audit, preview, mode, confirmation_token, "committed", None)
        return {"executed": True, "transport": "POST",
                "resource": preview["resource"], "record_id": None,
                "verification": {"status": "unverified", "state": None,
                                 "note": "POST succeeded but no created ID returned"},
                "post_result": post_result}
    try:
        after = await client._fetch(
            "%s/%s" % (endpoint, created_id),
            {"onlyData": "true"})
        observed = {k: after.get(k) for k in body.keys() if k in after}
        mismatched = [k for k, v in body.items() if k in after and after.get(k) != v]
        if mismatched:
            verification: Dict[str, Any] = {
                "status": "unverified", "state": observed,
                "note": "after-values differ for: %s" % ",".join(mismatched)}
        else:
            verification = {"status": "verified", "state": observed,
                            "new_id": created_id}
    except Exception as exc:
        verification = {"status": "unverified", "state": None,
                        "note": "POST succeeded but verification read failed: %s"
                                % str(exc)[:200]}
    _audit_result(audit, preview, mode, confirmation_token, "committed", None,
                  version=created_id)
    return {"executed": True, "transport": "POST",
            "resource": preview["resource"], "record_id": created_id,
            "verification": verification,
            "post_result": post_result}


def _discover_created_id(post_result: Any,
                         primary_key: Any = None) -> Any:
    """Find the created record's identity in a POST response.

    The bound primary key (from describe metadata) wins; well-known
    Fusion key candidates follow. Returns None when nothing matches.
    """
    if not isinstance(post_result, dict):
        return None
    candidates = []
    if isinstance(primary_key, str) and primary_key:
        candidates.append(primary_key)
    candidates.extend(("InvoiceId", "POHeaderId", "id", "Id"))
    for key in candidates:
        if key in post_result and post_result[key] not in (None, ""):
            return post_result[key]
    return None


async def _execute_fscm_update(client: Any, preview: Dict[str, Any],
                               audit: AuditSink, mode: str,
                               confirmation_token: Any) -> Dict[str, Any]:
    """Execute exactly one PATCH on a resolved item endpoint, then echo-verify."""
    endpoint = await _resolved_endpoint(preview)
    body = dict(preview.get("parameters") or {})
    try:
        patch_result = await client._patch(endpoint, body)
    except Exception as exc:
        _audit_result(audit, preview, mode, confirmation_token, "failed", exc)
        return {"executed": False, "transport": "PATCH",
                "resource": preview["resource"],
                "record_id": preview.get("record_id"),
                "verification": {"status": "failed", "state": None},
                "error": str(exc)[:300],
                "field_report": classify_write_error(
                    str(exc), system=preview.get("system"),
                    resource=preview.get("resource"),
                    scope=preview.get("scope"),
                    operation=preview.get("operation"),
                    action=preview.get("action"))}
    try:
        after = await client._fetch(endpoint, {"onlyData": "true"})
        if not isinstance(after, dict):
            raise ControlledActionError("verification read returned bad shape")
        mismatched = [k for k, v in body.items()
                      if k in after and after.get(k) != v]
        observed = {k: after.get(k) for k in body.keys() if k in after}
        if mismatched:
            verification: Dict[str, Any] = {
                "status": "unverified", "state": observed,
                "note": "after-values differ for: %s" % ",".join(mismatched)}
        else:
            verification = {"status": "verified", "state": observed}
    except Exception as exc:
        verification = {"status": "unverified", "state": None,
                        "note": "PATCH accepted but re-read failed: %s"
                                % str(exc)[:200]}
    _audit_result(audit, preview, mode, confirmation_token, "committed", None)
    return {"executed": True, "transport": "PATCH",
            "resource": preview["resource"],
            "record_id": preview.get("record_id"),
            "verification": verification,
            "patch_result": patch_result}


async def _execute_fscm_delete(client: Any, preview: Dict[str, Any],
                               audit: AuditSink, mode: str,
                               confirmation_token: Any) -> Dict[str, Any]:
    """Execute exactly one DELETE on a resolved item endpoint, then verify
    absence: the target GET must fail or return empty."""
    endpoint = await _resolved_endpoint(preview)
    try:
        delete_result = await client._delete(endpoint)
    except Exception as exc:
        _audit_result(audit, preview, mode, confirmation_token, "failed", exc)
        return {"executed": False, "transport": "DELETE",
                "resource": preview["resource"],
                "record_id": preview.get("record_id"),
                "verification": {"status": "failed", "state": None},
                "error": str(exc)[:300],
                "field_report": classify_write_error(
                    str(exc), system=preview.get("system"),
                    resource=preview.get("resource"),
                    scope=preview.get("scope"),
                    operation=preview.get("operation"),
                    action=preview.get("action"))}
    try:
        after = await client._fetch(endpoint, {"onlyData": "true"})
    except Exception:
        after = None
    if after is None or after == {} or after == {"items": []}:
        verification: Dict[str, Any] = {"status": "verified", "state": None,
                                        "note": "target absent after DELETE"}
    else:
        verification = {"status": "unverified", "state": None,
                        "note": "target still readable after DELETE"}
    _audit_result(audit, preview, mode, confirmation_token, "committed", None)
    return {"executed": True, "transport": "DELETE",
            "resource": preview["resource"],
            "record_id": preview.get("record_id"),
            "verification": verification,
            "delete_result": delete_result}
    if created_id is None:
        _audit_result(audit, preview, mode, confirmation_token, "committed", None)
        return {"executed": True, "transport": "POST",
                "resource": preview["resource"], "record_id": None,
                "verification": {"status": "unverified", "state": None,
                                "note": "POST succeeded but no created ID returned"},
                "post_result": post_result}
    try:
        after = await client._fetch(
            "%s/%s" % (preview["resource"], created_id),
            {"onlyData": "true"})
        observed = {k: after.get(k) for k in body.keys() if k in after}
        mismatched = [k for k, v in body.items() if k in after and after.get(k) != v]
        if mismatched:
            verification: Dict[str, Any] = {
                "status": "unverified", "state": observed,
                "note": "after-values differ for: %s" % ",".join(mismatched)}
        else:
            verification = {"status": "verified", "state": observed,
                            "new_id": created_id}
    except Exception as exc:
        verification = {"status": "unverified", "state": None,
                        "note": "POST succeeded but verification read failed: %s"
                                % str(exc)[:200]}
    _audit_result(audit, preview, mode, confirmation_token, "committed", None,
                  version=created_id)
    return {"executed": True, "transport": "POST",
            "resource": preview["resource"], "record_id": created_id,
            "verification": verification,
            "post_result": post_result}


def _extract_line_states(lines: Any) -> Any:
    """Normalize Oracle expand shapes ({lines: [...]} or {lines: {items}})."""
    if isinstance(lines, dict):
        lines = lines.get("items", [])
    if not isinstance(lines, list):
        return "unavailable"
    return [{"LineNumber": row.get("LineNumber"),
             "Status": row.get("Status"),
             "StatusCode": row.get("StatusCode")} for row in lines
            if isinstance(row, dict)]


async def _extract_schedule_states(client: Any, preview: Dict[str, Any],
                                   line_states: Any) -> Any:
    """Fetch per-line schedules via the metadata-advertised child template.

    Uses only the ``.../child/lines/{id}/child/schedules`` pattern Oracle
    advertises in the lines item links. A line without an observable key, or
    any failed child read, yields "unavailable" for that line — schedule
    detail never gates the header verdict, only enriches the report.
    """
    if not isinstance(line_states, list):
        return "unavailable"
    try:
        header_id = str(preview["record_id"])
    except Exception:
        return "unavailable"
    out = []
    for row in line_states:
        if not isinstance(row, dict):
            continue
        line_id = row.get("POLineId", row.get("LineId"))
        entry: Dict[str, Any] = {"LineNumber": row.get("LineNumber")}
        if line_id is None:
            entry["schedules"] = "unavailable"
            out.append(entry)
            continue
        try:
            data = await client._fetch(
                "purchaseOrders/%s/child/lines/%s/child/schedules"
                % (header_id, line_id),
                {"limit": "25", "onlyData": "true"})
            items = data.get("items", []) if isinstance(data, dict) else []
            entry["schedules"] = [
                {"ScheduleNumber": s.get("ScheduleNumber"),
                 "Status": s.get("Status"),
                 "StatusCode": s.get("StatusCode")}
                for s in items if isinstance(s, dict)]
        except Exception:
            entry["schedules"] = "unavailable"
        out.append(entry)
    return out


async def _execute_hcm(client: Any, preview: Dict[str, Any], audit: AuditSink,
                       mode: str, confirmation_token: Any) -> Dict[str, Any]:
    """Generic HCM dispatch on the resolved operation kind."""
    operation = preview.get("operation")
    if ("HCM", preview.get("scope"), operation) not in EXECUTABLE_SHAPES:
        raise ControlledActionError(
            "no HCM executor for scope %r operation %r"
            % (preview.get("scope"), operation))
    if operation == "create":
        return await _execute_hcm_create(client, preview, audit, mode,
                                         confirmation_token)
    if operation == "delete":
        return await _execute_hcm_delete(client, preview, audit, mode,
                                         confirmation_token)
    if operation == "update":
        return await _execute_hcm_update(client, preview, audit, mode,
                                         confirmation_token)
    raise ControlledActionError(
        "no HCM executor for operation %r" % (operation,))


async def _execute_hcm_update(client: Any, preview: Dict[str, Any],
                              audit: AuditSink, mode: str,
                              confirmation_token: Any) -> Dict[str, Any]:
    payload = dict(preview.get("parameters") or {})
    payload["objectVersionNumber"] = preview["version"]
    try:
        patch_result = await client.update(
            preview["resource"], str(preview["record_id"]), payload,
            redact=False, retries=0)
    except Exception as exc:
        _audit_result(audit, preview, mode, confirmation_token, "failed", exc)
        return {"executed": False, "transport": "PATCH",
                "resource": preview["resource"], "record_id": preview["record_id"],
                "verification": {"status": "failed", "state": None},
                "error": str(exc)[:300],
                "field_report": classify_write_error(
                    str(exc), system=preview.get("system"),
                    resource=preview.get("resource"),
                    scope=preview.get("scope"),
                    operation=preview.get("operation"),
                    action=preview.get("action"))}
    try:
        after = await client.get_record(
            preview["resource"], str(preview["record_id"]), redact=False)
        mismatched = [k for k, v in (preview.get("parameters") or {}).items()
                      if after.get(k) != v]
        new_version = after.get("objectVersionNumber",
                                after.get("ObjectVersionNumber"))
        observed = {k: after.get(k) for k in (preview.get("parameters") or {})
                    if k in after}
        if mismatched:
            verification: Dict[str, Any] = {
                "status": "unverified", "state": None,
                "note": "after-values differ for: %s" % ",".join(mismatched)}
        else:
            verification = {"status": "verified",
                            "state": observed,
                            "new_version": new_version}
    except Exception as exc:
        verification = {"status": "unverified", "state": None,
                        "note": "PATCH accepted but re-read failed: %s" % str(exc)[:200]}
    _audit_result(audit, preview, mode, confirmation_token, "committed", None,
                  version=verification.get("new_version", preview.get("version")))
    return {"executed": True, "transport": "PATCH",
            "resource": preview["resource"], "record_id": preview["record_id"],
            "verification": verification}


_HCM_CREATE_KEY_CANDIDATES = ("personId", "PersonId",
                              "personAbsenceEntryId", "PersonAbsenceEntryId",
                              "id", "Id")


async def _execute_hcm_create(client: Any, preview: Dict[str, Any],
                              audit: AuditSink, mode: str,
                              confirmation_token: Any) -> Dict[str, Any]:
    """Execute exactly one HCM collection POST, then GET-verify the echo."""
    body = dict(preview.get("parameters") or {})
    try:
        post_result = await client.create(
            preview["resource"], body, redact=False)
    except Exception as exc:
        _audit_result(audit, preview, mode, confirmation_token, "failed", exc)
        return {"executed": False, "transport": "POST",
                "resource": preview["resource"], "record_id": None,
                "verification": {"status": "failed", "state": None},
                "error": str(exc)[:300],
                "field_report": classify_write_error(
                    str(exc), system=preview.get("system"),
                    resource=preview.get("resource"),
                    scope=preview.get("scope"),
                    operation=preview.get("operation"),
                    action=preview.get("action"))}
    created_key = _discover_created_id(
        post_result if isinstance(post_result, dict) else {},
        preview.get("primary_key"))
    if created_key is None and isinstance(post_result, dict):
        for key in _HCM_CREATE_KEY_CANDIDATES:
            if post_result.get(key) not in (None, ""):
                created_key = post_result.get(key)
                break
    if created_key is None:
        _audit_result(audit, preview, mode, confirmation_token, "committed", None)
        return {"executed": True, "transport": "POST",
                "resource": preview["resource"], "record_id": None,
                "verification": {"status": "unverified", "state": None,
                                 "note": "POST succeeded but no created key returned"},
                "post_result": post_result}
    try:
        after = await client.get_record(
            preview["resource"], str(created_key), redact=False)
        if not isinstance(after, dict):
            raise ControlledActionError("verification read returned bad shape")
        mismatched = [k for k, v in body.items()
                      if k in after and after.get(k) != v]
        observed = {k: after.get(k) for k in body.keys() if k in after}
        if mismatched:
            verification = {"status": "unverified", "state": observed,
                            "note": "after-values differ for: %s"
                                    % ",".join(mismatched)}
        else:
            verification = {"status": "verified", "state": observed,
                            "new_id": created_key}
    except Exception as exc:
        verification = {"status": "unverified", "state": None,
                        "note": "POST succeeded but verification read failed: %s"
                                % str(exc)[:200]}
    _audit_result(audit, preview, mode, confirmation_token, "committed", None)
    return {"executed": True, "transport": "POST",
            "resource": preview["resource"], "record_id": created_key,
            "verification": verification,
            "post_result": post_result}


async def _execute_hcm_delete(client: Any, preview: Dict[str, Any],
                              audit: AuditSink, mode: str,
                              confirmation_token: Any) -> Dict[str, Any]:
    """Execute exactly one HCM DELETE, then verify absence."""
    try:
        delete_result = await client.delete(
            preview["resource"], str(preview["record_id"]), redact=False)
    except Exception as exc:
        _audit_result(audit, preview, mode, confirmation_token, "failed", exc)
        return {"executed": False, "transport": "DELETE",
                "resource": preview["resource"],
                "record_id": preview["record_id"],
                "verification": {"status": "failed", "state": None},
                "error": str(exc)[:300],
                "field_report": classify_write_error(
                    str(exc), system=preview.get("system"),
                    resource=preview.get("resource"),
                    scope=preview.get("scope"),
                    operation=preview.get("operation"),
                    action=preview.get("action"))}
    try:
        after = await client.get_record(
            preview["resource"], str(preview["record_id"]), redact=False)
    except Exception:
        after = None
    if after is None or after == {}:
        verification: Dict[str, Any] = {"status": "verified", "state": None,
                                        "note": "target absent after DELETE"}
    else:
        verification = {"status": "unverified", "state": None,
                        "note": "target still readable after DELETE"}
    _audit_result(audit, preview, mode, confirmation_token, "committed", None)
    return {"executed": True, "transport": "DELETE",
            "resource": preview["resource"],
            "record_id": preview["record_id"],
            "verification": verification,
            "delete_result": delete_result}


def _audit_result(audit: AuditSink, preview: Dict[str, Any], mode: str,
                  confirmation_token: Any, result: str, error: Any,
                  version: Any = None) -> None:
    audit.emit(build_event(
        "execute", resource=str(preview.get("resource", "")),
        scope=str(preview.get("scope", "")),
        action=str(preview.get("action", preview.get("operation", ""))),
        record_id=str(preview.get("record_id", "")),
        parameters=preview.get("parameters"), mode=mode,
        result=result, confirmation="consumed",
        confirmation_token=confirmation_token,
        version=version if version is not None else preview.get("version"),
        error=error))

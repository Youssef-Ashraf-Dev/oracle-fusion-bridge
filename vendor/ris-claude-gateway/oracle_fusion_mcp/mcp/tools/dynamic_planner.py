"""Dynamic controlled-write planner (internal only — NEVER an MCP tool).

Given a structured write *request* plus live metadata objects, this module
resolves the operation (method + endpoint identity) FROM the metadata,
validates fields/parameters strictly, classifies risk, and emits a preview
with an execution-eligibility verdict. It never performs I/O and never
executes anything.

Hard rules (tested):
- The caller supplies system/resource/scope/operation/record/fields only.
  There is deliberately no method/endpoint/url parameter anywhere in this
  module; method and path are resolved from metadata or fixed client
  contracts and re-derived (never trusted) at execution time.
- Unresolvable metadata -> NOT_RESOLVED. Structurally undispatched shape ->
  UNSUPPORTED. HIGH or UNKNOWN risk -> NOT_ELIGIBLE_FOR_DYNAMIC_EXECUTION.
- Only LOW (or MEDIUM with a verification strategy) + dispatchable +
  fully-resolved operations become ELIGIBLE_FOR_ONE_TIME_AUTHORIZATION,
  and even then execution requires the existing confirmation + executor
  path, which this module does not implement.
"""

import re
import uuid
from typing import Any, Dict, List, Optional

from .endpoint_discovery import _param_schema as _discover_param_schema
from .endpoint_discovery import canonical_fingerprint, describe_fingerprint
from .policy import get_table_entry as _policy_entry

VALID_SYSTEMS = ("HCM", "FSCM")
VALID_SCOPES = ("collection", "item", "child", "child_item")
VALID_OPERATIONS = ("create", "update", "delete", "business_action", "upsert")

ELIGIBLE = "ELIGIBLE_FOR_ONE_TIME_AUTHORIZATION"
HIGH_RISK_ELIGIBLE = "HIGH_RISK_ELIGIBLE"
NOT_ELIGIBLE = "NOT_ELIGIBLE"
NOT_RESOLVED = "NOT_RESOLVED"
NOT_READY = "NOT_READY"
UNSUPPORTED = "UNSUPPORTED"

CRUD = frozenset({"get", "create", "upsert", "update", "delete"})

_RECORD_ID_RE = re.compile(r"^[0-9]{1,32}$")

# Child record keys may be opaque alphanumeric tokens assigned by Fusion
# (e.g. invoice line keys) rather than numeric ids. Parent record ids
# stay strictly numeric; child keys admit letters, digits, hyphen and
# underscore only — no slashes, dots or whitespace — so a key cannot
# escape its path slot when bound into .../child/<name>/{key}.
_CHILD_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

# --- concrete risk policy (curated sets, not name inference) -----------------
# Membership below is a deliberate policy decision documented per entry.
# Anything not matched here falls through to MEDIUM (ordinary workflow) or
# UNKNOWN (insufficient metadata) — never to LOW by default.
HIGH_OPERATIONS = frozenset({"delete"})

# HCM resources whose rows are employment identity, compensation, payroll,
# or benefit-money records: any write is HIGH regardless of fields.
HIGH_HCM_RESOURCES = frozenset({
    "workers",               # employment identity / termination surface
    "salaries",              # compensation amounts
    "individualCompensations",
    "payrollFlows",          # payroll processing
    "payrollRelationships",
    "workforceCompensationPlans",
    "benefitEnrollments",    # benefit money records
})

# Named business actions with destructive, financial, or irreversible effect.
HIGH_ACTIONS = frozenset({
    # employment lifecycle
    "terminate", "reverseTermination", "cancelWorkRelationship",
    "changeLegalEmployer", "correctTermination",
    # destructive FSCM lifecycle
    "cancel", "cancelInvoice", "cancelLine",
    # money-moving / authorization flows
    "applyPrepayments", "unapplyPrepayments", "reverseDistributions",
    "initiateEscheat", "initiateStopPayment",
    "submitSpendAuthorizationRequest", "submitSpendAuthorizationRequestWithSites",
    "resubmitSpendAuthorizationRequest",
})

# Fields whose update is evidenced LOW-risk (pilot): free-text comments on an
# absence carry no workflow, payroll, balance, or approval semantics.
LOW_HCM_UPDATE_FIELDS = frozenset({
    ("absences", "comments"),
})

# Variant-selector parameters: the parameter chooses a sub-operation rather
# than carrying a free-form value, the metadata marks it non-mandatory with
# no default, and the valid values are curated from Oracle's documented
# action semantics because /describe carries no enum/default metadata for
# them. An omitted variant is NEVER defaulted — the planner demands it
# explicitly. Curated (not inferred): inventing values from descriptions
# with NLP would be guessing at user intent.
FSCM_VARIANT_PARAMS = {
    # purchaseOrders close: close | closeForReceiving | closeForInvoicing |
    # finallyClose (per the action description; finallyClose is blocked below).
    ("purchaseOrders", "close", "closeAction"): {
        "values": ("close", "closeForReceiving", "closeForInvoicing",
                   "finallyClose"),
    },
}

# (resource, action, param, value) combinations that must never execute
# through the dynamic framework. finallyClose liquidates reserved funds and
# requires additional reopen data the framework does not model.
FSCM_HIGH_RISK_ACTION_VALUES = frozenset({
    ("purchaseOrders", "close", "closeAction", "finallyClose"),
})

# Collection-scope business-action verification contracts (P4): the
# action endpoint carries no record segment, so the verification target
# must be derived from caller-supplied identifying parameters. Each entry
# names the id parameter(s) that locate the target plus a collection
# filter field and the state fields that evidence the action outcome.
# Actions without an entry resolve but stay NOT_READY (no verification
# strategy) — verification-gated universality.
FSCM_COLLECTION_ACTION_VERIFY: Dict[Any, Dict[str, Any]] = {
    ("invoices", "cancelInvoice"): {
        "id_params": ("InvoiceNumber",),
        "query_field": "InvoiceNumber",
        "observe": ("CanceledFlag", "CanceledDate", "CanceledBy"),
    },
    ("invoices", "validateInvoice"): {
        "id_params": ("InvoiceNumber",),
        "query_field": "InvoiceNumber",
        "observe": ("ValidationStatus", "ExceptionCode", "Exception"),
    },
    ("invoices", "cancelLine"): {
        "id_params": ("InvoiceNumber",),
        "query_field": "InvoiceNumber",
        "observe": ("CanceledFlag", "CanceledDate", "CanceledBy"),
    },
}
# Item-scope business-action observation contracts: header fields the
# executor re-reads after the POST. Actions without an entry use the
# generic state report; actions whose outcome lives in child collections
# (e.g. generateDistributions) get a dedicated executor proof hook, so
# the header observe list stays free of manufactured confidence.
FSCM_ITEM_ACTION_VERIFY: Dict[Any, Dict[str, Any]] = {
    ("invoices", "validateInvoice"): {
        "observe": ("ValidationStatus", "ExceptionCode", "Exception"),
    },
    # PO freeze/unfreeze: FrozenFlag is the authoritative freeze-state
    # field (read-only in describe; associated with freeze/unfreeze
    # state, unlike Status/StatusCode which track document lifecycle).
    # Observe-only: no serialized value is asserted here — the executor
    # re-reads and reports, the human confirms the business outcome.
    ("purchaseOrders", "freeze"): {
        "observe": ("FrozenFlag",),
    },
    ("purchaseOrders", "unfreeze"): {
        "observe": ("FrozenFlag",),
    },
}
# FSCM business actions with established MEDIUM handling: each has live
# execution or recorded-verification evidence behind its current risk
# (submit/close/reopen submit flows, validateInvoice, generateDistributions
# with its distributions proof hook) or a deliberately preserved output
# (hold/removeHold stay MEDIUM + NOT_READY — the refusal carries the
# safety, not the risk level). This is a risk override, NOT a capability
# allowlist: any (resource, action) absent here still resolves from live
# metadata and executes under the generic unknown-mutation rule below
# (HIGH + explicit confirmation + acknowledged-unverified semantics).
# Never add entries speculatively; entries require live evidence.
FSCM_ESTABLISHED_MEDIUM_ACTIONS: frozenset = frozenset({
    ("purchaseOrders", "submit"),
    ("purchaseOrders", "close"),
    ("purchaseOrders", "reopen"),
    ("purchaseOrders", "hold"),
    ("purchaseOrders", "removeHold"),
    ("draftPurchaseOrders", "submit"),
    ("draftPurchaseOrders", "hold"),
    ("draftPurchaseOrders", "removeHold"),
    ("invoices", "validateInvoice"),
    ("invoices", "generateDistributions"),
})
# FSCM (resource, action) pairs whose post-action state is evidenced to
# live in lifecycle Status/StatusCode (submit flows, close/reopen).
# Only these pairs receive the generic Status observation; any other
# action — even on the same resource — gets no Status inference, so an
# unknown action can never inherit Status as proof of success.
FSCM_STATUS_OBSERVED_ACTIONS: frozenset = frozenset({
    ("purchaseOrders", "submit"),
    ("purchaseOrders", "close"),
    ("purchaseOrders", "reopen"),
    ("draftPurchaseOrders", "submit"),
})

# Evidence-backed verification contracts (P3).
#
# A contract ENHANCES verification for an already dynamically discoverable
# action. It is NOT capability membership: absence of a contract never
# blocks execution — it yields acknowledged_unverified (transport accepted,
# no semantic predicate). Contracts are behavior upgrades, never gates.
#
# Entry shape:
#   "observe":    fields re-read after the action (evidence, not proof)
#   "predicate":  optional {"field": F, "equals": V} or {"field": F,
#                 "in": (...)} — the authoritative postcondition. Evaluated
#                 against the re-read state; pass -> verified, fail/missing
#                 -> unverified (outcome indeterminate).
#   "hook":       optional executor hook name for complex verification
#                 (e.g. roll-down checks a header field cannot express).
#   "evidence":   source of the predicate's authority. Every predicate must
#                 cite live tenant metadata, an authoritative API response,
#                 a deterministic recorded capture in-repo, or Oracle docs
#                 CONFIRMED against live metadata. Field-name inference is
#                 never sufficient; a missing predicate is left absent.
#
# Outcome vocabulary (machine-readable, verification.status):
#   "verified"                — authoritative postcondition evaluated & passed
#   "acknowledged_unverified" — transport accepted; no semantic predicate
#   "failed"                  — transport/API failure
#   "unverified"              — outcome cannot be reliably established
#                               (re-read failed, or predicate not met)
VERIFICATION_CONTRACTS: Dict[Any, Dict[str, Any]] = {
    ("purchaseOrders", "close"): {
        "observe": ("Status", "StatusCode"),
        "hook": "po_close",
        "evidence": "executor hook asserts StatusCode == 'CLOSED' "
                    "(documented close transition; live-observed)",
    },
    ("invoices", "generateDistributions"): {
        "observe": ("InvoiceId", "InvoiceNumber", "InvoiceAmount",
                    "InvoiceCurrency", "SupplierNumber"),
        "hook": "invoice_distributions",
        "evidence": "executor hook asserts every line carries a "
                    "distribution with account coding",
    },
    ("invoices", "cancelInvoice"): {
        "observe": ("CanceledFlag", "CanceledDate", "CanceledBy"),
        "predicate": {"field": "CanceledFlag",
                      "in": ("Y", "true", "True", True)},
        "evidence": "recorded response captures show CanceledFlag 'Y' "
                    "after cancel (P4 live verification + executor "
                    "fixtures)",
    },
    ("invoices", "cancelLine"): {
        "observe": ("CanceledFlag", "CanceledDate", "CanceledBy"),
        "predicate": {"field": "CanceledFlag",
                      "in": ("Y", "true", "True", True)},
        "evidence": "same recorded captures as cancelInvoice",
    },
    # Freeze/unfreeze: the observe field is evidenced (describe attribute),
    # but the SERIALIZED TRUTH VALUE is not established by any repo artifact
    # or recorded capture. The predicate is intentionally absent — inventing
    # a value (true/Y/"FROZEN") is forbidden. Once live evidence arrives,
    # add {"predicate": {"field": "FrozenFlag", "equals": <evidenced>}}.
    ("purchaseOrders", "freeze"): {
        "observe": ("FrozenFlag",),
        "predicate": None,
        "evidence": "field existence evidenced (describe attribute); "
                    "serialized truth value UNKNOWN — predicate absent "
                    "by design",
    },
    ("purchaseOrders", "unfreeze"): {
        "observe": ("FrozenFlag",),
        "predicate": None,
        "evidence": "same as freeze",
    },
}


def contract_for(system: str, resource: Any, scope: Any,
                 action: Any) -> Optional[Dict[str, Any]]:
    """Verification contract for one operation, or None.

    None means: no specialized verification — the action stays fully
    executable through the normal pipeline and reports
    acknowledged_unverified. Never a refusal, never a capability judgment.
    """
    if system != "FSCM":
        return None
    if not isinstance(resource, str) or not isinstance(action, str):
        return None
    return VERIFICATION_CONTRACTS.get((resource, action))


# HCM collection-create operations with unknown server-generated semantics.
# A mandatory-but-not-updatable HCM create field cannot be safely resolved
# to a caller input, so collection creates stay NOT_RESOLVED unless an
# explicit override entry exists. Empty by default: structure for future
# verified business rules, mirroring the FSCM override tables.
HCM_CREATE_CALLER_INPUT_OVERRIDES: Dict[Any, frozenset] = {
}

# Server-owned HCM create fields: Fusion /describe marks these mandatory
# but non-updatable, yet they are set by the server itself (creation
# timestamps, derived worker type) and must never be caller-supplied.
# Verified live against the emps resource 2026-10-01: attempting an emps
# create is otherwise unresolvable because CreationDate is listed first.
# They are skipped by the missing-mandatory check and rejected when
# supplied — mirroring FSCM server-generated semantics. Narrowly scoped
# to the exact (system, resource, operation) tuple; every other
# mandatory-but-not-updatable field still fails closed.
HCM_SERVER_OWNED_CREATE_FIELDS: Dict[Any, frozenset] = {
    ("HCM", "emps", "create"): frozenset({
        "CreationDate",
        "LastUpdateDate",
        "WorkerType",
    }),
    # Workers hire: PersonId is Fusion's server-assigned surrogate key.
    # Live /describe marks it mandatory (every record has one), which made
    # both the planner and the ask-first requirements demand it from the
    # caller. Oracle's own Create-a-worker REST reference lists an EMPTY
    # root required-fields set and takes identity from embedded
    # names/workRelationships; PersonId is documented, never required.
    # Demanding it was a validator error, confirmed against the public
    # reference 2026-10-06. Never caller-supplied on create (updates
    # address records by record_id, and the created key is discovered
    # from the POST response).
    ("HCM", "workers", "create"): frozenset({
        "PersonId",
    }),
}

# Audit columns are server-owned on every HCM resource ever observed
# (creation/writer stamps), mirroring FSCM _SERVER_GENERATED. They are
# never caller inputs regardless of their mandatory flag — no per-resource
# entry needed. Domain-owned locked fields (e.g. WorkerType) still need
# explicit HCM_SERVER_OWNED_CREATE_FIELDS entries.
HCM_AUDIT_COLUMNS = frozenset({
    "CreationDate", "CreatedBy", "LastUpdateDate", "LastUpdatedBy",
})
# HCM fields that bypass business workflow when written directly (e.g.
# setting an approval status without going through the approval action).
# Writing these is HIGH risk even though Oracle marks them updatable.
# (The old per-resource curated allowlist was retired: any
# metadata-updatable, non-workflow field now resolves, risk-classified
# normally.)
HCM_HIGH_RISK_FIELDS = frozenset({
    "approvalStatusCd",   # absence approval outcome — workflow-owned
    "absenceStatusCd",    # absence lifecycle state — workflow-owned
    "processingStatus",   # downstream processing state — workflow-owned
})

# FSCM collection-create operations that are financial transactions and must
# never become dynamically eligible, even with valid metadata.
FSCM_HIGH_RISK_CREATES = frozenset({
    "invoices",           # AP invoice creation: financial liability
})

# --- executor-dispatchable shapes (universal engine, P3) -------------------
# (system, scope, operation) triples the generic executor implements.
# Resource names appear NOWHERE here: dispatch is by operation kind, with
# verification and policy gates applied per resolution. Anything else is
# UNSUPPORTED (structurally outside the engine).
EXECUTABLE_SHAPES = frozenset({
    ("FSCM", "collection", "create"),
    ("FSCM", "collection", "upsert"),
    ("FSCM", "collection", "business_action"),
    ("FSCM", "item", "update"),
    ("FSCM", "item", "delete"),
    ("FSCM", "item", "business_action"),
    ("FSCM", "child", "create"),
    ("FSCM", "child", "upsert"),
    ("FSCM", "child", "business_action"),
    ("FSCM", "child_item", "update"),
    ("FSCM", "child_item", "delete"),
    ("FSCM", "child_item", "business_action"),
    ("HCM", "collection", "create"),
    ("HCM", "item", "update"),
    ("HCM", "item", "delete"),
})


def _dispatchable(system: str, scope: str, operation: str) -> bool:
    """True when the generic executor implements this operation kind."""
    return (system, scope, operation) in EXECUTABLE_SHAPES


class PlanError(ValueError):
    """Malformed request envelope (not a metadata-driven rejection)."""


def _fscm_scope_node(fscm_parsed: Any, scope: str) -> Any:
    """Select the parsed describe node resolution consumes (or None).

    Only top-level collection/item scopes resolve here; nested scopes
    are fingerprinted from the raw resource node instead.
    """
    if not isinstance(fscm_parsed, dict):
        return None
    if scope not in ("collection", "item"):
        return None
    node = (fscm_parsed.get("collection") if scope == "collection"
            else fscm_parsed.get("item"))
    return node if isinstance(node, dict) else None


def _metadata_fingerprint(system: str, resource: str, scope: str,
                           fscm_parsed: Any = None,
                           fscm_raw: Any = None,
                           hcm_attrs: Any = None,
                           hcm_children: Any = None) -> Optional[str]:
    """Fingerprint the exact metadata the resolution consumes.

    FSCM: the parsed scope node (deterministic from raw describe); falls
    back to the raw resource node when no parsed metadata is available.
    HCM: the attribute list plus any child metadata consumed for
    composite creates. Returns None when nothing usable is present
    (resolution then fails elsewhere — never silently unbound).
    """
    if system == "HCM":
        if hcm_attrs is None:
            return None
        obj = {"resource": resource, "attrs": hcm_attrs}
        if hcm_children is not None:
            obj["children"] = hcm_children
        return canonical_fingerprint(obj)
    node = _fscm_scope_node(fscm_parsed, scope)
    if node is not None:
        return canonical_fingerprint(
            {"resource": resource, "scope": scope, "node": node})
    return describe_fingerprint(fscm_raw, resource)


def _raw_child_def(fscm_raw: Any, resource: str,
                   child_path: str) -> Any:
    """Walk raw ``children`` segments to a nested child definition (or None).

    Same traversal guards as endpoint discovery: plain segment names only.
    """
    if not isinstance(fscm_raw, dict):
        return None
    resources = fscm_raw.get("Resources")
    if not isinstance(resources, dict):
        return None
    node = resources.get(resource)
    if not isinstance(node, dict):
        return None
    segments = [s for s in child_path.strip("/").split("/") if s and s != "child"]
    if not segments or any(s in ("..",) or "://" in s for s in segments):
        return None
    current: Any = node.get("children")
    child_def: Any = None
    for segment in segments:
        if not isinstance(current, dict) or segment not in current \
                or not isinstance(current[segment], dict):
            return None
        child_def = current[segment]
        current = child_def.get("children")
    return child_def


def _validate_update_fields(attr_list: Any, fields: Dict[str, Any],
                            server_generated: Any,
                            primary_key: Any) -> List[str]:
    """Validate a partial-update field set against attribute metadata.

    Update semantics differ from create: a field that can never change
    (``updatable`` false or ``allowChanges == "never"``) is non-settable —
    create-time caller-input overrides do NOT apply to updates. Mandatory
    presence is not required (PATCH is partial), but explicitly nulling a
    mandatory field is rejected.
    """
    reasons: List[str] = []
    by_name = {a.get("name"): a for a in (attr_list or [])
               if isinstance(a, dict) and a.get("name")}
    for key in fields:
        spec = by_name.get(key)
        if spec is None:
            reasons.append("unknown field: %r" % key)
            continue
        if key in (server_generated or set()) or key == primary_key:
            reasons.append("server-generated field rejected: %r" % key)
            continue
        if spec.get("updatable") is False or spec.get("allowChanges") == "never":
            reasons.append(
                "non-settable field rejected: %r "
                "(updatable=false or allowChanges=never)" % key)
            continue
        value = fields[key]
        if value is None:
            if spec.get("mandatory"):
                reasons.append("mandatory field %r must not be null" % key)
            continue
        if not _adf_type_matches(spec.get("type"), value):
            reasons.append("field %r expects %s, got %s"
                           % (key, spec.get("type"), type(value).__name__))
    return reasons


def _declared_from_attributes(attr_list: Any) -> List[Dict[str, Any]]:
    """Build display/binding parameter declarations from attributes."""
    declared = []
    for attr in attr_list or []:
        if not isinstance(attr, dict) or not attr.get("name"):
            continue
        declared.append({"name": attr["name"], "type": attr.get("type"),
                         "mandatory": False,
                         "updatable": attr.get("updatable")})
    return declared


def _adf_type_matches(declared_type: Any, value: Any) -> bool:
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


def _relative_template(href: Any) -> Optional[str]:
    if not isinstance(href, str) or not href:
        return None
    marker = "/resources/"
    if marker in href:
        _head, tail = href.split(marker, 1)
        _version, sep, rest = tail.partition("/")
        return rest if sep and rest else (tail or None)
    if "://" not in href and "action/" in href:
        return href.lstrip("/")
    return None


def _find_fscm_action(node: Any, action: str) -> Dict[str, Any]:
    """Resolve one FSCM business action from normalized describe entries.

    ``node`` is a ``parse_describe`` collection/item node whose ``actions``
    entries already fuse ``actions[]`` (method/params) with their
    ``rel=action`` link (URL template). Matching is exact on the action name.
    """
    actions = node.get("actions") if isinstance(node, dict) else None
    entry = next(
        (candidate for candidate in (actions or [])
         if isinstance(candidate, dict) and candidate.get("action") == action),
        None,
    )
    if entry is None:
        return {"resolved": False, "reason": "action %r not in actions[]" % action}
    href = entry.get("href")
    if not href:
        return {"resolved": False,
                "reason": "action %r has no rel=action link" % action}
    path = _relative_template(href)
    if not path:
        return {"resolved": False,
                "reason": "action %r link href is not a usable template" % action}
    method = entry.get("method")
    if not method:
        return {"resolved": False,
                "reason": "action %r declares no HTTP method" % action}
    return {"resolved": True, "entry": entry, "href": href, "path": path,
            "method": method}


def _over_value_cap(resource: str, fields: Dict[str, Any],
                    value_limits: Any) -> Optional[str]:
    """Return a rejection reason when a numeric field exceeds its cap.

    ``value_limits`` maps "Resource.Field" to a numeric maximum. Absent,
    malformed, or non-numeric entries never match. Caps are hard: exceeding
    one fails the plan as NOT_ELIGIBLE regardless of risk.
    """
    if not isinstance(value_limits, dict) or not value_limits:
        return None
    if not isinstance(fields, dict):
        return None
    for dotted, limit in value_limits.items():
        if not isinstance(dotted, str) or "." not in dotted:
            continue
        try:
            ceiling = float(limit)
        except (TypeError, ValueError):
            continue
        res, _, field = dotted.partition(".")
        if res != resource or field not in fields:
            continue
        value = fields[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > ceiling:
            return ("field %r value %s exceeds the configured limit %s "
                    "for %s: not eligible for dynamic execution"
                    % (field, value, ceiling, dotted))
    return None


def _operation_warnings(system: str, resource: str, scope: str,
                        operation: str, action: Optional[str],
                        verification: Any, risk: str) -> List[str]:
    """Human-facing warnings for the authorization prompt.

    These do not block anything — they make confirmation informed. Every
    warning describes a fact about the resolved operation, never advice.
    """
    warnings: List[str] = []
    if operation == "delete":
        warnings.append("IRREVERSIBLE: delete cannot be undone.")
    if action in HIGH_ACTIONS:
        warnings.append(
            "HIGH-impact action: confirm the business outcome, not just "
            "the parameters.")
    if risk == "HIGH" and operation != "delete" and action not in HIGH_ACTIONS:
        warnings.append("HIGH risk classification applies.")
    if isinstance(verification, dict) \
            and verification.get("strategy") == "get_after_action" \
            and not verification.get("observe") \
            and not (verification.get("target") or {}).get("query_field"):
        warnings.append(
            "LIMITED VERIFICATION: no action-specific success predicate "
            "exists; the target will be re-read after execution and its "
            "state reported without asserting the requested action's "
            "semantic postcondition.")
    return warnings


def _bind_path_ids(path: Any, scope: str, record_id: Any,
                   child_record_id: Any = None) -> Any:
    """Substitute record ids into endpoint-template placeholders.

    Top-level templates carry a single placeholder bound to ``record_id``
    (both ``{id}`` and ``{key}`` forms). Nested templates carry the parent
    slot first and the child slot second, per Fusion's own link templates
    (``.../{id}/child/<name>/{id}``): the first placeholder binds the
    parent, any remainder binds the child key (numeric or opaque token).
    Unbound placeholders are left intact and fail closed downstream.
    """
    if not isinstance(path, str) or not path:
        return path
    if scope in ("item", "child", "child_item") \
            and isinstance(record_id, (str, int)) \
            and _RECORD_ID_RE.match(str(record_id)):
        out = path
        for placeholder in ("{id}", "{key}"):
            if placeholder in out:
                out = out.replace(placeholder, str(record_id), 1)
                break
        if scope == "child_item" and child_record_id is not None:
            out = out.replace("{id}", str(child_record_id)).replace(
                "{key}", str(child_record_id))
        elif scope in ("collection", "item"):
            out = out.replace("{id}", str(record_id)).replace(
                "{key}", str(record_id))
        path = out
    return path


def _validate_params(declared: List[Dict[str, Any]],
                     supplied: Dict[str, Any]) -> List[str]:
    """Return a list of rejection reasons (empty == valid)."""
    reasons: List[str] = []
    by_name = {p.get("name"): p for p in declared if p.get("name")}
    for key in supplied:
        if key not in by_name:
            reasons.append("unknown parameter: %r" % key)
    for name, spec in by_name.items():
        if spec.get("mandatory") and name not in supplied:
            reasons.append("missing mandatory parameter: %s" % name)
        if spec.get("requires_explicit") and name not in supplied:
            reasons.append(
                "parameter %r declares allowed values %s with no default: "
                "must be explicitly specified"
                % (name, spec.get("allowed_values")))
    for name, value in supplied.items():
        spec = by_name.get(name)
        if spec is None:
            continue
        if value is None:
            # Null clears an optional field (restore path); mandatory
            # fields are already rejected above when absent, and an
            # explicit null for one is equally invalid.
            if spec.get("mandatory"):
                reasons.append("mandatory parameter %r must not be null" % name)
            continue
        if not _adf_type_matches(spec.get("type"), value):
            reasons.append("parameter %r expects %s, got %s"
                           % (name, spec.get("type"), type(value).__name__))
    return reasons


def _classify(system: str, resource: str, scope: str, operation: str,
              action: Optional[str], fields: Dict[str, Any]) -> str:
    """Return LOW / MEDIUM / HIGH / UNKNOWN per the curated policy above."""
    if operation == "delete" or operation in HIGH_OPERATIONS:
        return "HIGH"
    if action in HIGH_ACTIONS:
        return "HIGH"
    if system == "HCM" and resource in HIGH_HCM_RESOURCES:
        return "HIGH"
    if system == "FSCM":
        for pname, pvalue in fields.items():
            if (resource, action, pname, pvalue) in FSCM_HIGH_RISK_ACTION_VALUES:
                # Explicitly blocked variant values (e.g. finallyClose).
                return "HIGH"
    if system == "HCM" and operation == "update" \
            and any(name in HCM_HIGH_RISK_FIELDS for name in fields):
        # Workflow-controlled fields (approval/lifecycle status) must never
        # be written by field-level PATCH, even when metadata says updatable.
        return "HIGH"
    if system == "HCM" and operation == "create" \
            and any(isinstance(v, (list, dict)) for v in fields.values()):
        # Composite create (e.g. hire with embedded names/work
        # relationships): one POST creates multiple records atomically.
        # Explicit-confirmation path, never routine.
        return "HIGH"
    if system == "FSCM" and operation in ("create", "upsert") \
            and resource in FSCM_HIGH_RISK_CREATES:
        # Financial-transaction creates stay blocked regardless of metadata.
        return "HIGH"
    if operation == "update" and system == "HCM":
        if all((resource, name) in LOW_HCM_UPDATE_FIELDS for name in fields):
            return "LOW"
        return "MEDIUM"
    if operation == "business_action":
        return "MEDIUM"
    if operation == "create":
        return "MEDIUM"
    if operation == "upsert":
        return "MEDIUM"
    if operation == "update" and system == "FSCM":
        return "MEDIUM"
    return "UNKNOWN"


def _verification_strategy(system: str, resource: str, scope: str,
                           operation: str, action: Optional[str],
                           fields: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Return a verification contract, or None when none can be stated.

    Verification always observes the ACTUAL proposed fields (plus the
    concurrency carrier where applicable) — never a fixed unrelated field.
    """
    if system == "FSCM" and scope == "item" and operation == "business_action":
        if action in ("hold", "removeHold"):
            # Genuinely unverifiable: no authoritative hold-state field
            # exists in GET responses, so observing Status would
            # manufacture false confidence.
            return None
        contract = _policy_entry(
            "FSCM_ITEM_ACTION_VERIFY", (resource, action),
            FSCM_ITEM_ACTION_VERIFY.get((resource, action)))
        if isinstance(contract, dict) and contract.get("observe"):
            # Explicit per-action observation takes precedence over the
            # resource default below (e.g. PO freeze/unfreeze observe
            # FrozenFlag, not lifecycle Status).
            return {"strategy": "get_after_action",
                    "observe": list(contract["observe"])}
        if resource in ("purchaseOrders", "draftPurchaseOrders") \
                and (resource, action) in FSCM_STATUS_OBSERVED_ACTIONS:
            return {"strategy": "get_after_action",
                    "observe": ["Status", "StatusCode"]}
        # Generic rule (hybrid model): any other item action verifies by
        # state report — the executor observes the target record and
        # reports it for review without asserting postconditions.
        return {"strategy": "get_after_action", "observe": []}
    if system == "FSCM" and scope == "collection" \
            and operation == "business_action":
        contract = _policy_entry(
            "FSCM_COLLECTION_ACTION_VERIFY", (resource, action),
            FSCM_COLLECTION_ACTION_VERIFY.get((resource, action)))
        if contract is None:
            # Generic rule: unlisted collection actions verify at
            # acceptance level only — no derivable target exists, so the
            # executor reports the POST outcome without an independent
            # target read, explicitly labeled limited.
            return {"strategy": "get_after_action", "observe": [],
                    "target": None}
        # The identifying parameter must be supplied, or no verification
        # target can be derived.
        id_params = [p for p in contract.get("id_params", ())
                     if (fields or {}).get(p) not in (None, "")]
        if not id_params:
            return None
        return {"strategy": "get_after_action",
                "observe": list(contract.get("observe", ())),
                "target": {"query_field": contract.get("query_field"),
                           "id_param": id_params[0],
                           "id_value": (fields or {})[id_params[0]]}}
    if system == "FSCM" and scope == "collection" \
            and operation in ("create", "upsert"):
        if resource == "invoices":
            return {"strategy": "get_after_create",
                    "observe": ["InvoiceId", "InvoiceNumber", "InvoiceAmount",
                                "InvoiceCurrency", "SupplierNumber"]}
        if resource == "purchaseOrders":
            return {"strategy": "get_after_create",
                    "observe": ["POHeaderId", "OrderNumber", "Status",
                                "StatusCode"]}
        # Universal CRUD rule (hybrid model): any other collection create
        # verifies by echo of the supplied fields — exactly what the
        # generic executor checks. The fixed lists above are grandfathered
        # pinned behavior, not exceptions. Empty field sets verify nothing
        # and stay NOT_READY. Business-action rules below are untouched:
        # universality stops at CRUD.
        if not fields:
            return None
        return {"strategy": "get_after_create",
                "observe": sorted(fields.keys())}
    if system == "FSCM" and scope == "item" and operation == "update":
        return {"strategy": "get_equality",
                "observe": sorted((fields or {}).keys())}
    if system == "FSCM" and scope == "item" and operation == "delete":
        return {"strategy": "absence_check", "observe": ["record_id"]}
    if system == "FSCM" and scope in ("child", "child_item"):
        if operation in ("create", "upsert"):
            if not fields:
                return None
            return {"strategy": "get_after_create",
                    "observe": sorted((fields or {}).keys())}
        if operation == "update":
            return {"strategy": "get_equality",
                    "observe": sorted((fields or {}).keys())}
        if operation == "delete":
            return {"strategy": "absence_check", "observe": ["record_id"]}
        if operation == "business_action":
            return {"strategy": "get_after_action", "observe": []}
        return None
    if system == "HCM" and operation == "update":
        observe = sorted((fields or {}).keys()) + ["objectVersionNumber"]
        return {"strategy": "get_equality", "observe": observe}
    if system == "HCM" and operation == "create":
        return {"strategy": "get_after_create",
                "observe": sorted((fields or {}).keys())}
    if system == "HCM" and operation == "delete":
        return {"strategy": "absence_check",
                "observe": ["objectVersionNumber"]}
    return None


def plan_dynamic_write(*, system: str, resource: str, scope: str,
                       operation: str, record_id: Any = None,
                       fields: Optional[Dict[str, Any]] = None,
                       action: Optional[str] = None,
                       fscm_parsed: Optional[Dict[str, Any]] = None,
                       fscm_raw: Optional[Any] = None,
                       hcm_attrs: Optional[Any] = None,
                       hcm_children: Optional[Any] = None,
                       version: Any = None,
                       api_version: Any = None,
                       child_path: Any = None,
                       child_record_id: Any = None,
                       value_limits: Any = None) -> Dict[str, Any]:
    """Resolve + validate + classify a requested write. Never executes.

    ``fscm_parsed`` is ``parse_describe`` output; ``hcm_attrs`` is an
    attribute list of ``{name, type, updatable, mandatory}``.
    ``hcm_children`` optionally maps child collection name to the same
    attribute-list shape, enabling composite creates (e.g. hire with
    embedded names/work relationships); list/dict field values without
    a matching entry stay rejected as unknown fields. Metadata
    objects are inputs — this function performs no I/O. ``api_version``
    is carried through to the preview for confirmation binding (the
    caller supplies the deployment version; href-embedded versions take
    precedence where discovery extracts them).
    """
    if system not in VALID_SYSTEMS:
        raise PlanError("system must be one of %s" % (VALID_SYSTEMS,))
    if scope not in VALID_SCOPES:
        raise PlanError("scope must be one of %s" % (VALID_SCOPES,))
    if operation not in VALID_OPERATIONS:
        raise PlanError("operation must be one of %s" % (VALID_OPERATIONS,))
    for label, value in (("resource", resource), ("scope", scope)):
        if not isinstance(value, str) or not value.strip() or value.strip().startswith("/"):
            raise PlanError("%s must be a plain relative name" % label)
        if ".." in value or "://" in value:
            raise PlanError("%s must be a plain relative name" % label)
    fields = dict(fields or {})
    warnings: List[str] = []
    reasons: List[str] = []
    template_source: Any = None
    fingerprint = _metadata_fingerprint(
        system, resource, scope, fscm_parsed=fscm_parsed,
        fscm_raw=fscm_raw, hcm_attrs=hcm_attrs,
        hcm_children=hcm_children)

    if scope == "item":
        if not isinstance(record_id, (str, int)) \
                or not _RECORD_ID_RE.match(str(record_id)):
            return _preview(system, resource, scope, operation, record_id,
                            fields, action, None, None, "UNKNOWN", NOT_RESOLVED,
                            ["record ID is required and must be numeric"],
                            None, version, warnings,
                            api_version=api_version,
                            metadata_fingerprint=fingerprint,
                            template_source=template_source,
                            child_path=child_path,
                            child_record_id=child_record_id)

    if scope in ("child", "child_item"):
        # Child scopes address a nested collection under a parent record:
        # the parent key is required; child_item additionally requires the
        # nested record key. child_path names the nested collection.
        if not isinstance(record_id, (str, int)) \
                or not _RECORD_ID_RE.match(str(record_id)):
            return _preview(system, resource, scope, operation, record_id,
                            fields, action, None, None, "UNKNOWN", NOT_RESOLVED,
                            ["parent record ID is required and must be numeric"],
                            None, version, warnings,
                            api_version=api_version,
                            metadata_fingerprint=fingerprint,
                            template_source=template_source,
                            child_path=child_path,
                            child_record_id=child_record_id)
        if not isinstance(child_path, str) or not child_path.strip() \
                or child_path.strip().startswith("/"):
            return _preview(system, resource, scope, operation, record_id,
                            fields, action, None, None, "UNKNOWN", NOT_RESOLVED,
                            ["child scope requires a child_path like 'lines'"],
                            None, version, warnings,
                            api_version=api_version,
                            metadata_fingerprint=fingerprint,
                            template_source=template_source,
                            child_path=child_path,
                            child_record_id=child_record_id)
        if scope == "child_item" and (
                not isinstance(child_record_id, (str, int))
                or not _CHILD_KEY_RE.match(str(child_record_id))):
            return _preview(system, resource, scope, operation, record_id,
                            fields, action, None, None, "UNKNOWN", NOT_RESOLVED,
                            ["child record key is required (numeric id or "
                             "opaque token; no slashes)"],
                            None, version, warnings,
                            api_version=api_version,
                            metadata_fingerprint=fingerprint,
                            template_source=template_source,
                            child_path=child_path,
                            child_record_id=child_record_id)

    early_risk = _classify(system, resource, scope, operation, action, fields)

    if system == "FSCM":
        resolved = _resolve_fscm(fscm_parsed, resource, scope, operation,
                                 record_id, fields, action, reasons, warnings,
                                 raw_describe=fscm_raw, system=system,
                                 child_path=child_path,
                                 child_record_id=child_record_id)
    else:
        resolved = _resolve_hcm(hcm_attrs, resource, scope, operation,
                                record_id, fields, version, reasons, warnings,
                                hcm_children=hcm_children)

    if resolved is None:
        risk = _classify(system, resource, scope, operation, action, fields)
        if risk == "HIGH":
            reasons.append("unresolvable from live metadata (HIGH risk "
                           "noted, not the cause): not eligible for dynamic "
                           "execution")
            return _preview(system, resource, scope, operation, record_id,
                            fields, action, None, None, "HIGH", NOT_ELIGIBLE,
                            reasons, None, version, warnings,
                            api_version=api_version,
                            metadata_fingerprint=fingerprint,
                            template_source=template_source,
                            child_path=child_path,
                            child_record_id=child_record_id)
        return _preview(system, resource, scope, operation, record_id,
                        fields, action, None, None, risk, NOT_RESOLVED,
                        reasons, None, version, warnings,
                        api_version=api_version,
                        metadata_fingerprint=fingerprint,
                        template_source=template_source,
                        child_path=child_path,
                        child_record_id=child_record_id)

    method, path, declared_params, verification, template_source, \
        primary_key = resolved
    path = _bind_path_ids(path, scope, record_id, child_record_id)
    reasons.extend(_validate_params(declared_params, fields))
    variant_overridden = False
    if system == "FSCM" and operation == "business_action":
        variant, variant_reasons = validate_variant_params(resource, action, fields)
        if variant == "blocked":
            # Operator-explicit danger value: the user carries the outcome.
            # Forced HIGH risk with a severe warning instead of refusal;
            # confirmation text carries what the framework cannot model.
            variant_overridden = True
            warnings.append(
                "SEVERE: this variant liquidates reserved funds and "
                "requires additional reopen data this framework does not "
                "model. Proceed only if you will complete the workflow "
                "outside this tool.")
        else:
            reasons.extend(variant_reasons)
    risk = _classify(system, resource, scope, operation, action, fields)
    if not reasons and system == "FSCM" and operation == "business_action" \
            and (resource, action) not in FSCM_ESTABLISHED_MEDIUM_ACTIONS \
            and risk != "HIGH":
        # Unknown dynamically discovered mutation that resolved cleanly
        # from live metadata: no specialized policy entry exists, so
        # conservative handling applies — HIGH risk with explicit
        # confirmation. Gated on empty reasons so unresolvable or invalid
        # operations keep their existing NOT_RESOLVED status. This is NOT
        # a refusal and NOT a capability judgment: the operation executes
        # through the normal pipeline with acknowledged-unverified
        # semantics.
        risk = "HIGH"
    if variant_overridden:
        risk = "HIGH"
    dispatchable = _dispatchable(system, scope, operation)
    if reasons:
        if risk == "HIGH":
            status = NOT_ELIGIBLE
            reasons.append("input validation failed (HIGH risk noted, not "
                           "the cause): not eligible for dynamic execution")
        else:
            status = NOT_RESOLVED
    elif risk == "UNKNOWN":
        status = NOT_ELIGIBLE
        reasons.append("risk classified UNKNOWN: not eligible for dynamic execution")
    elif risk == "HIGH":
        if not dispatchable:
            status = NOT_ELIGIBLE
            reasons.append("no executor implements this operation shape")
        elif verification is None:
            status = NOT_READY
            reasons.append("no verification strategy available for HIGH-risk operation")
        elif system == "HCM" and operation in ("update", "delete") \
                and not isinstance(version, int):
            status = NOT_ELIGIBLE
            reasons.append("objectVersionNumber binding is required")
        else:
            status = HIGH_RISK_ELIGIBLE
    elif not dispatchable:
        status = UNSUPPORTED
        reasons.append("no executor implements this operation shape")
    elif verification is None:
        status = NOT_READY
        if system == "FSCM" and resource in ("purchaseOrders", "draftPurchaseOrders") \
                and action in ("hold", "removeHold"):
            reasons.append(
                "Operation is exposed by Fusion metadata, but no authoritative "
                "hold-state source is available for safe precondition/postcondition "
                "verification.")
        else:
            reasons.append("no verification strategy available")
    elif system == "HCM" and operation == "update" and not isinstance(version, int):
        status = NOT_RESOLVED
        reasons.append("objectVersionNumber binding is required")
    else:
        status = ELIGIBLE
    if status in (ELIGIBLE, HIGH_RISK_ELIGIBLE):
        cap_reason = _over_value_cap(resource, fields, value_limits)
        if cap_reason is not None:
            status = NOT_ELIGIBLE
            reasons.append(cap_reason)
    warnings.extend(_operation_warnings(
        system, resource, scope, operation, action, verification, risk))
    return _preview(system, resource, scope, operation, record_id, fields,
                    action, method, path, risk, status, reasons,
                    verification, version, warnings,
                    api_version=api_version,
                    metadata_fingerprint=fingerprint,
                    template_source=template_source,
                    child_path=child_path,
                    child_record_id=child_record_id,
                    primary_key=primary_key)


def _resolve_fscm(fscm_parsed, resource, scope, operation, record_id,
                  fields, action, reasons, warnings, raw_describe=None,
                  system="FSCM", child_path=None, child_record_id=None):
    """Return (method, path, declared_params, verification, template_source,
    primary_key).

    The template source is honest metadata provenance: business actions
    resolve from ``rel=action`` link templates ("metadata-link"); create
    paths follow the documented REST collection convention
    ("canonical-pattern") with the method taken from metadata.
    """
    if not isinstance(fscm_parsed, dict):
        reasons.append("FSCM describe metadata unavailable")
        return None
    if scope in ("child", "child_item"):
        return _resolve_fscm_child(
            fscm_parsed, raw_describe, resource, scope, operation,
            record_id, fields, action, reasons, warnings, system,
            child_path, child_record_id)
    node = _fscm_scope_node(fscm_parsed, scope)
    if node is None:
        reasons.append("scope %r absent from describe metadata" % scope)
        return None
    actions = node.get("actions") or []
    links = node.get("links") or []
    if operation == "business_action":
        if not action:
            reasons.append("business_action requires an action name")
            return None
        out = _find_fscm_action(node, action)
        if not out["resolved"]:
            reasons.append(out["reason"])
            return None
        if out["method"] != "POST":
            reasons.append(
                "business action %r declares unexpected method %r (expected POST)"
                % (action, out["method"]))
            return None
        entry = out["entry"]
        declared = []
        for p in (entry.get("parameters") or []):
            if not p.get("name"):
                continue
            enriched = _discover_param_schema(p) or {}
            declared.append({"name": p.get("name"), "type": p.get("type"),
                             "mandatory": bool(p.get("mandatory")),
                             "allowed_values": enriched.get("allowed_values"),
                             "has_default": enriched.get("has_default", False),
                             "requires_explicit": enriched.get(
                                 "requires_explicit", False)})
        verification = _verification_strategy("FSCM", resource, scope,
                                              operation, action,
                                              dict(fields))
        return out["method"], out["path"], declared, verification, \
            "metadata-link", None
    if operation in ("create", "upsert"):
        # upsert shares create contract semantics (Fusion upsert POST is
        # create-or-update by key); override/runtime tables stay keyed by
        # the create operation. The method prefers an advertised upsert
        # entry and falls back to the create entry.
        if scope not in ("collection", "child"):
            reasons.append(
                "operation %r resolves at collection scope only" % operation)
            return None
        want = operation
        entry = next((a for a in actions
                      if isinstance(a, dict) and a.get("action", a.get("name")) == want), None)
        if entry is None and operation == "upsert":
            entry = next((a for a in actions
                          if isinstance(a, dict) and a.get("action", a.get("name")) == "create"), None)
        if entry is None or not entry.get("method"):
            reasons.append("CRUD %r not advertised with a method" % operation)
            return None
        verification = _verification_strategy("FSCM", resource, scope,
                                              operation, action,
                                              dict(fields))
        declared = []
        create_path = None
        primary_key = None
        if raw_describe is not None:
            from .fscm_create import discover_create_contract, validate_create_input
            contract = discover_create_contract(raw_describe, resource)
            if contract.get("resolved"):
                is_valid, val_reasons = validate_create_input(
                    contract, fields, system=system, resource=resource,
                    operation="create")
                if not is_valid:
                    reasons.extend(val_reasons)
                    return None
                create_path = contract.get("endpoint")
                primary_key = contract.get("primary_key")
                for f in contract.get("mandatory_fields", []):
                    declared.append({"name": f["name"], "type": f["type"],
                                     "mandatory": f.get("caller_supplied", False),
                                     "updatable": f.get("updatable"),
                                     "caller_supplied": f.get("caller_supplied")})
                for f in contract.get("optional_fields", []):
                    declared.append({"name": f["name"], "type": f["type"],
                                     "mandatory": False,
                                     "updatable": f.get("updatable"),
                                     "caller_supplied": f.get("updatable") is not False})
        return entry["method"], create_path, declared, verification, \
            "canonical-pattern", primary_key
    if operation in ("update", "delete"):
        # Generic field-level resolution from the attribute contract (same
        # engine as create, update semantics). Method comes from metadata;
        # path follows the item convention.
        if scope not in ("item", "child_item"):
            reasons.append(
                "operation %r resolves at item scope only" % operation)
            return None
        want = {"update": "update", "delete": "delete"}[operation]
        entry = next((a for a in actions
                      if isinstance(a, dict) and a.get("action", a.get("name")) == want), None)
        if entry is None or not entry.get("method"):
            reasons.append("CRUD %r not advertised with a method" % operation)
            return None
        if raw_describe is None:
            reasons.append(
                "field-level %s requires raw describe metadata" % operation)
            return None
        from .fscm_create import discover_create_contract
        contract = discover_create_contract(raw_describe, resource)
        if not contract.get("resolved"):
            reasons.extend(contract.get("reasons", ["create contract not resolved"]))
            return None
        contract_fields = (contract.get("mandatory_fields", [])
                           + contract.get("optional_fields", []))
        server_generated = set(contract.get("server_generated_fields", []))
        if operation == "delete":
            if fields:
                reasons.append("delete takes no fields: %s supplied"
                               % ", ".join(sorted(fields)))
                return None
            declared = []
        else:
            if not fields:
                reasons.append("no fields supplied for update")
                return None
            update_reasons = _validate_update_fields(
                contract_fields, fields, server_generated,
                contract.get("primary_key"))
            if update_reasons:
                reasons.extend(update_reasons)
                return None
            declared = _declared_from_attributes(contract_fields)
        verification = _verification_strategy("FSCM", resource, scope,
                                              operation, action,
                                              dict(fields))
        return entry["method"], "%s/{key}" % resource, declared, \
            verification, "canonical-pattern", contract.get("primary_key")
    reasons.append("operation %r has no FSCM resolution rule" % operation)
    return None


def _resolve_fscm_child(fscm_parsed, raw_describe, resource, scope,
                        operation, record_id, fields, action, reasons,
                        warnings, system, child_path, child_record_id):
    """Resolve a nested child operation. Returns the 6-tuple or None.

    Actions resolve from the parsed child node's fused actions+links
    (metadata-link templates). Creates/updates/deletes validate against
    the raw child attribute contract (same engine as top level).
    """
    children = fscm_parsed.get("children") if isinstance(fscm_parsed, dict) \
        else None
    kind = "collection" if scope == "child" else "item"
    node = (children or {}).get("%s:%s" % (child_path, kind))
    if not isinstance(node, dict):
        reasons.append("child %r not found under %s" % (child_path, resource))
        return None
    trail = child_path.strip("/").replace("/", "/child/")
    if operation == "business_action":
        if not action:
            reasons.append("business_action requires an action name")
            return None
        out = _find_fscm_action(node, action)
        if not out["resolved"]:
            reasons.append(out["reason"])
            return None
        if out["method"] != "POST":
            reasons.append(
                "business action %r declares unexpected method %r (expected POST)"
                % (action, out["method"]))
            return None
        entry = out["entry"]
        declared = []
        for p in (entry.get("parameters") or []):
            if not p.get("name"):
                continue
            enriched = _discover_param_schema(p) or {}
            declared.append({"name": p.get("name"), "type": p.get("type"),
                             "mandatory": bool(p.get("mandatory")),
                             "allowed_values": enriched.get("allowed_values"),
                             "has_default": enriched.get("has_default", False),
                             "requires_explicit": enriched.get(
                                 "requires_explicit", False)})
        verification = _verification_strategy("FSCM", resource, scope,
                                              operation, action,
                                              dict(fields))
        return out["method"], out["path"], declared, verification, \
            "metadata-link", None
    actions = node.get("actions") or []
    if operation in ("create", "upsert"):
        want = operation
        entry = next((a for a in actions
                      if isinstance(a, dict) and a.get("action", a.get("name")) == want), None)
        if entry is None and operation == "upsert":
            entry = next((a for a in actions
                          if isinstance(a, dict) and a.get("action", a.get("name")) == "create"), None)
        if entry is None or not entry.get("method"):
            reasons.append("CRUD %r not advertised for child %s"
                           % (operation, child_path))
            return None
        if raw_describe is None:
            reasons.append("child create requires raw describe metadata")
            return None
        from .fscm_create import discover_create_contract, validate_create_input
        contract = discover_create_contract(
            raw_describe, resource, child_path)
        if not contract.get("resolved"):
            reasons.extend(contract.get("reasons", ["child contract not resolved"]))
            return None
        is_valid, val_reasons = validate_create_input(
            contract, fields, system=system, resource=resource,
            operation="create")
        if not is_valid:
            reasons.extend(val_reasons)
            return None
        declared = []
        for f in contract.get("mandatory_fields", []):
            declared.append({"name": f["name"], "type": f["type"],
                             "mandatory": f.get("caller_supplied", False),
                             "updatable": f.get("updatable"),
                             "caller_supplied": f.get("caller_supplied")})
        for f in contract.get("optional_fields", []):
            declared.append({"name": f["name"], "type": f["type"],
                             "mandatory": False,
                             "updatable": f.get("updatable"),
                             "caller_supplied": f.get("updatable") is not False})
        verification = _verification_strategy("FSCM", resource, scope,
                                              operation, action,
                                              dict(fields))
        return entry["method"], "%s/{key}/child/%s" % (resource, trail), \
            declared, verification, "canonical-pattern", \
            contract.get("primary_key")
    if operation in ("update", "delete"):
        want = {"update": "update", "delete": "delete"}[operation]
        entry = next((a for a in actions
                      if isinstance(a, dict) and a.get("action", a.get("name")) == want), None)
        if entry is None or not entry.get("method"):
            reasons.append("CRUD %r not advertised for child %s"
                           % (operation, child_path))
            return None
        if raw_describe is None:
            reasons.append(
                "field-level child %s requires raw describe metadata" % operation)
            return None
        from .fscm_create import discover_create_contract
        contract = discover_create_contract(
            raw_describe, resource, child_path)
        if not contract.get("resolved"):
            reasons.extend(contract.get("reasons", ["child contract not resolved"]))
            return None
        contract_fields = (contract.get("mandatory_fields", [])
                           + contract.get("optional_fields", []))
        server_generated = set(contract.get("server_generated_fields", []))
        if operation == "delete":
            if fields:
                reasons.append("delete takes no fields: %s supplied"
                               % ", ".join(sorted(fields)))
                return None
            declared = []
        else:
            if not fields:
                reasons.append("no fields supplied for update")
                return None
            update_reasons = _validate_update_fields(
                contract_fields, fields, server_generated,
                contract.get("primary_key"))
            if update_reasons:
                reasons.extend(update_reasons)
                return None
            declared = _declared_from_attributes(contract_fields)
        verification = _verification_strategy("FSCM", resource, scope,
                                              operation, action,
                                              dict(fields))
        if scope == "child_item":
            path = "%s/{key}/child/%s/{key}" % (resource, trail)
        else:
            path = "%s/{key}/child/%s" % (resource, trail)
        return entry["method"], path, declared, verification, \
            "canonical-pattern", contract.get("primary_key")
    reasons.append("operation %r has no FSCM child resolution rule" % operation)
    return None


def _validate_hcm_create_attrs(attr_list: Any, item: Any, label: str,
                               policy_key: Any, reasons: Any,
                               warnings: Any, children_meta: Any = None,
                               resource: str = "",
                               child_path: str = "") -> Any:
    """Validate one HCM create object against an attribute list.

    Shared by top-level fields and nested child items (composite
    creates): server-owned rejected, unknown rejected, read-only
    rejected, mandatory presence enforced with the mandatory-locked
    skip (only the live POST can arbitrate those). List/dict values
    recurse into advertised grandchildren (dotted keys like
    "workRelationships.assignments"); anything nested without metadata
    fails closed. Returns declared entries, or None on refusal
    (reasons appended).
    """
    by_name = {a.get("name"): a for a in (attr_list or [])
               if isinstance(a, dict) and a.get("name")}
    overrides = _policy_entry(
        "HCM_CREATE_CALLER_INPUT_OVERRIDES", policy_key,
        HCM_CREATE_CALLER_INPUT_OVERRIDES.get(policy_key, frozenset()))
    server_owned = _policy_entry(
        "HCM_SERVER_OWNED_CREATE_FIELDS", policy_key,
        HCM_SERVER_OWNED_CREATE_FIELDS.get(policy_key, frozenset()))
    if not isinstance(item, dict):
        reasons.append("%s must be an object" % (label or "fields"))
        return None
    kid_meta = children_meta if isinstance(children_meta, dict) else {}
    declared = []
    for name, value in item.items():
        if name in HCM_HIGH_RISK_FIELDS:
            # Workflow-controlled fields route to HIGH risk (explicit
            # confirmation path), not refusal: risk classification
            # carries the danger, the user carries the decision.
            warnings.append(
                "WORKFLOW-CONTROLLED field %r bypasses the normal "
                "business workflow; confirm the outcome, not just the "
                "value." % name)
        if isinstance(value, (list, dict)):
            dotted = name if not child_path else child_path + "." + name
            sub_attrs = kid_meta.get(dotted)
            if dotted not in kid_meta:
                reasons.append("unknown field: %r" % name)
                return None
            # P4 provenance shape: extract_child_schemas emits
            # {"attributes": [...], "source": ...}; plain lists from
            # older callers keep working unchanged.
            if isinstance(sub_attrs, dict):
                sub_attrs = sub_attrs.get("attributes", [])
            if not isinstance(sub_attrs, list) or not sub_attrs:
                reasons.append(
                    "child metadata unavailable for %r: failing closed"
                    % dotted)
                return None
            if not isinstance(value, list) or not value \
                    or any(not isinstance(entry, dict) for entry in value):
                reasons.append(
                    "child %r must be a non-empty array of objects"
                    % dotted)
                return None
            for index, entry in enumerate(value):
                sub = _validate_hcm_create_attrs(
                    sub_attrs, entry, "%s[%d]" % (dotted, index),
                    ("HCM", resource + "." + dotted, "create"),
                    reasons, warnings, kid_meta, resource, dotted)
                if sub is None:
                    return None
            declared.append({"name": name, "type": "array",
                             "mandatory": False})
            continue
        if name in server_owned:
            reasons.append(
                "server-owned field rejected: %r is set by Fusion, "
                "not by the caller" % name)
            return None
        spec = by_name.get(name)
        if spec is None:
            reasons.append("unknown field: %r" % name)
            return None
        if spec.get("updatable") is False and name not in overrides:
            reasons.append("read-only field rejected: %r" % name)
            return None
        declared.append({"name": name, "type": spec.get("type"),
                         "mandatory": bool(spec.get("mandatory"))})
    for name, spec in by_name.items():
        # Server-owned and audit columns are never caller inputs.
        # Mandatory-but-locked fields beyond those are skipped, never
        # failed: a caller cannot satisfy them, so only the live POST
        # can arbitrate (success, or an exact 400 naming the field,
        # which then earns an override entry). Supplying one is still
        # rejected above as read-only.
        if name in server_owned or name in HCM_AUDIT_COLUMNS:
            continue
        if spec.get("mandatory") and spec.get("updatable") is not False \
                and name not in item and name not in overrides:
            reasons.append("missing mandatory field: %s" % name)
            return None
    return declared


def _resolve_hcm(hcm_attrs, resource, scope, operation, record_id, fields,
                 version, reasons, warnings, hcm_children=None):
    """HCM resolution. Method comes from the fixed client contract
    (update -> PATCH on {resource}/{key}); the planner never accepts a
    caller-supplied method — it only verifies the shape is supported.

    P2 adds collection create, item delete, and an explicit dead-end for
    business actions (the attribute list carries no action metadata, so
    actions stay NOT_RESOLVED until catalog action plumbing exists).
    """
    if scope in ("child", "child_item"):
        reasons.append("child scopes are not modeled for HCM")
        return None
    if operation == "business_action":
        reasons.append(
            "HCM action metadata is not modeled in the attribute list: "
            "business actions stay unresolved")
        return None
    if operation == "create":
        if scope != "collection":
            reasons.append("HCM create resolves at collection scope only")
            return None
        if not isinstance(hcm_attrs, list) or not hcm_attrs:
            reasons.append("HCM attribute metadata unavailable")
            return None
        children_meta = hcm_children if isinstance(hcm_children, dict) else {}
        declared = _validate_hcm_create_attrs(
            hcm_attrs, fields, resource, ("HCM", resource, "create"),
            reasons, warnings, children_meta, resource, "")
        if declared is None:
            return None
        verification = _verification_strategy("HCM", resource, scope,
                                              operation, None,
                                              dict(fields))
        return "POST", resource, declared, verification, "canonical-pattern", \
            None
    if operation == "delete":
        if scope != "item":
            reasons.append("HCM delete resolves at item scope only")
            return None
        if fields:
            reasons.append("delete takes no fields: %s supplied"
                           % ", ".join(sorted(fields)))
            return None
        if not isinstance(hcm_attrs, list) or not hcm_attrs:
            reasons.append("HCM attribute metadata unavailable")
            return None
        verification = _verification_strategy("HCM", resource, scope,
                                              operation, None,
                                              dict(fields))
        return "DELETE", "%s/{key}" % resource, [], verification, \
            "canonical-pattern", None
    if scope != "item" or operation != "update":
        reasons.append("only item-scope update is dynamically resolvable for HCM")
        return None
    if not isinstance(hcm_attrs, list) or not hcm_attrs:
        reasons.append("HCM attribute metadata unavailable")
        return None
    by_name = {a.get("name"): a for a in hcm_attrs if a.get("name")}
    declared = []
    for name in fields:
        if name in HCM_HIGH_RISK_FIELDS:
            # Same route-to-HIGH rule as the create branch above.
            warnings.append(
                "WORKFLOW-CONTROLLED field %r bypasses the normal "
                "business workflow; confirm the outcome, not just the "
                "value." % name)
        spec = by_name.get(name)
        if spec is None:
            reasons.append("unknown field: %r" % name)
            return None
        if spec.get("updatable") is False:
            reasons.append("read-only field rejected: %r" % name)
            return None
        # Deny-list model: any metadata-updatable, non-workflow field is
        # writable (risk-classified normally). Updatability is necessary
        # and, outside the workflow-controlled set, sufficient.
        declared.append({"name": name, "type": spec.get("type"),
                         "mandatory": bool(spec.get("mandatory"))})
        verification = _verification_strategy("HCM", resource, scope, operation, None,
                                          dict(fields))
    return "PATCH", "%s/{key}" % resource, declared, verification, \
        "canonical-pattern", None


def validate_variant_params(resource: Optional[str], action: Optional[str],
                            fields: Dict[str, Any]):
    """Public variant-selector check shared by planner and curated lanes.

    Returns ``(status, reasons)`` where status is None (satisfied),
    "missing" (omitted or unlisted value — never defaulted), or "blocked"
    (explicitly prohibited value such as finallyClose).
    """
    reasons: List[str] = []
    status = _check_variant_params(resource, action, fields, reasons)
    return status, reasons


def _check_variant_params(resource: Optional[str], action: Optional[str],
                          fields: Dict[str, Any],
                          reasons: List[str]) -> Optional[str]:
    """Enforce explicit variant selection. Returns "blocked", "missing", or None.

    Returns None when no variant rule applies or the supplied value is an
    allowed explicit variant. Appends a precise reason otherwise; "missing"
    covers both omitted and unlisted values (never silently defaulted).
    """
    if action is None:
        return None
    for (res, act, param), rule in FSCM_VARIANT_PARAMS.items():
        if resource != res or action != act:
            continue
        value = fields.get(param)
        if value is None:
            reasons.append(
                "%s must be explicitly specified because live metadata exposes "
                "multiple valid values (%s) and no default."
                % (param, ", ".join(rule["values"])))
            return "missing"
        if value not in rule["values"]:
            reasons.append(
                "%s=%r is not a documented valid value (expected one of: %s)."
                % (param, value, ", ".join(rule["values"])))
            return "missing"
        if (res, act, param, value) in FSCM_HIGH_RISK_ACTION_VALUES:
            reasons.append(
                "%s=%r is blocked: it liquidates reserved funds and requires "
                "additional reopen data this framework does not model." % (param, value))
            return "blocked"
    return None


def _preview(system, resource, scope, operation, record_id, fields, action,
             method, path, risk, status, reasons, verification, version,
             warnings, api_version=None, metadata_fingerprint=None,
             template_source=None, child_path=None,
             child_record_id=None, primary_key=None):
    """Render the dynamic preview. Metadata only — never executes."""
    import uuid as _uuid
    # For CRUD operations there is no separate action name; mirror the
    # operation so confirmation binding (which requires an action field)
    # stays well-defined.
    if action is None:
        action = operation
    return {
        "mode": "dry_run",
        "preview_id": _uuid.uuid4().hex,
        "system": system,
        "resource": resource,
        "scope": scope,
        "operation": operation,
        "action": action,
        "method": method,
        "path": path,
        "template_source": template_source,
        "api_version": api_version,
        "metadata_fingerprint": metadata_fingerprint,
        "record_id": record_id,
        "child_path": child_path,
        "child_record_id": (str(child_record_id)
                            if child_record_id is not None else None),
        "primary_key": primary_key,
        "parameters": dict(fields),
        "version": version,
        "risk": risk,
        "eligibility": status,
        "reasons": list(reasons),
        "warnings": list(warnings),
        "verification": verification,
        "would_execute": False,
        "note": "dynamic preview only: nothing was sent",
    }

"""FSCM create-operation contract discovery and validation.

Discovers create contracts from live Fusion metadata and validates
caller-supplied input against them. Never executes anything.
"""

from typing import Any, Dict, List, Optional, Tuple

from .policy import get_table_entry as _policy_entry

# Fields that are always server-generated and must never be supplied by
# the caller during create. Curated from live metadata patterns.
_SERVER_GENERATED = frozenset({
    "InvoiceId", "POHeaderId", "CreationDate", "CreatedBy",
    "LastUpdatedBy", "LastUpdateDate", "LastUpdateLogin",
    "Stream", "StreamDefinitionCode",
})

# Curated CREATE omit-fields: Fusion /describe marks these fields
# mandatory (sometimes even updatable/inCreate), yet the runtime refuses
# any caller-supplied value — the server owns them outright. Omit them
# from validation requirements AND reject them when supplied.
#
# Verified live 2026-10-01 (Yamama pod): supplying SupplierPartyId on
# suppliers create fails with POZ-2130495 ("You can't provide a value
# for the SupplierPartyId attribute"). Oracle's own REST documentation
# corroborates: its sample request omits SupplierPartyId AND
# SupplierNumber, while its sample response shows both server-assigned
# (SupplierPartyId consecutive with SupplierId; SupplierNumber
# auto-generated though never sent).
#
# Scope: exact (system, resource, operation) tuples ONLY, top-level
# contracts only (nested children use pure metadata rules until a
# verified child-level rule exists). Never infer entries from metadata.
FSCM_CREATE_OMIT_FIELDS = {
    ("FSCM", "suppliers", "create"): frozenset({
        "SupplierPartyId",
    }),
}
# Runtime-required create fields: Fusion /describe marks these as optional
# (mandatory=false) but the runtime enforces them at creation time.
# Curated from verified live business rules where metadata is incomplete.
#
# AP-810667: "You must provide a valid value for the Supplier, Supplier Site
# attribute." — verified 2026-09-30 via live POST rejection.
#
# POZ-2130496: "You must provide a value for either the
# BusinessRelationship or BusinessRelationshipCode attribute" (+ same for
# TaxOrganizationType/Code) — verified 2026-10-01 via live POST rejection.
# The display-name variants are required (matching Oracle's documented
# sample request). Code-variant acceptance is a future refinement if live
# evidence demands it.
#
# Scope: exact (system, resource, operation) tuples ONLY.
# Do NOT apply globally — other resources may have different runtime rules.
FSCM_RUNTIME_REQUIRED_CREATE_FIELDS = {
    ("FSCM", "invoices", "create"): frozenset({"SupplierSite", "Supplier"}),
    ("FSCM", "suppliers", "create"): frozenset({"BusinessRelationship",
                                                "TaxOrganizationType"}),
    # Draft PO create: Fusion demands the requisitioning BU at runtime
    # although describe marks it optional. Verified live 2026-10-01
    # (Yamama pod): "You must provide a value for the RequisitioningBU
    # attribute." On this pod it equals the ProcurementBU.
    # PayOnReceiptFlag likewise demanded at runtime although optional in
    # describe. Verified live 2026-10-01 (Yamama pod): "You must provide
    # a value for the PayOnReceiptFlag attribute." First refusal ever
    # reported with a machine-generated field_report proposing exactly
    # this entry.
    # BuyerManagedTransportFlag likewise demanded at runtime. Verified
    # live 2026-10-01 (Yamama pod) via the same field_report loop.
    # Draft headers tend to demand their flag cluster one field at a
    # time; each refusal names the next entry exactly.
    ("FSCM", "draftPurchaseOrders", "create"): frozenset({
        "RequisitioningBU",
        "PayOnReceiptFlag",
        "BuyerManagedTransportFlag",
    }),
}

# Curated CREATE caller-input overrides.
#
# History (Yamama pod, 2026-10-01): InvoiceSourceCode, Party and
# SupplierNumber were each inferred as caller inputs from old-pod
# metadata (mandatory + allowChanges="never") and each was then proven
# NOT settable by live create POSTs — Fusion rejects all three
# categorically ("Attribute X in view object InvoiceHeader1 cannot be
# set", any value). The server derives source/party/number from the
# Supplier + SupplierSite; the caller supplies commercial data only
# (amounts, dates, terms, method) plus Supplier/SupplierSite (the latter
# enforced at runtime, AP-810667).
#
# Lesson codified: on invoice create, Fusion describe "mandatory" plus
# allowChanges="never" describes POST-write immutability, NOT create
# inputs. Metadata alone cannot distinguish caller inputs from
# server-owned fields (AccountCodingStatus / FundsStatus / ApprovalStatus
# / PaidStatus and the trio above), so allowChanges="never" means
# non-settable by default and any exception requires its own verified
# live business rule — inference from metadata is not evidence.
#
# Scope: system=FSCM, resource=invoices, operation=create ONLY.
# The key is kept (explicit scope marker) with an empty set: no current
# exception exists. Do NOT add entries without a successful live POST
# proving the field settable at create time.
FSCM_CREATE_CALLER_INPUT_OVERRIDES = {
    ("FSCM", "invoices", "create"): frozenset(),
}


def _walk_child_segments(node: Any, resource: str,
                          child_path: str) -> Any:
    """Walk raw ``children`` segments to a child definition (or None).

    Mirrors the traversal guards of endpoint discovery: segments must be
    plain names (no traversal, no URLs, no empty parts).
    """
    segments = [s for s in child_path.strip("/").split("/") if s and s != "child"]
    if not segments or any(s in ("..",) or "://" in s for s in segments):
        return None
    current: Any = node.get("children") if isinstance(node, dict) else None
    child_def: Any = None
    for segment in segments:
        if not isinstance(current, dict) or segment not in current \
                or not isinstance(current[segment], dict):
            return None
        child_def = current[segment]
        current = child_def.get("children")
    return child_def


def discover_create_contract(raw_describe: Any, resource: str,
                             child_path: Any = None) -> Dict[str, Any]:
    """Discover the create contract for a resource from live describe metadata.

    With ``child_path`` (e.g. ``"invoiceLines"``), the contract is built
    from that nested child's attributes instead of the top-level resource.
    Caller-input override and runtime-required tables stay keyed by
    (system, resource, operation): they apply wherever the same field
    names occur, which is a no-op for children that lack those fields.

    Returns a dict with:
    - endpoint: the collection endpoint
    - method: always POST
    - primary_key: the primary key field name
    - mandatory_fields: list of {name, type, updatable, caller_supplied}
    - optional_fields: list of {name, type, updatable}
    - read_only_fields: list of field names that are updatable=False
    - server_generated_fields: list of field names the server generates
    - child_resources: list of child resource names
    """
    if not isinstance(raw_describe, dict):
        return {"resolved": False, "reasons": ["describe metadata not a dict"]}
    resources = raw_describe.get("Resources")
    if not isinstance(resources, dict):
        return {"resolved": False, "reasons": ["no Resources in describe"]}
    node = resources.get(resource)
    if not isinstance(node, dict):
        return {"resolved": False,
                "reasons": ["resource %r not in describe" % resource]}
    scope_label = resource
    child_scope = bool(child_path)
    if child_path:
        if not isinstance(child_path, str):
            return {"resolved": False,
                    "reasons": ["invalid child_path %r" % (child_path,)]}
        child_def = _walk_child_segments(node, resource, child_path)
        if not isinstance(child_def, dict):
            return {"resolved": False,
                    "reasons": ["child %r not found under %s"
                                % (child_path, resource)]}
        node = child_def
        scope_label = "%s/child/%s" % (
            resource, child_path.strip("/").replace("/", "/child/"))
    attrs = node.get("attributes", [])
    if not isinstance(attrs, list):
        attrs = []
    # This module is FSCM-create-specific, so the override tuple is fixed
    # to ("FSCM", resource, "create"). Unknown resources resolve to an
    # empty set and keep the generic allowChanges="never" rule. Overrides
    # apply to top-level contracts only: nested children use pure metadata
    # rules until a verified child-level business rule exists.
    caller_input_overrides = _policy_entry(
        "FSCM_CREATE_CALLER_INPUT_OVERRIDES", ("FSCM", resource, "create"),
        FSCM_CREATE_CALLER_INPUT_OVERRIDES.get(
            ("FSCM", resource, "create"), frozenset())) \
        if not child_scope else frozenset()
    # Mandatory-but-must-omit fields (server owns them outright): same
    # top-level-only scoping as overrides.
    omit_fields = _policy_entry(
        "FSCM_CREATE_OMIT_FIELDS", ("FSCM", resource, "create"),
        FSCM_CREATE_OMIT_FIELDS.get(
            ("FSCM", resource, "create"), frozenset())) \
        if not child_scope else frozenset()
    mandatory = []
    optional = []
    read_only = []
    server_generated = []
    primary_key = None
    for attr in attrs:
        if not isinstance(attr, dict):
            continue
        name = attr.get("name")
        if not name:
            continue
        props = attr.get("properties") if isinstance(attr.get("properties"), dict) else {}
        is_primary = props.get("fnd:GLOBALLY_UNIQUE") == "true"
        if is_primary:
            primary_key = name
        updatable = attr.get("updatable")
        allow_changes = attr.get("allowChanges")
        is_mandatory = attr.get("mandatory")
        field_type = attr.get("type")
        is_server_gen = name in _SERVER_GENERATED or is_primary
        # Generic rule: allowChanges="never" => non-settable. Narrow
        # exception: an explicit create caller-input override entry above.
        # Separate rule: curated omit-fields are non-settable regardless
        # of metadata (the runtime refuses any caller-supplied value).
        is_non_settable = (((allow_changes == "never") and (
            name not in caller_input_overrides)) or (
            name in omit_fields))
        if is_mandatory:
            caller_supplied = not is_server_gen and not is_non_settable
            mandatory.append({
                "name": name,
                "type": field_type,
                "updatable": updatable,
                "allowChanges": allow_changes,
                "caller_supplied": caller_supplied,
            })
            if is_server_gen or is_non_settable:
                read_only.append(name)
            if is_server_gen:
                server_generated.append(name)
        else:
            optional.append({
                "name": name,
                "type": field_type,
                "updatable": updatable,
                "allowChanges": allow_changes,
            })
            if is_server_gen or is_non_settable:
                read_only.append(name)
    children = node.get("children", {})
    child_names = sorted(children.keys()) if isinstance(children, dict) else []
    coll = node.get("collection", {})
    coll_acts = coll.get("actions", []) if isinstance(coll, dict) else []
    create_method = None
    for a in coll_acts:
        if isinstance(a, dict) and a.get("name") == "create":
            create_method = a.get("method")
            break
    return {
        "resolved": True,
        "resource": resource,
        "child_path": child_path if child_path else None,
        "scope_node": scope_label,
        "endpoint": resource,
        "method": create_method or "POST",
        "primary_key": primary_key,
        "mandatory_fields": mandatory,
        "optional_fields": optional,
        "read_only_fields": read_only,
        "server_generated_fields": server_generated,
        "child_resources": child_names,
    }


def validate_create_input(
    contract: Dict[str, Any],
    fields: Dict[str, Any],
    system: str = "FSCM",
    resource: str = "",
    operation: str = "",
) -> Tuple[bool, List[str]]:
    """Validate caller-supplied input against a create contract.

    Returns (is_valid, reasons).
    """
    reasons: List[str] = []
    if not contract.get("resolved"):
        return False, ["create contract not resolved"]
    mandatory_names = {f["name"] for f in contract.get("mandatory_fields", [])}
    optional_names = {f["name"] for f in contract.get("optional_fields", [])}
    read_only = set(contract.get("read_only_fields", []))
    server_generated = set(contract.get("server_generated_fields", []))
    all_known = mandatory_names | optional_names
    runtime_required = _policy_entry(
        "FSCM_RUNTIME_REQUIRED_CREATE_FIELDS", (system, resource, operation),
        FSCM_RUNTIME_REQUIRED_CREATE_FIELDS.get(
            (system, resource, operation), frozenset()))
    if contract.get("child_path"):
        # Runtime requirements are top-level resource semantics (e.g. the
        # invoice header's Supplier/SupplierSite); they never apply to
        # nested child contracts.
        runtime_required = frozenset()
    # Narrow CREATE caller-input overrides; scoped to the exact
    # (system, resource, operation) tuple AND to top-level contracts (a
    # child contract carries child_path). Defensive second gate: contracts
    # built by discover_create_contract already exclude these fields from
    # read_only_fields, so this only matters for externally built contracts.
    caller_input_overrides = _policy_entry(
        "FSCM_CREATE_CALLER_INPUT_OVERRIDES", (system, resource, operation),
        FSCM_CREATE_CALLER_INPUT_OVERRIDES.get(
            (system, resource, operation), frozenset())) \
        if not contract.get("child_path") else frozenset()
    for key in fields:
        if key not in all_known:
            reasons.append("unknown field: %r" % key)
        elif key in read_only and key not in caller_input_overrides:
            reasons.append(
                "non-settable field rejected: %r "
                "(updatable=false or allowChanges=never)" % key)
        elif key in server_generated:
            reasons.append("server-generated field rejected: %r" % key)
    for f in contract.get("mandatory_fields", []):
        name = f["name"]
        if name not in fields:
            if f.get("caller_supplied"):
                reasons.append("missing mandatory field: %s" % name)
    for name in runtime_required:
        if name not in fields:
            reasons.append(
                "missing runtime-required field: %s "
                "(Fusion runtime requirement, AP-810667)" % name)
    type_map = {}
    for f in contract.get("mandatory_fields", []) + contract.get("optional_fields", []):
        type_map[f["name"]] = f.get("type")
    for name, value in fields.items():
        expected_type = type_map.get(name)
        if expected_type is None:
            continue
        if value is None:
            continue
        if expected_type == "string" and not isinstance(value, str):
            reasons.append("field %r expects string, got %s" % (name, type(value).__name__))
        elif expected_type == "integer" and not isinstance(value, int):
            reasons.append("field %r expects integer, got %s" % (name, type(value).__name__))
        elif expected_type == "number" and not isinstance(value, (int, float)):
            reasons.append("field %r expects number, got %s" % (name, type(value).__name__))
        elif expected_type == "boolean" and not isinstance(value, bool):
            reasons.append("field %r expects boolean, got %s" % (name, type(value).__name__))
        elif expected_type == "date" and not isinstance(value, str):
            reasons.append("field %r expects date string, got %s" % (name, type(value).__name__))
        elif expected_type == "datetime" and not isinstance(value, str):
            reasons.append("field %r expects datetime string, got %s" % (name, type(value).__name__))
    return len(reasons) == 0, reasons


def build_create_body(
    contract: Dict[str, Any],
    fields: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the request body from validated fields.

    Only includes fields that are known, settable, and not server-generated.
    Excludes fields with updatable=false or allowChanges="never".
    """
    body = {}
    read_only = set(contract.get("read_only_fields", []))
    server_generated = set(contract.get("server_generated_fields", []))
    for key, value in fields.items():
        if key in read_only or key in server_generated:
            continue
        body[key] = value
    return body

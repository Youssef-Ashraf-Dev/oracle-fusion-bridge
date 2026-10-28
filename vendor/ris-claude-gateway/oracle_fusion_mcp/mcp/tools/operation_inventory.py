"""Metadata-driven operation inventory (read-only, never executes).

Answers "what CAN we do with this resource?" purely from live describe
metadata — the dynamic counterpart to the static ``capability_inventory``
tables. Every advertised operation (CRUD, custom actions, nested child
operations) is reported with the facts the universal engine decides on:
resolution source, policy markers, verification availability, and
dispatchability. No caller input beyond system/resource is accepted, and
no write is performed.

Use ``fusion_create_requirements`` for the exact caller fields of any
listed create operation.
"""

from typing import Any, Dict, List, Optional

from .dynamic_planner import (
    HIGH_ACTIONS,
    HIGH_HCM_RESOURCES,
    HIGH_OPERATIONS,
    HCM_HIGH_RISK_FIELDS,
    _dispatchable,
    _verification_strategy,
)
from .endpoint_discovery import _resource_node, discover_actions


def _action_entries(node: Any, scope: str) -> List[Dict[str, Any]]:
    if not isinstance(node, dict):
        return []
    target = node.get("collection") if scope == "collection" \
        else node.get("item")
    if not isinstance(target, dict):
        return []
    return discover_actions(target, scope)


def _op_record(*, system: str, resource: str, scope: str, operation: str,
               action: Optional[str], method: Any,
               template_source: str) -> Dict[str, Any]:
    policy = []
    if operation in HIGH_OPERATIONS:
        policy.append("HIGH-risk operation")
    if action in HIGH_ACTIONS:
        policy.append("HIGH-risk action")
    if system == "HCM" and resource in HIGH_HCM_RESOURCES:
        policy.append("HIGH-risk resource")
    verification = _verification_strategy(system, resource, scope,
                                          operation, action, {})
    return {
        "scope": scope,
        "operation": operation,
        "action": action,
        "method": method,
        "template_source": template_source,
        "policy": policy,
        "verification": (verification or {}).get("strategy"),
        "dispatchable": _dispatchable(system, scope, operation),
        "requirements_via": "fusion_create_requirements"
        if operation in ("create", "upsert") else None,
    }


def describe_operations(*, system: str, resource: str,
                        fscm_raw: Any = None,
                        hcm_attrs: Any = None) -> Dict[str, Any]:
    """Inventory one resource's advertised operations from metadata."""
    if system not in ("FSCM", "HCM") or not isinstance(resource, str) \
            or not resource.strip():
        return {"resolved": False, "system": system, "resource": resource,
                "reasons": ["system must be HCM or FSCM with a plain name"]}
    if system == "HCM":
        return _hcm_inventory(resource, hcm_attrs)
    node = _resource_node(fscm_raw, resource)
    if node is None:
        return {"resolved": False, "system": system, "resource": resource,
                "reasons": ["resource %r not present in describe metadata"
                            % resource]}
    operations: List[Dict[str, Any]] = []
    for scope in ("collection", "item"):
        for entry in _action_entries(node, scope):
            name = entry.get("name")
            if not name:
                continue
            crud = name in ("get", "create", "upsert", "update", "delete")
            operations.append(_op_record(
                system=system, resource=resource, scope=scope,
                operation=name if crud else "business_action",
                action=None if crud else name,
                method=entry.get("method"),
                template_source=entry.get("template_source")
                or "canonical-pattern"))
    children = {}
    raw_children = node.get("children")
    if isinstance(raw_children, dict):
        for name, child in sorted(raw_children.items()):
            if not isinstance(child, dict):
                continue
            child_ops = []
            for scope in ("collection", "item"):
                for entry in _action_entries(child, scope):
                    if entry.get("name"):
                        child_ops.append(
                            "%s:%s:%s" % (scope, entry["name"],
                                          entry.get("method")))
            grandchildren = sorted(
                k for k in (child.get("children") or {})
                if isinstance((child.get("children") or {}).get(k), dict))
            children[name] = {"operations": child_ops,
                              "children": grandchildren}
    return {"resolved": True, "system": system, "resource": resource,
            "operations": operations, "children": children,
            "note": "Resolution sources and policy markers only; "
                    "eligibility additionally needs caller fields, live "
                    "validation, and confirmation."}


def _hcm_inventory(resource: str, hcm_attrs: Any) -> Dict[str, Any]:
    if not isinstance(hcm_attrs, list) or not hcm_attrs:
        return {"resolved": False, "system": "HCM", "resource": resource,
                "reasons": ["HCM attribute metadata unavailable"]}
    by_name = {a.get("name"): a for a in hcm_attrs if a.get("name")}
    mandatory = sorted(n for n, s in by_name.items() if s.get("mandatory"))
    updatable = sorted(n for n, s in by_name.items()
                       if s.get("updatable") is not False)
    risky = sorted(n for n in HCM_HIGH_RISK_FIELDS if n in by_name)
    writable = sorted(n for n in updatable if n not in risky)
    return {"resolved": True, "system": "HCM", "resource": resource,
            "operations": [
                _op_record(system="HCM", resource=resource,
                           scope="collection", operation="create",
                           action=None, method="POST",
                           template_source="canonical-pattern"),
                _op_record(system="HCM", resource=resource, scope="item",
                           operation="update", action=None, method="PATCH",
                           template_source="canonical-pattern"),
                _op_record(system="HCM", resource=resource, scope="item",
                           operation="delete", action=None, method="DELETE",
                           template_source="canonical-pattern"),
            ],
            "attributes": {"total": len(by_name), "mandatory": mandatory,
                           "updatable": updatable,
                           "writable": writable,
                           "workflow_controlled": risky},
            "children": {},
            "note": "HCM shape support is fixed (create/update/delete); "
                    "field writability is deny-list (updatable minus "
                    "workflow-controlled). "}

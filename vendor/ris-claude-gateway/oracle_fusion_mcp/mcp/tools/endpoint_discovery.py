"""Generic Fusion endpoint discovery (read-only metadata resolution).

Resolves collection / item / child / nested-child / action / CRUD endpoints
FROM live ``/describe`` metadata instead of hardcoded tool names. A request
like "Close PO-803" becomes: identify ``purchaseOrders`` -> resolve the
record key -> read the resource metadata -> discover item action ``close``
-> resolve its exact href template, method, content type and parameters.

Design rules (tested):
- Href templates from ``rel=action`` / ``rel=child`` links are authoritative.
  Canonical ``{resource}/{id}/action/{name}`` patterns are used ONLY when no
  link template exists, and the result is then marked ``template_source`` as
  ``canonical-pattern`` instead of ``metadata-link`` (never silently equal).
- No caller-supplied method/URL/endpoint exists anywhere in this module.
- Unknown resource / action / endpoint -> structured NOT_RESOLVED reasons,
  never invented endpoints. Ambiguous resource matches are reported, never
  silently chosen.
- Parameter ``enum`` / allowed-values / defaults are read from metadata when
  present. A parameter that declares allowed values but no default MUST be
  explicitly provided (``requires_explicit``); the planner refuses to guess.
  (Live Fusion ``/describe`` rarely carries enums; curated variant maps such
  as the PO ``closeAction`` set remain the evidenced source for those.)
"""

import re
from typing import Any, Dict, List, Optional

VALID_SCOPES = ("collection", "item", "child", "child_item")

CRUD_METHODS = ("get", "create", "update", "delete", "upsert")

_RECORD_ID_RE = re.compile(r"^[0-9]{1,32}$")

SERVICES = {
    "FSCM": "fscmRestApi",
    "HCM": "hcmRestApi",
}

# Metadata keys that, when present on a parameter, declare an explicit
# allowed-values list. Checked in order; first hit wins.
_ENUM_KEYS = ("enum", "allowedValues", "allowed_values", "values")

# Metadata keys that declare a server-side default value.
_DEFAULT_KEYS = ("default", "defaultValue", "default_value")


def _resource_node(raw_describe: Any, resource: str) -> Optional[Dict[str, Any]]:
    """Unwrap the Resources.<name> envelope (case-tolerant, no guessing)."""
    if not isinstance(raw_describe, dict):
        return None
    resources = raw_describe.get("Resources")
    if not isinstance(resources, dict):
        return None
    if resource in resources and isinstance(resources[resource], dict):
        return resources[resource]
    lowered = resource.lower()
    for name, node in resources.items():
        if isinstance(name, str) and name.lower() == lowered \
                and isinstance(node, dict):
            return node
    return None


def _relative_template(href: Any) -> Optional[str]:
    if not isinstance(href, str) or not href:
        return None
    marker = "/resources/"
    if marker in href:
        _head, tail = href.split(marker, 1)
        _version, sep, rest = tail.partition("/")
        return rest if sep and rest else (tail or None)
    if "://" not in href:
        return href.lstrip("/") or None
    return None


def _api_version_from_href(href: Any) -> Optional[str]:
    if not isinstance(href, str):
        return None
    marker = "/resources/"
    if marker not in href:
        return None
    _head, tail = href.split(marker, 1)
    version, sep, _rest = tail.partition("/")
    return version if sep and version else None


def canonical_fingerprint(obj: Any) -> Optional[str]:
    """Deterministic sha256 over canonical JSON (metadata-identity helper).

    Returns None for values that cannot be canonicalized. Used to bind a
    resolved operation to the exact metadata it was resolved from: any
    metadata change fails closed at execution time.
    """
    import hashlib
    import json as _json
    try:
        canonical = _json.dumps(obj, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def describe_fingerprint(raw_describe: Any, resource: str) -> Optional[str]:
    """Fingerprint the raw ``Resources.<name>`` describe node (or None)."""
    node = _resource_node(raw_describe, resource)
    if node is None:
        return None
    return canonical_fingerprint({"resource": resource, "node": node})


# Documented FSCM metadata-catalog request (Oracle "Access Metadata":
# GET {version-root}/describe?metadataMode=list returns only resource
# URLs). Constants (not inline literals) so the request composition is
# unit-testable without a live pod or server import.
FSCM_CATALOG_ENDPOINT = "describe"
FSCM_CATALOG_PARAMS = {"metadataMode": "list"}


def _catalog_entry_name(entry: Any) -> Optional[str]:
    """One catalog-list entry to a resource name (or None)."""
    if isinstance(entry, str):
        return entry.strip() or None
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    for key in ("href", "url", "link"):
        href = entry.get(key)
        if not isinstance(href, str) or not href:
            continue
        tail = href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if tail and tail not in ("resources", "describe"):
            return tail
    return None


def extract_service_catalog_names(doc: Any) -> List[str]:
    """Resource names from a version-root metadata response (never raises).

    Accepts the envelope shapes observed across Fusion families: a dict
    of name->node under "resources"/"items"/"elements" (matched
    case-insensitively — Oracle's envelope uses capital-R "Resources"),
    a list of name/href entries, or a bare dict whose keys are resource
    names. Container keys are never emitted as names. Entries that
    cannot be read as names are skipped. Returns [] for anything
    unrecognized — callers treat that as "no live names", never as
    "no resources exist".
    """
    try:
        if not isinstance(doc, dict):
            return []
        lowered = {k.lower(): k for k in doc.keys()
                   if isinstance(k, str)}
        for wanted in ("resources", "items", "elements"):
            if wanted not in lowered:
                continue
            pool = doc.get(lowered[wanted])
            if isinstance(pool, dict) and pool:
                return [n for n in pool.keys()
                        if isinstance(n, str) and n]
            if isinstance(pool, list) and pool:
                names = []
                for entry in pool:
                    name = _catalog_entry_name(entry)
                    if name and name not in names:
                        names.append(name)
                return names
            # Catalog key present but empty/unreadable: catalog shape with
            # no names — do NOT fall through to bare-key guessing.
            return []
        return [k for k in doc.keys()
                if isinstance(k, str) and k
                and not k.startswith("@")
                and k.lower() not in ("resources", "items", "elements")]
    except Exception:
        return []


def _template_placeholders(template: Any) -> List[str]:
    """Placeholder names inside ``{...}`` in an endpoint template."""
    if not isinstance(template, str) or not template:
        return []
    return sorted({m.group(1) for m in re.finditer(r"\{([^{}]+)\}", template)})


def _links_by_rel(node: Any) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    if not isinstance(node, dict):
        return grouped
    links = node.get("links")
    if not isinstance(links, list):
        return grouped
    for link in links:
        if isinstance(link, dict) and link.get("rel"):
            grouped.setdefault(link["rel"], []).append(link)
    return grouped


def _param_schema(param: Any) -> Optional[Dict[str, Any]]:
    """Normalize one action parameter, detecting enums/defaults generically."""
    if not isinstance(param, dict) or not param.get("name"):
        return None
    schema: Dict[str, Any] = {
        "name": param["name"],
        "type": param.get("type"),
        "mandatory": bool(param.get("mandatory", False)),
        "allowed_values": None,
        "default": None,
        "has_default": False,
        "requires_explicit": False,
    }
    for key in _ENUM_KEYS:
        values = param.get(key)
        if isinstance(values, (list, tuple)) and values \
                and all(isinstance(v, (str, int, float)) for v in values):
            schema["allowed_values"] = list(values)
            break
    for key in _DEFAULT_KEYS:
        if key in param and param[key] is not None:
            schema["default"] = param[key]
            schema["has_default"] = True
            break
    if schema["allowed_values"] is not None and not schema["has_default"]:
        # An enumerated parameter with no default cannot be defaulted by us:
        # the caller must state the value explicitly.
        schema["requires_explicit"] = True
    return schema


def discover_resource(raw_describe: Any, *, system: str,
                      resource: str) -> Dict[str, Any]:
    """Inventory one resource: CRUD, actions, children, keys (metadata-only)."""
    service = SERVICES.get(system, "")
    node = _resource_node(raw_describe, resource)
    if node is None:
        return {"resolved": False, "system": system, "resource": resource,
                "reasons": ["resource %r not present in describe metadata" % resource]}
    links = _links_by_rel(node.get("item", {}))
    actions = discover_actions(node, scope="item")
    collection_actions = discover_actions(node, scope="collection")
    children = {}
    raw_children = node.get("children")
    if isinstance(raw_children, dict):
        for name, child in raw_children.items():
            if isinstance(child, dict):
                children[name] = {
                    "actions": [a["name"] for a in discover_actions(child, scope="item")],
                }
    attributes = node.get("attributes") if isinstance(node.get("attributes"), list) else []
    primary_key = None
    for attr in attributes:
        if not isinstance(attr, dict):
            continue
        props = attr.get("properties") if isinstance(attr.get("properties"), dict) else {}
        if props.get("fnd:GLOBALLY_UNIQUE") == "true":
            primary_key = attr.get("name")
            break
    return {
        "resolved": True,
        "system": system,
        "service": service,
        "resource": resource,
        "collection_path": "%s" % resource,
        "primary_key": primary_key,
        "attribute_count": len(attributes),
        "children": children,
        "actions": actions,
        "collection_actions": collection_actions,
        "item_links": sorted({l.get("name") for l in links.get("child", [])
                              if l.get("name")}),
        "verification": "metadata-known",
    }


def discover_actions(node: Any, scope: str) -> List[Dict[str, Any]]:
    """List normalized actions for a collection/item node (metadata-only)."""
    found: List[Dict[str, Any]] = []
    if not isinstance(node, dict):
        return found
    actions = node.get("actions")
    if not isinstance(actions, list):
        return found
    by_rel = {l.get("name"): l for l in _links_by_rel(node).get("action", [])
              if l.get("name")}
    for entry in actions:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        params = []
        for raw_param in entry.get("parameters") or []:
            schema = _param_schema(raw_param)
            if schema is not None:
                params.append(schema)
        link = by_rel.get(entry["name"], {})
        found.append({
            "name": entry["name"],
            "method": entry.get("method"),
            "href": link.get("href"),
            "path_template": _relative_template(link.get("href")),
            "template_source": "metadata-link" if link.get("href") else "missing-link",
            "content_type": (entry.get("requestType") or [None])[0],
            "parameters": params,
            "result_type": entry.get("resultType"),
            "scope": scope,
        })
    return found


def discover_operation(*, system: str, resource: str, scope: str,
                       operation: str, action: Optional[str] = None,
                       child_path: Optional[str] = None,
                       record_key: Any = None,
                       raw_describe: Any = None,
                       api_version: Optional[str] = None) -> Dict[str, Any]:
    """Resolve one operation to a DiscoveredOperation dict (metadata-only).

    Never invents endpoints: every method/path comes from ``actions[]`` +
    ``rel=action`` links (business actions), CRUD entries (create/update/
    delete), or ``rel=child`` links (child scopes). Anything else is
    NOT_RESOLVED with reasons. ``api_version`` is a display fallback used
    only when no href carries a version.
    """
    service = SERVICES.get(system, "")
    if scope not in VALID_SCOPES:
        return _unresolved(system, resource, scope, operation, action,
                           ["unknown scope %r" % scope])
    node = _resource_node(raw_describe, resource)
    if node is None:
        return _unresolved(system, resource, scope, operation, action,
                           ["resource %r not present in describe metadata" % resource])
    if scope in ("child", "child_item"):
        return _discover_child(node, system, service, resource, scope,
                               child_path, operation, action, record_key)
    target_node = node.get("collection") if scope == "collection" else node.get("item")
    if not isinstance(target_node, dict):
        return _unresolved(system, resource, scope, operation, action,
                           ["scope %r absent from describe metadata" % scope])
    if operation == "business_action":
        if not action:
            return _unresolved(system, resource, scope, operation, action,
                               ["business_action requires an action name"])
        actions = {a["name"]: a for a in discover_actions(target_node, scope)
                   if a["name"]}
        entry = actions.get(action)
        if entry is None:
            return _unresolved(system, resource, scope, operation, action,
                               ["action %r not advertised for %s %s"
                                % (action, resource, scope)])
        if not entry.get("method") or not entry.get("path_template"):
            return _unresolved(system, resource, scope, operation, action,
                               ["action %r lacks usable method/link metadata" % action])
        return _operation(
            system, service, resource, scope, None, record_key, operation,
            action, entry["method"], entry["path_template"],
            entry["href"], entry["content_type"], entry["parameters"],
            entry.get("result_type"), "metadata-known", record_key,
            template_source=entry.get("template_source") or "metadata-link",
            api_version=api_version)
    if operation in CRUD_METHODS:
        entries = {a["name"]: a for a in discover_actions(target_node, scope)
                   if a["name"]}
        entry = entries.get(operation)
        if entry is None or not entry.get("method"):
            return _unresolved(system, resource, scope, operation, action,
                               ["CRUD %r not advertised with a method" % operation])
        # Prefer a metadata-link template when the CRUD entry carries one;
        # Fusion publishes none today, so this falls back to the documented
        # REST convention — explicitly marked, never silently equal.
        if entry.get("path_template"):
            path = entry["path_template"]
            template_source = entry.get("template_source") or "metadata-link"
        else:
            path = resource if scope == "collection" else "%s/{key}" % resource
            template_source = "canonical-pattern"
        return _operation(
            system, service, resource, scope, None, record_key, operation,
            None, entry["method"], path, entry.get("href"),
            entry["content_type"], [], entry.get("result_type"),
            "metadata-known", record_key, template_source=template_source,
            api_version=api_version)
    return _unresolved(system, resource, scope, operation, action,
                       ["operation %r has no resolution rule" % operation])


def _discover_child(node: Any, system: str, service: str, resource: str,
                    scope: str, child_path: Optional[str], operation: str,
                    action: Optional[str],
                    record_key: Any) -> Dict[str, Any]:
    """Walk ``child_path`` (e.g. ``lines/schedules``) through children dicts."""
    if not child_path or not isinstance(child_path, str):
        return _unresolved(system, resource, scope, operation, action,
                           ["child scope requires a child_path like 'lines'"])
    segments = [s for s in child_path.strip("/").split("/") if s and s != "child"]
    if not segments or any(s in ("..",) or "://" in s for s in segments):
        return _unresolved(system, resource, scope, operation, action,
                           ["invalid child_path %r" % child_path])
    current: Any = node.get("children")
    trail = []
    child_def: Any = None
    for segment in segments:
        if not isinstance(current, dict) or segment not in current \
                or not isinstance(current[segment], dict):
            return _unresolved(system, resource, scope, operation, action,
                               ["child %r not found under %s"
                                % (segment, "/".join(trail) or resource)])
        trail.append(segment)
        child_def = current[segment]
        current = child_def.get("children")
    child_node = (child_def.get("collection") if scope == "child"
                  else child_def.get("item"))
    if not isinstance(child_node, dict):
        # Some describes nest the child body directly without collection/item.
        child_node = child_def if isinstance(child_def.get("actions"), list) else None
    if not isinstance(child_node, dict):
        return _unresolved(system, resource, scope, operation, action,
                           ["child %r has no readable body" % child_path])
    if operation == "business_action":
        if not action:
            return _unresolved(system, resource, scope, operation, action,
                               ["business_action requires an action name"])
        actions = {a["name"]: a for a in discover_actions(child_node, scope)
                   if a["name"]}
        entry = actions.get(action)
        if entry is None or not entry.get("method") or not entry.get("path_template"):
            return _unresolved(system, resource, scope, operation, action,
                               ["action %r not advertised for child %s"
                                % (action, child_path)])
        return _operation(
            system, service, resource, scope, "/".join(trail), record_key,
            operation, action, entry["method"], entry["path_template"],
            entry["href"], entry["content_type"], entry["parameters"],
            entry.get("result_type"), "metadata-known", record_key,
            template_source=entry.get("template_source") or "metadata-link")
    if operation in CRUD_METHODS:
        entries = {a["name"]: a for a in discover_actions(child_node, scope)
                   if a["name"]}
        entry = entries.get(operation)
        if entry is None or not entry.get("method"):
            return _unresolved(system, resource, scope, operation, action,
                               ["CRUD %r not advertised for child %s"
                                % (operation, child_path)])
        if entry.get("path_template"):
            child_template = entry["path_template"]
            template_source = entry.get("template_source") or "metadata-link"
        else:
            child_template = "%s/{key}/child/%s" % (resource, "/child/".join(trail))
            template_source = "canonical-pattern"
        return _operation(
            system, service, resource, scope, "/".join(trail), record_key,
            operation, None, entry["method"], child_template,
            entry.get("href"), entry["content_type"], [], entry.get("result_type"),
            "metadata-known", record_key, template_source=template_source)
    return _unresolved(system, resource, scope, operation, action,
                       ["operation %r has no child resolution rule" % operation])


def _operation(system: str, service: str, resource: str, scope: str,
               child_path: Optional[str], record_key: Any, operation: str,
               action: Optional[str], method: str, path_template: str,
               href: Any, content_type: Any, parameters: List[Dict[str, Any]],
               result_type: Any, verification: str,
               record_id: Any = None,
               template_source: str = "metadata-link",
               api_version: Optional[str] = None) -> Dict[str, Any]:
    """Build the normalized DiscoveredOperation dict."""
    resolved: Optional[str] = None
    target = record_key if record_key is not None else record_id
    if path_template and target is not None and str(target):
        resolved = path_template.replace("{id}", str(target)).replace(
            "{key}", str(target))
    return {
        "resolved": True,
        "system": system,
        "service": service,
        "api_version": _api_version_from_href(href) or api_version,
        "resource": resource,
        "resource_path": resource if child_path is None
        else "%s/child/%s" % (resource, child_path.replace("/", "/child/")),
        "scope": scope,
        "child_path": child_path,
        "target": target,
        "endpoint_template": path_template,
        "template_source": template_source,
        "path_parameters": _template_placeholders(path_template),
        "query_parameters": [],
        "resolved_endpoint": resolved,
        "http_method": method,
        "content_type": content_type,
        "operation_type": operation,
        "operation": operation,
        "action": action,
        "parameters": parameters,
        "result_type": result_type,
        "metadata_source": "describe",
        "verification": verification,
    }


def _unresolved(system: str, resource: str, scope: str, operation: str,
                action: Optional[str], reasons: List[str]) -> Dict[str, Any]:
    return {"resolved": False, "system": system, "resource": resource,
            "scope": scope, "operation": operation, "action": action,
            "reasons": list(reasons), "verification": "not-resolved"}


def find_resources(catalog_names: List[str], query: str) -> Dict[str, Any]:
    """Match a resource name against a catalog. Ambiguity is reported."""
    if not isinstance(query, str) or not query.strip():
        return {"resolved": False, "reasons": ["empty resource query"]}
    needle = query.strip().lower()
    exact = [n for n in catalog_names if n.lower() == needle]
    if len(exact) == 1:
        return {"resolved": True, "resource": exact[0], "ambiguous": False}
    partial = [n for n in catalog_names if needle in n.lower()]
    if len(partial) == 1:
        return {"resolved": True, "resource": partial[0], "ambiguous": False,
                "note": "single partial match"}
    if not partial:
        return {"resolved": False,
                "reasons": ["no catalog resource matches %r" % query]}
    return {"resolved": False, "ambiguous": True, "candidates": sorted(partial),
            "reasons": ["ambiguous query %r matches %d resources"
                        % (query, len(partial))]}


async def discover_scope(*, system: str, resource: str, record_id: Any,
                         describe_fn, fetch_fn,
                         max_depth: int = 3) -> Dict[str, Any]:
    """Recursively walk a record's child tree with live GETs (read-only).

    ``describe_fn(resource)`` returns the root raw ``/describe`` JSON once;
    ``fetch_fn(resource_path, params)`` performs collection GETs.
    Every level records counts or marks itself unavailable with a reason —
    never claims zero children without a successful read.
    """
    tree: Dict[str, Any] = {"resource": resource, "record_id": str(record_id),
                            "children": {}, "unavailable": []}

    async def _walk(node: Any, base_path: str, depth: int) -> None:
        # Nesting comes from the inline ``children`` dicts that live Fusion
        # /describe embeds at every level (verified in fixtures).
        children = node.get("children") if isinstance(node, dict) else None
        if not isinstance(children, dict):
            return
        for name in sorted(children.keys()):
            child_path = "%s/child/%s" % (base_path, name)
            if depth >= max_depth:
                tree["unavailable"].append(
                    {"path": child_path,
                     "reason": "max depth %d reached" % max_depth})
                continue
            child_def = children[name]
            if not isinstance(child_def, dict):
                continue
            try:
                data = await fetch_fn(child_path, {"limit": "25",
                                                   "onlyData": "true"})
            except Exception as exc:
                tree["unavailable"].append(
                    {"path": child_path,
                     "reason": "child read failed: %s" % str(exc)[:160]})
                continue
            items = data.get("items", []) if isinstance(data, dict) else []
            tree["children"][child_path] = {
                "count": len(items),
                "has_more": bool(data.get("hasMore")) if isinstance(data, dict) else False,
                "sample_keys": sorted(items[0].keys())[:12] if items else [],
            }
            if depth + 1 < max_depth and items:
                first = items[0]
                key_field = next(
                    (k for k in ("POLineId", "ScheduleId", "DistributionId",
                                 "RequisitionLineId", "InvoiceId",
                                 "SupplierId", "PersonAbsenceEntryId", "PersonId")
                     if k in first), None)
                if key_field is not None:
                    await _walk(child_def, "%s/%s" % (child_path, first[key_field]),
                                depth + 1)

    root = await describe_fn(resource)
    node = _resource_node(root, resource)
    if node is None:
        tree["unavailable"].append(
            {"path": resource, "reason": "describe unavailable"})
        return tree
    await _walk(node, "%s/%s" % (resource, record_id), 0)
    return tree

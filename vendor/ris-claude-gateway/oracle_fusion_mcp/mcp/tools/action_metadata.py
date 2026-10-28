"""FSCM action-metadata parser (read-only, offline).

Parses Oracle Fusion FSCM ``/describe`` JSON into normalized action records.

Pure functions only: no HTTP, no credentials, no writes. The parser receives
Oracle's own describe metadata (a dict) and returns normalized structures.

Verified FSCM layout (GET-only harvest, 5 resources, 200+ nodes)::

    Resources.<resource>.collection.{actions, links}
    Resources.<resource>.item.{actions, links}
    Resources.<resource>.children.<child>.{collection, item}.{actions, links}
    ...children recurse (lines -> schedules -> distributions)

``actions[]`` carries method/parameters/content-types; ``links[]`` entries
with ``rel == "action"`` carry the URL template. The two correlate 1:1 on
every node observed so far; the parser verifies (never assumes) that.
"""

from typing import Any, Dict, List, Optional, Tuple

CRUD_ACTION_NAMES = frozenset({"get", "create", "upsert", "update", "delete"})

_VALID_SCOPES = ("collection", "item", "child_item")


def is_crud_action(name: Any) -> bool:
    """Case-insensitive CRUD check. Non-string names are not CRUD."""
    return isinstance(name, str) and name.lower() in CRUD_ACTION_NAMES


def _relative_template(href: Any) -> Optional[str]:
    """Reduce an absolute Oracle href to a relative ``path`` template.

    Example:
      https://host:443/fscmRestApi/resources/11.13.18.05/purchaseOrders/{id}/action/submit
      -> purchaseOrders/{id}/action/submit

    IDs are never substituted; ``{id}`` placeholders are preserved as-is.
    Returns None when no usable href is present.
    """
    if not isinstance(href, str) or not href:
        return None
    marker = "/resources/"
    if marker in href:
        _head, tail = href.split(marker, 1)
        # tail looks like 11.13.18.05/purchaseOrders/... -> drop version segment
        _version, sep, rest = tail.partition("/")
        if sep and rest:
            return rest
        return tail or None
    # Already relative (no scheme/host): accept as-is if it names an action path.
    if "://" not in href and "action/" in href:
        return href.lstrip("/")
    return None


def _annotation_descriptions(action_node: Dict[str, Any]) -> Dict[str, str]:
    """Map parameter name -> human description from annotations.parameters."""
    out: Dict[str, str] = {}
    annotations = action_node.get("annotations")
    if isinstance(annotations, dict):
        for entry in annotations.get("parameters") or []:
            if isinstance(entry, dict) and entry.get("name"):
                if isinstance(entry.get("description"), str):
                    out[entry["name"]] = entry["description"]
    return out


def normalize_parameter(
    param: Any, descriptions: Optional[Dict[str, str]] = None
) -> Optional[Dict[str, Any]]:
    """Normalize one ``actions[].parameters[]`` entry. Returns None if nameless.

    Preserves machine-readable constraint metadata (allowed-values lists and
    defaults) when the pod provides it, so downstream validation can require
    explicit values instead of guessing.
    """
    if not isinstance(param, dict) or not param.get("name"):
        return None
    descriptions = descriptions or {}
    mandatory = param.get("mandatory", False)
    normalized: Dict[str, Any] = {
        "name": param.get("name"),
        "type": param.get("type"),
        "mandatory": bool(mandatory) if isinstance(mandatory, bool) else False,
        "description": descriptions.get(param.get("name")),
    }
    for key in ("enum", "allowedValues", "allowed_values", "values"):
        values = param.get(key)
        if isinstance(values, (list, tuple)) and values \
                and all(isinstance(v, (str, int, float)) for v in values):
            normalized["allowed_values"] = list(values)
            break
    for key in ("default", "defaultValue", "default_value"):
        if key in param and param[key] is not None:
            normalized["default"] = param[key]
            normalized["has_default"] = True
            break
    else:
        normalized["has_default"] = False
    if normalized.get("allowed_values") is not None \
            and not normalized["has_default"]:
        normalized["requires_explicit"] = True
    return normalized


def normalize_action(
    resource: str,
    scope: str,
    child_path: Optional[str],
    node_kind: str,
    action_node: Dict[str, Any],
    href_by_name: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """Fuse one ``actions[]`` entry with its ``rel=action`` link href.

    Method/params/types come from ``actions[]``; the URL template comes from
    ``links[]``. Returns None for nameless entries.
    """
    if not isinstance(action_node, dict):
        return None
    name = action_node.get("name")
    if not name:
        return None
    annotations = action_node.get("annotations")
    if not isinstance(annotations, dict):
        annotations = {}
    descriptions = _annotation_descriptions(action_node)
    parameters: List[Dict[str, Any]] = []
    for raw_param in action_node.get("parameters") or []:
        normalized = normalize_parameter(raw_param, descriptions)
        if normalized is not None:
            parameters.append(normalized)
    href = href_by_name.get(name)
    ret = annotations.get("return")
    return {
        "resource": resource,
        "scope": scope,
        "child_path": child_path,
        "node": node_kind,
        "action": name,
        "method": action_node.get("method"),
        "path": _relative_template(href),
        "href": href,
        "title": annotations.get("title"),
        "description": annotations.get("description"),
        "parameters": parameters,
        "requestTypes": list(action_node.get("requestType") or []),
        "responseTypes": list(action_node.get("responseType") or []),
        "resultType": action_node.get("resultType"),
        "batchEnabled": action_node.get("batchEnabled", False),
        "kind": "crud" if is_crud_action(name) else "business",
        "source": _source_label(scope, child_path, node_kind),
    }


def _source_label(
    scope: str, child_path: Optional[str], node_kind: str
) -> str:
    """Provenance tag showing exactly which describe node an entry came from."""
    if scope == "collection":
        return "collection.actions+collection.links"
    if scope == "item":
        return "item.actions+item.links"
    base = "children.%s.%s.actions+%s.links" % (
        (child_path or "").replace("/", ".children."),
        node_kind,
        node_kind,
    )
    return base


def _action_link_hrefs(node: Any) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """Return ({name: href} for rel=action links, raw link list)."""
    href_by_name: Dict[str, str] = {}
    raw_links: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        links = node.get("links")
        if isinstance(links, list):
            for link in links:
                if not isinstance(link, dict):
                    continue
                raw_links.append(link)
                if link.get("rel") == "action" and link.get("name"):
                    href_by_name[link["name"]] = link.get("href")
    return href_by_name, raw_links


def parse_node(
    resource: str,
    scope: str,
    child_path: Optional[str],
    node_kind: str,
    node: Any,
) -> Dict[str, Any]:
    """Parse one collection/item node into normalized actions + agreement."""
    actions: List[Dict[str, Any]] = []
    warnings: List[str] = []
    if not isinstance(node, dict):
        return {
            "actions": actions,
            "agreement": {"actions_links_match": True, "only_in_actions": [], "only_in_links": []},
            "warnings": ["node is not an object at %s" % _source_label(scope, child_path, node_kind)],
        }
    raw_actions = node.get("actions")
    if raw_actions is None:
        warnings.append("no actions[] at %s" % _source_label(scope, child_path, node_kind))
        raw_actions = []
    elif not isinstance(raw_actions, list):
        warnings.append("actions[] is not a list at %s" % _source_label(scope, child_path, node_kind))
        raw_actions = []
    href_by_name, _raw_links = _action_link_hrefs(node)
    for entry in raw_actions:
        normalized = normalize_action(resource, scope, child_path, node_kind, entry, href_by_name)
        if normalized is None:
            warnings.append("nameless action entry at %s" % _source_label(scope, child_path, node_kind))
            continue
        if normalized["kind"] == "business" and not normalized["href"]:
            warnings.append(
                "business action %r has no rel=action link at %s"
                % (normalized["action"], _source_label(scope, child_path, node_kind))
            )
        actions.append(normalized)
    biz_names = sorted(a["action"] for a in actions if a["kind"] == "business")
    link_names = sorted(href_by_name.keys())
    only_in_actions = sorted(set(biz_names) - set(link_names))
    only_in_links = sorted(set(link_names) - set(biz_names))
    return {
        "actions": actions,
        "agreement": {
            "actions_links_match": not only_in_actions and not only_in_links,
            "only_in_actions": only_in_actions,
            "only_in_links": only_in_links,
        },
        "warnings": warnings,
    }


def _unwrap_resource(raw: Any, resource: str) -> Tuple[Any, List[str]]:
    """Return (resource node, warnings) handling the Resources.<name> envelope."""
    warnings: List[str] = []
    node: Any = raw
    if isinstance(raw, dict) and isinstance(raw.get("Resources"), dict):
        resources = raw["Resources"]
        if resource in resources:
            node = resources[resource]
        elif resources:
            fallback = next(iter(resources.keys()))
            node = resources[fallback]
            warnings.append(
                "resource %r not found; fell back to %r" % (resource, fallback)
            )
        else:
            node = None
            warnings.append("empty Resources envelope")
    if not isinstance(node, dict):
        warnings.append("resource node for %r is not an object" % resource)
        return None, warnings
    return node, warnings


def parse_describe(resource: str, raw: Any) -> Dict[str, Any]:
    """Parse a full FSCM ``/describe`` payload (collection/item/children)."""
    node, warnings = _unwrap_resource(raw, resource)
    if node is None:
        return {
            "resource": resource,
            "collection": {"actions": []},
            "item": {"actions": []},
            "children": {},
            "agreement": {"actions_links_match": True, "mismatches": []},
            "warnings": warnings,
        }
    collection = parse_node(resource, "collection", None, "collection", node.get("collection", {}))
    item = parse_node(resource, "item", None, "item", node.get("item", {}))
    children: Dict[str, Any] = {}
    mismatches: List[Dict[str, Any]] = []
    if not collection["agreement"]["actions_links_match"]:
        mismatches.append({"scope": "collection", **collection["agreement"]})
    if not item["agreement"]["actions_links_match"]:
        mismatches.append({"scope": "item", **item["agreement"]})
    warnings = list(warnings) + collection["warnings"] + item["warnings"]

    def _walk(children_node: Any, prefix: str) -> None:
        if not isinstance(children_node, dict):
            return
        for child_name, child_node in children_node.items():
            if not isinstance(child_node, dict):
                continue
            child_path = "%s/%s" % (prefix, child_name) if prefix else str(child_name)
            for node_kind in ("collection", "item"):
                sub = child_node.get(node_kind)
                if sub is None:
                    continue
                parsed = parse_node(resource, "child_item", child_path, node_kind, sub)
                key = "%s:%s" % (child_path, node_kind)
                children[key] = {"child_path": child_path, "node": node_kind, **parsed}
                if not parsed["agreement"]["actions_links_match"]:
                    mismatches.append(
                        {"scope": "child_item", "child_path": child_path,
                         "node": node_kind, **parsed["agreement"]}
                    )
                warnings.extend(parsed["warnings"])
            _walk(child_node.get("children"), child_path)

    _walk(node.get("children"), "")
    return {
        "resource": resource,
        "collection": collection,
        "item": item,
        "children": children,
        "agreement": {
            "actions_links_match": not mismatches,
            "mismatches": mismatches,
        },
        "warnings": warnings,
    }


def business_actions(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter normalized entries to business actions only (no hard-coded names)."""
    return [e for e in entries if e.get("kind") == "business"]


def select_child_path(
    parsed: Dict[str, Any], child_path: str
) -> Dict[str, Any]:
    """Return the parsed subtree(s) for ``child_path`` (exact or descendants)."""
    selected = {
        key: value
        for key, value in (parsed.get("children") or {}).items()
        if value.get("child_path") == child_path
        or (value.get("child_path") or "").startswith(child_path + "/")
    }
    return selected

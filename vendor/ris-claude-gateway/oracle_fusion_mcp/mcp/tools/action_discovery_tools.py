"""Read-only FSCM action discovery tool (GET /describe only, normalized output).

``fusion_describe_actions`` answers questions like "what actions are available
for purchase orders?" without executing anything. It performs exactly one GET
(``{resource}/describe`` via ``OracleClient._fetch``) � child metadata already
rides along in that response, so no per-child requests are made.

This module never imports or calls ``_post`` / ``_patch`` / ``_post_action``.
"""

from typing import Any, Dict, List, Optional

from oracle_fusion_mcp._types import Request
from ...auth.entra import ValidatedUser
from ...config.settings import get_settings
from ...oracle.client import OracleClient
from ._helpers import _text_result
from .action_metadata import business_actions, parse_describe, select_child_path
from .generic_tools import _validate_resource_path


def _validate_child_path(child_path: str) -> List[str]:
    """Split and validate ``lines`` / ``lines/schedules`` style paths."""
    if not isinstance(child_path, str) or not child_path.strip():
        raise ValueError("child_path must be a non-empty string")
    raw = child_path.strip()
    if raw.startswith("/"):
        raise ValueError("child_path must not start with '/'")
    if ".." in raw:
        raise ValueError("child_path must not contain '..'")
    low = raw.lower()
    if low.startswith("http://") or low.startswith("https://") or "://" in raw:
        raise ValueError("child_path must be a relative resource path")
    segments = raw.strip("/").split("/")
    for segment in segments:
        if not segment:
            raise ValueError("invalid empty child_path segment")
    return segments


TOOLS = [
    {
        "name": "fusion_describe_actions",
        "description": "Describe available FSCM REST actions for a resource (read-only). GETs {resource}/describe once and returns normalized collection/item/child actions with methods, URL templates, parameters and content types. Business actions only by default; never executes anything.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "FSCM REST resource e.g. purchaseOrders, suppliers, invoices, purchaseRequisitions, draftPurchaseOrders"},
                "child_path": {"type": "string", "description": "Optional child path e.g. lines or lines/schedules or lines/schedules/distributions"},
                "include_crud": {"type": "boolean", "description": "Include CRUD actions (get/create/upsert/update/delete). Default false = business actions only.", "default": False},
                "include_params": {"type": "boolean", "description": "Include action parameter details. Default true.", "default": True},
                "refresh": {"type": "boolean", "description": "Reserved for future caching; currently always fetches live. Default false.", "default": False},
            },
            "required": ["resource"],
        },
    },
]


def _project_entry(entry: Dict[str, Any], include_params: bool) -> Dict[str, Any]:
    """Claude-friendly projection of one normalized action (metadata only)."""
    projected: Dict[str, Any] = {
        "action": entry.get("action"),
        "method": entry.get("method"),
        "path": entry.get("path"),
        "scope": entry.get("scope"),
        "child_path": entry.get("child_path"),
        "title": entry.get("title"),
        "description": entry.get("description"),
        "requestTypes": entry.get("requestTypes") or [],
        "responseTypes": entry.get("responseTypes") or [],
        "resultType": entry.get("resultType"),
        "batchEnabled": entry.get("batchEnabled", False),
        "kind": entry.get("kind"),
    }
    if include_params:
        projected["parameters"] = entry.get("parameters") or []
    if entry.get("href"):
        projected["href"] = entry.get("href")
    return projected


def _project_list(
    entries: List[Dict[str, Any]], include_crud: bool, include_params: bool
) -> List[Dict[str, Any]]:
    if not include_crud:
        entries = business_actions(entries)
    return [_project_entry(e, include_params) for e in entries]


async def handle_fusion_describe_actions(
    request: Request,
    session: ValidatedUser,
    params: Dict[str, Any],
    oracle_client: OracleClient,
) -> Dict[str, Any]:
    resource = params.get("resource", "")
    child_path = params.get("child_path")
    include_crud = params.get("include_crud", False)
    include_params = params.get("include_params", True)

    _validate_resource_path(resource)
    resource = resource.strip()
    segments: Optional[List[str]] = None
    normalized_child: Optional[str] = None
    if child_path is not None:
        segments = _validate_child_path(child_path)
        normalized_child = "/".join(segments)

    if not isinstance(include_crud, bool):
        include_crud = bool(include_crud)
    if not isinstance(include_params, bool):
        include_params = bool(include_params)

    # Single read-only GET; child metadata is embedded in the response.
    data = await oracle_client._fetch("%s/describe" % resource, {})
    parsed = parse_describe(resource, data)

    settings = get_settings()
    describe_endpoint = "GET %s/%s/describe" % (
        settings.oracle_api_base,
        resource,
    )

    if normalized_child is not None:
        selected = select_child_path(parsed, normalized_child)
        if not selected:
            available = sorted(
                {v.get("child_path") for v in (parsed.get("children") or {}).values()}
            )
            return _text_result({
                "error": "child_path %r not found under resource %r" % (normalized_child, resource),
                "available_child_paths": available,
            })
        children_out: Dict[str, Any] = {}
        for key in sorted(selected.keys()):
            value = selected[key]
            projected = _project_list(value.get("actions") or [], include_crud, include_params)
            if projected or include_crud:
                children_out[key] = projected
        return _text_result({
            "resource": resource,
            "describe_endpoint": describe_endpoint,
            "child_path": normalized_child,
            "actions": {"children": children_out},
            "agreement": parsed.get("agreement"),
            "warnings": parsed.get("warnings") or [],
        })

    children_out_all: Dict[str, Any] = {}
    for key in sorted((parsed.get("children") or {}).keys()):
        value = parsed["children"][key]
        projected = _project_list(value.get("actions") or [], include_crud, include_params)
        # Omit CRUD-only child nodes from the default business view to stay concise,
        # unless include_crud explicitly asks for everything.
        if projected or include_crud:
            children_out_all[key] = projected

    return _text_result({
        "resource": resource,
        "describe_endpoint": describe_endpoint,
        "actions": {
            "collection": _project_list(
                (parsed.get("collection") or {}).get("actions") or [], include_crud, include_params
            ),
            "item": _project_list(
                (parsed.get("item") or {}).get("actions") or [], include_crud, include_params
            ),
            "children": children_out_all,
        },
        "agreement": parsed.get("agreement"),
        "warnings": parsed.get("warnings") or [],
    })


HANDLERS = {
    "fusion_describe_actions": handle_fusion_describe_actions,
}

"""Generic read-only FSCM discovery tools (describe, query, get)."""

from typing import Any, Dict, Optional

from oracle_fusion_mcp._types import Request
from ...auth.entra import ValidatedUser
from ...oracle.client import OracleClient
from ._helpers import _text_result


def _validate_resource_path(resource: str) -> None:
    """Reject empty, absolute, traversal or URL-like resource paths."""
    if not resource or not resource.strip():
        raise ValueError("resource is required")
    r = resource.strip()
    if r.startswith("/"):
        raise ValueError("resource must not start with '/'")
    if ".." in r:
        raise ValueError("resource must not contain '..'")
    low = r.lower()
    if low.startswith("http://") or low.startswith("https://"):
        raise ValueError("resource must be a relative Fusion REST resource path")
    if "://" in r:
        raise ValueError("resource must be a relative path")


TOOLS = [
    {
        "name": "fusion_describe_resource",
        "description": "Describe an arbitrary FSCM REST resource using Oracle Fusion's /describe endpoint. Top-level: GET {oracle_api_base}/{resource}/describe. Child: GET {oracle_api_base}/{resource}/child/{child}/describe. Uses settings.oracle_api_base (no hard-coded version).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "FSCM REST resource e.g. purchaseRequisitions, purchaseOrders, suppliers, invoices"},
                "child": {"type": "string", "description": "Optional child collection e.g. lines"},
            },
            "required": ["resource"],
        },
    },
    {
        "name": "fusion_query_resource",
        "description": "Execute a generic READ-ONLY GET against an FSCM collection. Uses OracleClient._fetch and _build_params. Supports q, fields, expand, limit, offset, totalResults. GET-only, relative resource path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string", "description": "Relative FSCM resource path e.g. purchaseRequisitions or purchaseRequisitions/123/child/lines"},
                "q": {"type": "string"},
                "fields": {"type": "string", "description": "Comma-separated fields e.g. Requisition,RequisitionHeaderId"},
                "expand": {"type": "string", "description": "Expand child e.g. lines"},
                "limit": {"type": "integer", "default": 25},
                "offset": {"type": "integer", "default": 0},
                "totalResults": {"type": "boolean", "default": False},
            },
            "required": ["resource"],
        },
    },
    {
        "name": "fusion_get_resource_record",
        "description": "Retrieve one FSCM resource record by ID via GET {oracle_api_base}/{resource}/{record_id}. Supports fields and expand. GET-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "resource": {"type": "string"},
                "record_id": {"type": "string", "description": "Record ID e.g. 300000012345678"},
                "fields": {"type": "string"},
                "expand": {"type": "string"},
            },
            "required": ["resource", "record_id"],
        },
    },
]


async def handle_fusion_describe_resource(
    request: Request,
    session: ValidatedUser,
    params: Dict[str, Any],
    oracle_client: OracleClient,
) -> Dict[str, Any]:
    resource = params.get("resource", "")
    child = params.get("child")

    _validate_resource_path(resource)
    if child is not None:
        if not isinstance(child, str) or not child.strip():
            raise ValueError("child must be a non-empty string")
        if ".." in child or child.strip().startswith("/"):
            raise ValueError("invalid child")
        if child.strip().lower().startswith("http"):
            raise ValueError("child must be relative")
        endpoint = f"{resource.strip()}/child/{child.strip()}/describe"
    else:
        endpoint = f"{resource.strip()}/describe"

    # Use OracleClient._fetch - settings.oracle_api_base is used internally (no hard-coded version)
    data = await oracle_client._fetch(endpoint, {})
    return _text_result(data)


async def handle_fusion_query_resource(
    request: Request,
    session: ValidatedUser,
    params: Dict[str, Any],
    oracle_client: OracleClient,
) -> Dict[str, Any]:
    resource = params.get("resource", "")
    _validate_resource_path(resource)
    resource = resource.strip()

    q = params.get("q")
    fields = params.get("fields")
    expand = params.get("expand")
    limit = params.get("limit", 25)
    offset = params.get("offset", 0)
    total_results = params.get("totalResults", False)

    # Normalize fields: comma-separated string -> List[str]
    fields_list: Optional[list] = None
    if fields is not None:
        if isinstance(fields, str):
            fields_list = [f.strip() for f in fields.split(",") if f.strip()]
            if not fields_list:
                fields_list = None
        elif isinstance(fields, list):
            fields_list = fields
        else:
            raise ValueError("fields must be a comma-separated string")

    # Normalize expand
    expand_val: Optional[str] = None
    if expand is not None:
        if not isinstance(expand, str) or not expand.strip():
            raise ValueError("expand must be a non-empty string")
        expand_val = expand.strip()
        if ".." in expand_val or expand_val.lower().startswith("http"):
            raise ValueError("invalid expand")

    # Normalize limit/offset
    try:
        limit = int(limit)
    except Exception:
        raise ValueError("limit must be integer")
    try:
        offset = int(offset)
    except Exception:
        raise ValueError("offset must be integer")

    if not isinstance(total_results, bool):
        # allow string "true"/"false" or int
        if isinstance(total_results, str):
            total_results = total_results.lower() in ("true", "1", "yes")
        else:
            total_results = bool(total_results)

    # Use existing _build_params - no arbitrary URL, GET-only, relative path already validated
    query_params = oracle_client._build_params(
        fields=fields_list,
        q=q,
        expand=expand_val,
        limit=limit,
        offset=offset,
        total_results=total_results,
    )

    data = await oracle_client._fetch(resource, query_params)
    return _text_result(data)


async def handle_fusion_get_resource_record(
    request: Request,
    session: ValidatedUser,
    params: Dict[str, Any],
    oracle_client: OracleClient,
) -> Dict[str, Any]:
    resource = params.get("resource", "")
    record_id = params.get("record_id", "")
    fields = params.get("fields")
    expand = params.get("expand")

    _validate_resource_path(resource)
    if not record_id or not str(record_id).strip():
        raise ValueError("record_id is required")
    rid = str(record_id).strip()
    if ".." in rid or rid.startswith("/") or rid.lower().startswith("http"):
        raise ValueError("invalid record_id")
    if "://" in rid:
        raise ValueError("invalid record_id")

    endpoint = f"{resource.strip()}/{rid}"

    fields_list: Optional[list] = None
    if fields is not None:
        if isinstance(fields, str):
            fields_list = [f.strip() for f in fields.split(",") if f.strip()]
            if not fields_list:
                fields_list = None
        elif isinstance(fields, list):
            fields_list = fields
        else:
            raise ValueError("fields must be a comma-separated string")

    expand_val: Optional[str] = None
    if expand is not None:
        if not isinstance(expand, str) or not expand.strip():
            raise ValueError("expand must be a non-empty string")
        expand_val = expand.strip()
        if ".." in expand_val or expand_val.lower().startswith("http"):
            raise ValueError("invalid expand")

    # Use _build_params if fields or expand supplied, otherwise empty params
    if fields_list is not None or expand_val is not None:
        query_params = oracle_client._build_params(
            fields=fields_list,
            expand=expand_val,
            limit=25,
            offset=0,
        )
        # _build_params adds limit/offset/onlyData - remove limit/offset for single record? keep onlyData
        # For single record we don't need pagination, but keep as is - Oracle ignores limit for single ID path
        # To avoid sending limit/offset unnecessarily, we keep only fields/expand/onlyData
        # However _build_params always adds limit/offset - acceptable for Oracle GET
        # Alternatively use minimal params:
        # query_params = {}
        # if fields_list: query_params["fields"]=",".join(fields_list)
        # if expand_val: query_params["expand"]=expand_val
        # query_params["onlyData"]="true"
        # Use the helper to stay compliant with spec "If fields or expand are supplied, use _build_params()"
        pass
    else:
        query_params = {}

    # Ensure onlyData for consistency
    if "onlyData" not in query_params:
        query_params["onlyData"] = "true"

    data = await oracle_client._fetch(endpoint, query_params)
    return _text_result(data)


HANDLERS = {
    "fusion_describe_resource": handle_fusion_describe_resource,
    "fusion_query_resource": handle_fusion_query_resource,
    "fusion_get_resource_record": handle_fusion_get_resource_record,
}

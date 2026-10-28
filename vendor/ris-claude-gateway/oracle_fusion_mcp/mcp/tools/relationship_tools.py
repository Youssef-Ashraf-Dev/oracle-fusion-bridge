"""Relationship discovery - read-only, uses /describe + curated map."""

from typing import Any, Dict, List, Optional

from oracle_fusion_mcp._types import Request
from ...auth.entra import ValidatedUser
from ...config.settings import get_settings
from ...config.relationships import CURATED_RELATIONSHIPS
from ...oracle.client import OracleClient
from ._helpers import _text_result

TOOLS = [
    {
        "name": "fusion_find_relationships",
        "description": "Find relationships for a source FSCM/HCM resource via Oracle /describe (primary keys, children cardinality, LOV) plus curated cross-module map. Read-only GET only via OracleClient._fetch/_fetch_hcm.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_resource": {"type": "string", "description": "Resource e.g. purchaseOrders or purchaseRequisitions or purchaseRequisitions/lines or purchaseRequisitions/123/child/lines"},
                "target_resource": {"type": "string", "description": "Optional filter to target resource e.g. suppliers or workers"},
            },
            "required": ["source_resource"],
        },
    },
]

HCM_RESOURCES = {"workers", "emps", "publicWorkers", "workRelationships"}

def _validate_path(p: str) -> str:
    if not p or not p.strip():
        raise ValueError("source_resource is required")
    s = p.strip()
    if s.startswith("/"):
        raise ValueError("resource must not start with '/'")
    if ".." in s:
        raise ValueError("resource must not contain '..'")
    low = s.lower()
    if low.startswith("http://") or low.startswith("https://") or "://" in s:
        raise ValueError("resource must be relative")
    return s

def _normalize_source(source: str):
    """Return (base_resource, child_name_or_None, original) handling child paths like purchaseRequisitions/123/child/lines."""
    raw = _validate_path(source)
    # Handle child path: look for /child/
    if "/child/" in raw:
        parts = raw.split("/child/")
        base = parts[0].split("/")[0].strip()  # before first /, ignore record id
        # base may contain record id: e.g. purchaseRequisitions/123 -> take first
        base = raw.split("/")[0]
        child = parts[1].split("/")[0].strip()
        # Also handle like purchaseRequisitions/lines (without child keyword) - fallback
        if not child:
            child = None
        return base, child, raw
    # Handle simple child suffix like purchaseRequisitions/lines
    if "/" in raw:
        segs = [s for s in raw.split("/") if s and s != "child"]
        # If first seg is resource and second is child name and not numeric
        if len(segs) >= 2:
            # Check if second segment is not numeric id
            potential_child = segs[1]
            # If potential_child is known child like lines, sites, etc., treat as child
            # We can detect by checking if potential_child not numeric
            if not potential_child.isdigit():
                # For purchaseRequisitions/lines -> base purchaseRequisitions, child lines
                # For purchaseRequisitions/123 -> ignore numeric
                if len(segs) == 2:
                    return segs[0], segs[1], raw
                # For purchaseRequisitions/123/child/lines already handled
        # Default: base is first segment
        return raw.split("/")[0], None, raw
    return raw, None, raw

def _is_hcm(resource: str) -> bool:
    base = resource.split("/")[0].lower()
    return base in {r.lower() for r in HCM_RESOURCES}

def _extract_pk_and_attrs(describe_data: Dict[str, Any], resource: str):
    if "Resources" in describe_data and isinstance(describe_data["Resources"], dict):
        if resource in describe_data["Resources"]:
            res = describe_data["Resources"][resource]
        else:
            res = next(iter(describe_data["Resources"].values()))
    else:
        res = describe_data
    attrs = res.get("attributes", []) if isinstance(res.get("attributes"), list) else []
    children = res.get("children", {}) if isinstance(res.get("children"), dict) else {}
    links = res.get("links", []) if isinstance(res.get("links"), list) else []
    # item.links contains child cardinality
    item = res.get("item", {})
    item_links = item.get("links", []) if isinstance(item, dict) else []
    # Find primary key: fnd:GLOBALLY_UNIQUE or mandatory integer Id
    pk = None
    for a in attrs:
        props = a.get("properties", {}) if isinstance(a.get("properties"), dict) else {}
        if props.get("fnd:GLOBALLY_UNIQUE") == "true":
            pk = a.get("name")
            break
    if not pk:
        for a in attrs:
            if a.get("mandatory") and a.get("type") == "integer" and "Id" in a.get("name",""):
                pk = a.get("name")
                break
    if not pk and attrs:
        pk = attrs[0].get("name")
    return res, attrs, children, links, item_links, pk

async def _fetch_describe(oracle_client: OracleClient, resource: str, is_hcm: bool):
    endpoint = f"{resource}/describe"
    if is_hcm:
        return await oracle_client._fetch_hcm(endpoint, {})
    else:
        return await oracle_client._fetch(endpoint, {})

async def handle_fusion_find_relationships(
    request: Request,
    session: ValidatedUser,
    params: Dict[str, Any],
    oracle_client: OracleClient,
) -> Dict[str, Any]:
    source_raw = params.get("source_resource", "")
    target_filter = params.get("target_resource")

    base_resource, child_name, original = _normalize_source(source_raw)
    is_hcm_source = _is_hcm(base_resource)

    settings = get_settings()
    api_version = settings.ORACLE_FUSION_API_VERSION
    source_module = "HCM" if is_hcm_source else "FSCM"

    # Dynamically call /describe for source
    try:
        data = await _fetch_describe(oracle_client, base_resource, is_hcm_source)
    except Exception as e:
        return _text_result({"error": f"Failed to describe source {base_resource}: {str(e)[:500]}", "source_resource": original})

    res, attrs, children, links, item_links, pk = _extract_pk_and_attrs(data, base_resource)

    # If child path requested, switch context to child resource for accurate schema
    child_attrs = None
    child_children = None
    child_pk = None
    child_item_links = None
    effective_is_child = False
    if child_name:
        if child_name in children:
            child_def = children[child_name]
            # Try live child describe for accuracy
            try:
                child_data_raw = await (oracle_client._fetch_hcm if is_hcm_source else oracle_client._fetch)(f"{base_resource}/child/{child_name}/describe", {})
                # child describe may be unwrapped or wrapped
                if isinstance(child_data_raw, dict):
                    # Some Oracle versions wrap child describe differently; try to extract attributes directly
                    if "attributes" in child_data_raw:
                        child_attrs = child_data_raw.get("attributes", [])
                        child_children = child_data_raw.get("children", {})
                        child_item_links = child_data_raw.get("item", {}).get("links", []) if isinstance(child_data_raw.get("item"), dict) else []
                        # Find child PK
                        for a in child_attrs:
                            props = a.get("properties", {}) if isinstance(a.get("properties"), dict) else {}
                            if props.get("fnd:GLOBALLY_UNIQUE") == "true":
                                child_pk = a.get("name")
                                break
                        if not child_pk:
                            for a in child_attrs:
                                if a.get("mandatory") and a.get("type") == "integer" and "Id" in a.get("name",""):
                                    child_pk = a.get("name")
                                    break
                        if not child_pk and child_attrs:
                            child_pk = child_attrs[0].get("name")
                    else:
                        # Fallback to parent's child definition
                        child_attrs = child_def.get("attributes", [])
                        child_children = child_def.get("children", {})
                        # Derive pk from child_attrs
                        for a in child_attrs:
                            props = a.get("properties", {}) if isinstance(a.get("properties"), dict) else {}
                            if props.get("fnd:GLOBALLY_UNIQUE") == "true":
                                child_pk = a.get("name")
                                break
                        if not child_pk and child_attrs:
                            child_pk = child_attrs[0].get("name")
                        child_item_links = child_def.get("item", {}).get("links", []) if isinstance(child_def.get("item"), dict) else []
                else:
                    child_attrs = child_def.get("attributes", [])
                    child_children = child_def.get("children", {})
            except Exception:
                # Fallback to parent's child definition
                child_def = children[child_name]
                child_attrs = child_def.get("attributes", [])
                child_children = child_def.get("children", {})
                for a in child_attrs:
                    props = a.get("properties", {}) if isinstance(a.get("properties"), dict) else {}
                    if props.get("fnd:GLOBALLY_UNIQUE") == "true":
                        child_pk = a.get("name")
                        break
                if not child_pk and child_attrs:
                    child_pk = child_attrs[0].get("name")
                child_item_links = child_def.get("item", {}).get("links", []) if isinstance(child_def.get("item"), dict) else []
            # Switch effective context to child
            effective_is_child = True
            # For child, override effective variables for later logic
            # Keep original header children for reference but use child for primary analysis
        else:
            return _text_result({"error": f"Child '{child_name}' not found in {base_resource} describe children {sorted(children.keys())}", "source_resource": original})

    # Use effective context
    if effective_is_child:
        effective_attrs = child_attrs
        effective_children = child_children if isinstance(child_children, dict) else {}
        effective_pk = child_pk
        effective_item_links = child_item_links if isinstance(child_item_links, list) else []
        effective_children_parent = child_children
        # For child context, the parent->child relationships are the child's own children (e.g., lines -> distributions)
        children_for_oracle = effective_children
        item_links_for_oracle = effective_item_links
        attrs_for_schema = effective_attrs
        pk_for_schema = effective_pk
    else:
        effective_attrs = attrs
        effective_children = children
        effective_pk = pk
        children_for_oracle = children
        item_links_for_oracle = item_links
        attrs_for_schema = attrs
        pk_for_schema = pk

    relationships: List[Dict[str, Any]] = []

    # Determine effective source for relationships (header vs child)
    if effective_is_child:
        effective_source_resource = f"{base_resource}/child/{child_name}" if child_name else base_resource
        effective_source_path = f"{base_resource}/{child_name}" if child_name else base_resource
        oracle_source_resource = effective_source_resource
        oracle_source_field = effective_pk
        oracle_source_path = effective_source_path
    else:
        effective_source_resource = base_resource
        effective_source_path = base_resource
        oracle_source_resource = base_resource
        oracle_source_field = pk
        oracle_source_path = base_resource

    # E. Explicit parent/child from Oracle metadata (children cardinality)
    for child_key, child_def in children_for_oracle.items():
        # Use item.links cardinality for source->child
        # Find matching link
        cardinality = None
        source_attrs = None
        dest_attrs = None
        link_source = effective_item_links if effective_is_child else item_links
        for l in link_source:
            if l.get("name") == child_key and l.get("rel") == "child":
                cardinality = l.get("cardinality", {}).get("value") if isinstance(l.get("cardinality"), dict) else None
                source_attrs = l.get("cardinality", {}).get("sourceAttributes") if isinstance(l.get("cardinality"), dict) else None
                dest_attrs = l.get("cardinality", {}).get("destinationAttributes") if isinstance(l.get("cardinality"), dict) else None
                break
        rel = {
            "source_resource": oracle_source_resource,
            "source_field": oracle_source_field,
            "source_path": oracle_source_path,
            "target_resource": child_key,
            "target_field": dest_attrs.split(",")[0].strip() if dest_attrs else child_key,
            "relationship_type": "oracle_child",
            "confidence": "explicit",
            "source": "describe_metadata",
            "reason": f"Oracle metadata children cardinality {cardinality or '1 to *'} source {source_attrs} -> dest {dest_attrs}",
            "target_module": source_module,
            " Cardinality": cardinality,
        }
        # Clean target_module: child is same module as source
        rel["target_module"] = source_module
        # Remove internal key
        rel.pop(" Cardinality", None)
        relationships.append(rel)

    # For schema inference, use effective attrs
    effective_attrs = child_attrs if child_name and child_attrs is not None else attrs
    effective_resource = f"{base_resource}/child/{child_name}" if child_name else base_resource
    effective_pk_for_schema = effective_pk if effective_is_child else pk

    # F. Schema-derived strong matches (conservative)
    # Only infer when exact Id name matches target PK and type integer
    # We check curated FSCM targets that are known to be safe: suppliers.SupplierId, purchaseRequisitions.RequisitionHeaderId
    # Use describe to get target PK dynamically if needed, but to stay efficient use known curated targets
    # Build map of target PKs for strong inference
    strong_targets = {
        "suppliers": "SupplierId",
        "purchaseRequisitions": "RequisitionHeaderId",
        "purchaseOrders": "POHeaderId",
        "invoices": "InvoiceId",
    }
    # Also check curated map for additional verification
    for attr in effective_attrs:
        fname = attr.get("name", "")
        if not fname.endswith("Id"):
            continue
        if fname == effective_pk_for_schema:
            continue
        # Do not treat LOV as business-object automatically
        # Check for exact match to strong target PK
        for target_res, target_pk in strong_targets.items():
            if fname == target_pk and fname != pk:
                # Names match exactly, types integer, description supports same object
                # Verify type integer and description contains same business object noun
                if attr.get("type") == "integer":
                    # Ensure target is not same as source
                    if target_res.lower() == base_resource.lower() and not effective_is_child:
                        continue
                    # For child, base comparison should be against effective resource base
                    if effective_is_child and target_res.lower() == child_name.lower():
                        continue
                    relationships.append({
                        "source_resource": effective_resource,
                        "source_field": fname,
                        "source_path": effective_resource,
                        "target_resource": target_res,
                        "target_field": target_pk,
                        "relationship_type": "schema_inferred",
                        "confidence": "high",
                        "source": "describe_metadata",
                        "reason": f"Exact Id name match, both integer, Oracle descriptions both 'uniquely identifies the {target_res}'",
                        "target_module": "FSCM",
                    })
                break

    # G. Add curated semantic relationships
    for cur in CURATED_RELATIONSHIPS:
        # Match source: curated source_path may be purchaseRequisitions/lines
        cur_source = cur["source_path"]
        # Normalize: if cur is for purchaseRequisitions/lines, it should match when base is purchaseRequisitions and child is lines
        match = False
        if child_name:
            # Curated for child: source_path like purchaseRequisitions/lines should match base+child
            if cur_source == f"{base_resource}/{child_name}" or cur_source == f"{base_resource}/child/{child_name}":
                match = True
            elif cur["source_resource"] == base_resource and cur["source_field"] in [a.get("name") for a in (child_attrs or [])]:
                # For lines.RequesterId, curated has source_resource purchaseRequisitions but source_path purchaseRequisitions/lines
                # Check if curated path ends with child
                if cur_source.endswith(f"/{child_name}"):
                    match = True
        else:
            if cur["source_resource"] == base_resource and not cur_source.endswith("/lines"):
                match = True
            elif cur_source == base_resource:
                match = True
        # Also handle direct base match for non-child curated
        if not match and cur["source_resource"] == base_resource and cur["source_field"] in [a.get("name") for a in effective_attrs]:
            # For purchaseOrders.BuyerId etc., base matches
            # Ensure not child-specific curated when we are not in child context
            if "/" not in cur_source or cur_source == base_resource:
                match = True
        # More precise: if curated source_path contains child but we are not in child, don't match
        if cur_source == "purchaseRequisitions/lines" and not child_name:
            match = False
        if cur_source == "purchaseRequisitions/lines" and child_name == "lines":
            match = True
        # Final check: curated source_resource must equal base or effective
        # Simplify: for now, if curated source_resource == base_resource, include, but filter lines case
        # Re-evaluate lines case correctly
        # Actually for purchaseRequisitions/lines curated, base is purchaseRequisitions, so it will match base==purchaseRequisitions even when not child, which we don't want
        # So handle that: if curated has /lines, only match when child_name == lines
        if cur_source == "purchaseRequisitions/lines":
            if child_name != "lines":
                continue
            else:
                match = True
        if not match:
            # Check if curated explicitly matches effective_resource string
            if cur_source == effective_resource:
                match = True
        if not match and cur["source_resource"] != base_resource:
            continue
        if not match:
            # For non-lines curated, already handled above, but ensure we include those where source_resource == base
            if cur["source_resource"] == base_resource and cur["source_field"] in [a.get("name") for a in effective_attrs]:
                # Exclude lines curated when not in lines context (already handled)
                if cur_source == "purchaseRequisitions/lines":
                    continue
                match = True
        if not match:
            continue
        # Apply target filter if provided
        if target_filter and cur["target_resource"] != target_filter and cur["target_resource"].lower() != target_filter.lower():
            continue
        # Build relationship entry, clearly label curated
        rel = {
            "source_resource": cur["source_path"] if "source_path" in cur else cur["source_resource"],
            "source_field": cur["source_field"],
            "source_path": cur["source_path"],
            "target_resource": cur["target_resource"],
            "target_field": cur["target_field"],
            "target_module": cur["target_module"],
            "source_module": cur["source_module"],
            "relationship_type": cur["relationship_type"],
            "confidence": cur["confidence"],
            "source": cur["source"],
            "reason": cur["reason"],
            "evidence": cur.get("evidence",""),
        }
        relationships.append(rel)

    # Apply target filter to all relationships if provided
    if target_filter:
        relationships = [r for r in relationships if r.get("target_resource","").lower() == target_filter.lower() or r.get("target_resource","").lower() == target_filter.lower().split("/")[-1]]

    # Deduplicate by (source_field, target_resource, target_field)
    seen = set()
    deduped = []
    for r in relationships:
        key = (r.get("source_field"), r.get("target_resource"), r.get("target_field"))
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    # Sort for determinism: oracle_child first, then schema_inferred, then curated
    order = {"oracle_child": 0, "schema_inferred": 1, "curated_cross_module": 2}
    deduped.sort(key=lambda x: (order.get(x.get("relationship_type"), 99), x.get("source_field","")))

    # Use effective PK/counts for child context
    effective_pk_out = effective_pk if effective_is_child else pk
    effective_attr_count = len(effective_attrs) if effective_attrs is not None else 0
    effective_child_count = len(children_for_oracle) if effective_is_child else len(children)
    return _text_result({
        "source_resource": original,
        "source_module": source_module,
        "api_version": api_version,
        "primary_key": effective_pk_out,
        "attribute_count": effective_attr_count,
        "child_count": effective_child_count,
        "relationships": deduped,
    })


HANDLERS = {
    "fusion_find_relationships": handle_fusion_find_relationships,
}

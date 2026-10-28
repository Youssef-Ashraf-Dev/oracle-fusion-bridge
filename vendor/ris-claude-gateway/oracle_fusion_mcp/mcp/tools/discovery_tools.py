"""FSCM discovery tools - curated seed plus /describe validation (read-only)."""

import asyncio
from typing import Any, Dict, List, Optional

from oracle_fusion_mcp._types import Request
from ...auth.entra import ValidatedUser
from ...config.settings import get_settings
from ...oracle.client import OracleClient
from ._helpers import _text_result
from .endpoint_discovery import extract_service_catalog_names
from aj_fusion_hcm_mcp.core.client import HcmClient

# ... rest of the file

# Curated seed derived from actual endpoint strings in oracle/client.py
# Verified via: _fetch("...") calls in client.py and field_maps.py
# Excludes write endpoints (draftPurchaseOrders) and HCM (userAccounts) is FSCM-focused
SEED_RESOURCES: List[str] = [
    "invoices",
    "purchaseOrders",
    "suppliers",
    "purchaseRequisitions",
    "payablesPayments",
    "journalBatches",
    "ledgerBalances",
    "ledgersLOV",
    "chartOfAccountsLOV",
    "currencyRates",
    "payablesPaymentTerms",
    "procurementApprovedSupplierListEntries",
    "browsingCategories",
    "receivablesInvoices",
    "standardReceipts",
    "expenseReports",
    "receivingReceiptRequests",
]

# HCM seed resources (verified against HCM describe catalog)
# These are the HCM resources that are commonly available and verified
HCM_SEED_RESOURCES: List[str] = [
    "workers",
    "jobs",
    "positions",
    "departments",
    "locations",
    "grades",
    "absences",
    "absenceTypes",
    "benefitEnrollments",
    "compensationPlans",
    "salaries",
    "elements",
    "payrollFlows",
    "payrollRelationships",
    "payrollStatutoryUnits",
]

TOOLS = [
    {
        "name": "fusion_list_resources",
        "description": "List known FSCM REST resources validated via Oracle /describe endpoint. Curated seed from package field_maps/client plus live describe validation, plus the live tenant catalog. Read-only GET only. Result tiers: 'resources' (seed names individually describe-validated), 'unavailable' (seeds that failed validation, with errors), 'catalog_resources' (additional names from the live tenant catalog describe?metadataMode=list — NOT individually validated by this call; use fusion_describe_resource to verify one explicitly). Absence of a name anywhere here is never evidence it does not exist on the tenant.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def _extract_schema_summary(resource: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract primary_key, attribute_count, child_count, children from /describe JSON."""
    # Oracle describe wraps in Resources.{resource} for most FSCM resources
    if "Resources" in data and isinstance(data["Resources"], dict):
        if resource in data["Resources"]:
            res = data["Resources"][resource]
        else:
            # Fallback: first value if key mismatch (e.g. case)
            res = next(iter(data["Resources"].values())) if data["Resources"] else data
    else:
        res = data

    attrs = res.get("attributes", []) if isinstance(res.get("attributes"), list) else []
    children = res.get("children", {}) if isinstance(res.get("children"), dict) else {}

    # Primary key heuristic: fnd:GLOBALLY_UNIQUE true, else mandatory integer Id, else first attr
    primary_key = None
    for a in attrs:
        props = a.get("properties", {}) if isinstance(a.get("properties"), dict) else {}
        if props.get("fnd:GLOBALLY_UNIQUE") == "true":
            primary_key = a.get("name")
            break
    if not primary_key:
        for a in attrs:
            if a.get("mandatory") and a.get("type") == "integer" and "Id" in a.get("name", ""):
                primary_key = a.get("name")
                break
    if not primary_key and attrs:
        primary_key = attrs[0].get("name")

    attribute_count = len(attrs)
    child_count = len(children)
    children_list = sorted(children.keys())

    return {
        "resource": resource,
        "available": True,
        "primary_key": primary_key,
        "attribute_count": attribute_count,
        "child_count": child_count,
        "children": children_list,
    }


async def handle_fusion_list_resources(
    request: Request,
    session: ValidatedUser,
    params: Dict[str, Any],
    oracle_client: OracleClient,
) -> Dict[str, Any]:
    settings = get_settings()
    api_version = settings.ORACLE_FUSION_API_VERSION

    sem = asyncio.Semaphore(4)

    async def describe_one(resource: str) -> Dict[str, Any]:
        async with sem:
            try:
                # Use only OracleClient._fetch (safe GET) - oracle_api_base via settings
                data = await oracle_client._fetch(f"{resource}/describe", {})
                summary = _extract_schema_summary(resource, data)
                return summary
            except Exception as e:
                # Preserve error handling (existing Oracle HTTP error handling via _fetch raise)
                err = str(e)
                # Truncate to avoid credential exposure
                if len(err) > 800:
                    err = err[:800]
                return {"resource": resource, "available": False, "error": err}

    # Concurrent describe, limit 4 to avoid overwhelming Fusion pod
    tasks = [describe_one(r) for r in SEED_RESOURCES]
    results = await asyncio.gather(*tasks)

    available = [r for r in results if r.get("available")]
    unavailable = [{"resource": r["resource"], "error": r["error"]} for r in results if not r.get("available")]

    # Sort for determinism
    available.sort(key=lambda x: x["resource"])
    unavailable.sort(key=lambda x: x["resource"])

    # Live tenant catalog tier (additive only): exactly one GET against
    # the documented describe?metadataMode=list endpoint. Names found
    # here are reported UNVALIDATED — no per-name describe fan-out is
    # performed by this call; verify any single name explicitly with
    # fusion_describe_resource. Any catalog-fetch failure (401/403/5xx,
    # timeout, network, unparseable) degrades to seed-only results and
    # is reported via catalog_error, never as proof of absence.
    catalog_resources: List[Dict[str, Any]] = []
    catalog_error: Optional[str] = None
    try:
        catalog_doc = await oracle_client._fetch(
            "describe", {"metadataMode": "list"})
    except Exception as exc:
        catalog_doc = None
        err = str(exc)
        if len(err) > 200:
            err = err[:200]
        catalog_error = "live catalog unavailable: %s" % err
    if catalog_doc is not None:
        known = {r.get("resource") for r in available}
        known.update(r.get("resource") for r in unavailable)
        for name in extract_service_catalog_names(catalog_doc):
            if name not in known:
                known.add(name)
                catalog_resources.append({
                    "resource": name,
                    "source": "live-catalog",
                    "validated": False,
                })

    payload: Dict[str, Any] = {
        "discovery_source": "curated_seed_plus_describe_plus_live_catalog"
        if catalog_error is None
        else "curated_seed_plus_describe",
        "api_version": api_version,
        "resources": available,
        "unavailable": unavailable,
        "catalog_resources": catalog_resources,
    }
    if catalog_error is not None:
        payload["catalog_error"] = catalog_error
    return _text_result(payload)


async def handle_fusion_list_hcm_resources(
    request: Request,
    session: ValidatedUser,
    params: Dict[str, Any],
    hcm_client: HcmClient,
) -> Dict[str, Any]:
    """List known HCM REST resources validated via Oracle /describe endpoint.
    
    Curated seed from package catalog plus live describe validation,
    plus the live tenant catalog. Read-only GET only.
    """
    settings = get_settings()
    api_version = settings.ORACLE_FUSION_API_VERSION

    sem = asyncio.Semaphore(4)

    async def describe_one(resource: str) -> Dict[str, Any]:
        async with sem:
            try:
                # Use only HcmClient._fetch (safe GET) - oracle_api_base via settings
                data = await hcm_client._fetch(f"{resource}/describe", {})
                summary = _extract_schema_summary(resource, data)
                return {"resource": resource, "available": True, **summary}
            except Exception as e:
                err = str(e)
                if len(err) > 800:
                    err = err[:800]
                return {"resource": resource, "available": False, "error": err}

    # Concurrent describe, limit 4 to avoid overwhelming Fusion pod
    tasks = [describe_one(r) for r in HCM_SEED_RESOURCES]
    results = await asyncio.gather(*tasks)

    available = [r for r in results if r.get("available")]
    unavailable = [{"resource": r["resource"], "error": r["error"]} for r in results if not r.get("available")]

    # Sort for determinism
    available.sort(key=lambda x: x["resource"])
    unavailable.sort(key=lambda x: x["resource"])

    # Live tenant catalog tier (additive only): exactly one GET against
    # the documented describe?metadataMode=list endpoint. Names found
    # here are reported UNVALIDATED — no per-name describe fan-out is
    # performed by this call; verify any single name explicitly with
    # fusion_describe_hcm_resource. Any catalog-fetch failure (401/403/5xx,
    # timeout, network, unparseable) degrades to seed-only results and
    # is reported via catalog_error, never as proof of absence.
    catalog_resources: List[Dict[str, Any]] = []
    catalog_error: Optional[str] = None
    try:
        catalog_doc = await hcm_client.describe_catalog()
        if isinstance(catalog_doc, dict):
            resources = catalog_doc.get("Resources")
            if isinstance(resources, dict):
                known = {r.get("resource") for r in results if r.get("available")}
                known.update(r.get("resource") for r in results if not r.get("available"))
                for name in resources.keys():
                    if name not in known:
                        known.add(name)
                        catalog_resources.append({
                            "resource": name,
                            "source": "live-catalog",
                            "validated": False,
                        })
    except Exception as exc:
        catalog_doc = None
        err = str(exc)
        if len(err) > 200:
            err = err[:200]
        catalog_error = "live catalog unavailable: %s" % err

    payload: Dict[str, Any] = {
        "discovery_source": "curated_seed_plus_describe_plus_live_catalog"
        if catalog_error is None
        else "curated_seed_plus_describe",
        "api_version": api_version,
        "resources": available,
        "unavailable": unavailable,
        "catalog_resources": catalog_resources,
    }
    if catalog_error is not None:
        payload["catalog_error"] = catalog_error
    return _text_result(payload)


HANDLERS = {
    "fusion_list_resources": handle_fusion_list_resources,
    "fusion_list_hcm_resources": handle_fusion_list_hcm_resources,
}

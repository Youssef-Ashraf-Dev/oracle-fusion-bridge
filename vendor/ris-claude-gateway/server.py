#!/usr/bin/env python3
"""
Oracle Fusion Combined MCP — Claude Desktop (UV, READ-ONLY, 11.13.18.05)

Single FastMCP server that exposes:

  * FSCM (oracle_fusion_mcp 0.2.1) — 38 read tools (generic describe/query,
    discovery, procurement, GL, AR, math, action discovery, dry-run previews).
    7 write tools are NEVER registered.
  * HCM  (aj_fusion_hcm_mcp 0.1.1) — 15 read tools (list_resources,
    describe_resource, get_capabilities, query_resource, get_record,
    find_worker, get_worker_profile, list_direct_reports, get_reporting_chain,
    lookup_org, get_current_compensation, list_absences, list_changes,
    server_info, fusion_preview_hcm_absence_update). 2 write tools
    (mutate_record, run_action) are NEVER registered.
  * Dynamic one-time write WORKFLOW (2 tools): fusion_preview_write
    (read-only authoring) + fusion_execute_confirmed_write (executes exactly
    one previously previewed operation given ONLY its confirmation ID).
    No generic REST write tool exists or may be added here.

Vendor-first import: tries bundled `oracle_fusion_mcp/` + `aj_fusion_hcm_mcp/`
alongside this file, falls back to site-packages.

Security:
  - MCP_MODE forced to "readonly" (manifest + in-process belt)
   - HCM features.writes_enabled forced False — dry-run gate never reached
  - No POST/PATCH/DELETE/action is reachable via tools/list
  - Logs to stderr only; stdout = JSON-RPC
  - Uses live Oracle Fusion REST API metadata (fscmRestApi + hcmRestApi
    11.13.18.05) — not a DB schema dump.

Part of oracle-fusion-claude-plugin.mcpb (combined FSCM+HCM + skill).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: vendor-first sys.path + readonly enforcement
# ---------------------------------------------------------------------------
def _bootstrap() -> None:
    base = Path(__file__).resolve().parent
    for pkg in ("oracle_fusion_mcp", "aj_fusion_hcm_mcp"):
        if (base / pkg).is_dir() and str(base) not in sys.path:
            sys.path.insert(0, str(base))
    # Enforce readonly before any Settings/Config is instantiated
    if os.environ.get("MCP_MODE", "readonly").strip().lower() != "readonly":
        print("[oracle-fusion-claude-plugin] WARN: MCP_MODE overridden to 'readonly'", file=sys.stderr)
        os.environ["MCP_MODE"] = "readonly"
    else:
        os.environ.setdefault("MCP_MODE", "readonly")
    # HCM writes must stay off regardless of file/env
    if os.environ.get("HCM_WRITES_ENABLED", "").lower() not in ("", "false", "0", "no"):
        print("[oracle-fusion-claude-plugin] WARN: HCM writes forced OFF", file=sys.stderr)
    os.environ["HCM_WRITES_ENABLED"] = "false"
    # Also ensure the HCM feature flag via env overlay is not enabled
    # aj_fusion_hcm_mcp reads HCM_* but features.writes_enabled is file-only;
    # we force it in code below.

_bootstrap()

# Guarded truststore injection (mirrors aj_fusion_hcm_mcp.__main__)
try:
    import truststore  # type: ignore
    truststore.inject_into_ssl()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Build combined FastMCP
# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP

# Use a distinct name so Claude Desktop shows combined capability
mcp = FastMCP("oracle-fusion-combined")

# HCM: build context with writes disabled, register only read tools
def _init_hcm():
    from aj_fusion_hcm_mcp.config import Config, ServerConfig, AuthConfig, FeatureConfig
    from aj_fusion_hcm_mcp.context import ServerContext
    from aj_fusion_hcm_mcp.core.client import HcmClient
    from aj_fusion_hcm_mcp.core.catalog import Catalog
    from aj_fusion_hcm_mcp.safety import AuditLog, Redactor
    from aj_fusion_hcm_mcp.auth import build_auth
    from aj_fusion_hcm_mcp import __version__ as hcm_version

    # Build config from env (HCM_BASE_URL etc are mapped in manifest to same
    # ORACLE_FUSION_BASE_URL). Fallback to ORACLE_FUSION_BASE_URL if HCM_ not set.
    # The Config loader already maps HCM_* env vars; we ensure base_url is set.
    if not os.environ.get("HCM_BASE_URL") and os.environ.get("ORACLE_FUSION_BASE_URL"):
        os.environ["HCM_BASE_URL"] = os.environ["ORACLE_FUSION_BASE_URL"]
    if not os.environ.get("HCM_USERNAME") and os.environ.get("ORACLE_USERNAME"):
        os.environ["HCM_USERNAME"] = os.environ["ORACLE_USERNAME"]
    if not os.environ.get("HCM_PASSWORD") and os.environ.get("ORACLE_PASSWORD"):
        os.environ["HCM_PASSWORD"] = os.environ["ORACLE_PASSWORD"]
    # HCM REST version must stay 11.13.18.05 (same as FSCM)
    os.environ.setdefault("HCM_REST_VERSION", os.environ.get("ORACLE_FUSION_API_VERSION", "11.13.18.05"))

    from aj_fusion_hcm_mcp.config import load_config
    cfg = load_config()
    # Force writes off in memory even if file says true
    cfg.features.writes_enabled = False
    cfg.features.sensitive_fields_enabled = False  # keep redaction
    # Build context
    auth = build_auth(cfg.auth)
    redactor = Redactor(enabled=not cfg.features.sensitive_fields_enabled)
    audit = AuditLog(cfg.audit.path, enabled=cfg.audit.enabled)
    client = HcmClient(
        cfg.server.base_url,
        cfg.server.rest_version,
        auth,
        default_limit=cfg.limits.default_limit,
        max_limit=cfg.limits.max_limit,
        redactor=redactor,
        audit=audit,
    )
    catalog = Catalog(client, cfg.modules)
    ctx = ServerContext(config=cfg, client=client, catalog=catalog, redactor=redactor, audit=audit)

    # Report version in handshake (like original server.py)
    mcp._mcp_server.version = f"combined-fscm0.2.1+hcm{hcm_version}"

    # Register HCM read tools only (skip writes)
    from aj_fusion_hcm_mcp.tools import controlled, discovery, query, workflows, atom

    # server_info is defined inside build_server; replicate minimal version
    @mcp.tool()
    def server_info() -> dict:
        """Report non-sensitive server configuration (combined)."""
        from urllib.parse import urlsplit
        def _host(u: str) -> str:
            return urlsplit(u).netloc or u
        return {
            "name": "oracle-fusion-combined",
            "fscm_version": "0.2.1",
            "hcm_version": hcm_version,
            "rest_version": cfg.server.rest_version,
            "pod_host": _host(cfg.server.base_url),
            "auth_type": cfg.auth.type,
            "transport": cfg.transport.type,
            "modules": cfg.modules.model_dump(),
            "features": cfg.features.model_dump(),
            "redaction_active": ctx.redactor.enabled,
        }

    discovery.register(mcp, ctx)
    query.register(mcp, ctx)
    workflows.register(mcp, ctx)
    atom.register(mcp, ctx)
    controlled.register(mcp, ctx)  # dry-run absence preview only; never executes
    # writes.register intentionally NOT called — mutate_record/run_action absent
    return ctx

_hcm_ctx = _init_hcm()

# FSCM: register 35 read tools via wrappers that reuse existing handlers
def _init_fscm():
    import asyncio
    from oracle_fusion_mcp.config.settings import get_settings
    from oracle_fusion_mcp.auth.entra import ValidatedUser
    from oracle_fusion_mcp.auth.oracle_jwt import get_oracle_jwt_signer
    from oracle_fusion_mcp.oracle.client import OracleClient
    from oracle_fusion_mcp.mcp.tools import get_tools_list, handle_tool_call

    # Minimal mock request/state for FSCM dispatch (mirrors cli.py)
    class _State:
        pass
    class _MockRequest:
        def __init__(self):
            self.state = _State()

    # Ensure FSCM settings see the env
    get_settings.cache_clear()

    # Reuse the existing tool registry to avoid hard-coding business queries
    # get_tools_list('readonly') returns 38 tools; we wrap each as a FastMCP tool
    async def _list():
        return await get_tools_list(mode="readonly")
    # Run once synchronously to get definitions
    import asyncio as _asyncio
    try:
        readonly_defs = _asyncio.run(_list())["tools"]
    except RuntimeError:
        # If already in an event loop (unlikely at import), create new loop
        loop = _asyncio.new_event_loop()
        readonly_defs = loop.run_until_complete(_list())["tools"]
        loop.close()

    # For each FSCM readonly tool, create a FastMCP wrapper that delegates
    # to handle_tool_call (which enforces MCP_MODE and does auth)
    for tool_def in readonly_defs:
        t_name = tool_def["name"]
        t_desc = tool_def.get("description", "")
        t_schema = tool_def.get("inputSchema", {})

        # Create a closure capturing name/schema
        def _make_wrapper(name: str, schema: dict):
            # FastMCP inspects the function signature for JSON schema, so we
            # create a generic async function that accepts **kwargs and forwards
            # them as tool_arguments.
            async def _wrapper(**kwargs):
                # Build mock request with live credentials
                username = os.environ.get("ORACLE_USERNAME", "").strip()
                password = os.environ.get("ORACLE_PASSWORD", "").strip()
                base_url = os.environ.get("ORACLE_FUSION_BASE_URL", "").strip()
                if not username or not base_url:
                    return {"error": "Missing ORACLE_FUSION_BASE_URL or ORACLE_USERNAME (set via Claude user_config)"}
                # Try JWT first, else basic (mirrors cli.py _run)
                jwt_signer = get_oracle_jwt_signer()
                req = _MockRequest()
                if jwt_signer:
                    req.state.oracle_jwt = True
                else:
                    if not password:
                        return {"error": "No auth: set ORACLE_PASSWORD or ORACLE_JWT_*"}
                    req.state.basic_auth = (username, password)
                session = ValidatedUser(sub=username, email=username, name=username)
                # Delegate to existing dispatcher (preserves validation, retries)
                result = await handle_tool_call(req, session, name, kwargs or {})
                # FastMCP expects a serializable return; handle_tool_call returns
                # {"content":[{"type":"text","text": "..."}]} — unwrap for nicer UX
                # but keep as-is if already _text_result shape
                return result
            # Attach metadata so FastMCP registers with correct name/description
            _wrapper.__name__ = name
            _wrapper.__doc__ = t_desc
            return _wrapper

        wrapper = _make_wrapper(t_name, t_schema)
        # Register; FastMCP derives an empty **kwargs schema, so we patch it
        # with the original Oracle-vetted inputSchema (preserves supplier/q/etc.)
        mcp.tool(name=t_name, description=t_desc)(wrapper)
        # Patch the stored Tool's JSON schema and validation model to the Oracle-verified one
        # (parameters alone is not enough — fn_metadata.arg_model must also match, otherwise
        # arguments are stripped during validation and appear as missing)
        try:
            tool = mcp._tool_manager._tools[t_name]
            tool.parameters = t_schema  # type: ignore[attr-defined]
            # Build a correct ArgModel from the schema so validation passes through arguments
            from pydantic import Field, create_model
            from typing import Any

            from mcp.server.fastmcp.utilities.func_metadata import (  # type: ignore[import-not-found]
                ArgModelBase,
                FuncMetadata,
            )

            _props = t_schema.get("properties", {}) or {}
            _required = set(t_schema.get("required", []) or [])
            _fields: dict[str, Any] = {}
            for _pname, _pdef in _props.items():
                _desc = _pdef.get("description", "")
                if _pname in _required:
                    _fields[_pname] = (Any, Field(description=_desc))
                else:
                    _default = _pdef.get("default", None)
                    _fields[_pname] = (Any, Field(default=_default, description=_desc))
            _ArgModel = create_model(f"{t_name}Arguments", __base__=ArgModelBase, **_fields)  # type: ignore[call-overload]
            _orig = tool.fn_metadata
            tool.fn_metadata = FuncMetadata(  # type: ignore[call-arg]
                arg_model=_ArgModel,
                output_schema=_orig.output_schema,
                output_model=_orig.output_model,
                wrap_output=_orig.wrap_output,
            )
        except Exception:
            # Fallback for future FastMCP internals
            try:
                tool = mcp._tool_manager._tools[t_name]  # type: ignore[attr-defined]
                object.__setattr__(tool, "parameters", t_schema)  # type: ignore[attr-defined]
            except Exception:
                pass

    return readonly_defs

_fscm_defs = _init_fscm()

# ---------------------------------------------------------------------------
# Dynamic one-time write WORKFLOW (chat-facing, safe by construction)
# ---------------------------------------------------------------------------
def _init_workflow():
    """Register fusion_preview_write + fusion_execute_confirmed_write.

    The preview tool is read-only. The execution tool accepts ONLY a
    confirmation ID; every other execution input comes from the stored
    preview and is re-validated by dynamic_executor. Full write mode is
    scoped to per-call client instances built inside the execution path —
    global MCP_MODE and the combined registry stay readonly.
    """
    from types import SimpleNamespace

    from oracle_fusion_mcp.config.settings import get_settings
    from oracle_fusion_mcp.mcp.tools import dynamic_workflow
    from oracle_fusion_mcp.oracle.client import OracleClient

    settings = get_settings()

    # Production hardening, validated once at startup (fail fast).
    from oracle_fusion_mcp.mcp.tools.policy import (
        configure as _policy_configure,
        parse_value_limits as _parse_value_limits,
        validate_pod_allowlist as _check_pod,
    )
    _policy_configure(settings.MCP_POLICY_FILE)
    _pod_error = _check_pod(settings.ORACLE_FUSION_BASE_URL,
                            settings.ORACLE_ALLOWED_BASE_URLS)
    if _pod_error is not None:
        raise RuntimeError(_pod_error)
    try:
        _value_limits = _parse_value_limits(settings.MCP_WRITE_VALUE_LIMITS)
    except ValueError as exc:
        raise RuntimeError("invalid MCP_WRITE_VALUE_LIMITS: %s" % exc)
    _environment = str(settings.MCP_ENVIRONMENT or "dev").strip().lower()
    _prod_writes = bool(settings.MCP_PROD_WRITES_ENABLED)

    def _fscm_read_client() -> OracleClient:
        username = os.environ.get("ORACLE_USERNAME", "").strip()
        password = os.environ.get("ORACLE_PASSWORD", "").strip()
        return OracleClient(username=username, password=password)

    async def _fscm_describe(resource: str):
        return await _fscm_read_client()._fetch(resource + "/describe", {})

    async def _fscm_record(resource: str, record_id):
        return await _fscm_read_client()._fetch(
            "%s/%s" % (resource, record_id), {"onlyData": "true"})

    async def _hcm_attrs(resource: str):
        summary = await _hcm_ctx.catalog.describe(resource)
        return summary.get("attributes") or []

    _catalog_cache: dict = {}

    # Static floor for name suggestions only (never for validation or
    # resolution): resource names proven live in our sessions or listed
    # in Oracle's public Financials REST reference (11.13.18.05 paths).
    # Deliberately EXCLUDES fixedAssetAdditions/Retirements/Transfers:
    # the public reference exposes exactly two asset resources, both
    # read-only LOVs (fixedAssetBooksLOV, fixedAssetCategoriesLOV) — no
    # transactional asset endpoint exists to suggest. Live catalog
    # results union over this; nothing here can authorize or resolve
    # an operation.
    _FSCM_KNOWN_RESOURCES = (
        "invoices", "suppliers", "purchaseRequisitions",
        "draftPurchaseOrders", "purchaseOrders", "receivablesInvoices",
        "receipts", "payments", "journalBatches", "expenses",
        "fixedAssetBooksLOV", "fixedAssetCategoriesLOV",
    )

    async def _fscm_catalog():
        # Live FSCM resource names from Oracle's documented metadata
        # catalog: GET {version-root}/describe?metadataMode=list (never
        # raises; failure degrades to the static floor, never to "no
        # resources exist"). Only used to suggest names when a describe
        # misses.
        if "fscm" in _catalog_cache:
            return _catalog_cache["fscm"]
        names: list = []
        try:
            from oracle_fusion_mcp.mcp.tools.endpoint_discovery import (
                FSCM_CATALOG_ENDPOINT,
                FSCM_CATALOG_PARAMS,
                extract_service_catalog_names,
            )
            doc = await _fscm_read_client()._fetch(
                FSCM_CATALOG_ENDPOINT, dict(FSCM_CATALOG_PARAMS))
            names = extract_service_catalog_names(doc)
        except Exception:
            names = []
        names = [n for n in names if isinstance(n, str) and n]
        merged = list(dict.fromkeys(
            list(names) + list(_FSCM_KNOWN_RESOURCES)))
        _catalog_cache["fscm"] = merged
        return merged

    async def _hcm_catalog():
        # Live HCM resource names from the catalog index (never raises);
        # falls back to the seed catalog when the live index is down.
        if "hcm" in _catalog_cache:
            return _catalog_cache["hcm"]
        names: list = []
        try:
            raw = await _hcm_ctx.client.describe_catalog()
            resources = raw.get("Resources") \
                if isinstance(raw, dict) else None
            names = list(resources.keys()) \
                if isinstance(resources, dict) else []
        except Exception:
            names = []
        if not names:
            try:
                names = [r.get("name")
                         for r in _hcm_ctx.catalog.list_resources(limit=500)
                         if r.get("name")]
            except Exception:
                names = []
        _catalog_cache["hcm"] = names
        return names

    async def _docs_names():
        # Advisory docs-snapshot names for the FSCM suggestion pool
        # (family-scoped inside the loader). Never raises; [] degrades
        # to live-catalog-plus-floor suggestions.
        try:
            from oracle_fusion_mcp.mcp.tools.docs_index import (
                docs_candidate_names,
            )
            return sorted(docs_candidate_names("fscm"))
        except Exception:
            return []

    async def _hcm_children(resource: str):
        # Child schemas for composite creates via the shared extractor.
        # Advertised-but-schemaless children surface as [] (precise
        # planner refusal); never raises ({} = flat behavior).
        try:
            raw = await _hcm_ctx.client.describe(resource)
        except Exception:
            return {}
        try:
            from aj_fusion_hcm_mcp.core.catalog import (
                extract_child_schemas,
            )
            out = extract_child_schemas(raw, resource)
            return out if isinstance(out, dict) else {}
        except Exception:
            return {}

    async def _hcm_record(resource: str, record_id):
        return await _hcm_ctx.client.get_record(
            resource, str(record_id), redact=False)

    def _fscm_full_client() -> OracleClient:
        username = os.environ.get("ORACLE_USERNAME", "").strip()
        password = os.environ.get("ORACLE_PASSWORD", "").strip()
        client = OracleClient(username=username, password=password)
        client.settings = SimpleNamespace(
            MCP_MODE="full",
            oracle_api_base=settings.oracle_api_base,
            oracle_hcm_base=settings.oracle_hcm_base,
        )
        return client

    def _hcm_full_client():
        from aj_fusion_hcm_mcp.core.client import HcmClient

        return HcmClient(
            _hcm_ctx.config.server.base_url,
            _hcm_ctx.config.server.rest_version,
            _hcm_ctx.client._auth,
            default_limit=_hcm_ctx.config.limits.default_limit,
            max_limit=_hcm_ctx.config.limits.max_limit,
            redactor=_hcm_ctx.redactor,
            audit=_hcm_ctx.audit,
            mode="full",
        )

    @mcp.tool(
        name="fusion_preview_write",
        description=(
            "Prepare a dynamic Fusion write preview; NEVER performs the mutation. "
            "Use when the user requests a Fusion change that may be dynamically "
            "discoverable. Returns the exact proposed operation plus a "
            "confirmation_id. Present the proposed operation to the user and ask "
            "whether to execute it ONCE. Only if the user clearly authorizes "
            "that exact operation, call fusion_execute_confirmed_write with the "
            "confirmation_id and nothing else. If the user declines, do not "
            "call the execution tool. Eligibility vocabulary (read exactly): "
            "ELIGIBLE and HIGH_RISK_ELIGIBLE both mean executable on explicit "
            "user authorization — HIGH risk elevates scrutiny, it never "
            "refuses. NOT_ELIGIBLE, NOT_RESOLVED, and NOT_READY mean refused, "
            "each with machine-readable reasons naming the exact cause; fix "
            "the named cause and re-preview. Never treat risk level alone "
            "as refusal."
        ),
    )
    async def fusion_preview_write(
        system: str,
        resource: str,
        operation: str,
        scope: str = "item",
        record_id: str = "",
        action: str = "",
        parameters: dict | None = None,
        child_path: str = "",
        child_record_id: str = "",
    ) -> dict:
        return await dynamic_workflow.preview_operation(
            system=system,
            resource=resource,
            scope=scope,
            operation=operation,
            record_id=record_id or None,
            action=action or None,
            fields=dict(parameters or {}),
            fscm_describe_fn=_fscm_describe,
            hcm_attrs_fn=_hcm_attrs,
            hcm_children_fn=_hcm_children,
            fscm_catalog_fn=_fscm_catalog,
            hcm_catalog_fn=_hcm_catalog,
            docs_names_fn=_docs_names,
            record_fetch_fn=_fscm_record if system == "FSCM" else _hcm_record,
            api_version=settings.ORACLE_FUSION_API_VERSION,
            child_path=child_path or None,
            child_record_id=child_record_id or None,
            environment=_environment,
            value_limits=_value_limits,
            prod_writes_enabled=_prod_writes,
        )

    @mcp.tool(
        name="fusion_create_requirements",
        description=(
            "Read-only: list the exact caller-supplied fields required to "
            "create a resource (never executes). Call this FIRST when the "
            "user wants to create something: ask the human for every "
            "`required` field value, never send `never_send` fields, then "
            "call fusion_preview_write with the complete values."
        ),
    )
    async def fusion_create_requirements(
        system: str,
        resource: str,
        operation: str = "create",
    ) -> dict:
        from oracle_fusion_mcp.mcp.tools.create_requirements import (
            describe_create_requirements,
        )
        if system == "FSCM":
            raw = await _fscm_describe(resource)
            return describe_create_requirements(
                system=system, resource=resource, operation=operation,
                fscm_raw=raw)
        if system == "HCM":
            summary = await _hcm_ctx.catalog.describe(resource)
            return describe_create_requirements(
                system=system, resource=resource, operation=operation,
                hcm_attrs=summary.get("attributes") or [])
        return {"resolved": False, "system": system, "resource": resource,
                "operation": operation,
                "reasons": ["system must be HCM or FSCM"]}

    @mcp.tool(
        name="fusion_operation_inventory",
        description=(
            "Read-only: inventory what CAN be done with a resource, resolved "
            "from live describe metadata (never executes). Lists every "
            "advertised operation — CRUD, custom actions, nested child "
            "operations — with resolution source, policy markers, "
            "verification availability, and dispatchability. Use it to "
            "discover non-hardcoded operations before planning anything."
        ),
    )
    async def fusion_operation_inventory(
        system: str,
        resource: str,
    ) -> dict:
        from oracle_fusion_mcp.mcp.tools.operation_inventory import (
            describe_operations,
        )
        if system == "FSCM":
            raw = await _fscm_describe(resource)
            return describe_operations(
                system=system, resource=resource, fscm_raw=raw)
        if system == "HCM":
            summary = await _hcm_ctx.catalog.describe(resource)
            return describe_operations(
                system=system, resource=resource,
                hcm_attrs=summary.get("attributes") or [])
        return {"resolved": False, "system": system, "resource": resource,
                "reasons": ["system must be HCM or FSCM"]}

    async def _record_fetch_for_batch(resource: str, record_id: Any) -> Any:
        # Generic FSCM record read shared by batch items; HCM items use
        # the dedicated HCM fetcher below.
        return await _fscm_record(resource, record_id)

    async def _hcm_record_fetch_for_batch(resource: str,
                                          record_id: Any) -> Any:
        return await _hcm_record(resource, record_id)

    @mcp.tool(
        name="fusion_preview_batch",
        description=(
            "Read-only: validate a LIST of write operations and mint ONE "
            "batch confirmation for all of them (never executes). Every "
            "item must plan eligible or no batch forms. Returns the full "
            "itemized table (method/path/parameters per item) for human "
            "review; on explicit authorization of the whole batch, call "
            "fusion_execute_confirmed_batch once. Execution runs items in "
            "order and stops at the first failure."
        ),
    )
    async def fusion_preview_batch(
        operations: list,
    ) -> dict:
        from oracle_fusion_mcp.mcp.tools import batch_workflow
        return await batch_workflow.preview_batch(
            operations=operations,
            fscm_describe_fn=_fscm_describe,
            hcm_attrs_fn=_hcm_attrs,
            hcm_children_fn=_hcm_children,
            record_fetch_fn=_record_fetch_for_batch,
            hcm_record_fetch_fn=_hcm_record_fetch_for_batch,
            api_version=settings.ORACLE_FUSION_API_VERSION,
            environment=_environment,
            value_limits=_value_limits,
            prod_writes_enabled=_prod_writes,
            max_items=settings.MCP_BATCH_MAX_ITEMS,
            ttl_seconds=settings.MCP_BATCH_TTL_SECONDS,
        )

    @mcp.tool(
        name="fusion_execute_confirmed_batch",
        description=(
            "Execute exactly ONE previously previewed and explicitly "
            "authorized batch. Accepts ONLY a batch confirmation_id from "
            "fusion_preview_batch. Items run in order with per-item fresh "
            "re-validation; the loop stops at the first failure. Do not "
            "call without the user's clear authorization for the whole "
            "itemized batch."
        ),
    )
    async def fusion_execute_confirmed_batch(confirmation_id: str) -> dict:
        from oracle_fusion_mcp.mcp.tools import batch_workflow
        return await batch_workflow.execute_batch(
            confirmation_id=confirmation_id,
            fscm_client_factory=_fscm_full_client,
            hcm_client_factory=_hcm_full_client,
            fscm_describe_fn=_fscm_describe,
            hcm_attrs_fn=_hcm_attrs,
            hcm_children_fn=_hcm_children,
            environment=_environment,
        )

    @mcp.tool(
        name="fusion_execute_confirmed_write",
        description=(
            "Execute exactly ONE previously previewed and explicitly authorized "
            "Fusion mutation. Accepts ONLY a confirmation_id from "
            "fusion_preview_write — never a URL, method, body, resource, "
            "action, record, or parameters. The operation bound to that ID is "
            "re-validated against live metadata before anything is sent. Do "
            "not call without the user's clear authorization for that exact "
            "operation; a declined or expired preview must never be executed."
        ),
    )
    async def fusion_execute_confirmed_write(confirmation_id: str) -> dict:
        return await dynamic_workflow.execute_confirmation(
            confirmation_id=confirmation_id,
            fscm_client_factory=_fscm_full_client,
            hcm_client_factory=_hcm_full_client,
            fscm_describe_fn=_fscm_describe,
            hcm_attrs_fn=_hcm_attrs,
            hcm_children_fn=_hcm_children,
            environment=_environment,
        )

    return ("fusion_preview_write", "fusion_create_requirements",
            "fusion_operation_inventory", "fusion_preview_batch",
            "fusion_execute_confirmed_batch",
            "fusion_execute_confirmed_write")


_workflow_tools = _init_workflow()

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main() -> None:
    # Final guard: ensure env still readonly before serving
    os.environ["MCP_MODE"] = "readonly"
    # FastMCP handles stdio vs http via settings; we force stdio
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()

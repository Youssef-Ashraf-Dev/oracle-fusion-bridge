"""Resource catalog: bundled seed list + live ``/describe`` cache + live index +
capability discovery.

The seed catalog lets ``list_resources`` work offline and degrade gracefully;
``describe`` confirms reality against the pod and caches schemas; the live index
distills the ~38MB catalog describe to name/title for full-surface browsing.
Capability discovery probes one representative resource per ``auto`` module,
concurrently (DESIGN.md §12.3).
"""

from __future__ import annotations

import asyncio
import json
from importlib.resources import files
from typing import Any

from . import filters
from .client import HcmClient
from .errors import HcmApiError

# One representative resource per module (DESIGN.md §12.4).
MODULE_PROBES: dict[str, str] = {
    "core_hr": "workers",
    "compensation": "salaries",
    "absence": "absences",
    "payroll": "payrollFlows",
    "recruiting": "recruitingCEJobRequisitions",
    "talent": "workerGoals",
    "learning": "learnerLearningRecords",
    "benefits": "benefitEnrollments",
    "time_labor": "timeRecords",
}

# Generic CRUD verbs excluded when surfacing a child's business actions.
_GENERIC_ACTIONS = {"get", "create", "update", "delete", "upsert", "replace"}


def _load_seed() -> list[dict[str, str]]:
    raw = files("aj_fusion_hcm_mcp.data").joinpath("seed_catalog.json").read_text("utf-8")
    return json.loads(raw).get("resources", [])


def _action_names(actions_src: Any) -> list[str]:
    if isinstance(actions_src, dict):
        return [a for a in actions_src.keys()]
    if isinstance(actions_src, list):
        return [a.get("name") if isinstance(a, dict) else a for a in actions_src]
    return []


def summarize_describe(resource: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce Oracle's verbose ``/describe`` to attributes/children/actions.

    CRITICAL: on the live pod ``children`` is a dict, and each child's business
    actions (terminate, changeLegalEmployer, ...) live under ``child.item.actions``.
    We surface them as ``child_actions: {childName: [action, ...]}`` (excluding
    generic CRUD verbs) — otherwise the most important write operations are hidden.
    """
    resources = raw.get("Resources") if isinstance(raw, dict) else None
    node: Any = None
    if isinstance(resources, dict):
        node = resources.get(resource) or (next(iter(resources.values()), None))
    if not isinstance(node, dict):
        return {"resource": resource, "raw": raw}

    attributes = [
        {k: a.get(k) for k in ("name", "type", "title", "updatable", "mandatory") if k in a}
        for a in (node.get("attributes") or [])
        if isinstance(a, dict)
    ]

    child_names: list[str] = []
    child_actions: dict[str, list[str]] = {}

    def _add_child(name: str, cnode: Any) -> None:
        child_names.append(name)
        actions_src = None
        if isinstance(cnode, dict):
            item = cnode.get("item")
            if isinstance(item, dict):
                actions_src = item.get("actions")
            if actions_src is None:
                actions_src = cnode.get("actions")
        business = [
            a for a in _action_names(actions_src) if a and a.lower() not in _GENERIC_ACTIONS
        ]
        if business:
            child_actions[name] = business

    children_node = node.get("children")
    if isinstance(children_node, dict):
        for name, cnode in children_node.items():
            _add_child(name, cnode)
    elif isinstance(children_node, list):
        for c in children_node:
            if isinstance(c, dict) and c.get("name"):
                _add_child(c["name"], c)
            elif isinstance(c, str):
                child_names.append(c)

    actions = [a for a in _action_names(node.get("actions")) if a]

    return {
        "resource": resource,
        "title": node.get("title"),
        "attributes": attributes,
        "children": child_names,
        "actions": actions,
        "child_actions": child_actions,
    }


def _clean_child_attrs(node: Any):
    # Try several envelope shapes Fusion uses across versions; entries
    # may be a list or a name-keyed dict. Direct schema wins; item.* is
    # the fallback. Returns (attrs_list, source) where source is
    # "direct", "item_fallback", or None if not found.
    if isinstance(node, dict):
        for key in ("attributes", "fields", "properties"):
            val = node.get(key)
            if isinstance(val, dict):
                val = [{"name": k, **(v if isinstance(v, dict) else {})}
                       for k, v in val.items()]
            if isinstance(val, list) and val:
                clean = [{k: a.get(k) for k in
                          ("name", "type", "title", "updatable",
                           "mandatory", "description", "queryable", "precision", "scale", "defaultValue") if k in a}
                         for a in val if isinstance(a, dict)]
                clean = [a for a in clean if a.get("name")]
                if clean:
                    return clean, "direct"
        item = node.get("item")
        if isinstance(item, dict):
            attrs, _src = _clean_child_attrs(item)
            if attrs:
                return attrs, "item_fallback"
        return [], None
    return [], None


def _child_items(kids: Any) -> list:
    if isinstance(kids, dict):
        return list(kids.items())
    if isinstance(kids, list):
        out = []
        for c in kids:
            if isinstance(c, dict) and c.get("name"):
                out.append((c.get("name"), c))
            elif isinstance(c, str) and c:
                out.append((c, {}))
        return out
    return []


def extract_child_schemas(raw: Any, resource: str) -> dict:
    """Best-effort child-name to attribute-list map from a raw describe.

    Pure function (no I/O); never raises — returns {} on anything
    unrecognized. Children advertised without an inline field schema
    are kept as EMPTY lists so validators refuse nested writes under
    them with "child metadata unavailable" (precise) instead of
    "unknown field" (wrong: the child exists). Grandchildren surface
    under dotted keys (e.g. "workRelationships.assignments").
    
    Provenance tracking:
    - Each child entry includes 'source' field indicating discovery method
    - 'direct' = schema found inline in child definition
    - 'item_fallback' = schema found via item.* fallback
    - 'grandchild' = nested child (grandchild)
    - 'grandchild_item_fallback' = grandchild via item.* fallback
    - 'empty' = advertised but no schema found
    """
    try:
        resources = raw.get("Resources") if isinstance(raw, dict) else None
        node = (resources or {}).get(resource)
        if not isinstance(node, dict):
            nodes = [v for v in (resources or {}).values()
                     if isinstance(v, dict)]
            node = nodes[0] if nodes else None
        if not isinstance(node, dict):
            return {}
        out: dict = {}
        for name, cnode in _child_items(node.get("children") or {}):
            if not name:
                continue
            if not isinstance(cnode, dict):
                out[name] = {"attributes": [], "source": "empty", "error": "Child advertised but no schema object"}
                continue
            attrs, src = _clean_child_attrs(cnode)
            if attrs:
                out[name] = {"attributes": attrs, "source": src}
            else:
                out[name] = {"attributes": [], "source": "empty", "error": "Child advertised but no schema found"}
            grandkids = cnode.get("children")
            item = cnode.get("item")
            if grandkids is None and isinstance(item, dict):
                grandkids = item.get("children")
            for gname, gnode in _child_items(grandkids):
                if not gname:
                    continue
                if not isinstance(gnode, dict):
                    out["%s.%s" % (name, gname)] = {"attributes": [], "source": "empty", "error": "Grandchild advertised but no schema object found"}
                    continue
                gattrs, gsrc = _clean_child_attrs(gnode)
                if gattrs:
                    out["%s.%s" % (name, gname)] = {"attributes": gattrs, "source": gsrc}
                    continue
                gitem_attrs, gitem_src = _clean_child_attrs(gnode.get("item") or {})
                if gitem_attrs:
                    out["%s.%s" % (name, gname)] = {"attributes": gitem_attrs, "source": gitem_src}
                else:
                    out["%s.%s" % (name, gname)] = {"attributes": [], "source": "empty", "error": "Grandchild advertised but no schema found"}
        return out
    except Exception:
        return {}


class Catalog:
    def __init__(self, client: HcmClient, modules_config: Any) -> None:
        self._client = client
        self._modules = modules_config
        self._seed = _load_seed()
        self._describe_cache: dict[str, dict[str, Any]] = {}
        self._attrs_cache: dict[str, set[str]] = {}
        self._capabilities: dict[str, dict[str, Any]] | None = None
        self._live_index: dict[str, str] | None = None

    # ---- seed catalog (offline) ------------------------------------------

    def list_resources(
        self, search: str | None = None, module: str | None = None, limit: int = 50
    ) -> list[dict[str, str]]:
        results = self._seed
        if module:
            results = [r for r in results if r.get("module") == module]
        if search:
            needle = search.lower()
            results = [
                r
                for r in results
                if needle in r["name"].lower()
                or needle in r.get("title", "").lower()
                or needle in r.get("description", "").lower()
            ]
        return results[:limit]

    # ---- live full index (~650 resources) --------------------------------

    async def _load_live_index(self) -> dict[str, str]:
        if self._live_index is None:
            raw = await self._client.describe_catalog()
            resources = raw.get("Resources") if isinstance(raw, dict) else None
            index: dict[str, str] = {}
            if isinstance(resources, dict):
                for name, node in resources.items():
                    title = node.get("title") if isinstance(node, dict) else None
                    index[name] = title or name
            self._live_index = index
        return self._live_index

    async def list_live(
        self, search: str | None = None, module: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Seed catalog merged with the live index (deduped against seed)."""
        seed = self.list_resources(search=search, module=module, limit=10_000)
        results: list[dict[str, Any]] = [{**r, "source": "seed-catalog"} for r in seed]
        seed_names = {r["name"] for r in results}
        index = await self._load_live_index()
        needle = search.lower() if search else None
        for name, title in index.items():
            if name in seed_names:
                continue
            if module:  # module filter applies to seed-tagged resources only
                continue
            if needle and needle not in name.lower() and needle not in (title or "").lower():
                continue
            results.append({"name": name, "title": title, "module": None, "source": "live-index"})
        return results[:limit]

    # ---- live describe (cached) ------------------------------------------

    async def describe(self, resource: str) -> dict[str, Any]:
        if resource not in self._describe_cache:
            raw = await self._client.describe(resource)
            summary = summarize_describe(resource, raw)
            self._describe_cache[resource] = summary
            self._attrs_cache[resource] = {
                a["name"] for a in summary.get("attributes", []) if a.get("name")
            }
        return self._describe_cache[resource]

    async def validate_filter(self, resource: str, q: str) -> None:
        """Validate a ``q`` filter against the resource's attributes (best-effort)."""
        attrs = self._attrs_cache.get(resource)
        if attrs is None:
            try:
                await self.describe(resource)
                attrs = self._attrs_cache.get(resource)
            except HcmApiError:
                return  # can't validate offline / on error — let the pod decide
        filters.validate_q(q, attrs or set())

    # ---- capability discovery (concurrent) -------------------------------

    async def get_capabilities(self, refresh: bool = False) -> dict[str, dict[str, Any]]:
        if self._capabilities is not None and not refresh:
            return self._capabilities

        modes = self._modules.model_dump()
        caps: dict[str, dict[str, Any]] = {}
        auto: list[str] = []
        for module, mode in modes.items():
            if mode == "off":
                caps[module] = {"mode": mode, "status": "disabled", "enabled": False}
            elif mode == "on":
                caps[module] = {"mode": mode, "status": "enabled", "enabled": True}
            elif MODULE_PROBES.get(module) is None:
                caps[module] = {"mode": mode, "status": "unknown", "enabled": False}
            else:
                auto.append(module)

        if auto:
            # Probe concurrently — sequential probing blows MCP tool timeouts.
            statuses = await asyncio.gather(
                *(self._client.classify_module(MODULE_PROBES[m]) for m in auto)
            )
            for module, status in zip(auto, statuses):
                caps[module] = {
                    "mode": "auto",
                    "status": status,
                    "enabled": status == "provisioned",
                    "probe": MODULE_PROBES[module],
                }

        self._capabilities = caps
        return caps

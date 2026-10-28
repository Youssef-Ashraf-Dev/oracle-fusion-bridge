"""Write tools (Phase 3) with a 4-layer gate stack.

Layer 1: ``features.writes_enabled`` off  -> refuse, never touch the pod.
Layer 2: ``dry_run`` (default True)         -> preview only (update diffs RAW then
         redacts the diff, so unchanged sensitive fields aren't falsely flagged).
Layer 3: schema validation                  -> block unknown / read-only attributes;
         validate action names against child_actions. FAIL CLOSED if the schema
         can't be fetched (unlike reads, which fail open).
Layer 4: audit                              -> dry-run vs committed recorded distinctly.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..context import ServerContext
from ..core.errors import HcmApiError

_WRITES_OFF = {
    "blocked": "writes_disabled",
    "note": "Writes are off. Set features.writes_enabled = true to enable (then use dry_run first).",
}


def _split_child(resource: str) -> tuple[str, str | None]:
    base = resource.split("/")[0]
    if "/child/" in resource:
        child = resource.split("/child/")[-1].split("/")[0]
        return base, child
    return base, None


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    async def _validate_write_payload(resource: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        base, _ = _split_child(resource)
        try:
            summary = await ctx.catalog.describe(base)
        except HcmApiError:
            return {"blocked": "schema_unavailable",
                    "note": "Cannot validate payload against schema; failing closed (write refused)."}
        attrs = summary.get("attributes") or []
        if not attrs:
            return {"blocked": "schema_unavailable",
                    "note": "No schema attributes available; failing closed (write refused)."}
        allowed = {a.get("name") for a in attrs if a.get("name")}
        readonly = {a.get("name") for a in attrs if a.get("updatable") is False}
        unknown = [k for k in payload if k not in allowed]
        read_only = [k for k in payload if k in readonly]
        if unknown or read_only:
            return {"error": "Payload rejected by schema validation.",
                    "unknown_attributes": unknown, "read_only_attributes": read_only}
        return None

    async def _validate_action(resource: str, action: str) -> dict[str, Any] | None:
        base, child = _split_child(resource)
        try:
            summary = await ctx.catalog.describe(base)
        except HcmApiError:
            return {"blocked": "schema_unavailable",
                    "note": "Cannot validate action against schema; failing closed (action refused)."}
        allowed = summary.get("child_actions", {}).get(child, []) if child else summary.get("actions", [])
        if action not in allowed:
            return {"error": f"Action {action!r} not available on {resource}.",
                    "available_actions": allowed}
        return None

    @mcp.tool()
    async def mutate_record(
        resource: str,
        op: str = "create",
        payload: dict[str, Any] | None = None,
        key: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Create, update, or delete a record. Off by default; dry-run by default.

        ``op``: create | update | delete. ``payload`` for create/update; ``key``
        for update/delete. With ``dry_run=True`` (default) returns a preview and
        writes nothing. Set ``dry_run=False`` to commit. Payloads are validated
        against the live schema (unknown/read-only attributes are rejected).

        LIMITATION: writes do not support effective-dating — date-effective
        resources (workers, assignments) commit with the pod's default
        effective date. Do not use for retroactive or future-dated changes.
        """
        if not ctx.config.features.writes_enabled:
            return _WRITES_OFF
        op = op.lower()
        if op not in ("create", "update", "delete"):
            return {"error": f"Unknown op {op!r}. Use create | update | delete."}
        if op in ("create", "update"):
            if not payload:
                return {"error": f"payload is required for {op}."}
            problem = await _validate_write_payload(resource, payload)
            if problem:
                ctx.audit.record(tool="mutate_record", resource=resource, key=key, write=True,
                                 status="blocked:schema")
                return problem
        if op in ("update", "delete") and not key:
            return {"error": f"key is required for {op}."}

        if dry_run:
            preview = await _preview(resource, op, payload, key)
            ctx.audit.record(tool="mutate_record", resource=resource, key=key, write=True,
                             status="dry_run")
            return preview

        if op == "create":
            result = await ctx.client.create(resource, payload or {})
        elif op == "update":
            result = await ctx.client.update(resource, key or "", payload or {})
        else:
            result = await ctx.client.delete(resource, key or "")
        ctx.audit.record(tool="mutate_record", resource=resource, key=key, write=True,
                         status="committed")
        return {"committed": op, "resource": resource, "key": key, "result": result}

    async def _preview(
        resource: str, op: str, payload: dict[str, Any] | None, key: str | None
    ) -> dict[str, Any]:
        if op == "create":
            return {"dry_run": True, "would_create": payload, "resource": resource}
        if op == "delete":
            return {"dry_run": True, "would_delete": key, "resource": resource}
        # update: fetch current RAW, diff RAW, then redact the diff.
        current = await ctx.client.get_record(resource, key or "", redact=False)
        changes: dict[str, Any] = {}
        for field, new_value in (payload or {}).items():
            if current.get(field) != new_value:
                changes[field] = {"from": current.get(field), "to": new_value}
        changes = ctx.redactor.redact(changes)
        return {"dry_run": True, "would_update": key, "resource": resource, "changes": changes}

    @mcp.tool()
    async def run_action(
        resource: str,
        key: str,
        action: str,
        params: dict[str, Any] | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Invoke a custom ADF action (e.g. terminate). Off by default; dry-run by default.

        The action name is validated against the resource's actions (or
        ``child_actions`` for a ``.../child/...`` path). With ``dry_run=True``
        (default) returns a preview and invokes nothing. Set ``dry_run=False`` to
        commit.
        """
        if not ctx.config.features.writes_enabled:
            return _WRITES_OFF
        problem = await _validate_action(resource, action)
        if problem:
            ctx.audit.record(tool="run_action", resource=resource, key=key, write=True,
                             status="blocked:schema")
            return problem
        if dry_run:
            ctx.audit.record(tool="run_action", resource=resource, key=key, write=True,
                             status="dry_run")
            return {"dry_run": True, "would_invoke": action, "resource": resource, "key": key,
                    "parameters": params}
        result = await ctx.client.invoke_action(resource, key, action, params)
        ctx.audit.record(tool="run_action", resource=resource, key=key, write=True,
                         status="committed")
        return {"committed_action": action, "resource": resource, "key": key, "result": result}

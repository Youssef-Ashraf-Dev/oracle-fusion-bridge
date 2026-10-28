"""Discovery tools: catalog browsing, schema introspection, capability report."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..context import ServerContext


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool()
    async def list_resources(
        search: str | None = None,
        module: str | None = None,
        limit: int = 50,
        full_index: bool = False,
    ) -> dict[str, Any]:
        """Search Oracle Fusion HCM REST resources.

        By default searches a bundled seed list of ~29 common resources (offline,
        instant). Set ``full_index=True`` to also search the pod's full live index
        (~650 resources, one-time load). Filter by ``search`` (name/title/
        description) and/or ``module`` (core_hr, compensation, absence, payroll,
        recruiting, talent, learning, benefits, time_labor). ``module`` applies to
        seed resources only. Use ``describe_resource`` to confirm a live schema.
        """
        if full_index:
            results = await ctx.catalog.list_live(search=search, module=module, limit=limit)
        else:
            seed = ctx.catalog.list_resources(search=search, module=module, limit=limit)
            results = [{**r, "source": "seed-catalog"} for r in seed]
        ctx.audit.record(tool="list_resources", count=len(results))
        return {"resources": results, "count": len(results)}

    @mcp.tool()
    async def describe_resource(resource: str) -> dict[str, Any]:
        """Return a resource's live schema: attributes, children, actions, child_actions.

        Hits Oracle's ``/describe`` endpoint (cached). This is the authoritative
        source of field names for ``fields=`` and ``q=``. ``child_actions`` lists
        business actions (e.g. terminate) that live on child collections — the
        most important write operations are found here, not in top-level actions.
        """
        summary = await ctx.catalog.describe(resource)
        ctx.audit.record(tool="describe_resource", resource=resource)
        return summary

    @mcp.tool()
    async def get_capabilities(refresh: bool = False) -> dict[str, Any]:
        """Report which HCM modules are live on this pod.

        For each module: its mode (on/off/auto) and discovered status
        (enabled/disabled/provisioned/not_provisioned/no_access/unreachable).
        ``auto`` modules are probed concurrently against the pod. Pass
        ``refresh=True`` to re-probe.
        """
        caps = await ctx.catalog.get_capabilities(refresh=refresh)
        ctx.audit.record(tool="get_capabilities")
        return {"modules": caps}

"""Read tools: the generic query workhorse and single-record fetch."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..context import ServerContext


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    exposed_sensitive = not ctx.redactor.enabled

    @mcp.tool()
    async def query_resource(
        resource: str,
        q: str | None = None,
        fields: list[str] | None = None,
        expand: list[str] | None = None,
        order_by: str | None = None,
        limit: int = 25,
        offset: int = 0,
        total_results: bool = False,
        effective_date: str | None = None,
    ) -> dict[str, Any]:
        """Query any Oracle Fusion HCM resource (the generic read workhorse).

        - ``q``: ADF filter, e.g. ``PersonNumber = 100010`` or ``LastName LIKE 'Sm%'``.
        - ``fields``: restrict returned attributes (strongly recommended to keep
          responses small). Use ``describe_resource`` to find valid names.
        - ``expand``: child collections to inline.
        - ``effective_date``: as-of date (YYYY-MM-DD) for date-effective data.
        Sensitive fields (national IDs, salary, DOB) are redacted unless the
        deployment enables them. Filters are validated against the schema first.
        """
        if q:
            await ctx.catalog.validate_filter(resource, q)
        result = await ctx.client.query(
            resource,
            q=q,
            fields=fields,
            expand=expand,
            order_by=order_by,
            limit=limit,
            offset=offset,
            total_results=total_results,
            effective_date=effective_date,
        )
        result["items"] = ctx.redactor.redact(result["items"])
        ctx.audit.record(
            tool="query_resource",
            resource=resource,
            fields=fields,
            count=result.get("count"),
            sensitive=exposed_sensitive,
        )
        return result

    @mcp.tool()
    async def get_record(
        resource: str,
        key: str,
        fields: list[str] | None = None,
        expand: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch a single record by its key, optionally expanding child collections.

        ``key`` is the resource's primary key (e.g. PersonId for ``workers``).
        Sensitive fields are redacted unless the deployment enables them.
        """
        record = await ctx.client.get_record(resource, key, fields=fields, expand=expand)
        record = ctx.redactor.redact(record)
        ctx.audit.record(
            tool="get_record",
            resource=resource,
            key=key,
            fields=fields,
            sensitive=exposed_sensitive,
        )
        return record

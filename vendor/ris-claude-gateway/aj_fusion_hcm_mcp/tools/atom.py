"""ATOM change-feed tool (Phase 4). Off unless ``features.atom_enabled``."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..context import ServerContext

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_WORKSPACE = "employee"
_FEEDS = {
    "newhire": "newhire",
    "termination": "termination",
    "empupdate": "empupdate",
    "assignment": "empassignment",
}


def _expand_since(since: str | None) -> str | None:
    if since and len(since) == 10 and since[4] == "-" and since[7] == "-":
        return f"{since}T00:00:00.000Z"
    return since


def _parse_entries(xml_text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return entries
    for entry in root.findall(f"{_ATOM_NS}entry"):
        title_el = entry.find(f"{_ATOM_NS}title")
        updated_el = entry.find(f"{_ATOM_NS}updated")
        content_el = entry.find(f"{_ATOM_NS}content")
        context: Any = None
        changed: Any = None
        if content_el is not None and content_el.text:
            try:
                payload = json.loads(content_el.text)
                if isinstance(payload, dict):
                    context = payload.get("Context")
                    changed = payload.get("ChangedAttributes") or payload.get("changedAttributes")
            except (ValueError, TypeError):
                context = None
        entries.append({
            "title": title_el.text if title_el is not None else None,
            "updated": updated_el.text if updated_el is not None else None,
            "context": context,
            "changed_attributes": changed,
        })
    return entries


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool()
    async def list_changes(feed: str, since: str, limit: int = 25) -> dict[str, Any]:
        """List HCM change events from an ATOM feed since a timestamp.

        ``feed`` is one of: newhire, termination, empupdate, assignment.
        ``since`` is an ISO timestamp or YYYY-MM-DD date. Disabled unless the
        deployment sets ``features.atom_enabled`` — otherwise returns a note and
        does not touch the pod.
        """
        if not ctx.config.features.atom_enabled:
            return {
                "note": "ATOM feeds are disabled. Set features.atom_enabled = true to use them.",
                "feed": feed,
            }
        collection = _FEEDS.get(feed)
        if not collection:
            return {"error": f"Unknown feed {feed!r}. Use one of {sorted(_FEEDS)}."}

        xml_text = await ctx.client.atom_feed(
            _WORKSPACE, collection, updated_min=_expand_since(since), page_size=limit
        )
        entries = _parse_entries(xml_text)[:limit]
        entries = ctx.redactor.redact(entries)
        ctx.audit.record(tool="list_changes", resource=f"{_WORKSPACE}/{collection}",
                         count=len(entries))
        return {"feed": feed, "changes": entries, "count": len(entries)}

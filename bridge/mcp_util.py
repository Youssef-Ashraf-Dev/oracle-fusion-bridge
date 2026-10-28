"""Helpers for talking to the vendor MCP directly (preflight, scripts, tests). See the cheat sheet."""
from __future__ import annotations

import json
from typing import Any


class ToolError(Exception):
    pass


def unwrap(result: Any) -> Any:
    """MCP tool result -> the payload.

    Handles the server's extra wrapper (FSCM and compute tools) and failures that arrive with
    isError false. Accepts either a dict result or an mcp CallToolResult object.
    """
    if not isinstance(result, dict):
        result = {"content": [{"text": getattr(c, "text", "")} for c in result.content],
                  "isError": bool(getattr(result, "isError", False) or getattr(result, "is_error", False))}
    text = result["content"][0]["text"] if result.get("content") else ""
    if result.get("isError"):
        raise ToolError(text)
    data = json.loads(text)
    if isinstance(data, dict) and isinstance(data.get("content"), list):          # the server's own wrapper
        data = json.loads(data["content"][0]["text"])
    if isinstance(data, dict) and "error" in data:                                # failure with isError false
        raise ToolError(str(data["error"]))
    return data

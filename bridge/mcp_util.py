"""Helpers for talking to the vendor MCP directly (preflight, scripts, tests). See the cheat sheet."""
from __future__ import annotations

import json
import re
from typing import Any, Optional


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


_URL_PREFIX = re.compile(r"https?://[^/\s']+/(?:fscmRestApi|hcmRestApi)/resources/[\d.]+/")
_MORE_INFO = re.compile(r"\s*For more information check:.*$", re.S)


def shorten_error(text: str, limit: int = 400) -> str:
    """Make a long HTTP error readable: no host, no version path, no boilerplate, so the query that failed stays in view."""
    text = _MORE_INFO.sub("", _URL_PREFIX.sub("\u2026/", text or "")).strip()
    return text[:limit]


_PLAIN_ERROR = re.compile(r"^(Error executing tool|Unknown tool|Error:)")
_ERROR_KEY = re.compile(r'\\*"error\\*"\s*:')


def payload_error(text: str) -> Optional[str]:
    """The failure message inside a tool result's text, or None when it is a normal result.

    Finance tools wrap their payload twice, so an error can arrive as {"content":[{"text":"{\\"error\\": ...}"}]} with the
    quotes escaped. A plain-text 'Error executing tool ...' also counts. Normal results return None.
    """
    t = (text or "").strip()
    if not t:
        return None
    if _PLAIN_ERROR.match(t):
        return t[:900]
    try:
        data = json.loads(t)
    except ValueError:
        return t[:900] if _ERROR_KEY.search(t[:600]) else None
    for _ in range(3):                                     # unwrap the server's own wrapper, however many times it was applied
        block = data["content"][0] if isinstance(data, dict) and isinstance(data.get("content"), list) and data["content"] else None
        if not (isinstance(block, dict) and isinstance(block.get("text"), str)):
            break
        inner = block["text"].strip()
        if _PLAIN_ERROR.match(inner):
            return inner[:900]
        try:
            data = json.loads(inner)
        except ValueError:
            return inner[:900] if _ERROR_KEY.search(inner[:600]) else None
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])[:900]
    return None

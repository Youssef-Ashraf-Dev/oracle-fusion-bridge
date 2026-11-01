"""Which fields does a pod accept? Test them one at a time, so a single bad name in a list is found."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Awaitable, Callable

from .mcp_util import ToolError, shorten_error

Call = Callable[[str, dict], Awaitable[Any]]


def field_map_keys(path: Path, name: str) -> list[str]:
    """The keys of a dict assigned to `name` in a Python file, read without importing the file."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return [str(k) for k in ast.literal_eval(node.value).keys()]
    raise KeyError(f"{name} is not defined in {path}")


async def probe_fields(call: Call, resource: str, fields: list[str]) -> dict[str, Any]:
    """Ask for one row of the resource with each field alone, and then with all of them together."""
    single: list[tuple[str, bool, str]] = []
    for f in fields:
        try:
            await call("fusion_query_resource", {"resource": resource, "fields": f, "limit": 1})
            single.append((f, True, ""))
        except ToolError as e:
            single.append((f, False, shorten_error(str(e), 200)))
    try:
        await call("fusion_query_resource", {"resource": resource, "fields": ",".join(fields), "limit": 1})
        together, together_error = True, ""
    except ToolError as e:
        together, together_error = False, shorten_error(str(e), 300)
    return {"fields": single, "together_ok": together, "together_error": together_error,
            "bad": [f for f, ok, _ in single if not ok], "good": [f for f, ok, _ in single if ok]}

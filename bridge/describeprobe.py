"""Look inside a resource description without loading it into a model: size, structure, and where a word appears.

The finance and HR descriptions of big resources (invoices, workers) are far larger than a model can read in one tool
result. This finds the field or child resource you are after, by keyword, straight from the pod.
"""
from __future__ import annotations

import json
from typing import Any, Iterator


def size_report(payload: Any, cap_tokens: int = 25000) -> dict:
    text = json.dumps(payload, ensure_ascii=False)
    tokens = len(text) // 4                                   # a rough rule: four characters to a token
    top = {}
    if isinstance(payload, dict):
        for k, v in payload.items():
            top[k] = {"type": type(v).__name__, "items": len(v) if isinstance(v, (list, dict)) else None,
                      "chars": len(json.dumps(v, ensure_ascii=False))}
    return {"chars": len(text), "approx_tokens": tokens, "fits_in_one_result": tokens <= cap_tokens, "top_level": top}


def _walk(value: Any, path: str = "") -> Iterator[tuple[str, Any, Any]]:
    """(path, key-or-None, value) for every node. List items are labelled by their own name field when they have one."""
    if isinstance(value, dict):
        for k, v in value.items():
            p = f"{path}.{k}" if path else str(k)
            yield p, k, v
            yield from _walk(v, p)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            label = None
            if isinstance(v, dict):
                label = next((str(v[k]) for k in ("name", "Name", "attributeName", "title") if isinstance(v.get(k), str)), None)
            p = f"{path}[{label or i}]"
            yield p, None, v
            yield from _walk(v, p)


def find_hits(payload: Any, words: list[str], limit: int = 40) -> list[str]:
    """Where any word appears, as a key, a name or a short text value. Case-insensitive; one line per place."""
    words = [w.lower() for w in words if w]
    hits: list[str] = []
    seen: set[str] = set()
    for path, key, value in _walk(payload):
        keyname = str(key).lower() if key is not None else ""
        short = value if isinstance(value, str) and len(value) <= 160 else None
        hay = path.lower() if key is None else keyname
        text_hit = short is not None and any(w in short.lower() for w in words)
        key_hit = any(w in hay for w in words)
        if key_hit or text_hit:
            detail = short
            if detail is None and isinstance(value, dict):
                bits = [f"{k}={v}" for k, v in value.items() if isinstance(v, (str, int, float, bool)) and k.lower() != "name"
                        and len(str(v)) <= 60]
                detail = ", ".join(bits[:6]) or None
            line = path + (f" = {detail}" if detail is not None else "")
            if line not in seen:
                seen.add(line)
                hits.append(line)
                if len(hits) >= limit:
                    break
    return hits


def child_names(payload: Any) -> list[str]:
    """Names of child resources, wherever the description keeps them (a key like children/childResources)."""
    out: list[str] = []
    for path, key, value in _walk(payload):
        if key is not None and str(key).lower() in ("children", "childresources", "child_resources", "links") and isinstance(value, (list, dict)):
            items = value if isinstance(value, list) else list(value.keys())
            for it in items:
                name = it if isinstance(it, str) else (next((str(it[k]) for k in ("name", "Name", "rel", "resource") if isinstance(it, dict) and isinstance(it.get(k), str)), None))
                if name and name not in out:
                    out.append(name)
    return out

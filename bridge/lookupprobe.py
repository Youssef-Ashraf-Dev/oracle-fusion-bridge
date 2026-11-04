"""Why did only one name come back? Look the same people up in several ways and compare what each way returns.

Only counts and key values (person numbers) are reported, never names or any other field, so the output is safe to share.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from .mcp_util import ToolError, shorten_error

Call = Callable[[str, dict], Awaitable[Any]]


def rows_of(payload: Any) -> list[dict]:
    """The rows in a tool result, wherever the server put them: the longest list of dicts found anywhere in the payload."""
    best: list[dict] = []

    def walk(v: Any) -> None:
        nonlocal best
        if isinstance(v, list):
            if v and all(isinstance(x, dict) for x in v) and len(v) > len(best):
                best = v
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
    walk(payload)
    return best


def values_of(rows: Iterable[dict], key: str) -> list[str]:
    """The distinct values of `key` (any capitalisation), in the order they first appear."""
    seen: dict[str, None] = {}
    for r in rows:
        for k, v in r.items():
            if k.lower() == key.lower() and v not in (None, ""):
                seen.setdefault(str(v), None)
    return list(seen)


def build_in_q(key: str, values: list[str]) -> str:
    return f"{key} IN (" + ", ".join(f"'{v.replace(chr(39), chr(39) * 2)}'" for v in values) + ")"


def build_q(key: str, values: list[str], quoted: bool = True) -> str:
    return " or ".join(f"{key} = '{v.replace(chr(39), chr(39) * 2)}'" if quoted else f"{key} = {v}" for v in values)


@dataclass
class Result:
    label: str
    asked: list[str]
    found: set[str] = field(default_factory=set)
    calls: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [v for v in self.asked if v not in self.found]


async def run_strategy(call: Call, resource: str, key: str, fields: list[str], values: list[str], size: int, quoted: bool,
                       label: str, as_of: str | None = None, use_in: bool = False) -> Result:
    res = Result(label, list(values))
    for i in range(0, len(values), size):
        part = values[i:i + size]
        q = build_in_q(key, part) if use_in else build_q(key, part, quoted)
        args: dict = {"resource": resource, "q": q, "fields": fields, "limit": max(25, len(part) + 5)}
        if as_of:
            args["effective_date"] = as_of
        res.calls += 1
        try:
            res.found |= set(values_of(rows_of(await call("query_resource", args)), key)) & set(part)
        except ToolError as e:
            res.errors.append(shorten_error(str(e), 160))
    return res


async def probe_lookup(call: Call, resource: str, key: str, fields: list[str], values: list[str], as_of: str | None = None) -> list[Result]:
    n = len(values)
    plan = [(f"all {n} in one query, joined with or (quoted)", n, True, False), (f"all {n} in one query, joined with or (not quoted)", n, False, False),
            (f"all {n} in one query, using IN (...)", n, True, True), ("batches of 5", 5, True, False), ("one at a time", 1, True, False)]
    return [await run_strategy(call, resource, key, fields, values, size, quoted, label, as_of, use_in) for label, size, quoted, use_in in plan]


def advise(results: list[Result]) -> list[str]:
    asked = set(results[0].asked)
    union = set().union(*(r.found for r in results))
    never = [v for v in results[0].asked if v not in union]
    single = next(r for r in results if r.label == "one at a time")
    lines = []
    if never:
        lines.append(f"{len(never)} of {len(asked)} people are not returned by ANY way of asking ({', '.join(never[:12])}): the resource does "
                     "not show them. Try --as-of with an earlier date (they may have left or changed since), or another resource.")
    complete = [r for r in results if not r.missing]
    if not never and not single.missing:
        best = max(complete, key=lambda r: len(r.asked) / max(r.calls, 1)) if complete else single
        if best is not single:
            lines.append(f"Everyone is found, and '{best.label}' returns all of them: that way is safe.")
        in_all = next((r for r in results if "using IN" in r.label and not r.missing), None)
        if in_all:
            lines.append("One call can fetch everyone with IN (...): that is the way to ask. Tell me and I will put it in the recipe.")
        broken = [r for r in results if r.missing and r.label != "one at a time"]
        if broken:
            lines.append("These ways of asking LOSE people, so batch the lookup instead: " + "; ".join(f"'{r.label}' returned {len(r.found)} of {len(asked)}" for r in broken)
                         + f". Only '{single.label}' is complete" + (f" (and '{complete[0].label}')" if complete and complete[0] is not single else "") + ".")
    if not lines:
        lines.append("Every way of asking returned everyone. The lookup itself is fine: the earlier gap came from how the question was asked that time.")
    return lines

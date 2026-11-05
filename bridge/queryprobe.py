"""Try one read the way the bot might ask for it, and see what the pod does: does it accept this sort order, are the rows really in order?

Only the fields you ask to see are printed, so no name appears unless you choose to show one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .lookupprobe import rows_of
from .mcp_util import ToolError, shorten_error
from .verify import find_total

Call = Callable[[str, dict], Awaitable[Any]]


def build_args(resource: str, q: Optional[str] = None, fields: Optional[list[str]] = None, order_by: Optional[str] = None, limit: int = 5,
               offset: int = 0, effective_date: Optional[str] = None, total: bool = False, finance: bool = False) -> tuple[str, dict]:
    """The tool name and arguments. The HR tool takes fields as a list and has order_by; the finance tool takes a comma string and has none."""
    args: dict = {"resource": resource, "limit": limit}
    if offset:
        args["offset"] = offset
    if q:
        args["q"] = q
    if fields:
        args["fields"] = ",".join(fields) if finance else list(fields)
    if finance:
        if total:
            args["totalResults"] = True
        return "fusion_query_resource", args
    if order_by:
        args["order_by"] = order_by
    if effective_date:
        args["effective_date"] = effective_date
    if total:
        args["total_results"] = True
    return "query_resource", args


def ordering(values: list[str]) -> str:
    """'descending', 'ascending', 'unordered' or 'n/a' for what came back. Numbers are compared as numbers, other values as text."""
    if len(values) < 2:
        return "n/a"
    try:
        keyed = [float(v) for v in values]
    except ValueError:
        keyed = list(values)
    if keyed == sorted(keyed, reverse=True) and keyed != sorted(keyed):
        return "descending"
    if keyed == sorted(keyed) and keyed != sorted(keyed, reverse=True):
        return "ascending"
    return "same value" if len(set(keyed)) == 1 else "unordered"


def shown_values(rows: list[dict], name: str) -> list[str]:
    return [str(v) for r in rows for k, v in r.items() if k.lower() == name.lower() and v is not None]


@dataclass
class Result:
    label: str
    ok: bool
    rows: int = 0
    error: str = ""
    keys: list[str] = field(default_factory=list)
    shown: dict[str, list[str]] = field(default_factory=dict)
    order: str = "n/a"


async def probe_orderings(call: Call, resource: str, variants: list[Optional[str]], show: list[str], q: Optional[str] = None,
                          fields: Optional[list[str]] = None, limit: int = 5, effective_date: Optional[str] = None) -> list[Result]:
    """One call without a sort (the baseline) and one per sort spelling. The first shown field is judged for order."""
    out: list[Result] = []
    for v in [None] + list(variants):
        tool, args = build_args(resource, q, fields, v, limit, effective_date=effective_date)
        label = "no order_by (baseline)" if v is None else f"order_by = {v}"
        try:
            rows = rows_of(await call(tool, args))
        except ToolError as e:
            out.append(Result(label, False, error=shorten_error(str(e), 150)))
            continue
        shown = {f: shown_values(rows, f) for f in show}
        out.append(Result(label, True, len(rows), keys=sorted({k for r in rows for k in r}), shown=shown,
                          order=ordering(shown[show[0]]) if show else "n/a"))
    return out


def verdict(results: list[Result], descending_wanted: bool = True) -> list[str]:
    """What the results mean, in words."""
    want = "descending" if descending_wanted else "ascending"
    good = [r for r in results[1:] if r.ok and r.order == want]
    accepted_but_wrong = [r for r in results[1:] if r.ok and r.order not in (want, "n/a")]
    lines = []
    if good:
        lines.append(f"Works: {', '.join(r.label for r in good)} returned the rows {want}. One call with a small limit can answer 'the latest N'.")
    if accepted_but_wrong:
        lines.append("Accepted but did not sort: " + ", ".join(f"{r.label} ({r.order})" for r in accepted_but_wrong) + ".")
    rejected = [r.label for r in results[1:] if not r.ok]
    if rejected:
        lines.append("Rejected by the pod: " + ", ".join(rejected) + ".")
    if not good:
        lines.append("No spelling sorted the rows, so 'the latest N' still needs reading a date window and sorting it in the bridge's answer.")
    return lines


def outside_window(values: list[str], low: str, high: str) -> tuple[int, int]:
    """How many of these dates fall outside [low, high], and how many there are. ISO dates compare correctly as text."""
    days = [str(v)[:10] for v in values]
    return sum(1 for d in days if d < low or d > high), len(days)


@dataclass
class FilterResult:
    q: Optional[str]
    ok: bool
    rows: int = 0
    total: Optional[int] = None
    error: str = ""
    shown: dict[str, list[str]] = field(default_factory=dict)
    outside: Optional[tuple[int, int]] = None


async def probe_filters(call: Call, resource: str, qs: list[Optional[str]], show: list[str], fields: Optional[list[str]] = None,
                        limit: int = 10, window: Optional[tuple[str, str]] = None, effective_date: Optional[str] = None,
                        finance: bool = False) -> list[FilterResult]:
    """Send each filter spelling to the pod and report what came back: accepted or rejected, how many rows match in total, and the values of the
    fields to show. With a window, also how many of the first shown field's dates fall outside it. The total is the strongest evidence: a condition
    that is silently dropped leaves the total unchanged. finance=True reads through the finance tool (fusion_query_resource) instead of the HR one."""
    out: list[FilterResult] = []
    for q in qs:
        tool, args = build_args(resource, q, fields, None, limit, effective_date=effective_date, total=True, finance=finance)
        try:
            payload = await call(tool, args)
        except ToolError as e:
            out.append(FilterResult(q, False, error=shorten_error(str(e), 150)))
            continue
        rows = rows_of(payload)
        try:
            total: Optional[int] = find_total(payload)
        except LookupError:
            total = None
        shown = {f: shown_values(rows, f) for f in show}
        outside = outside_window(shown[show[0]], *window) if window and show else None
        out.append(FilterResult(q, True, len(rows), total, shown=shown, outside=outside))
    return out


def filter_verdicts(results: list[FilterResult]) -> list[str]:
    lines = []
    for r in results:
        label = r.q or "(no filter)"
        if not r.ok:
            lines.append(f"REJECTED  {label}")
        elif r.outside is not None:
            bad, n = r.outside
            lines.append(f"no rows   {label}" if n == 0 else
                         f"inside    {label}   (all {n} rows read are inside the window)" if bad == 0 else
                         f"OUTSIDE   {label}   ({bad} of {n} rows read are outside the window)")
    return lines

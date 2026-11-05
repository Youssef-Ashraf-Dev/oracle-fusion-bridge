"""Is the answer RIGHT? Ask Fusion the same thing directly, and compare it with what the bot said.

The scenarios check behaviour (it read data, it refused a write, it stayed in scope). They never check that a number is correct.
A verifier does: it computes the true answer with a direct, deterministic query before and after the bot answers, finds the numbers
in the bot's answer, and compares. It catches wrong counts, invented counts, wrong filters and 'a sample passed off as the whole'.
It cannot catch a misunderstanding that the verifier and the bot share, so each real question needs its verifier reviewed by a person
who knows what the business means by it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

Call = Callable[[str, dict], Awaitable[Any]]
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

_DIGITS = str.maketrans("\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9",
                        "01234567890123456789")
_NOT_COUNTS = [re.compile(r"\d{4}-\d{1,2}-\d{1,2}"), re.compile(r"\d{1,2}:\d{2}(?::\d{2})?"), re.compile(r"(?m)^\s*\d+[.)]\s"),
               re.compile(r"[A-Za-z][\w-]*\d[\w.-]*")]                       # dates, times, list markers, host names and ids like dev1


def numbers_in(text: str) -> set[int]:
    """Every whole number in an answer, read the way people write counts: 4,520 or 4.520 or 4 520, and Arabic-Indic digits.
    Dates, times, list numbers and ids that contain letters are removed first, so '2025-12-31' cannot match a count of 12."""
    t = (text or "").translate(_DIGITS).replace("\u066c", ",").replace("\u066b", ".")
    for rx in _NOT_COUNTS:
        t = rx.sub(" ", t)
    out: set[int] = set()
    for m in re.finditer(r"\d[\d,.\u00a0 ]*\d|\d", t):
        raw = m.group(0).replace("\u00a0", " ")
        if re.fullmatch(r"\d{1,3}( \d{3})+", raw):                       # 4 520
            out.add(int(raw.replace(" ", "")))
        elif re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", raw):             # 4,520 or 1,234.56
            out.add(int(raw.split(".")[0].replace(",", "")))
        elif re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", raw):             # 4.520 or 1.234,56
            out.add(int(raw.split(",")[0].replace(".", "")))
        elif re.fullmatch(r"\d+[.,]\d{1,2}", raw):                       # 12.5 or 3,75: a decimal, whose whole part is the only count
            out.add(int(re.split(r"[.,]", raw)[0]))
        else:
            out.update(int(p) for p in re.findall(r"\d+", raw))
    return out


def find_total(payload: Any) -> int:
    """The total a read returned when totalResults was asked for, wherever the server put it."""
    keys = ("totalresults", "total_results", "total")

    def walk(v: Any) -> Optional[int]:
        if isinstance(v, dict):
            for k, x in v.items():
                if str(k).lower() in keys and re.fullmatch(r"\d+", str(x).strip()):
                    return int(str(x).strip())
            for x in v.values():
                r = walk(x)
                if r is not None:
                    return r
        elif isinstance(v, list):
            for x in v:
                r = walk(x)
                if r is not None:
                    return r
        return None
    found = walk(payload)
    if found is None:
        seen = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise LookupError(f"no total in the response (looked for totalResults, total_results, total); top-level keys: {seen}")
    return found


@dataclass(frozen=True)
class Case:
    vid: str
    question: str
    label: str
    truth: Callable[[Call], Awaitable[int]]             # the true number, from a direct query


@dataclass
class Asked:
    answer: str
    status: str = "ok"
    cost_usd: float = 0.0
    seconds: float = 0.0


Ask = Callable[[str], Awaitable[Asked]]


@dataclass
class Verdict:
    vid: str
    status: str
    expected_before: Optional[int] = None
    expected_after: Optional[int] = None
    found: list[int] = field(default_factory=list)
    note: str = ""
    cost_usd: float = 0.0
    seconds: float = 0.0
    answer: str = ""                                     # kept in memory only; never written to the report


async def verify_case(case: Case, call: Call, ask: Ask) -> Verdict:
    try:
        before = await case.truth(call)
    except Exception as e:                                           # the truth itself could not be read: say so, do not guess
        return Verdict(case.vid, WARN, note=f"could not get the true number from Fusion: {str(e)[:160]}")
    asked = await ask(case.question)
    v = Verdict(case.vid, FAIL, expected_before=before, cost_usd=asked.cost_usd, seconds=asked.seconds, answer=asked.answer)
    if asked.status != "ok" or not asked.answer.strip():
        v.note = f"the bot gave no answer (status {asked.status})"
        return v
    try:
        after = await case.truth(call)
    except Exception:
        after = before
    v.expected_after = after
    nums = numbers_in(asked.answer)
    v.found = sorted(nums)[:12]
    want = {before, after}
    if nums & want:
        v.status = PASS
        v.note = "" if before == after else f"the data changed while the bot was answering ({before} before, {after} after); the bot matched one of them"
        if before == after and before < 10 and len(nums) > 1:
            v.note = "matched, but the number is small and the answer holds other numbers too, so a coincidence is possible: check by eye"
            v.status = WARN
    elif not nums:
        v.note = f"the answer contains no number; Fusion says {before}"
    else:
        v.note = f"the bot gave {', '.join(map(str, v.found[:6]))}; Fusion says {before}" + ("" if before == after else f" (then {after})")
    return v


# ---- the cases: questions whose answer is one number a direct query can produce ------------------------------------------------

def _fscm_total(resource: str, q: Optional[str] = None) -> Callable[[Call], Awaitable[int]]:
    async def truth(call: Call) -> int:
        args: dict = {"resource": resource, "limit": 1, "totalResults": True}
        if q:
            args["q"] = q
        return find_total(await call("fusion_query_resource", args))
    return truth


def _hcm_total(resource: str, q: str) -> Callable[[Call], Awaitable[int]]:
    async def truth(call: Call) -> int:
        return find_total(await call("query_resource", {"resource": resource, "q": q, "limit": 1, "total_results": True}))
    return truth


def _fscm_difference(resource: str, q_from: str, q_to: str) -> Callable[[Call], Awaitable[int]]:
    """The same idea on the finance side: total(q_from) minus total(q_to), each with ONE condition on the date, because the pod keeps only the
    last of two conditions on one attribute (measured on invoices: InvoiceDate >= A;InvoiceDate <= B returned the total of <= B alone)."""
    async def truth(call: Call) -> int:
        a = find_total(await call("fusion_query_resource", {"resource": resource, "q": q_from, "limit": 1, "totalResults": True}))
        b = find_total(await call("fusion_query_resource", {"resource": resource, "q": q_to, "limit": 1, "totalResults": True}))
        return a - b
    return truth


def _hcm_difference(resource: str, q_from: str, q_to: str) -> Callable[[Call], Awaitable[int]]:
    """A count over a window as the difference of two totals: total(q_from) minus total(q_to). The pod ignores all but the last of several
    conditions on one attribute, so a window cannot be asked for in one filter; two filters with ONE date condition each can."""
    async def truth(call: Call) -> int:
        a = find_total(await call("query_resource", {"resource": resource, "q": q_from, "limit": 1, "total_results": True}))
        b = find_total(await call("query_resource", {"resource": resource, "q": q_to, "limit": 1, "total_results": True}))
        return a - b
    return truth


def cases() -> list[Case]:
    return [
        Case("V1", "How many payables invoices are there in total? Give me just the number.", "Payables invoices, total",
             _fscm_total("invoices")),
        Case("V2", "How many ledgers are there on this pod? Give me just the number.", "Ledgers, total", _fscm_total("ledgersLOV")),
        Case("V3", "How many absences started on or after 1 September 2026? Give me just the number.", "Absences starting on or after 2026-09-01",
             _hcm_total("absences", "startDate >= 2026-09-01")),               # ONE condition: the pod ignores all but the last of several
        Case("V4", "How many sick leave absences started in 2025? Give me just the number.", "Sick leave absences starting in 2025",
             _hcm_difference("absences", "absenceType LIKE 'Sick%';startDate >= 2025-01-01", "absenceType LIKE 'Sick%';startDate >= 2026-01-01")),
        Case("V5", "How many payables invoices are dated in January 2026? Give me just the number.", "Invoices dated in January 2026",
             _fscm_difference("invoices", "InvoiceDate >= '2026-01-01'", "InvoiceDate >= '2026-02-01'")),
    ]


def render(verdicts: list[tuple[Case, Verdict]]) -> str:
    L = ["| Case | Result | Fusion says | Bot said | Cost | Seconds | Note |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for c, v in verdicts:
        exp = "?" if v.expected_before is None else (str(v.expected_before) if v.expected_before == v.expected_after else f"{v.expected_before} then {v.expected_after}")
        L.append(f"| {v.vid} {c.label} | {v.status} | {exp} | {', '.join(map(str, v.found[:5])) or '-'} | ${v.cost_usd:.3f} | {v.seconds:.0f} | {v.note} |")
    return "\n".join(L)

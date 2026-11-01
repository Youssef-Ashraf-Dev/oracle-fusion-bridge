"""Read the bot's own record of what happened: recent runs, outcomes, slow and costly ones, refusals."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


def _p90(values: list[float]) -> float:
    v = sorted(values)
    return v[(9 * len(v) + 9) // 10 - 1] if v else 0.0


def load_runs(db_path: Path | str, last: int = 50) -> list[dict[str, Any]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM runs WHERE kind='ask' ORDER BY id DESC LIMIT ?", (last,)).fetchall()
        has_meta = "meta" in rows[0].keys() if rows else True
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["meta"] = json.loads(d.get("meta") or "{}") if has_meta else {}
            except ValueError:
                d["meta"] = {}
            out.append(d)
    finally:
        con.close()
    return list(reversed(out))


def denial_reasons(db_path: Path | str, last: int = 500) -> Counter:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute("SELECT detail FROM audit WHERE event='tool_denied' ORDER BY id DESC LIMIT ?", (last,)).fetchall()
    finally:
        con.close()
    c: Counter = Counter()
    for (detail,) in rows:
        reason = detail.split(" :: ", 1)[-1]
        c[re.sub(r"\d{4,}", "#", reason)[:110]] += 1
    return c


def summary(runs: list[dict]) -> dict[str, Any]:
    secs = [r["duration_s"] for r in runs]
    kinds = Counter((r["meta"].get("error_kind") or "") for r in runs if r["status"] == "error")
    return {
        "runs": len(runs), "by_status": dict(Counter(r["status"] for r in runs)),
        "cost_total": round(sum(r["cost_usd"] for r in runs), 4),
        "secs_avg": round(statistics.mean(secs), 1) if secs else 0.0, "secs_p90": round(_p90(secs), 1),
        "error_kinds": {k or "(none given)": n for k, n in kinds.items()},
        "with_tool_errors": sum(1 for r in runs if r["meta"].get("tool_errors")),
        "results_cut": sum(int(r["meta"].get("results_cut") or 0) for r in runs),
        "avg_plans": round(statistics.mean([r["meta"].get("plans", 0) for r in runs]), 2) if runs else 0.0,
    }


def render(db_path: Path | str, last: int = 30) -> str:
    runs = load_runs(db_path, last)
    if not runs:
        return "No runs recorded yet. Ask the bot a question first."
    s = summary(runs)
    L = [f"Last {s['runs']} questions: {s['by_status']}  |  cost ${s['cost_total']:.3f}  |  seconds avg {s['secs_avg']}, p90 {s['secs_p90']}"
         f"  |  plans per question {s['avg_plans']}",
         f"Runs with tool errors: {s['with_tool_errors']}  |  results cut: {s['results_cut']}  |  engine errors: {s['error_kinds'] or 'none'}", "",
         "When      status    cost     secs turns plans ok/tried  tools"]
    for r in runs:
        m = r["meta"]
        when = dt.datetime.fromtimestamp(r["ts"]).strftime("%d %H:%M")
        L.append(f"{when:9} {r['status']:9} ${r['cost_usd']:<7.4f} {r['duration_s']:>4.0f} {m.get('turns', ''):>5} {m.get('plans', ''):>5} "
                 f"{str(m.get('data_ok', '')) + '/' + str(m.get('data_tried', '')):>9}  {str(m.get('tools', ''))[:70]}")
    slow = sorted(runs, key=lambda r: r["duration_s"], reverse=True)[:3]
    L += ["", "Slowest: " + "; ".join(f"{r['duration_s']:.0f}s ({r['status']}, {r['meta'].get('turns', '?')} turns)" for r in slow)]
    errs = [(r["status"], e) for r in runs for e in r["meta"].get("tool_errors", [])]
    if errs:
        L += ["", "Tool errors seen (first 8):"] + [f"  {st}: {e[:150]}" for st, e in errs[:8]]
    den = denial_reasons(db_path)
    if den:
        L += ["", "Why the policy refused calls (all time, newest 500):"] + [f"  {n:3}x {why}" for why, n in den.most_common(8)]
    return "\n".join(L)

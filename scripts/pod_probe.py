"""Probe the Oracle pod step by step, outside the MCP, to see exactly what it answers.

    python scripts/pod_probe.py

Uses the same .env as everything else. It prints statuses and, for failures, a short piece of the pod's own reply.
It never prints the password.
"""
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.logsafe import safe_streams     # noqa: E402
safe_streams()

import httpx                                                              # noqa: E402

from bridge.config import env_conflicts, load_settings, origin             # noqa: E402


@dataclass
class Result:
    name: str
    path: str
    status: Optional[int]
    note: str = ""
    body: str = ""


def steps(version: str) -> list[tuple[str, str, bool]]:
    fscm, hcm = f"/fscmRestApi/resources/{version}", f"/hcmRestApi/resources/{version}"
    return [("Pod front door (no login)", "/", False),
            ("Finance REST: invoices", f"{fscm}/invoices?limit=1&onlyData=true&fields=InvoiceId", True),
            ("Finance REST: purchase orders", f"{fscm}/purchaseOrders?limit=1&onlyData=true&fields=POHeaderId", True),
            ("Finance REST: suppliers", f"{fscm}/suppliers?limit=1&onlyData=true&fields=SupplierId", True),
            ("HR REST: workers", f"{hcm}/workers?limit=1&onlyData=true", True)]


def snippet(text: str, n: int = 220) -> str:
    text = re.sub(r"<(script|style).*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:n]


async def probe(base: str, user: str, password: str, version: str, timeout: float = 30,
                transport: Optional[httpx.AsyncBaseTransport] = None) -> list[Result]:
    base = base.rstrip("/")
    out: list[Result] = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, transport=transport) as client:
        for name, path, auth in steps(version):
            try:
                r = await client.get(base + path, auth=httpx.BasicAuth(user, password) if auth else None,
                                     headers={"Accept": "application/json"})
                body = "" if r.status_code < 400 else snippet(r.text)
                note = f"retry-after {r.headers['retry-after']}" if "retry-after" in r.headers else ""
                out.append(Result(name, path.split("?")[0], r.status_code, note, body))
            except Exception as e:                                         # DNS, TLS, proxy, timeout
                out.append(Result(name, path.split("?")[0], None, f"{type(e).__name__}: {str(e)[:160]}"))
    return out


def advice(results: list[Result]) -> list[str]:
    door, rest = results[0], results[1:]
    codes = [r.status for r in rest]
    if door.status is None or door.status >= 500:
        return ["The pod's front door does not answer normally. Open the base URL in a browser on this machine.",
                "If the browser also fails, the pod is down or unreachable from here (stopped dev pod, VPN, firewall or proxy).",
                "If the browser works but this fails, a proxy or certificate setting on this machine is the likely cause."]
    if all(c == 200 for c in codes):
        return ["Everything answered. The login and the REST services work: rerun scripts/preflight.py."]
    tips = []
    if any(c == 401 for c in codes):
        tips.append("401: the username or password is wrong (or the account is locked or expired). Check for typos and quotes in .env.")
    if any(c == 403 for c in codes):
        tips.append("403: the login works but the account lacks REST access or the roles for that area. Ask the admin for view-only roles.")
    if any(c == 404 for c in codes):
        tips.append("404: the API version or the module is not on this pod. Ask which REST version it runs (ORACLE_FUSION_API_VERSION).")
    if any(c is not None and c >= 500 for c in codes):
        bad = [r.name for r in rest if r.status is not None and r.status >= 500]
        if len(bad) == len(rest):
            tips.append("5xx on every REST call while the front door is fine: the REST services are down or restarting. Try again later, or ask the pod owner.")
        else:
            tips.append("5xx only on " + ", ".join(bad) + ": that area is unavailable on this pod (not provisioned or failing). The others work.")
    if any(c is None for c in codes):
        tips.append("Some calls did not complete at all: check the network, VPN and proxy settings.")
    return tips


def report(base: str, user: str, password: str, results: list[Result], sources: Optional[dict] = None,
           problems: Optional[list] = None) -> str:
    proxies = [k for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy") if os.environ.get(k)]
    src = sources or {}
    lines = [f"Pod:      {base!r}" + (f"   <- from {src['pod']}" if "pod" in src else ""),
             f"User:     {user!r} ({len(user)} characters)" + (f"   <- from {src['user']}" if "user" in src else ""),
             f"Password: {'set' if password else 'EMPTY'} ({len(password)} characters)" + (f"   <- from {src['password']}" if "password" in src else "")]
    if "placeholder" in base.lower() or "your-nonprod-pod" in base.lower():
        lines.append("!! The pod URL is still the example value from .env.example. Put the real pod URL in .env.")
    lines += [f"!! {p}" for p in (problems or [])]
    if proxies:
        lines.append(f"Proxy variables set: {', '.join(proxies)}")
    lines.append("")
    for r in results:
        verdict = "OK" if r.status and r.status < 400 else "FAIL"
        lines.append(f"{verdict:4} {r.name:32} {r.path[:46]:46} -> {r.status if r.status is not None else 'no answer'}"
                     + (f"  ({r.note})" if r.note else ""))
        if r.body:
            lines.append(f"       the pod says: {r.body}")
    lines += ["", "What this means:"] + ["- " + a for a in advice(results)]
    return "\n".join(lines)


async def main() -> int:
    s = load_settings()
    if not (s.oracle_base_url and s.oracle_user and s.oracle_password):
        print("ORACLE_FUSION_BASE_URL, ORACLE_USERNAME and ORACLE_PASSWORD must all be set in .env")
        return 2
    results = await probe(s.oracle_base_url, s.oracle_user, s.oracle_password, s.oracle_api_version)
    sources = {"pod": origin("ORACLE_FUSION_BASE_URL"), "user": origin("ORACLE_USERNAME"), "password": origin("ORACLE_PASSWORD")}
    print(report(s.oracle_base_url, s.oracle_user, s.oracle_password, results, sources, env_conflicts()))
    return 0 if all(r.status == 200 for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

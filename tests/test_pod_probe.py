import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location("pod_probe", Path(__file__).resolve().parents[1] / "scripts" / "pod_probe.py")
pp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pp)


def server(routes):
    """routes: {path-substring: (status, body)}; first match wins; the front door is '/'."""
    def handler(request: httpx.Request) -> httpx.Response:
        for key, (status, body) in routes.items():
            if key == request.url.path or (key != "/" and key in request.url.path):
                return httpx.Response(status, text=body, headers={"retry-after": "120"} if status == 503 else {})
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)


def run(routes):
    return asyncio.run(pp.probe("https://pod.example.com/", "u", "p", "11.13.18.05", transport=server(routes)))


OK = {"/": (200, "<html>sign in</html>"), "invoices": (200, "{}"), "purchaseOrders": (200, "{}"),
      "suppliers": (200, "{}"), "workers": (200, "{}")}


def test_all_good_says_rerun_the_preflight():
    r = run(OK)
    assert [x.status for x in r] == [200] * 5 and "rerun scripts/day1_preflight.py" in pp.advice(r)[0]
    assert "OK" in pp.report("https://pod.example.com", "u", "p", r)


def test_wrong_password_is_a_401_and_says_so():
    r = run({**OK, "invoices": (401, "unauthorized"), "purchaseOrders": (401, ""), "suppliers": (401, ""), "workers": (401, "")})
    assert any("401" in a and "password" in a for a in pp.advice(r))


def test_missing_roles_is_a_403():
    r = run({**OK, "invoices": (403, "Forbidden")})
    assert any("403" in a and "roles" in a for a in pp.advice(r))


def test_503_everywhere_while_the_front_door_is_fine_points_at_the_rest_services():
    all503 = {**OK, **{k: (503, "<html><body>Service Unavailable. Please try later.</body></html>") for k in ("invoices", "purchaseOrders", "suppliers", "workers")}}
    r = run(all503)
    assert "REST services are down" in " ".join(pp.advice(r))
    text = pp.report("https://pod.example.com", "u", "p", r)
    assert "the pod says: Service Unavailable. Please try later." in text and "retry-after 120" in text


def test_503_on_one_area_only_names_that_area():
    r = run({**OK, "purchaseOrders": (503, "down")})
    assert "5xx only on Finance REST: purchase orders" in " ".join(pp.advice(r))


def test_front_door_down_points_at_the_pod_or_network():
    r = run({**OK, "/": (503, "maintenance")})
    assert "front door" in pp.advice(r)[0]


def test_unreachable_host_is_reported_not_raised():
    def boom(request): raise httpx.ConnectError("Name or service not known")
    r = asyncio.run(pp.probe("https://nope.invalid", "u", "p", "11.13.18.05", transport=httpx.MockTransport(boom)))
    assert all(x.status is None for x in r) and "ConnectError" in r[0].note and "front door" in pp.advice(r)[0]


def test_report_never_prints_the_password():
    r = run(OK)
    text = pp.report("https://pod.example.com", "svc_user", "S3cret-Value", r)
    assert "S3cret-Value" not in text and "svc_user" in text and "(12 characters)" in text


def test_snippet_strips_markup():
    assert pp.snippet("<html><style>x{}</style><body>Hello   <b>world</b></body></html>") == "Hello world"

"""The ADF REST client — the single point of contact with Oracle Fusion HCM,
and the inescapable safety floor.

Every semantic operation runs through ``_guarded``, which redacts the result
(unless ``redact=False``) and audits the call — so even a caller that bypasses
the tools and uses the client directly cannot leak PII or evade audit. Reads
redact by default; writes are flagged ``write=True`` and still audited even when
Oracle rejects them.

Request mechanics: inject auth headers, refresh+retry once on 401, back off on
429/503 honoring ``Retry-After``, normalize errors to :class:`HcmApiError`
(``status == 0`` = transport failure), and strip HATEOAS ``links`` (a token sink)
unless asked to keep them.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import httpx

from ..auth.base import AuthProvider
from .errors import HcmApiError, HcmWriteBlockedError

_DEFAULT_RETRIES = 3
_MAX_BACKOFF_SECONDS = 30.0

# The only client mode value that permits mutation — mirrors the FSCM
# OracleClient semantics (``MCP_MODE == "full"``). Anything else fails closed.
WRITE_ENABLED_MODES = frozenset({"full"})

# ADF content types for write operations.
_CT_ITEM = "application/vnd.oracle.adf.resourceitem+json"
_CT_ACTION = "application/vnd.oracle.adf.action+json"

# Generic CRUD verbs excluded when surfacing business actions.
_JSON = "application/json"


def _strip_links(obj: Any) -> Any:
    """Recursively drop Oracle HATEOAS ``links`` keys."""
    if isinstance(obj, dict):
        return {k: _strip_links(v) for k, v in obj.items() if k != "links"}
    if isinstance(obj, list):
        return [_strip_links(v) for v in obj]
    return obj


class HcmClient:
    def __init__(
        self,
        base_url: str,
        rest_version: str,
        auth: AuthProvider,
        *,
        default_limit: int = 25,
        max_limit: int = 500,
        timeout: float = 60.0,
        include_links: bool = False,
        redactor: Any | None = None,
        audit: Any | None = None,
        transport: httpx.BaseTransport | None = None,
        mode: str = "readonly",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._version = rest_version
        self._auth = auth
        self._mode = mode
        self._default_limit = default_limit
        self._max_limit = max_limit
        self._include_links = include_links
        self._redactor = redactor
        self._audit = audit
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- URL / header construction ---------------------------------------

    def _url(self, resource: str, key: str | None = None, suffix: str | None = None) -> str:
        url = f"{self._base}/hcmRestApi/resources/{self._version}/{resource}"
        if key is not None:
            url += f"/{key}"
        if suffix is not None:
            url += f"/{suffix}"
        return url

    @staticmethod
    def _effective_date_header(day: str) -> dict[str, str]:
        # Pod-specific syntax; isolated here so it is easy to correct.
        return {"Effective-Of": f"RangeStartDate={day};RangeEndDate={day}"}

    # ---- the safety floor -------------------------------------------------

    def _require_write_permission(self, http_method: str, resource: str | None = None,
                                  key: str | None = None) -> None:
        """Centralized mutation guard. Every mutating method calls this first.

        READ paths (``query``/``get_record``/``get_href``/``describe``/
        ``describe_catalog``/``atom_feed``) never call it and are always
        allowed. Any mode other than exactly ``"full"`` raises
        :class:`HcmWriteBlockedError` before any network traffic.
        """
        mode = getattr(self, "_mode", None)
        if mode not in WRITE_ENABLED_MODES:
            target = resource or ""
            if key is not None:
                target = "%s/%s" % (target, key)
            raise HcmWriteBlockedError(
                "Fusion HCM write operation blocked: MCP is running in readonly mode "
                "(HTTP %s to %s blocked; mode=%r)."
                % (http_method, target, mode)
            )

    def _audit_op(
        self, op: str, resource: str | None, key: str | None, write: bool, status: str
    ) -> None:
        if self._audit is None:
            return
        sensitive = bool(self._redactor is not None and not self._redactor.enabled)
        self._audit.record(
            tool=f"client:{op}",
            resource=resource,
            key=key,
            write=write,
            sensitive=sensitive,
            status=status,
        )

    async def _guarded(
        self,
        make_coro: Callable[[], Awaitable[Any]],
        *,
        op: str,
        resource: str | None = None,
        key: str | None = None,
        write: bool = False,
        redact: bool = True,
    ) -> Any:
        try:
            result = await make_coro()
        except HcmApiError as exc:
            # Blocked/failed operations (including rejected writes) ARE logged.
            self._audit_op(op, resource, key, write, status=f"error:{exc.status}")
            raise
        if redact and self._redactor is not None:
            result = self._redactor.redact(result)
        self._audit_op(op, resource, key, write, status="ok")
        return result

    # ---- core request with retry semantics --------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        retries: int = _DEFAULT_RETRIES,
        timeout: float | None = None,
        parse_json: bool = True,
        _retry_auth: bool = True,
        _attempt: int = 0,
    ) -> Any:
        req_headers = await self._auth.headers()
        req_headers.setdefault("Content-Type", _JSON)
        if headers:
            req_headers.update(headers)

        kwargs: dict[str, Any] = {"params": params, "json": json, "headers": req_headers}
        if timeout is not None:
            kwargs["timeout"] = timeout

        try:
            resp = await self._client.request(method, url, **kwargs)
        except httpx.RequestError as exc:
            raise HcmApiError(status=0, title="network_error", detail=str(exc)) from exc

        if resp.status_code == 401 and _retry_auth:
            await self._auth.refresh()
            return await self._request(
                method, url, params=params, json=json, headers=headers, retries=retries,
                timeout=timeout, parse_json=parse_json, _retry_auth=False, _attempt=_attempt,
            )

        if resp.status_code in (429, 503) and _attempt < retries:
            await asyncio.sleep(self._retry_after(resp, _attempt))
            return await self._request(
                method, url, params=params, json=json, headers=headers, retries=retries,
                timeout=timeout, parse_json=parse_json, _retry_auth=_retry_auth,
                _attempt=_attempt + 1,
            )

        if resp.status_code >= 400:
            raise self._to_error(resp)

        if not parse_json:
            return resp.text
        if not resp.content:
            return {}
        return resp.json()

    @staticmethod
    def _retry_after(resp: httpx.Response, attempt: int) -> float:
        header = resp.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        return min(2.0**attempt, _MAX_BACKOFF_SECONDS)

    @staticmethod
    def _to_error(resp: httpx.Response) -> HcmApiError:
        title = detail = errorpath = None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            title = body.get("title")
            detail = body.get("detail")
            errorpath = body.get("o:errorPath") or body.get("errorPath")
        if detail is None and not isinstance(body, dict):
            detail = (resp.text or "").strip()[:500] or None
        return HcmApiError(resp.status_code, title=title, detail=detail, errorpath=errorpath)

    # ---- read operations (guarded) ---------------------------------------

    async def query(
        self,
        resource: str,
        *,
        q: str | None = None,
        fields: list[str] | None = None,
        expand: list[str] | None = None,
        order_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        total_results: bool = False,
        only_data: bool = True,
        effective_date: str | None = None,
        keep_links: bool = False,
        retries: int | None = None,
        timeout: float | None = None,
        redact: bool = True,
    ) -> dict[str, Any]:
        async def _op() -> dict[str, Any]:
            effective_limit = min(limit or self._default_limit, self._max_limit)
            params: dict[str, Any] = {"limit": effective_limit, "offset": offset}
            # onlyData strips links server-side, so it must be suppressed when keeping links.
            if only_data and not keep_links:
                params["onlyData"] = "true"
            if q:
                params["q"] = q
            if fields:
                params["fields"] = ",".join(fields)
            if expand:
                params["expand"] = ",".join(expand)
            if order_by:
                params["orderBy"] = order_by
            if total_results:
                params["totalResults"] = "true"
            headers = self._effective_date_header(effective_date) if effective_date else None
            data = await self._request(
                "GET", self._url(resource), params=params, headers=headers,
                retries=_DEFAULT_RETRIES if retries is None else retries, timeout=timeout,
            )
            items = data.get("items", [])
            if not (self._include_links or keep_links):
                items = [_strip_links(item) for item in items]
            return {
                "items": items,
                "count": data.get("count", len(items)),
                "has_more": data.get("hasMore", False),
                "total": data.get("totalResults"),
            }

        return await self._guarded(_op, op="query", resource=resource, redact=redact)

    async def get_record(
        self,
        resource: str,
        key: str,
        *,
        fields: list[str] | None = None,
        expand: list[str] | None = None,
        only_data: bool = True,
        redact: bool = True,
    ) -> dict[str, Any]:
        async def _op() -> dict[str, Any]:
            params: dict[str, Any] = {}
            if only_data:
                params["onlyData"] = "true"
            if fields:
                params["fields"] = ",".join(fields)
            if expand:
                params["expand"] = ",".join(expand)
            data = await self._request("GET", self._url(resource, key), params=params)
            return data if self._include_links else _strip_links(data)

        return await self._guarded(
            _op, op="get_record", resource=resource, key=key, redact=redact
        )

    async def get_href(
        self,
        href: str,
        *,
        only_data: bool = True,
        effective_date: str | None = None,
        retries: int | None = None,
        redact: bool = True,
    ) -> dict[str, Any]:
        """GET a HATEOAS href for child navigation. Rejects hrefs outside base_url."""

        async def _op() -> dict[str, Any]:
            if not href.startswith(self._base):
                raise HcmApiError(
                    status=0,
                    title="ssrf_blocked",
                    detail="Refusing to fetch a href outside the configured pod base URL.",
                )
            params: dict[str, Any] = {}
            if only_data:
                params["onlyData"] = "true"
            headers = self._effective_date_header(effective_date) if effective_date else None
            data = await self._request(
                "GET", href, params=params, headers=headers,
                retries=_DEFAULT_RETRIES if retries is None else retries,
            )
            return data if self._include_links else _strip_links(data)

        return await self._guarded(_op, op="get_href", resource=href, redact=redact)

    async def describe(self, resource: str) -> dict[str, Any]:
        async def _op() -> dict[str, Any]:
            return await self._request("GET", self._url(resource, suffix="describe"))

        return await self._guarded(_op, op="describe", resource=resource, redact=False)

    async def describe_catalog(self) -> dict[str, Any]:
        """GET the version-root describe (~38MB). Never redacted; long timeout."""

        async def _op() -> dict[str, Any]:
            url = f"{self._base}/hcmRestApi/resources/{self._version}/describe"
            return await self._request("GET", url, timeout=180.0)

        return await self._guarded(_op, op="describe_catalog", redact=False)

    # ---- write operations (guarded, write=True) --------------------------

    async def create(self, resource: str, payload: dict[str, Any], *, redact: bool = True) -> Any:
        self._require_write_permission("POST", resource)

        async def _op() -> Any:
            return await self._request("POST", self._url(resource), json=payload)

        return await self._guarded(
            _op, op="create", resource=resource, write=True, redact=redact
        )

    async def update(
        self, resource: str, key: str, payload: dict[str, Any], *, redact: bool = True,
        retries: int | None = None, timeout: float | None = None,
    ) -> Any:
        """PATCH one record. Pass ``retries=0`` for mutations that must not
        be retried (write retries can duplicate non-idempotent effects)."""
        self._require_write_permission("PATCH", resource, key)

        async def _op() -> Any:
            return await self._request(
                "PATCH", self._url(resource, key), json=payload,
                headers={"Content-Type": _CT_ITEM},
                retries=_DEFAULT_RETRIES if retries is None else retries,
                timeout=timeout,
            )

        return await self._guarded(
            _op, op="update", resource=resource, key=key, write=True, redact=redact
        )

    async def delete(self, resource: str, key: str, *, redact: bool = True) -> Any:
        self._require_write_permission("DELETE", resource, key)

        async def _op() -> Any:
            return await self._request("DELETE", self._url(resource, key))

        return await self._guarded(
            _op, op="delete", resource=resource, key=key, write=True, redact=redact
        )

    async def invoke_action(
        self,
        resource: str,
        key: str | None,
        action: str,
        params: dict[str, Any] | list[dict[str, Any]] | None = None,
        *,
        redact: bool = True,
    ) -> Any:
        """Invoke a custom ADF action.

        ``parameters`` must be a LIST of single key-value objects; a combined
        dict is split automatically (a combined object yields Oracle 400).
        """
        self._require_write_permission("POST", resource, key)
        if isinstance(params, dict):
            parameters = [{k: v} for k, v in params.items()]
        else:
            parameters = params or []
        body = {"name": action, "parameters": parameters}

        async def _op() -> Any:
            return await self._request(
                "POST", self._url(resource, key), json=body, headers={"Content-Type": _CT_ACTION}
            )

        return await self._guarded(
            _op, op="invoke_action", resource=resource, key=key, write=True, redact=redact
        )

    # ---- ATOM feeds (guarded, XML) ---------------------------------------

    async def atom_feed(
        self,
        workspace: str,
        collection: str,
        *,
        updated_min: str | None = None,
        page_size: int | None = None,
    ) -> str:
        async def _op() -> str:
            url = f"{self._base}/hcmRestApi/atomservlet/{workspace}/{collection}"
            params: dict[str, Any] = {}
            if updated_min:
                params["updated-min"] = updated_min
            if page_size:
                params["max-results"] = page_size
            return await self._request("GET", url, params=params, parse_json=False)

        return await self._guarded(
            _op, op="atom_feed", resource=f"{workspace}/{collection}", redact=False
        )

    # ---- capability probe -------------------------------------------------

    async def classify_module(self, probe_resource: str) -> str:
        """Single-shot probe for capability discovery.

        Returns ``provisioned`` / ``not_provisioned`` (404) / ``no_access``
        (401/403) / ``unreachable`` (network or 5xx). No retries; short timeout
        because some resources hang and probes fan out concurrently.
        """
        try:
            await self.query(probe_resource, limit=1, retries=0, timeout=15.0, redact=False)
        except HcmApiError as exc:
            if exc.status == 404:
                return "not_provisioned"
            if exc.status in (401, 403):
                return "no_access"
            return "unreachable"
        return "provisioned"

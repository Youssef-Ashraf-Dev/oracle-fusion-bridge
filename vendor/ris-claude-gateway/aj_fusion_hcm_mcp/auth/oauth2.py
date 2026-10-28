"""OAuth2 client-credentials auth provider (OCI IAM / IDCS).

Fetches a bearer token from the token endpoint and caches it until shortly
before expiry. ``refresh()`` forces a new token (called on a 401). The exact
token endpoint shape varies between OCI IAM and IDCS — validate against the
target pod during the live-pod phase (DESIGN.md §3, §10).
"""

from __future__ import annotations

import time

import httpx

from ..core.errors import ConfigError, HcmApiError

# Refresh this many seconds before the token actually expires, to avoid races.
_EXPIRY_SKEW_SECONDS = 60.0


class OAuth2JwtProvider:
    def __init__(
        self,
        token_url: str | None,
        client_id: str | None,
        client_secret: str | None,
        scope: str | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        if not token_url or not client_id or not client_secret:
            raise ConfigError(
                "OAuth2 auth requires auth.token_url, auth.client_id and auth.client_secret."
            )
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._timeout = timeout
        self._token: str | None = None
        self._expires_at = 0.0

    def _is_valid(self) -> bool:
        return self._token is not None and time.monotonic() < self._expires_at

    async def headers(self) -> dict[str, str]:
        if not self._is_valid():
            await self.refresh()
        return {"Authorization": f"Bearer {self._token}"}

    async def refresh(self) -> None:
        form = {"grant_type": "client_credentials"}
        if self._scope:
            form["scope"] = self._scope
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    self._token_url,
                    data=form,
                    auth=(self._client_id, self._client_secret),
                    headers={"Accept": "application/json"},
                )
        except httpx.RequestError as exc:
            raise HcmApiError(status=0, title="token_request_failed", detail=str(exc)) from exc

        if resp.status_code >= 400:
            raise HcmApiError(
                status=resp.status_code,
                title="token_request_rejected",
                detail=resp.text[:500] or None,
            )

        payload = resp.json()
        self._token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 3600))
        self._expires_at = time.monotonic() + max(expires_in - _EXPIRY_SKEW_SECONDS, 0.0)

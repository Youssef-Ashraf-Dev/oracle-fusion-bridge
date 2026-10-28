"""Auth provider protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AuthProvider(Protocol):
    """Supplies Authorization headers and refreshes credentials on demand.

    ``headers()`` is awaited before every request; ``refresh()`` is called by
    the REST client after a 401 to force re-authentication, then the request
    is retried once.
    """

    async def headers(self) -> dict[str, str]: ...

    async def refresh(self) -> None: ...

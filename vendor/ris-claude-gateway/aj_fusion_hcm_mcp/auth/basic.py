"""HTTP Basic auth provider with OS-credential-store fallback.

Password precedence: explicit config/env value, else the OS credential store
(via ``keyring``) under service ``fusion-hcm-mcp-server``. This lets Claude Desktop
run without a plaintext password on disk. For development; use OAuth2 in prod.
"""

from __future__ import annotations

import base64

from ..core.errors import ConfigError

KEYRING_SERVICE = "fusion-hcm-mcp-server"


def keyring_password(username: str | None) -> str | None:
    """Look up a password in the OS credential store. Never raises.

    Returns None if ``keyring`` is not installed, no entry exists, or any
    backend error occurs — the caller decides whether a missing password is fatal.
    """
    if not username:
        return None
    try:
        import keyring
    except Exception:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:
        return None


class BasicAuthProvider:
    def __init__(self, username: str | None, password: str | None) -> None:
        if not username:
            raise ConfigError("Basic auth requires auth.username (config or HCM_USERNAME).")
        if not password:
            raise ConfigError(
                "Basic auth password not found. Provide auth.password / HCM_PASSWORD, "
                f"or store it in the OS credential store under service '{KEYRING_SERVICE}' "
                f"for user '{username}' (e.g. `keyring set {KEYRING_SERVICE} {username}`)."
            )
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._header = f"Basic {token}"

    async def headers(self) -> dict[str, str]:
        return {"Authorization": self._header}

    async def refresh(self) -> None:
        # Static credentials — nothing to refresh. A 401 is a real auth failure.
        return None

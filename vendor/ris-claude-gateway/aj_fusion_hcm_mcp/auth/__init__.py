"""Auth provider selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import AuthProvider
from .basic import BasicAuthProvider, keyring_password
from .oauth2 import OAuth2JwtProvider

if TYPE_CHECKING:
    from ..config import AuthConfig

__all__ = [
    "AuthProvider",
    "BasicAuthProvider",
    "OAuth2JwtProvider",
    "keyring_password",
    "build_auth",
]


def build_auth(cfg: AuthConfig) -> AuthProvider:
    if cfg.type == "basic":
        password = cfg.password or keyring_password(cfg.username)
        return BasicAuthProvider(cfg.username, password)
    if cfg.type == "oauth2":
        return OAuth2JwtProvider(cfg.token_url, cfg.client_id, cfg.client_secret, cfg.scope)
    raise ValueError(f"Unknown auth type: {cfg.type!r}")

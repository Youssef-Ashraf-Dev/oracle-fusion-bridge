"""Configuration loading for the Fusion HCM MCP server.

Configuration comes from a TOML file (path via ``CONFIG_FILE``, default
``config.toml``) with **secrets overlaid from environment variables**. Env
always wins, so credentials never have to live in the file. This keeps the
same distributed artifact reusable across customers — only config differs.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .core.errors import ConfigError

ModuleMode = Literal["on", "off", "auto"]


class ServerConfig(BaseModel):
    base_url: str
    rest_version: str = "11.13.18.05"


class AuthConfig(BaseModel):
    type: Literal["basic", "oauth2"] = "basic"
    # basic
    username: str | None = None
    password: str | None = None
    # oauth2 (client credentials)
    token_url: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scope: str | None = None


class FeatureConfig(BaseModel):
    writes_enabled: bool = False
    sensitive_fields_enabled: bool = False
    atom_enabled: bool = False
    bip_enabled: bool = False


class ModulesConfig(BaseModel):
    """Module-mirrored tool groups (DESIGN.md §12.4).

    ``on`` always enabled, ``off`` always hidden, ``auto`` resolved by
    capability discovery against the live pod.
    """

    core_hr: ModuleMode = "on"
    compensation: ModuleMode = "auto"
    absence: ModuleMode = "auto"
    payroll: ModuleMode = "auto"
    recruiting: ModuleMode = "auto"
    talent: ModuleMode = "auto"
    learning: ModuleMode = "auto"
    benefits: ModuleMode = "auto"
    time_labor: ModuleMode = "auto"


class LimitsConfig(BaseModel):
    default_limit: int = 25
    max_limit: int = 500


class TransportConfig(BaseModel):
    type: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000


class AuditConfig(BaseModel):
    enabled: bool = True
    path: str = "audit/audit.jsonl"


class Config(BaseModel):
    server: ServerConfig
    auth: AuthConfig = Field(default_factory=AuthConfig)
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    modules: ModulesConfig = Field(default_factory=ModulesConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    transport: TransportConfig = Field(default_factory=TransportConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)


# Maps environment variable -> (section, key). Env overrides file values.
_ENV_MAP: dict[str, tuple[str, str]] = {
    "HCM_BASE_URL": ("server", "base_url"),
    "HCM_REST_VERSION": ("server", "rest_version"),
    "HCM_AUTH_TYPE": ("auth", "type"),
    "HCM_USERNAME": ("auth", "username"),
    "HCM_PASSWORD": ("auth", "password"),
    "HCM_TOKEN_URL": ("auth", "token_url"),
    "HCM_CLIENT_ID": ("auth", "client_id"),
    "HCM_CLIENT_SECRET": ("auth", "client_secret"),
    "HCM_SCOPE": ("auth", "scope"),
    "HCM_TRANSPORT": ("transport", "type"),
    "HCM_HOST": ("transport", "host"),
    "HCM_PORT": ("transport", "port"),
    "HCM_AUDIT_PATH": ("audit", "path"),
}


def _apply_env_overrides(data: dict[str, Any]) -> None:
    for env_var, (section, key) in _ENV_MAP.items():
        value = os.environ.get(env_var)
        if value is None:
            continue
        data.setdefault(section, {})[key] = value


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load and validate configuration from TOML + environment overrides."""
    config_path = Path(path or os.environ.get("CONFIG_FILE", "config.toml"))

    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            with config_path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"Failed to read config file {config_path}: {exc}") from exc

    _apply_env_overrides(data)

    if "server" not in data or not data["server"].get("base_url"):
        raise ConfigError(
            "Missing required setting: server.base_url "
            "(set it in config.toml or via the HCM_BASE_URL env var)."
        )

    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration: {exc}") from exc

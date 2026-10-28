"""Core REST engine: client, errors, and (later) catalog + filters."""

from .client import HcmClient
from .errors import ConfigError, FilterError, FusionMcpError, HcmApiError

__all__ = ["HcmClient", "ConfigError", "FilterError", "FusionMcpError", "HcmApiError"]

"""Shared server context passed to tool modules."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .core.catalog import Catalog
from .core.client import HcmClient
from .safety import AuditLog, Redactor


@dataclass
class ServerContext:
    config: Config
    client: HcmClient
    catalog: Catalog
    redactor: Redactor
    audit: AuditLog

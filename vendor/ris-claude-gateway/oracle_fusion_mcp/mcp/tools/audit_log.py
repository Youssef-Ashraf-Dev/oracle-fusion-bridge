"""Audit event model and sinks for controlled Fusion actions (pilot).

Design rules:
- Events carry business-operation facts only: who/what was requested and
  what happened. Never credentials, tokens, headers, cookies, clients, or
  unvalidated payloads. Only validated action parameters are recorded, and
  even those pass a secret-key deny-list as defense in depth.
- Storage is swappable: tests use :class:`InMemoryAuditSink`, production
  wiring defaults to stdlib logging, and an append-only JSONL file sink is
  available with an explicit path. No database, no new dependencies.
- Sinks never perform Fusion I/O. A failing sink must surface as an error
  to the caller; it must never trigger, retry, or substitute a write.
"""

import hashlib
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fusion.audit")

# Parameter keys that must never reach an audit record, even if validation
# ever widens beyond the current boolean-only submit flags.
_SECRET_KEY_HINTS = ("password", "passwd", "secret", "token", "authorization",
                     "cookie", "set-cookie", "api_key", "apikey", "private")

# Environment variables whose VALUES must never appear in audit output,
# even embedded inside parameter values or error text. Compared by value
# at emit time so rotation is honored without restarts.
_SECRET_VALUE_ENV_VARS = ("ORACLE_PASSWORD", "ORACLE_JWT_PRIVATE_KEY",
                          "AZURE_AD_CLIENT_SECRET", "ENTRA_SECRET")

# Minimum secret length scrubbed: avoids mangling innocent text when a
# variable holds a short/placeholder value.
_MIN_SECRET_LEN = 8

_REDACTED = "[REDACTED]"


def scrub_secret_values(text: Any) -> Any:
    """Replace any configured secret value occurring in text (never raises)."""
    if not isinstance(text, str) or not text:
        return text
    redacted = text
    for var in _SECRET_VALUE_ENV_VARS:
        try:
            secret = os.environ.get(var)
        except Exception:
            continue
        if secret and len(secret) >= _MIN_SECRET_LEN \
                and secret in redacted:
            redacted = redacted.replace(secret, _REDACTED)
    return redacted


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_parameters(parameters: Any) -> Dict[str, Any]:
    """Return a JSON-safe copy of validated parameters minus secret-like keys."""
    if not isinstance(parameters, dict):
        return {}
    clean: Dict[str, Any] = {}
    for key, value in parameters.items():
        if not isinstance(key, str):
            continue
        lowered = key.lower()
        if any(hint in lowered for hint in _SECRET_KEY_HINTS):
            continue
        if isinstance(value, str):
            clean[key] = scrub_secret_values(value)
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            clean[key] = scrub_secret_values(repr(value))
        else:
            clean[key] = value
    return clean


def token_fingerprint(token: Any) -> Optional[str]:
    """Non-reversible short fingerprint for audit correlation (never the token)."""
    if not isinstance(token, str) or not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


@dataclass
class AuditEvent:
    timestamp: str = field(default_factory=_utcnow_iso)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    operation: str = ""
    resource: str = ""
    scope: str = ""
    action: str = ""
    record_id: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    mode: str = ""
    result: str = ""
    confirmation: str = ""
    confirmation_fingerprint: Optional[str] = None
    version: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["parameters"] = sanitize_parameters(data.get("parameters"))
        return data

    def to_jsonl(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, default=str)


def build_event(operation: str, *, resource: str = "", scope: str = "",
                action: str = "", record_id: str = "",
                parameters: Any = None, mode: str = "", result: str = "",
                confirmation: str = "", confirmation_token: Any = None,
                version: Any = None, error: Any = None) -> AuditEvent:
    """Construct an event; token is stored as fingerprint only."""
    error_text = None
    if error is not None:
        error_text = scrub_secret_values(str(error))[:500]
    return AuditEvent(
        operation=operation,
        resource=resource,
        scope=scope,
        action=action,
        record_id=str(record_id or ""),
        parameters=sanitize_parameters(parameters),
        mode=mode,
        result=result,
        confirmation=confirmation,
        confirmation_fingerprint=token_fingerprint(confirmation_token),
        version=None if version is None else str(version),
        error=error_text,
    )


class AuditSink:
    """Storage interface. Implementations must not perform Fusion I/O."""

    def emit(self, event: AuditEvent) -> None:
        raise NotImplementedError


class InMemoryAuditSink(AuditSink):
    """Test sink: retains serialized events in process memory."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event.to_dict())


class LoggingAuditSink(AuditSink):
    """Default sink: one structured line via stdlib logging (no files)."""

    def __init__(self, logger_name: str = "fusion.audit") -> None:
        self._logger = logging.getLogger(logger_name)

    def emit(self, event: AuditEvent) -> None:
        self._logger.info("fusion_audit %s", event.to_jsonl())


class JsonlAuditSink(AuditSink):
    """Append-only JSONL file sink. One event per line; file created on demand."""

    def __init__(self, path: Any) -> None:
        self.path = os.fspath(path)

    def emit(self, event: AuditEvent) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(event.to_jsonl() + "\n")


class FailingAuditSink(AuditSink):
    """Test sink simulating a broken audit backend (must fail closed upstream)."""

    def emit(self, event: AuditEvent) -> None:
        raise RuntimeError("audit backend unavailable")

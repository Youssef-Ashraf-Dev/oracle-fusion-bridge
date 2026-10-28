"""Single-use explicit confirmation tokens for the pilot controlled action.

Model:
- A token is minted ONLY from a validated dry-run preview (never standalone).
- The token is an opaque random identifier; all meaning lives server-side in
  the store, bound to the exact preview: resource, scope, action, record_id,
  validated parameters, and preview_id.
- Tokens expire (default 300s), are consumed atomically exactly once, and any
  deviation in record/action/resource/parameters fails closed.
- Process-local store guarded by a threading lock. Sufficient for the pilot
  single-process MCP server; documented limitation: not shared across
  processes or restarts (a restart invalidates all outstanding tokens, which
  is itself fail-closed).
"""

import hashlib
import json
import secrets
import threading
import time
import uuid
from typing import Any, Dict, Optional

DEFAULT_CONFIRMATION_TTL_SECONDS = 300


def _canonical_binding(resource: str, scope: str, action: str,
                       record_id: Any, parameters: Any,
                       preview_id: Any, system: str = "FSCM",
                       version: Any = None, state: Any = None,
                       method: Any = None, endpoint: Any = None,
                       risk: Any = None, eligibility: Any = None,
                       api_version: Any = None,
                       template_source: Any = None,
                       metadata_fingerprint: Any = None,
                       child_path: Any = None,
                       child_record_id: Any = None,
                       primary_key: Any = None,
                       environment: Any = None) -> str:
    """Deterministic fingerprint of the exact confirmed proposal.

    ``system`` (FSCM/HCM), ``version`` (optimistic-concurrency value),
    ``state`` (bound safety-relevant snapshot), ``method``, ``endpoint``,
    ``risk``, ``eligibility``, ``api_version``, ``template_source``
    (metadata-link vs canonical-pattern provenance),
    ``metadata_fingerprint`` (hash of the exact describe metadata the
    operation was resolved from), ``child_path`` and ``child_record_id``
    (nested target identity), and ``primary_key`` (created-record identity
    discovery for creates) extend the binding; callers that omit
    them behave exactly as before. Tokens are process-local, so no
    persisted fingerprints are affected.
    """
    canonical = json.dumps({
        "system": system,
        "resource": resource,
        "scope": scope,
        "action": action,
        "record_id": str(record_id),
        "child_path": child_path,
        "child_record_id": (str(child_record_id)
                            if child_record_id is not None else None),
        "primary_key": primary_key,
        "parameters": parameters if isinstance(parameters, dict) else {},
        "preview_id": preview_id,
        "version": version,
        "state": state if isinstance(state, dict) else {},
        "method": method,
        "endpoint": endpoint,
        "risk": risk,
        "eligibility": eligibility,
        "api_version": api_version,
        "template_source": template_source,
        "metadata_fingerprint": metadata_fingerprint,
        "environment": environment,
    }, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConfirmationError(ValueError):
    """Raised when a confirmation token is missing, invalid, or unusable."""


class ConfirmationStore:
    """Process-local single-use token registry."""

    def __init__(self, ttl_seconds: int = DEFAULT_CONFIRMATION_TTL_SECONDS,
                 time_fn=None) -> None:
        self.ttl_seconds = ttl_seconds
        self._time = time_fn or time.time
        self._lock = threading.Lock()
        self._entries: Dict[str, Dict[str, Any]] = {}

    def issue(self, preview: Dict[str, Any],
              ttl_seconds: Optional[int] = None) -> Dict[str, str]:
        """Mint a token bound to a validated preview. No preview -> no token."""
        if not isinstance(preview, dict):
            raise ConfirmationError("confirmation requires a validated preview")
        for field in ("resource", "scope", "action", "preview_id"):
            if not preview.get(field):
                raise ConfirmationError(
                    "preview missing %r: cannot confirm" % field)
        if preview.get("scope") != "collection" and not preview.get("record_id"):
            raise ConfirmationError(
                "preview missing 'record_id': cannot confirm")
        parameters = preview.get("parameters")
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise ConfirmationError("preview parameters must be an object")
        token = secrets.token_urlsafe(32)
        lifetime = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        binding = _canonical_binding(
            preview["resource"], preview["scope"], preview["action"],
            preview["record_id"], parameters, preview["preview_id"],
            preview.get("system", "FSCM"), preview.get("version"),
            preview.get("bound_state"),
            method=preview.get("method"),
            endpoint=preview.get("path"),
            risk=preview.get("risk"),
            eligibility=preview.get("eligibility"),
            api_version=preview.get("api_version"),
            template_source=preview.get("template_source"),
            metadata_fingerprint=preview.get("metadata_fingerprint"),
            child_path=preview.get("child_path"),
            child_record_id=preview.get("child_record_id"),
            primary_key=preview.get("primary_key"),
            environment=preview.get("environment"))
        with self._lock:
            self._entries[token] = {
                "binding": binding,
                "resource": preview["resource"],
                "action": preview["action"],
                "record_id": str(preview["record_id"]),
                "expires_at": self._time() + lifetime,
                "used": False,
            }
        return {"confirmation_token": token, "preview_id": preview["preview_id"]}

    def consume(self, token: Any, preview: Dict[str, Any]) -> Dict[str, Any]:
        """Atomically validate + single-use consume. Returns the stored entry."""
        if not isinstance(token, str) or not token:
            raise ConfirmationError("missing or malformed confirmation token")
        if not isinstance(preview, dict):
            raise ConfirmationError("consume requires the confirmed preview")
        expected = _canonical_binding(
            preview.get("resource"), preview.get("scope"), preview.get("action"),
            preview.get("record_id"), preview.get("parameters") or {},
            preview.get("preview_id"), preview.get("system", "FSCM"),
            preview.get("version"), preview.get("bound_state"),
            method=preview.get("method"),
            endpoint=preview.get("path"),
            risk=preview.get("risk"),
            eligibility=preview.get("eligibility"),
            api_version=preview.get("api_version"),
            template_source=preview.get("template_source"),
            metadata_fingerprint=preview.get("metadata_fingerprint"),
            child_path=preview.get("child_path"),
            child_record_id=preview.get("child_record_id"),
            primary_key=preview.get("primary_key"),
            environment=preview.get("environment"))
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                raise ConfirmationError("unknown confirmation token")
            if entry["used"]:
                raise ConfirmationError("confirmation already consumed")
            if self._time() > entry["expires_at"]:
                raise ConfirmationError("confirmation expired")
            if entry["binding"] != expected:
                raise ConfirmationError(
                    "confirmation does not match this preview "
                    "(record/action/parameters changed?)")
            entry["used"] = True
            return dict(entry)

    def outstanding(self) -> int:
        """Count of issued, unused, unexpired tokens (observability only)."""
        with self._lock:
            now = self._time()
            return sum(1 for e in self._entries.values()
                       if not e["used"] and now <= e["expires_at"])

    def _batch_binding(self, batch_preview: Dict[str, Any]) -> str:
        """Deterministic fingerprint of a whole ordered operation list."""
        items = batch_preview.get("items")
        preview_id = batch_preview.get("preview_id")
        if not isinstance(items, list) or not items or not preview_id:
            raise ConfirmationError("malformed batch preview")
        item_bindings = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ConfirmationError(
                    "batch item %d is not an object" % index)
            item_bindings.append(_canonical_binding(
                item.get("resource"), item.get("scope"), item.get("action"),
                item.get("record_id"), item.get("parameters") or {},
                "%s#%d" % (preview_id, index),
                item.get("system", "FSCM"), item.get("version"),
                item.get("bound_state"), method=item.get("method"),
                endpoint=item.get("path"), risk=item.get("risk"),
                eligibility=item.get("eligibility"),
                api_version=item.get("api_version"),
                template_source=item.get("template_source"),
                metadata_fingerprint=item.get("metadata_fingerprint"),
                child_path=item.get("child_path"),
                child_record_id=item.get("child_record_id"),
                primary_key=item.get("primary_key"),
                environment=batch_preview.get("environment")))
        return hashlib.sha256(json.dumps({
            "batch_id": preview_id,
            "count": len(item_bindings),
            "items": item_bindings,
        }, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def issue_batch(self, batch_preview: Dict[str, Any],
                    ttl_seconds: Optional[int] = None) -> Dict[str, str]:
        """Mint one token bound to a whole ordered operation list.

        Reordering, adding, removing, or altering any item invalidates
        the token. Single-use and expiring like single tokens.
        """
        if not isinstance(batch_preview, dict):
            raise ConfirmationError("batch confirmation requires a preview")
        binding = self._batch_binding(batch_preview)
        token = secrets.token_urlsafe(32)
        lifetime = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        with self._lock:
            self._entries[token] = {
                "binding": binding,
                "resource": "batch:%d" % len(batch_preview["items"]),
                "action": "batch",
                "record_id": "",
                "expires_at": self._time() + lifetime,
                "used": False,
            }
        return {"confirmation_token": token,
                "preview_id": batch_preview["preview_id"]}

    def consume_batch(self, token: Any,
                      batch_preview: Dict[str, Any]) -> Dict[str, Any]:
        """Atomically validate + single-use consume a batch token."""
        if not isinstance(token, str) or not token:
            raise ConfirmationError("missing or malformed confirmation token")
        expected = self._batch_binding(batch_preview)
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                raise ConfirmationError("unknown confirmation token")
            if entry["used"]:
                raise ConfirmationError("confirmation already consumed")
            if self._time() > entry["expires_at"]:
                raise ConfirmationError("confirmation expired")
            if entry["binding"] != expected:
                raise ConfirmationError(
                    "confirmation does not match this batch "
                    "(items changed?)")
            entry["used"] = True
            return dict(entry)


CONFIRMATION_STORE = ConfirmationStore()


def issue_confirmation(preview: Dict[str, Any]) -> Dict[str, str]:
    """Issue against the process-global store (used by the preview tool)."""
    return CONFIRMATION_STORE.issue(preview)

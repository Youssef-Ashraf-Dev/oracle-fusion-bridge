"""Hot-reloadable curated policy tables (no restart required).

The curated semantic tables (runtime-required fields, caller-input
overrides, omit lists, collection-action verification contracts) live as
compiled-in constants in their modules — those stay as defaults and keep
every existing import working. This module adds a JSON file OVERLAY on
top: entries listed here replace the compiled default for their exact
key. The file is re-read whenever its mtime changes, so operators can
promote a field_report suggestion without restarting the server. A
corrupt file never takes effect (last-good content is kept and the
failure is logged).

Overlay file format (JSON)::
    {"policy_schema": 1,
     "overrides": {
       "FSCM_RUNTIME_REQUIRED_CREATE_FIELDS": {
         "FSCM|draftPurchaseOrders|create": ["RequisitioningBU"]
       },
       "FSCM_COLLECTION_ACTION_VERIFY": {
         "invoices|cancelInvoice": {
           "id_params": ["InvoiceNumber"],
           "query_field": "InvoiceNumber",
           "observe": ["CanceledFlag"]}}}}

Only the six tables in ``POLICY_TABLES`` may be overridden; anything
else in the file is ignored. Risk sets, allowlists, and structural
constants stay in code by design (they change through review, not ops).
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("fusion.policy")

POLICY_SCHEMA_VERSION = 1

POLICY_TABLES = (
    "FSCM_RUNTIME_REQUIRED_CREATE_FIELDS",
    "FSCM_CREATE_CALLER_INPUT_OVERRIDES",
    "FSCM_CREATE_OMIT_FIELDS",
    "HCM_CREATE_CALLER_INPUT_OVERRIDES",
    "HCM_SERVER_OWNED_CREATE_FIELDS",
    "FSCM_COLLECTION_ACTION_VERIFY",
)

_policy_path: str = ""
_policy_mtime: Any = None
_policy_overlays: Dict[str, Dict[tuple, Any]] = {}
_test_overrides: Dict[Tuple[str, tuple], Any] = {}


def _tuple_key(key: Any) -> tuple:
    if isinstance(key, (list, tuple)):
        return tuple(key)
    return (key,)


def configure(path: Any) -> None:
    """Point the store at an overlay file (empty disables). Server startup."""
    global _policy_path, _policy_mtime, _policy_overlays
    _policy_path = os.fspath(path) if path else ""
    _policy_mtime = None
    _policy_overlays = {}


def set_test_entry(table: str, key: Any, value: Any) -> None:
    """Pin one entry for tests. Cleared by ``clear_test_entries``."""
    _test_overrides[(table, _tuple_key(key))] = value


def clear_test_entries() -> None:
    """Remove all test pins (test teardown)."""
    _test_overrides.clear()


def _reload_if_changed() -> None:
    """Re-read the overlay file when its mtime moved (never raises)."""
    global _policy_mtime, _policy_overlays
    if not _policy_path:
        return
    try:
        mtime = os.path.getmtime(_policy_path)
    except OSError:
        return
    if mtime == _policy_mtime:
        return
    try:
        with open(_policy_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        logger.warning("policy overlay unreadable, keeping last-good: %s",
                       exc)
        _policy_mtime = mtime
        return
    try:
        overlays = _parse_overlay(data)
    except Exception as exc:
        logger.warning("policy overlay invalid, keeping last-good: %s", exc)
        _policy_mtime = mtime
        return
    _policy_overlays = overlays
    _policy_mtime = mtime


def _parse_overlay(data: Any) -> Dict[str, Dict[tuple, Any]]:
    if not isinstance(data, dict):
        raise ValueError("overlay must be a JSON object")
    if data.get("policy_schema") != POLICY_SCHEMA_VERSION:
        raise ValueError("unsupported policy_schema (want %d)"
                         % POLICY_SCHEMA_VERSION)
    raw = data.get("overrides", {})
    if not isinstance(raw, dict):
        raise ValueError("overrides must be an object")
    overlays: Dict[str, Dict[tuple, Any]] = {}
    for table, entries in raw.items():
        if table not in POLICY_TABLES:
            continue
        if not isinstance(entries, dict):
            raise ValueError("table %r entries must be an object" % table)
        parsed: Dict[tuple, Any] = {}
        for key, value in entries.items():
            if not isinstance(key, str) or not key:
                raise ValueError("table %r has an invalid key" % table)
            parsed[tuple(key.split("|"))] = value
        overlays[table] = parsed
    return overlays


def get_table_entry(table: str, key: Any, default: Any = None) -> Any:
    """Return the effective entry: test pin > file overlay > default."""
    tkey = _tuple_key(key)
    if (table, tkey) in _test_overrides:
        return _test_overrides[(table, tkey)]
    _reload_if_changed()
    table_overlays = _policy_overlays.get(table)
    if table_overlays is not None and tkey in table_overlays:
        return table_overlays[tkey]
    return default


def parse_value_limits(raw: Any) -> Dict[str, float]:
    """Parse {"Resource.Field": max} caps. Raises ValueError when invalid."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ValueError("value limits must be a JSON object")
    limits: Dict[str, float] = {}
    for key, value in data.items():
        if not isinstance(key, str) or "." not in key:
            raise ValueError("limit key must look like 'Resource.Field'")
        number = float(value)
        if number < 0:
            raise ValueError("limit must be non-negative")
        limits[key] = number
    return limits


def validate_pod_allowlist(base_url: Any, allowlist: Any) -> Optional[str]:
    """Return an error string when base_url violates the allowlist (else None)."""
    if not allowlist or not str(allowlist).strip():
        return None
    wanted = [s.strip().lower() for s in str(allowlist).split(",") if s.strip()]
    if not wanted:
        return None
    actual = str(base_url or "").lower()
    if not any(w in actual for w in wanted):
        return ("pod %r matches none of the allowed base-URL substrings; "
                "refusing to serve a misconfigured deployment" % (base_url,))
    return None

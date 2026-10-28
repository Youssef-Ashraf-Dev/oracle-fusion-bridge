"""Append-only audit log.

Every tool call is recorded as one JSONL line: timestamp, tool, resource, key,
fields returned, row count, and whether the call was a write or exposed
sensitive fields. Per-deployment and never shared across customers (DESIGN.md
§7, §0). Audit failures never break a tool call.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: str, enabled: bool = True) -> None:
        self.enabled = enabled
        self.path = Path(path)
        if self.enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # pragma: no cover - filesystem edge case
                print(f"[audit] cannot create log dir: {exc}", file=sys.stderr)
                self.enabled = False

    def record(
        self,
        *,
        tool: str,
        resource: str | None = None,
        key: str | None = None,
        fields: list[str] | None = None,
        count: int | None = None,
        write: bool = False,
        sensitive: bool = False,
        status: str = "ok",
    ) -> None:
        if not self.enabled:
            return
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "resource": resource,
            "key": key,
            "fields": fields,
            "count": count,
            "write": write,
            "sensitive": sensitive,
            "status": status,
        }
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError as exc:  # pragma: no cover - filesystem edge case
            print(f"[audit] write failed: {exc}", file=sys.stderr)

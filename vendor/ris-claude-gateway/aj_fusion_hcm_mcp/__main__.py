"""Console entry point: ``fusion-hcm-mcp-server``."""

from __future__ import annotations

import sys

# Use the OS trust store for TLS (corporate TLS-inspection CAs, common on
# Windows work machines). Guarded so the Linux container silently falls back to
# certifi. Must run BEFORE any module that opens TLS connections.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

from .core.errors import ConfigError  # noqa: E402
from .server import run  # noqa: E402


def main() -> int:
    try:
        run()
    except ConfigError as exc:
        # Never write to stdout on the stdio transport — it corrupts JSON-RPC.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

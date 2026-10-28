"""Exception hierarchy for the Fusion HCM MCP server."""

from __future__ import annotations


class FusionMcpError(Exception):
    """Base class for all errors raised by this server."""


class ConfigError(FusionMcpError):
    """Raised when configuration is missing or invalid."""


class FilterError(FusionMcpError):
    """Raised when a ``q=`` filter references unknown attributes or bad operators.

    Caught locally against the cached schema *before* any network call, so the
    model gets an actionable message instead of an opaque Oracle 400.
    """


class HcmWriteBlockedError(FusionMcpError, PermissionError):
    """Raised when a mutating HCM call is attempted outside write mode.

    Carries only the HTTP method and the relative resource/key — never
    credentials, tokens, headers, or request bodies. Raised before any
    network traffic is attempted (fail closed).
    """


class HcmApiError(FusionMcpError):
    """A normalized Oracle Fusion HCM REST error.

    Oracle returns errors in a few shapes; we flatten them into a stable
    structure so tools and the model can reason about them consistently.
    ``status == 0`` denotes a transport/network failure (no HTTP response).
    """

    def __init__(
        self,
        status: int,
        title: str | None = None,
        detail: str | None = None,
        errorpath: str | None = None,
    ) -> None:
        self.status = status
        self.title = title
        self.detail = detail
        self.errorpath = errorpath
        message = f"[{status}] {title or ''}: {detail or ''}".strip(" :")
        super().__init__(message or f"HCM API error {status}")

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "title": self.title,
            "detail": self.detail,
            "errorpath": self.errorpath,
        }

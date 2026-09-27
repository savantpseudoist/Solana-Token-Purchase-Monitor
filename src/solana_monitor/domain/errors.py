"""Error hierarchy for the monitor.

Every error raised deliberately by this application derives from
:class:`MonitorError`.  Errors carry structured ``context`` so log records stay
useful without echoing secrets, and carry machine-readable flags (``retryable``,
``status``) so retry decisions never depend on string matching.
"""

from __future__ import annotations

from collections.abc import Mapping


class MonitorError(Exception):
    """Base class for all errors raised deliberately by this application."""

    def __init__(self, message: str, *, context: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, object] = dict(context or {})

    def __str__(self) -> str:
        if not self.context:
            return self.message
        details = ", ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.message} ({details})"


class ConfigurationError(MonitorError):
    """Runtime configuration is missing, invalid, or contradicts itself."""


class StateError(MonitorError):
    """Persisted state could not be read or written."""


class AuthorizationError(MonitorError):
    """The caller is not allowed to perform the requested action."""


class InvalidAddressError(MonitorError):
    """A value was expected to be a well-formed Solana address but was not."""

    def __init__(self, value: str, *, reason: str) -> None:
        super().__init__(
            f"Invalid Solana address {value!r}: {reason}",
            context={"reason": reason, "length": len(value)},
        )
        self.value = value
        self.reason = reason


class ExternalServiceError(MonitorError):
    """Base class for failures originating from a third-party service."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class HeliusError(ExternalServiceError):
    """A Helius API call failed in a way we did not classify more precisely."""


class HeliusTransportError(HeliusError):
    """The connection to Helius failed before a response was received."""

    def __init__(self, message: str, *, context: Mapping[str, object] | None = None) -> None:
        super().__init__(message, retryable=True, context=context)


class HeliusAuthError(HeliusError):
    """Helius rejected our credentials; retrying cannot help."""

    def __init__(self, status: int, *, context: Mapping[str, object] | None = None) -> None:
        super().__init__(
            f"Helius rejected the API key (HTTP {status}). "
            "Check HELIUS_API_KEY and HELIUS_API_BASE_URL.",
            status=status,
            retryable=False,
            context=context,
        )


class HeliusRateLimitError(HeliusError):
    """Helius rate limited the request; retry after ``retry_after`` seconds."""

    def __init__(
        self,
        *,
        retry_after: float | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(
            "Helius rate limit reached (HTTP 429).",
            status=429,
            retryable=True,
            retry_after=retry_after,
            context=context,
        )


class HeliusResponseError(HeliusError):
    """Helius returned a response we could not interpret."""


class NotificationError(ExternalServiceError):
    """A user-facing notification could not be delivered."""

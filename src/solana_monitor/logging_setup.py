"""Structured logging with mandatory secret redaction.

Two formatters are provided, both wrapped in a redactor that removes anything
that looks like a configured secret, an ``api-key=`` query parameter, or a
Telegram bot token.  Redaction is deliberately applied to the *formatted*
record, so it also covers tracebacks and third-party log lines.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Final

from solana_monitor.config import Settings

REDACTED: Final = "***"

_PATTERNS: Final = (
    re.compile(r"(?i)\b(api[-_]?key=)([^&\s\"']+)"),
    re.compile(r"(?i)\b(bot)(\d{4,}:[A-Za-z0-9_-]{20,})"),
)

_TEXT_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT: Final = "%Y-%m-%dT%H:%M:%S%z"
_NOISY_LOGGERS: Final = ("httpx", "httpcore", "aiohttp.client", "telegram.ext.ExtBot")


class Redactor:
    """Replaces configured secrets and secret-shaped strings with ``***``."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            text = pattern.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
        return text


class _RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor, fmt: str, datefmt: str | None = None) -> None:
        super().__init__(fmt, datefmt)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.redact(super().format(record))


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, including any structured ``extra`` fields."""

    _RESERVED: Final = frozenset(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__
    ) | frozenset({"message", "asctime", "taskName"})

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
        }
        try:
            payload["message"] = record.getMessage()
        except Exception:  # noqa: BLE001 - logging must never raise
            payload["message"] = str(record.msg)
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in self._RESERVED and not key.startswith("_")
            }
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        serialized = json.dumps(payload, default=str, ensure_ascii=False)
        return self._redactor.redact(serialized)


def configure_logging(settings: Settings) -> None:
    """Install the process-wide logging configuration (idempotent)."""
    redactor = Redactor(settings.secret_values())
    handler = logging.StreamHandler(sys.stderr)
    if settings.log_format == "json":
        handler.setFormatter(_JsonFormatter(redactor))
    else:
        handler.setFormatter(_RedactingFormatter(redactor, _TEXT_FORMAT, _DATE_FORMAT))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Third-party libraries log full request URLs, which contain credentials.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

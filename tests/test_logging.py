"""Tests for structured logging and, most importantly, secret redaction."""

from __future__ import annotations

import json
import logging

from solana_monitor.config import Settings
from solana_monitor.logging_setup import (
    REDACTED,
    Redactor,
    _JsonFormatter,
    _RedactingFormatter,
    configure_logging,
)
from tests.conftest import VALID_API_KEY, VALID_BOT_TOKEN, make_log_record


def test_literal_secret_is_redacted() -> None:
    redactor = Redactor([VALID_API_KEY])

    assert redactor.redact(f"key is {VALID_API_KEY}!") == f"key is {REDACTED}!"


def test_api_key_query_parameter_is_redacted() -> None:
    redactor = Redactor()

    redacted = redactor.redact(
        "GET https://api.example.com/v0/addresses/abc/transactions?api-key=deadbeef&limit=10"
    )

    assert "deadbeef" not in redacted
    assert "api-key=***" in redacted
    assert "limit=10" in redacted


def test_telegram_bot_token_in_url_is_redacted() -> None:
    redactor = Redactor()
    url = "HTTP Request: POST https://api.telegram.org/bot123456789:AAF-abc_1234567890123456/sendMessage"

    redacted = redactor.redact(url)

    assert "AAF-abc_1234567890123456" not in redacted
    assert "***" in redacted


def _raise_with_secret(message: str) -> None:
    """Raise from a helper so the traceback contains a secret-shaped message."""
    raise RuntimeError(message)


def test_text_formatter_redacts_tracebacks() -> None:
    redactor = Redactor([VALID_API_KEY])
    formatter = _RedactingFormatter(redactor, "%(message)s")
    try:
        _raise_with_secret(f"failed calling api-key={VALID_API_KEY}")
    except RuntimeError as error:
        record = make_log_record("boom", exc_info=(type(error), error, error.__traceback__))

    rendered = formatter.format(record)

    assert VALID_API_KEY not in rendered
    assert "RuntimeError" in rendered


def test_json_formatter_emits_structured_fields() -> None:
    formatter = _JsonFormatter(Redactor([VALID_BOT_TOKEN]))
    record = make_log_record("alert sent", args=(), chat_id=-100500, mint="abc")

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "solana_monitor.test"
    assert payload["message"] == "alert sent"
    assert payload["chat_id"] == -100500
    assert payload["mint"] == "abc"
    assert "timestamp" in payload


def test_json_formatter_serialises_unknown_extra_values() -> None:
    formatter = _JsonFormatter(Redactor())
    record = make_log_record("value", payload={"nested": {1, 2}})

    payload = json.loads(formatter.format(record))

    assert "nested" in payload["payload"]


def test_json_formatter_never_raises_on_broken_templates() -> None:
    formatter = _JsonFormatter(Redactor())
    record = make_log_record("bad %d template", args=("not-an-int",))

    payload = json.loads(formatter.format(record))

    assert "bad %d template" in payload["message"]


def test_configure_logging_is_idempotent_and_respects_level(
    settings: Settings, restore_logging: None
) -> None:
    configure_logging(settings)
    first_handlers = list(logging.getLogger().handlers)
    configure_logging(settings)

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.handlers[0] is not first_handlers[0]
    assert root.level == logging.INFO


def test_configure_logging_quiets_noisy_http_loggers(
    settings: Settings, restore_logging: None
) -> None:
    configure_logging(settings)

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING

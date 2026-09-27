"""Typed, validated runtime configuration.

All configuration comes from environment variables (optionally seeded from a
``.env`` file).  Nothing is hardcoded and no secret has a default: a missing or
malformed value fails fast with an actionable message that never contains the
offending secret.

Design notes
------------
* ``Settings`` is frozen: configuration is read once at start-up and cannot be
  mutated accidentally by later code.
* Values that users naturally write as comma-separated lists
  (``TELEGRAM_ADMIN_USER_IDS=1,2``) use :data:`pydantic_settings.NoDecode` plus a
  before-validator, because pydantic-settings would otherwise insist on JSON.
* :func:`format_validation_error` deliberately reports only field locations and
  messages, never the rejected input, so validation failures can be logged
  safely.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from solana_monitor.domain.constants import WRAPPED_SOL_MINT
from solana_monitor.domain.errors import ConfigurationError

_BOT_TOKEN_PATTERN: Final = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
_TELEGRAM_USERNAME_PATTERN: Final = re.compile(r"[A-Za-z0-9_]{5,32}")
_LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})

CommaSeparatedInts = Annotated[frozenset[int], NoDecode]
CommaSeparatedStrings = Annotated[frozenset[str], NoDecode]


def _split_csv(value: object) -> object:
    """Turn ``"a, b"`` into ``["a", "b"]``, also accepting a JSON array literal.

    ``NoDecode`` ensures pydantic-settings hands us the raw string, so both the
    friendly comma-separated form and the JSON form are parsed here.
    """
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return ()
    if stripped.startswith("["):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError as error:
            msg = "must be a comma separated list or a JSON array"
            raise ValueError(msg) from error
        if not isinstance(decoded, list):
            msg = "must be a comma separated list or a JSON array"
            raise ValueError(msg)
        return decoded
    return [part.strip() for part in stripped.split(",") if part.strip()]


class Settings(BaseSettings):
    """Validated application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # -- Telegram ------------------------------------------------------------
    telegram_bot_token: SecretStr = Field(
        description="BotFather token, e.g. 123456789:AAH...",
    )
    telegram_admin_user_ids: CommaSeparatedInts = Field(
        default_factory=frozenset,
        description="Telegram user IDs allowed to run state-changing commands.",
    )
    telegram_allowed_chat_ids: CommaSeparatedInts = Field(
        default_factory=frozenset,
        description="Chats the bot answers in; empty means every chat.",
    )
    telegram_commands_per_minute: int = Field(default=12, ge=1, le=600)

    # -- Helius --------------------------------------------------------------
    helius_api_key: SecretStr = Field(description="Helius API key.")
    helius_api_base_url: str = Field(default="https://mainnet.helius-rpc.com")
    helius_commitment: Literal["confirmed", "finalized"] = Field(default="confirmed")
    helius_token_accounts: Literal["none", "balanceChanged", "all"] = Field(
        default="balanceChanged"
    )
    helius_page_size: int = Field(default=100, ge=1, le=100)
    helius_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    helius_requests_per_second: float = Field(default=2.0, gt=0, le=100)
    helius_max_retries: int = Field(default=4, ge=0, le=10)
    helius_retry_base_seconds: float = Field(default=0.5, gt=0, le=30)
    helius_retry_max_seconds: float = Field(default=20.0, gt=0, le=300)
    helius_metadata_cache_seconds: int = Field(default=3600, ge=0, le=86_400)

    # -- Monitoring ----------------------------------------------------------
    monitor_poll_interval_seconds: float = Field(default=20.0, ge=1.0, le=3600)
    monitor_max_catchup_pages: int = Field(default=10, ge=1, le=100)
    monitor_max_backoff_seconds: float = Field(default=300.0, ge=1.0, le=3600)
    monitor_alert_on_receipts: bool = Field(default=True)
    monitor_alert_on_repeat: bool = Field(default=False)
    monitor_resume_on_start: bool = Field(default=True)
    monitor_max_seen_mints: int = Field(default=5000, ge=100, le=1_000_000)
    monitor_max_processed_signatures: int = Field(default=2000, ge=100, le=1_000_000)
    monitor_ignored_mints: CommaSeparatedStrings = Field(
        default_factory=lambda: frozenset({WRAPPED_SOL_MINT}),
        description="Mints that never trigger an alert.",
    )

    # -- Analysis ------------------------------------------------------------
    analyze_max_pages: int = Field(default=10, ge=1, le=100)
    analyze_max_tokens_listed: int = Field(default=10, ge=1, le=50)

    # -- Presentation --------------------------------------------------------
    alerts_dexscreener_base_url: str = Field(default="https://dexscreener.com/solana")
    alerts_trojan_bot_username: str = Field(default="achilles_trojanbot")

    # -- Runtime -------------------------------------------------------------
    state_file: Path = Field(default=Path("var/state.json"))
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")
    log_format: Literal["text", "json"] = Field(default="text")

    @field_validator("telegram_bot_token")
    @classmethod
    def _validate_bot_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if not _BOT_TOKEN_PATTERN.fullmatch(token):
            msg = "does not look like a BotFather token ('<digits>:<30+ characters>')"
            raise ValueError(msg)
        return SecretStr(token)

    @field_validator("helius_api_key")
    @classmethod
    def _validate_api_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value().strip()
        if len(key) < 8 or any(char.isspace() for char in key):
            msg = "must be a non-empty Helius API key without whitespace"
            raise ValueError(msg)
        return SecretStr(key)

    @field_validator("helius_api_base_url", "alerts_dexscreener_base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        candidate = value.strip().rstrip("/")
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            msg = "must be an absolute http(s) URL"
            raise ValueError(msg)
        if parsed.scheme != "https" and parsed.hostname not in _LOCAL_HOSTS:
            msg = "must use https (plain http is only allowed for localhost)"
            raise ValueError(msg)
        return candidate

    @field_validator("alerts_trojan_bot_username")
    @classmethod
    def _validate_trojan_username(cls, value: str) -> str:
        username = value.strip().lstrip("@")
        if username and not _TELEGRAM_USERNAME_PATTERN.fullmatch(username):
            msg = "must be a valid Telegram username (5-32 word characters) or empty"
            raise ValueError(msg)
        return username

    @field_validator("telegram_admin_user_ids", "telegram_allowed_chat_ids", mode="before")
    @classmethod
    def _parse_int_list(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("monitor_ignored_mints", mode="before")
    @classmethod
    def _parse_mint_list(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("monitor_ignored_mints")
    @classmethod
    def _strip_empty_mints(cls, value: frozenset[str]) -> frozenset[str]:
        return frozenset(mint.strip() for mint in value if mint.strip())

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.helius_retry_max_seconds < self.helius_retry_base_seconds:
            msg = "helius_retry_max_seconds must be >= helius_retry_base_seconds"
            raise ValueError(msg)
        return self


    # -- Derived values ------------------------------------------------------
    def secret_values(self) -> tuple[str, ...]:
        """Secret strings that must never appear in logs."""
        return (self.telegram_bot_token.get_secret_value(), self.helius_api_key.get_secret_value())

    def configuration_warnings(self) -> tuple[str, ...]:
        """Non-fatal configuration advice, surfaced at start-up."""
        warnings: list[str] = []
        if not self.telegram_admin_user_ids:
            warnings.append(
                "TELEGRAM_ADMIN_USER_IDS is empty: state-changing commands "
                "(/set_wallet, /start_monitoring, /stop_monitoring) are refused for "
                "everybody. Send /whoami in Telegram and set this variable."
            )
        if self.helius_requests_per_second > 5:
            warnings.append(
                "HELIUS_REQUESTS_PER_SECOND above 5 exceeds free/developer plan limits "
                "and will cause HTTP 429 responses."
            )
        if self.monitor_poll_interval_seconds < 10:
            warnings.append(
                "MONITOR_POLL_INTERVAL_SECONDS below 10 burns Helius credits quickly "
                "(each poll costs roughly 100 credits)."
            )
        return tuple(warnings)

    def describe(self) -> dict[str, object]:
        """A log-safe summary of the effective configuration (never secrets)."""
        return {
            "log_level": self.log_level,
            "log_format": self.log_format,
            "state_file": str(self.state_file),
            "telegram_bot_token_configured": bool(self.telegram_bot_token.get_secret_value()),
            "telegram_admin_user_ids": sorted(self.telegram_admin_user_ids),
            "telegram_allowed_chat_ids": sorted(self.telegram_allowed_chat_ids),
            "helius_api_base_url": self.helius_api_base_url,
            "helius_api_key_configured": bool(self.helius_api_key.get_secret_value()),
            "helius_commitment": self.helius_commitment,
            "helius_token_accounts": self.helius_token_accounts,
            "monitor_poll_interval_seconds": self.monitor_poll_interval_seconds,
            "monitor_alert_on_receipts": self.monitor_alert_on_receipts,
            "monitor_alert_on_repeat": self.monitor_alert_on_repeat,
            "monitor_resume_on_start": self.monitor_resume_on_start,
            "monitor_ignored_mints": sorted(self.monitor_ignored_mints),
        }


def format_validation_error(error: ValidationError) -> str:
    """Render a :class:`ValidationError` without echoing rejected input values."""
    problems: list[str] = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        problems.append(f"  - {location}: {item['msg']}")
    joined = "\n".join(problems)
    return f"Invalid configuration:\n{joined}"


def load_settings(
    env_file: Path | str | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Load settings from the environment, raising :class:`ConfigurationError`.

    ``overrides`` is used by the CLI for flags such as ``--log-level`` that take
    precedence over the environment.
    """
    settings_kwargs: dict[str, Any] = dict(overrides or {})
    try:
        return Settings(_env_file=env_file, **settings_kwargs)
    except ValidationError as error:
        raise ConfigurationError(format_validation_error(error)) from error

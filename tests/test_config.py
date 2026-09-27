"""Tests for typed configuration loading and validation.

Every case drives configuration through environment variables, exactly like
production does, so pydantic-settings' parsing behaviour is covered too.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from solana_monitor.config import Settings, load_settings
from solana_monitor.domain.constants import WRAPPED_SOL_MINT
from solana_monitor.domain.errors import ConfigurationError
from tests.conftest import VALID_API_KEY, VALID_BOT_TOKEN

EnvConfig = Callable[..., Settings]


def test_defaults_are_applied(settings: Settings) -> None:
    assert settings.helius_commitment == "confirmed"
    assert settings.helius_token_accounts == "balanceChanged"
    assert settings.helius_page_size == 100
    assert settings.monitor_ignored_mints == frozenset({WRAPPED_SOL_MINT})
    assert settings.monitor_alert_on_repeat is False
    assert settings.monitor_resume_on_start is True
    assert settings.state_file == Path("var/state.json")
    assert settings.log_level == "INFO"
    assert settings.log_format == "text"


def test_secrets_are_not_plain_strings(settings: Settings) -> None:
    assert settings.telegram_bot_token.get_secret_value() == VALID_BOT_TOKEN
    assert VALID_BOT_TOKEN not in repr(settings)
    assert VALID_API_KEY not in repr(settings)
    assert settings.secret_values() == (VALID_BOT_TOKEN, VALID_API_KEY)


def test_missing_required_values_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env_file=None)

    message = str(excinfo.value)
    assert message.startswith("Invalid configuration:")
    assert "telegram_bot_token" in message
    assert "helius_api_key" in message


def test_invalid_bot_token_error_never_echoes_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_looking_value = "super-secret-not-a-token"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", secret_looking_value)
    monkeypatch.setenv("HELIUS_API_KEY", VALID_API_KEY)

    with pytest.raises(ConfigurationError) as excinfo:
        load_settings(env_file=None)

    assert secret_looking_value not in str(excinfo.value)
    assert "telegram_bot_token" in str(excinfo.value)


def test_api_key_with_whitespace_is_rejected(env_config: EnvConfig) -> None:
    with pytest.raises(ConfigurationError, match="helius_api_key"):
        env_config(HELIUS_API_KEY="abc def ghi")


@pytest.mark.parametrize("raw", ["123, 456", "123,456", "123,456,"])
def test_comma_separated_admin_ids_are_parsed(env_config: EnvConfig, raw: str) -> None:
    settings = env_config(TELEGRAM_ADMIN_USER_IDS=raw)

    assert settings.telegram_admin_user_ids == frozenset({123, 456})


def test_json_array_admin_ids_are_parsed(env_config: EnvConfig) -> None:
    settings = env_config(TELEGRAM_ALLOWED_CHAT_IDS="[-1001234567890, 42]")

    assert settings.telegram_allowed_chat_ids == frozenset({-1001234567890, 42})


@pytest.mark.parametrize("raw", ["abc", "[not-json", '{"a": 1}'])
def test_malformed_list_values_are_rejected(env_config: EnvConfig, raw: str) -> None:
    with pytest.raises(ConfigurationError, match="telegram_admin_user_ids"):
        env_config(TELEGRAM_ADMIN_USER_IDS=raw)


def test_empty_admin_list_produces_a_fail_closed_warning(settings: Settings) -> None:
    warnings = settings.configuration_warnings()

    assert any("TELEGRAM_ADMIN_USER_IDS is empty" in warning for warning in warnings)
    assert any("refused" in warning for warning in warnings)


def test_configuration_warnings_are_empty_when_well_configured(env_config: EnvConfig) -> None:
    settings = env_config(TELEGRAM_ADMIN_USER_IDS="42")

    assert settings.configuration_warnings() == ()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://mainnet.helius-rpc.com/", "https://mainnet.helius-rpc.com"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
    ],
)
def test_base_url_normalisation(env_config: EnvConfig, raw: str, expected: str) -> None:
    settings = env_config(HELIUS_API_BASE_URL=raw)

    assert settings.helius_api_base_url == expected


@pytest.mark.parametrize("raw", ["http://evil.example.com", "ftp://example.com", "not-a-url"])
def test_insecure_or_relative_base_url_is_rejected(env_config: EnvConfig, raw: str) -> None:
    with pytest.raises(ConfigurationError, match="helius_api_base_url"):
        env_config(HELIUS_API_BASE_URL=raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("@achilles_trojanbot", "achilles_trojanbot"), ("", ""), ("bot_1", "bot_1")],
)
def test_trojan_username_normalisation(env_config: EnvConfig, raw: str, expected: str) -> None:
    settings = env_config(ALERTS_TROJAN_BOT_USERNAME=raw)

    assert settings.alerts_trojan_bot_username == expected


def test_invalid_trojan_username_is_rejected(env_config: EnvConfig) -> None:
    with pytest.raises(ConfigurationError, match="alerts_trojan_bot_username"):
        env_config(ALERTS_TROJAN_BOT_USERNAME="not a username!")


@pytest.mark.parametrize("raw", ["0", "0.5", "4000"])
def test_poll_interval_bounds(env_config: EnvConfig, raw: str) -> None:
    with pytest.raises(ConfigurationError, match="monitor_poll_interval_seconds"):
        env_config(MONITOR_POLL_INTERVAL_SECONDS=raw)


def test_short_poll_interval_is_allowed_but_warned(env_config: EnvConfig) -> None:
    settings = env_config(MONITOR_POLL_INTERVAL_SECONDS="5")

    assert any("credits" in warning for warning in settings.configuration_warnings())


def test_retry_window_consistency_is_enforced(env_config: EnvConfig) -> None:
    with pytest.raises(ConfigurationError, match="helius_retry_max_seconds"):
        env_config(HELIUS_RETRY_BASE_SECONDS="10", HELIUS_RETRY_MAX_SECONDS="5")


def test_ignored_mints_are_overridable(env_config: EnvConfig) -> None:
    other_mint = "OtherMint1111111111111111111111111111111111"
    settings = env_config(MONITOR_IGNORED_MINTS=f"{WRAPPED_SOL_MINT}, ,{other_mint}")

    assert settings.monitor_ignored_mints == frozenset({WRAPPED_SOL_MINT, other_mint})


def test_describe_contains_no_secrets(settings: Settings) -> None:
    described = settings.describe()

    serialized = repr(described)
    assert VALID_BOT_TOKEN not in serialized
    assert VALID_API_KEY not in serialized
    assert described["telegram_bot_token_configured"] is True
    assert described["helius_api_key_configured"] is True


def test_load_settings_reads_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"TELEGRAM_BOT_TOKEN={VALID_BOT_TOKEN}\nHELIUS_API_KEY={VALID_API_KEY}\nLOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file=env_file)

    assert settings.log_level == "DEBUG"


def test_overrides_win_over_the_environment(settings: Settings) -> None:
    overridden = load_settings(
        env_file=None, overrides={"log_level": "ERROR", "state_file": "tmp/state.json"}
    )

    assert overridden.log_level == "ERROR"
    assert overridden.state_file == Path("tmp/state.json")


def test_invalid_override_raises_configuration_error(settings: Settings) -> None:
    with pytest.raises(ConfigurationError, match="log_level"):
        load_settings(env_file=None, overrides={"log_level": "LOUD"})


def test_settings_are_immutable(settings: Settings) -> None:
    with pytest.raises(Exception, match="frozen"):
        settings.log_level = "DEBUG"  # type: ignore[misc]

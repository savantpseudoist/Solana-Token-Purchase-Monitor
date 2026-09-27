"""Tests for the command-line entry point."""

from __future__ import annotations

from pathlib import Path

import pytest

from solana_monitor import __version__, cli
from tests.conftest import VALID_API_KEY, VALID_BOT_TOKEN


def test_version_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])

    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_check_config_succeeds_with_a_valid_environment(
    base_env: dict[str, str], restore_logging: None
) -> None:
    exit_code = cli.main(["--check-config"])

    assert exit_code == cli.EXIT_OK


def test_check_config_reports_a_missing_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("HELIUS_API_KEY", VALID_API_KEY)

    exit_code = cli.main(["--check-config"])

    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_CONFIG_ERROR
    assert "telegram_bot_token" in captured.err
    assert VALID_API_KEY not in captured.err
    assert ".env.example" in captured.err


def test_configuration_warnings_are_printed(
    monkeypatch: pytest.MonkeyPatch,
    base_env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    restore_logging: None,
) -> None:
    exit_code = cli.main(["--check-config"])

    assert exit_code == cli.EXIT_OK
    assert "configuration warning" in capsys.readouterr().err


def test_flag_overrides_reach_the_settings(base_env: dict[str, str], restore_logging: None) -> None:
    assert cli.main(["--check-config", "--log-level", "ERROR", "--log-format", "json"]) == 0
    assert cli.main(["--check-config", "--state-file", "var/other.json"]) == 0


def test_invalid_flag_value_is_rejected() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--log-level", "LOUD"])

    assert excinfo.value.code == 2


def test_env_file_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    env_file = tmp_path / "custom.env"
    env_file.write_text(
        f"TELEGRAM_BOT_TOKEN={VALID_BOT_TOKEN}\nHELIUS_API_KEY={VALID_API_KEY}\n",
        encoding="utf-8",
    )

    assert cli.main(["--check-config", "--env-file", str(env_file)]) == cli.EXIT_OK


def test_missing_env_file_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_logging: None
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", VALID_BOT_TOKEN)
    monkeypatch.setenv("HELIUS_API_KEY", VALID_API_KEY)

    # A path that does not exist simply means "no dotenv overrides".
    assert cli.main(["--check-config", "--env-file", str(tmp_path / "absent.env")]) == 0

"""Shared test fixtures and helpers.

The suite never touches the network, never reads a developer's ``.env`` file and
never sleeps: every external boundary is replaced by a fake or by injected
time/sleep functions.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from solana_monitor.config import Settings, load_settings

VALID_BOT_TOKEN = "123456789:" + "A" * 35
VALID_API_KEY = "00000000-1111-2222-3333-444444444444"
FIXED_NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Minimal valid environment for :class:`Settings`."""
    env = {"TELEGRAM_BOT_TOKEN": VALID_BOT_TOKEN, "HELIUS_API_KEY": VALID_API_KEY}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def settings(base_env: dict[str, str]) -> Settings:
    """Settings built from the environment only (no ``.env`` lookup)."""
    return Settings(_env_file=None)


@pytest.fixture
def env_config(monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]) -> Any:
    """Return a helper that applies environment overrides and loads settings.

    Using environment variables (rather than constructor kwargs) keeps the tests
    honest: it exercises exactly the code path production uses, including
    pydantic-settings' parsing of complex values.
    """

    def _load(**env: str) -> Settings:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return load_settings(env_file=None)

    return _load


@pytest.fixture
def now() -> datetime:
    return FIXED_NOW


class FakeClock:
    """Deterministic monotonic clock for tests of time-based code."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeSleeper:
    """Records requested sleeps and advances a :class:`FakeClock` instead of waiting."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.sleeps: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock.advance(seconds)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_sleeper(fake_clock: FakeClock) -> FakeSleeper:
    return FakeSleeper(fake_clock)


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Snapshot and restore the root logger around tests that reconfigure it."""
    import logging

    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
        root.handlers = handlers
        root.setLevel(level)


def make_log_record(
    message: str,
    *,
    args: tuple[Any, ...] = (),
    exc_info: tuple[Any, Any, Any] | None = None,
    level: int = 20,
    **extra: Any,
) -> Any:
    """Build a :class:`logging.LogRecord` for formatter tests."""
    import logging

    record = logging.LogRecord(
        name="solana_monitor.test",
        level=level,
        pathname=__file__,
        lineno=42,
        msg=message,
        args=args,
        exc_info=exc_info,
    )
    record.__dict__.update(extra)
    return record

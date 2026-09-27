"""Tests for Telegram access control and rate limiting."""

from __future__ import annotations

import pytest

from solana_monitor.config import Settings
from solana_monitor.domain.errors import AuthorizationError
from solana_monitor.telegram.authorization import CommandAuthorization, CommandThrottle
from tests.conftest import VALID_API_KEY, VALID_BOT_TOKEN, FakeClock

ADMIN = 1
STRANGER = 2
ALLOWED_CHAT = -100
OTHER_CHAT = -200


def authorization(**env: str) -> CommandAuthorization:
    values: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": VALID_BOT_TOKEN,
        "HELIUS_API_KEY": VALID_API_KEY,
        **env,
    }
    settings = Settings(_env_file=None, **values)  # type: ignore[arg-type]
    return CommandAuthorization.from_settings(settings)


def test_without_a_chat_allowlist_every_chat_is_allowed() -> None:
    policy = authorization(TELEGRAM_ADMIN_USER_IDS=str(ADMIN))

    assert policy.is_chat_allowed(OTHER_CHAT) is True


def test_listed_chats_are_the_only_ones_answered() -> None:
    policy = authorization(
        TELEGRAM_ADMIN_USER_IDS=str(ADMIN), TELEGRAM_ALLOWED_CHAT_IDS=str(ALLOWED_CHAT)
    )

    assert policy.is_chat_allowed(ALLOWED_CHAT) is True
    assert policy.is_chat_allowed(OTHER_CHAT) is False
    with pytest.raises(AuthorizationError, match="not configured"):
        policy.check_chat(OTHER_CHAT)


def test_read_only_commands_skip_the_admin_check() -> None:
    policy = authorization(TELEGRAM_ADMIN_USER_IDS=str(ADMIN))

    policy.check_command(user_id=STRANGER, command="status", requires_admin=False)


def test_admins_may_change_the_watch() -> None:
    policy = authorization(TELEGRAM_ADMIN_USER_IDS=str(ADMIN))

    policy.check_command(user_id=ADMIN, command="set_wallet", requires_admin=True)


def test_strangers_may_not_change_the_watch() -> None:
    policy = authorization(TELEGRAM_ADMIN_USER_IDS=str(ADMIN))

    with pytest.raises(AuthorizationError, match="administrators"):
        policy.check_command(user_id=STRANGER, command="set_wallet", requires_admin=True)


def test_nobody_may_change_the_watch_without_an_admin_list() -> None:
    policy = authorization(TELEGRAM_ADMIN_USER_IDS="")

    assert policy.admins_configured is False
    with pytest.raises(AuthorizationError, match="TELEGRAM_ADMIN_USER_IDS"):
        policy.check_command(user_id=ADMIN, command="set_wallet", requires_admin=True)


def test_throttle_allows_a_burst_then_blocks() -> None:
    clock = FakeClock()
    throttle = CommandThrottle(2, clock=clock)

    assert throttle.allow(ADMIN) is True
    assert throttle.allow(ADMIN) is True
    assert throttle.allow(ADMIN) is False


def test_throttle_is_per_user() -> None:
    throttle = CommandThrottle(1, clock=FakeClock())

    assert throttle.allow(ADMIN) is True
    assert throttle.allow(STRANGER) is True


def test_throttle_recovers_after_the_window() -> None:
    clock = FakeClock()
    throttle = CommandThrottle(1, clock=clock)
    throttle.allow(ADMIN)

    clock.advance(61)

    assert throttle.allow(ADMIN) is True


def test_throttle_bounds_tracked_users() -> None:
    throttle = CommandThrottle(1, clock=FakeClock())

    for index in range(2000):
        throttle.allow(index)

    # Oldest buckets are evicted, so the limiter cannot grow without bound.
    assert len(throttle._buckets) <= 1024

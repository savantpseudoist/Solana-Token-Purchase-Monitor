"""Tests for alert delivery through the Telegram sink."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import pytest
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError

from solana_monitor.config import Settings
from solana_monitor.domain.errors import NotificationError
from solana_monitor.domain.models import AcquisitionKind, TokenAcquisition
from solana_monitor.telegram.sink import TelegramAlertSink, classify_notification_error
from tests import factories as f
from tests.conftest import VALID_API_KEY, VALID_BOT_TOKEN

CHAT_ID = -1001234567890


class FakeBot:
    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.sent: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object) -> object:
        self.sent.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return object()


def settings() -> Settings:
    values: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": VALID_BOT_TOKEN,
        "HELIUS_API_KEY": VALID_API_KEY,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def acquisition() -> TokenAcquisition:
    return TokenAcquisition(
        mint=f.TOKEN_MINT,
        amount_raw=Decimal("1500000"),
        decimals=6,
        symbol="USDC",
        kind=AcquisitionKind.PURCHASE,
        signature=f.SIGNATURE,
        slot=1,
        timestamp=datetime(2026, 9, 27, 11, 0, tzinfo=UTC),
        description="swap",
        sol_spent_lamports=1_000_000_000,
    )


async def _no_sleep(_seconds: float) -> None:
    """Retry backoff is irrelevant to these assertions."""


def build_sink(bot: FakeBot) -> TelegramAlertSink:
    return TelegramAlertSink(
        sender=bot,
        chat_id=CHAT_ID,
        wallet=f.WATCHED_WALLET,
        settings=settings(),
        sleep=_no_sleep,
    )


async def test_alerts_are_delivered_as_escaped_html_with_links() -> None:
    bot = FakeBot()
    sink = build_sink(bot)

    await sink.send(acquisition())

    message = bot.sent[0]
    assert message["chat_id"] == CHAT_ID
    assert message["parse_mode"] == "HTML"
    assert message["disable_web_page_preview"] is True
    assert "USDC" in str(message["text"])
    keyboard = cast("InlineKeyboardMarkup", message["reply_markup"])
    assert len(keyboard.inline_keyboard) == 1
    assert [button.url for button in keyboard.inline_keyboard[0]] == [
        f"https://dexscreener.com/solana/{f.TOKEN_MINT}",
        f"https://t.me/achilles_trojanbot?start={f.TOKEN_MINT}",
    ]


async def test_transient_failures_are_retried() -> None:
    bot = FakeBot(NetworkError("flaky"))
    sink = build_sink(bot)

    await sink.send(acquisition())

    assert len(bot.sent) == 2


async def test_permanent_failures_are_not_retried() -> None:
    bot = FakeBot(Forbidden("bot was blocked by the user"))
    sink = build_sink(bot)

    with pytest.raises(NotificationError, match="cannot post"):
        await sink.send(acquisition())

    assert len(bot.sent) == 1


async def test_retry_budget_is_finite() -> None:
    bot = FakeBot(*[NetworkError("flaky") for _ in range(5)])
    sink = build_sink(bot)

    with pytest.raises(NotificationError):
        await sink.send(acquisition())

    assert len(bot.sent) == 4


def test_error_classification_preserves_retryability() -> None:
    assert classify_notification_error(NetworkError("x")).retryable is True
    assert classify_notification_error(Forbidden("x")).retryable is False
    assert classify_notification_error(BadRequest("x")).retryable is False
    assert classify_notification_error(RuntimeError("x")).retryable is False

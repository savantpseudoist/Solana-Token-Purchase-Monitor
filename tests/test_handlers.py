"""Tests for the python-telegram-bot handler adapters.

These exercise the framework boundary with real ``Update`` objects (deserialised
by the library itself) and a stub sender, so the wiring - argument extraction,
single-reply behaviour, error handling - is covered without any network access.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from telegram import Bot, Update
from telegram.error import Forbidden

from solana_monitor.config import Settings
from solana_monitor.helius.client import HistoryScan
from solana_monitor.monitoring.detector import DetectorPolicy, PurchaseDetector
from solana_monitor.monitoring.manager import WatchManager
from solana_monitor.monitoring.monitor import WalletMonitor
from solana_monitor.monitoring.state import WatchState, WatchStore
from solana_monitor.telegram.app import (
    build_command_handler,
    build_error_handler,
    build_fallback_handler,
)
from solana_monitor.telegram.commands import CommandService
from tests import factories as f
from tests.conftest import VALID_API_KEY

CHAT_ID = -1001234567890
USER_ID = 4242
TOKEN = "123456789:" + "A" * 35


class FakeHistory:
    async def collect_history(
        self,
        _address: str,
        *,
        gte_time: int | None = None,
        sort_order: str = "desc",
        start_after: str | None = None,
        start_before: str | None = None,
        page_size: int | None = None,
        max_pages: int | None = None,
    ) -> HistoryScan:
        return HistoryScan((), 1, False)


class NullSink:
    async def send(self, acquisition: object) -> None:
        """Handlers never deliver alerts."""


class RecordingBot:
    """Stands in for ``telegram.Bot``; never touches the network."""

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.error = error

    async def send_message(self, **kwargs: Any) -> object:
        if self.error is not None:
            raise self.error
        self.sent.append(kwargs)
        return object()

    async def reply_text(self, text: str, **kwargs: Any) -> object:
        """Mirrors ``telegram.Message.reply_text``'s positional signature."""
        self.sent.append({"text": text, **kwargs})
        return object()


class FakeContext:
    def __init__(self, bot: RecordingBot, args: list[str] | None = None) -> None:
        self.bot = bot
        self.args = args
        self.error: Exception | None = None


def build_update(sender: RecordingBot, text: str, *, user_id: int = USER_ID) -> Update:
    """Deserialise a real ``Update`` with a stub sender (no network involved)."""
    command = text.split(maxsplit=1)[0]
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": int(datetime(2026, 9, 27, tzinfo=UTC).timestamp()),
            "chat": {"id": CHAT_ID, "type": "group", "title": "test"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
        },
    }
    return Update.de_json(payload, cast("Bot", sender))


def build_service(tmp_path: Path) -> tuple[CommandService, WatchStore]:
    values: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "HELIUS_API_KEY": VALID_API_KEY,
        "TELEGRAM_ADMIN_USER_IDS": str(USER_ID),
    }
    settings = Settings(_env_file=None, **values)  # type: ignore[arg-type]
    store = WatchStore(tmp_path / "state.json")
    detector = PurchaseDetector(DetectorPolicy())

    def factory(state: WatchState) -> WalletMonitor:
        return WalletMonitor(
            state=state,
            store=store,
            source=FakeHistory(),
            detector=detector,
            sink=NullSink(),
        )

    service = CommandService(
        settings=settings,
        store=store,
        manager=WatchManager(store, factory),
        history=FakeHistory(),
        detector=detector,
    )
    return service, store


async def test_a_command_handler_replies_once_with_arguments(tmp_path: Path) -> None:
    service, store = build_service(tmp_path)
    bot = RecordingBot()
    context = FakeContext(bot, [f.WATCHED_WALLET])
    handler = build_command_handler(service, "set_wallet")
    update = build_update(bot, "/set_wallet " + f.WATCHED_WALLET)

    await handler(update, cast(Any, context))

    assert len(bot.sent) == 1
    assert store.require(CHAT_ID).wallet == f.WATCHED_WALLET


async def test_a_command_handler_tells_unauthorised_callers_off(tmp_path: Path) -> None:
    service, store = build_service(tmp_path)
    bot = RecordingBot()
    context = FakeContext(bot, [f.WATCHED_WALLET])
    handler = build_command_handler(service, "set_wallet")
    update = build_update(bot, "/set_wallet " + f.WATCHED_WALLET, user_id=1)

    await handler(update, cast(Any, context))

    assert len(bot.sent) == 1
    assert "administrators" in str(bot.sent[0]["text"])
    assert store.get(CHAT_ID) is None


async def test_a_command_handler_is_silent_without_a_chat(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    bot = RecordingBot()
    handler = build_command_handler(service, "status")

    await handler(cast("Update", None), cast(Any, FakeContext(bot)))

    assert bot.sent == []


async def test_blocked_chats_do_not_raise(tmp_path: Path) -> None:
    service, store = build_service(tmp_path)
    await store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    bot = RecordingBot(error=Forbidden("bot was blocked"))
    handler = build_command_handler(service, "status")
    update = build_update(bot, "/status")

    await handler(update, cast(Any, FakeContext(bot)))

    assert bot.sent == []


async def test_unknown_commands_fall_back_to_help(tmp_path: Path) -> None:
    service, _ = build_service(tmp_path)
    bot = RecordingBot()
    handler = build_fallback_handler(service)
    update = build_update(bot, "/not_a_command")

    await handler(update, cast(Any, FakeContext(bot)))

    assert len(bot.sent) == 1
    assert "Available commands" in str(bot.sent[0]["text"])


async def test_the_error_handler_logs_the_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _, _store = build_service(tmp_path)
    bot = RecordingBot()
    context = FakeContext(bot)
    context.error = RuntimeError("secret internal detail")
    update = build_update(bot, "/status")

    with caplog.at_level("ERROR"):
        await build_error_handler()(update, cast(Any, context))

    assert any("unhandled error" in record.message for record in caplog.records)
    assert "secret internal detail" in caplog.text
    # The user is told the error was logged, but not what it was.
    assert len(bot.sent) == 1
    assert "logged" in str(bot.sent[0]["text"])
    assert "secret internal detail" not in str(bot.sent[0]["text"])


async def test_the_error_handler_tolerates_an_update_without_a_message() -> None:
    context = FakeContext(RecordingBot())
    context.error = RuntimeError("boom")

    await build_error_handler()(cast("Update", object()), cast(Any, context))

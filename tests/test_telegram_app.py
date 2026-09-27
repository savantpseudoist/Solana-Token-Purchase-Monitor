"""Tests for the python-telegram-bot wiring (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from telegram.ext import CommandHandler, MessageHandler

from solana_monitor.config import Settings
from solana_monitor.domain.models import TokenAcquisition
from solana_monitor.helius.client import HistoryScan
from solana_monitor.monitoring.detector import DetectorPolicy, PurchaseDetector
from solana_monitor.monitoring.manager import WatchManager
from solana_monitor.monitoring.monitor import WalletMonitor
from solana_monitor.monitoring.state import WatchState, WatchStore
from solana_monitor.telegram.app import build_application
from solana_monitor.telegram.commands import CommandService
from solana_monitor.telegram.registry import COMMANDS, CommandSpec
from tests.conftest import VALID_API_KEY, VALID_BOT_TOKEN


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


def build_settings() -> Settings:
    values: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": VALID_BOT_TOKEN,
        "HELIUS_API_KEY": VALID_API_KEY,
        "TELEGRAM_ADMIN_USER_IDS": "1",
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def build_service(tmp_path: Path) -> CommandService:
    settings = build_settings()
    store = WatchStore(tmp_path / "state.json")
    detector = PurchaseDetector(DetectorPolicy())
    manager = WatchManager(store, lambda state: _never_started(state, store, detector))
    return CommandService(
        settings=settings,
        store=store,
        manager=manager,
        history=FakeHistory(),
        detector=detector,
    )


def _never_started(
    state: WatchState, store: WatchStore, detector: PurchaseDetector
) -> WalletMonitor:
    return WalletMonitor(
        state=state,
        store=store,
        source=FakeHistory(),
        detector=detector,
        sink=NullSink(),
    )


class NullSink:
    async def send(self, acquisition: TokenAcquisition) -> None:
        """Commands never deliver alerts in these tests."""


def build_app(tmp_path: Path):
    return build_application(build_settings(), build_service(tmp_path))


def test_every_registered_command_gets_a_handler(tmp_path: Path) -> None:
    application = build_app(tmp_path)

    registered = {
        command
        for group in application.handlers.values()
        for handler in group
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }

    assert registered == {spec.name for spec in COMMANDS}


def test_unknown_commands_are_handled_in_a_later_group(tmp_path: Path) -> None:
    groups = build_app(tmp_path).handlers

    assert all(isinstance(handler, CommandHandler) for handler in groups[0])
    assert any(isinstance(handler, MessageHandler) for handler in groups[1])


def test_an_error_handler_is_registered(tmp_path: Path) -> None:
    application = build_app(tmp_path)

    assert application.error_handlers


def test_building_the_application_does_not_touch_the_network(tmp_path: Path) -> None:
    # A valid token must be enough to construct the application offline.
    application = build_app(tmp_path)

    assert application.bot is not None
    assert application.updater is not None


@pytest.mark.parametrize("spec", COMMANDS, ids=lambda spec: spec.name)
def test_registry_usage_strings_match_their_command_names(spec: CommandSpec) -> None:
    assert spec.usage.split()[0] == f"/{spec.name}"

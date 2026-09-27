"""End-to-end flow across every layer, with only the network faked.

This is the regression net for the whole system: a chat issues a command, a
monitor is created, a purchase appears upstream, and exactly one escaped alert is
produced and remembered across a restart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from telegram.error import NetworkError

from solana_monitor.config import Settings
from solana_monitor.domain.errors import NotificationError
from solana_monitor.domain.models import Transaction
from solana_monitor.helius.client import HistoryScan
from solana_monitor.helius.metadata import TokenMetadataResolver
from solana_monitor.helius.schemas import DasAssetPayload
from solana_monitor.monitoring.detector import DetectorPolicy, PurchaseDetector
from solana_monitor.monitoring.manager import WatchManager
from solana_monitor.monitoring.monitor import MonitorConfig, WalletMonitor
from solana_monitor.monitoring.state import WatchState, WatchStore
from solana_monitor.telegram.authorization import CommandAuthorization, CommandThrottle
from solana_monitor.telegram.commands import CommandService
from solana_monitor.telegram.sink import TelegramAlertSink
from tests import factories as f
from tests.conftest import VALID_API_KEY, VALID_BOT_TOKEN, FakeClock

pytestmark = pytest.mark.integration

CHAT_ID = -1001234567890
ADMIN_ID = 4242
CURSOR = "cursor-1"


class FakeHelius:
    """A scripted stand-in for the whole Helius client surface."""

    def __init__(self) -> None:
        self.scripts: list[HistoryScan] = []
        self.holdings: frozenset[str] | None = frozenset()

    def queue(self, *scans: HistoryScan) -> None:
        self.scripts.extend(scans)

    async def collect_history(
        self,
        address: str,
        *,
        gte_time: int | None = None,
        sort_order: str = "desc",
        start_after: str | None = None,
        start_before: str | None = None,
        page_size: int | None = None,
        max_pages: int | None = None,
    ) -> HistoryScan:
        if self.scripts:
            return self.scripts.pop(0)
        return HistoryScan((), 1, False)

    async def get_asset(self, mint: str) -> DasAssetPayload | None:
        return DasAssetPayload.model_validate(
            {
                "id": mint,
                "interface": "FungibleToken",
                "content": {"metadata": {"name": "Fake Token", "symbol": "FAKE"}},
                "token_info": {"decimals": 6},
            }
        )

    async def get_owned_fungible_mints(self, address: str) -> frozenset[str] | None:
        return self.holdings


class FakeTelegramBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> object:
        self.sent.append(kwargs)
        return object()


class FlakyTelegramBot(FakeTelegramBot):
    """Fails every delivery until :meth:`recover` is called."""

    def __init__(self) -> None:
        super().__init__()
        self.available = False

    def recover(self) -> None:
        self.available = True

    async def send_message(self, **kwargs: Any) -> object:
        if not self.available:
            error = NetworkError("telegram is unreachable")
            raise error
        return await super().send_message(**kwargs)


async def _no_sleep(_seconds: float) -> None:
    """No real waiting in an integration test."""


def build_settings(state_file: Path) -> Settings:
    values: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": VALID_BOT_TOKEN,
        "HELIUS_API_KEY": VALID_API_KEY,
        "TELEGRAM_ADMIN_USER_IDS": str(ADMIN_ID),
        "STATE_FILE": str(state_file),
        "HELIUS_METADATA_CACHE_SECONDS": "60",
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def scan(*transactions: Transaction) -> HistoryScan:
    return HistoryScan(tuple(transactions), 1, False)


def build_monitor_factory(
    settings: Settings, store: WatchStore, helius: FakeHelius, bot: FakeTelegramBot, detector: Any
) -> Any:
    resolver = TokenMetadataResolver(helius, ttl_seconds=60, clock=FakeClock())

    def factory(state: WatchState) -> WalletMonitor:
        sink = TelegramAlertSink(
            sender=bot,
            chat_id=state.chat_id,
            wallet=state.wallet,
            settings=settings,
            sleep=_no_sleep,
        )
        return WalletMonitor(
            state=state,
            store=store,
            source=helius,
            detector=detector,
            sink=sink,
            holdings=helius,
            resolver=resolver,
            config=MonitorConfig(poll_interval_seconds=1.0),
        )

    return factory


async def test_full_alert_flow_from_command_to_telegram(tmp_path: Path) -> None:
    settings = build_settings(tmp_path / "state.json")
    store = WatchStore(settings.state_file)
    store.load()
    helius = FakeHelius()
    bot = FakeTelegramBot()
    detector = PurchaseDetector(DetectorPolicy())
    manager = WatchManager(store, build_monitor_factory(settings, store, helius, bot, detector))
    service = CommandService(
        settings=settings,
        store=store,
        manager=manager,
        history=helius,
        detector=detector,
        holdings=helius,
        authorization=CommandAuthorization.from_settings(settings),
        throttle=CommandThrottle(60, clock=FakeClock()),
    )

    # 1. An admin configures a wallet; the bot reports existing holdings.
    helius.holdings = frozenset({f.SECOND_MINT})
    configured = await service.execute(
        "/set_wallet", (f.WATCHED_WALLET,), chat_id=CHAT_ID, user_id=ADMIN_ID
    )
    assert configured is not None
    assert f.WATCHED_WALLET in configured.text

    # 2. Monitoring starts and primes a baseline without alerting.
    helius.queue(scan(f.buy_transaction(signature="older")))
    started = await service.execute("/start_monitoring", (), chat_id=CHAT_ID, user_id=ADMIN_ID)
    monitor = manager.monitor_for(CHAT_ID)
    assert started is not None
    assert monitor is not None
    assert bot.sent == []

    await monitor.sync_once()
    state = store.require(CHAT_ID)
    assert state.primed is True
    assert state.baseline_mints == {f.SECOND_MINT}

    # 3. A new purchase produces exactly one escaped alert with metadata.
    helius.queue(scan(f.buy_transaction(signature="sig-new", amount=2_500_000)))
    await monitor.sync_once()

    assert len(bot.sent) == 1
    message = bot.sent[0]
    assert message["chat_id"] == CHAT_ID
    text = str(message["text"])
    assert "FAKE" in text
    assert "Amount:</b> 2.5" in text
    assert f.TOKEN_MINT in text

    # 4. The same purchase never alerts twice.
    helius.queue(scan(f.buy_transaction(signature="sig-new", amount=2_500_000)))
    await monitor.sync_once()
    assert len(bot.sent) == 1

    # 5. /status reflects the alert.
    status = await service.execute("/status", (), chat_id=CHAT_ID, user_id=ADMIN_ID)
    assert status is not None
    assert "Alerts sent:</b> 1" in status.text

    # 6. A restart resumes the watch without re-alerting old activity.
    await manager.shutdown()
    reloaded = WatchStore(settings.state_file)
    reloaded.load()
    assert reloaded.require(CHAT_ID).alerts_sent == 1
    assert reloaded.require(CHAT_ID).cursor_signature == "sig-new"

    resumed = WatchManager(
        reloaded, build_monitor_factory(settings, reloaded, helius, bot, detector)
    )
    assert await resumed.resume_all() == (CHAT_ID,)
    resumed_monitor = resumed.monitor_for(CHAT_ID)
    assert resumed_monitor is not None
    helius.queue(scan(f.buy_transaction(signature="sig-new", amount=2_500_000)))
    await resumed_monitor.sync_once()
    assert len(bot.sent) == 1
    await resumed.shutdown()


async def test_a_failed_alert_is_retried_instead_of_lost(tmp_path: Path) -> None:
    settings = build_settings(tmp_path / "state.json")
    store = WatchStore(settings.state_file)
    helius = FakeHelius()
    bot = FlakyTelegramBot()
    detector = PurchaseDetector(DetectorPolicy())
    manager = WatchManager(store, build_monitor_factory(settings, store, helius, bot, detector))
    state = await store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    state.prime([], CURSOR)
    service = CommandService(
        settings=settings,
        store=store,
        manager=manager,
        history=helius,
        detector=detector,
        holdings=helius,
    )
    await service.execute("/start_monitoring", (), chat_id=CHAT_ID, user_id=ADMIN_ID)
    monitor = manager.monitor_for(CHAT_ID)
    assert monitor is not None

    # Telegram is down for the whole first pass (4 sink attempts all fail).
    helius.queue(scan(f.buy_transaction(signature="sig-1")))
    with pytest.raises(NotificationError):
        await monitor.sync_once()
    assert bot.sent == []
    assert state.is_seen(f.TOKEN_MINT) is False
    assert state.cursor_signature == CURSOR, "the cursor must not skip undelivered alerts"

    # Telegram recovers: the same purchase is delivered on the next pass.
    bot.recover()
    helius.queue(scan(f.buy_transaction(signature="sig-1")))
    await monitor.sync_once()

    assert len(bot.sent) == 1
    assert state.alerts_sent == 1
    assert state.cursor_signature == "sig-1"
    await manager.shutdown()

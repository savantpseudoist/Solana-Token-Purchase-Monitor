"""Tests for the wallet monitor loop and the watch manager."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from solana_monitor.domain.errors import HeliusAuthError, HeliusError, NotificationError
from solana_monitor.domain.models import TokenAcquisition, TokenMetadata, Transaction
from solana_monitor.helius.client import HistoryScan
from solana_monitor.monitoring.detector import DetectorPolicy, PurchaseDetector
from solana_monitor.monitoring.manager import WatchManager
from solana_monitor.monitoring.monitor import MonitorConfig, WalletMonitor
from solana_monitor.monitoring.state import WatchState, WatchStore
from tests import factories as f
from tests.conftest import FIXED_NOW, FakeSleeper

CHAT_ID = -1001234567890
OTHER_CHAT_ID = -1009876543210


class FakeSource:
    """Serves scripted history scans and records how it was called."""

    def __init__(self, *scans: HistoryScan | Exception) -> None:
        self._scans: list[HistoryScan | Exception] = list(scans)
        self.calls: list[dict[str, object]] = []
        self.holdings: frozenset[str] | None = frozenset()

    async def collect_history(self, address: str, **kwargs: object) -> HistoryScan:
        self.calls.append({"address": address, **kwargs})
        if not self._scans:
            return HistoryScan((), 1, False)
        outcome = self._scans.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def get_owned_fungible_mints(self, _address: str) -> frozenset[str] | None:
        return self.holdings


class FakeSink:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[TokenAcquisition] = []
        self.error = error

    async def send(self, acquisition: TokenAcquisition) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(acquisition)


class FakeResolver:
    def __init__(self, metadata: TokenMetadata | None = None) -> None:
        self.metadata = metadata
        self.calls: list[str] = []

    async def resolve(self, mint: str) -> TokenMetadata | None:
        self.calls.append(mint)
        return self.metadata


def scan(*transactions: Transaction) -> HistoryScan:
    return HistoryScan(tuple(transactions), 1, False)


async def build_monitor(
    tmp_path: Path,
    source: FakeSource,
    sink: FakeSink,
    sleeper: FakeSleeper,
    *,
    config: MonitorConfig | None = None,
    resolver: FakeResolver | None = None,
    chat_id: int = CHAT_ID,
    primed: bool = True,
    cursor: str = "cursor-1",
    baseline: tuple[str, ...] = (),
) -> tuple[WalletMonitor, WatchState, WatchStore]:
    """Wire a monitor on top of a *real* store, so persistence is exercised."""
    store = WatchStore(tmp_path / "state.json")
    state = await store.set_wallet(chat_id, f.WATCHED_WALLET)
    if primed:
        state.prime(baseline, cursor)
    monitor = WalletMonitor(
        state=state,
        store=store,
        source=source,
        detector=PurchaseDetector(DetectorPolicy()),
        sink=sink,
        holdings=source,
        resolver=resolver,
        config=config or MonitorConfig(poll_interval_seconds=10.0, max_backoff_seconds=60.0),
        sleep=sleeper,
        clock=lambda: FIXED_NOW,
    )
    return monitor, state, store


# -- Priming (baseline) ------------------------------------------------------
async def test_first_sync_establishes_a_baseline_without_alerting(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    source = FakeSource(scan(f.buy_transaction(signature="old-sig")))
    source.holdings = frozenset({f.SECOND_MINT})
    sink = FakeSink()
    monitor, state, _ = await build_monitor(tmp_path, source, sink, fake_sleeper, primed=False)

    await monitor.sync_once()

    assert sink.sent == []
    assert state.primed is True
    assert state.cursor_signature == "old-sig"
    assert state.baseline_mints == {f.SECOND_MINT}
    assert source.calls[0]["sort_order"] == "desc"
    assert monitor.stats.primed is True


async def test_first_sync_falls_back_to_recent_activity_when_holdings_are_unknown(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    source = FakeSource(scan(f.buy_transaction()))
    source.holdings = None
    sink = FakeSink()
    monitor, state, _ = await build_monitor(tmp_path, source, sink, fake_sleeper, primed=False)

    await monitor.sync_once()

    assert state.baseline_mints == {f.TOKEN_MINT}
    assert sink.sent == []


# -- Incremental syncing -----------------------------------------------------
async def test_new_purchase_triggers_exactly_one_alert(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    source = FakeSource(scan(f.buy_transaction(signature="sig-new")))
    sink = FakeSink()
    monitor, state, _ = await build_monitor(tmp_path, source, sink, fake_sleeper)

    await monitor.sync_once()

    assert [alert.mint for alert in sink.sent] == [f.TOKEN_MINT]
    assert state.cursor_signature == "sig-new"
    assert state.alerts_sent == 1
    assert monitor.stats.polls == 1
    assert monitor.stats.alerts_sent == 1
    assert source.calls[0]["sort_order"] == "asc"
    assert source.calls[0]["start_after"] == "cursor-1"


async def test_alerts_carry_resolved_metadata(tmp_path: Path, fake_sleeper: FakeSleeper) -> None:
    resolver = FakeResolver(
        TokenMetadata(
            mint=f.TOKEN_MINT, symbol="USDC", name="USD Coin", decimals=6, is_fungible=True
        )
    )
    source = FakeSource(scan(f.buy_transaction(decimals=None)))
    sink = FakeSink()
    monitor, _, _ = await build_monitor(tmp_path, source, sink, fake_sleeper, resolver=resolver)

    await monitor.sync_once()

    assert resolver.calls == [f.TOKEN_MINT]
    assert sink.sent[0].symbol == "USDC"
    assert sink.sent[0].amount == Decimal(1)


async def test_metadata_is_resolved_only_once_per_mint(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    resolver = FakeResolver(
        TokenMetadata(
            mint=f.TOKEN_MINT, symbol="USDC", name="USD Coin", decimals=6, is_fungible=True
        )
    )
    source = FakeSource(
        scan(f.buy_transaction(signature="sig-1")),
        scan(f.buy_transaction(signature="sig-2")),
    )
    sink = FakeSink()
    monitor, _, _ = await build_monitor(tmp_path, source, sink, fake_sleeper, resolver=resolver)

    await monitor.sync_once()
    await monitor.sync_once()

    # The second purchase is a duplicate, so it is neither alerted nor resolved.
    assert resolver.calls == [f.TOKEN_MINT]
    assert len(sink.sent) == 1


async def test_the_same_purchase_is_never_alerted_twice(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    source = FakeSource(
        scan(f.buy_transaction(signature="sig-1")),
        scan(f.buy_transaction(signature="sig-1")),
    )
    sink = FakeSink()
    monitor, _, _ = await build_monitor(tmp_path, source, sink, fake_sleeper)

    await monitor.sync_once()
    await monitor.sync_once()

    assert len(sink.sent) == 1


async def test_repeat_purchases_can_be_enabled(tmp_path: Path, fake_sleeper: FakeSleeper) -> None:
    source = FakeSource(
        scan(f.buy_transaction(signature="sig-1")),
        scan(f.buy_transaction(signature="sig-2")),
    )
    sink = FakeSink()
    monitor, _, _ = await build_monitor(
        tmp_path,
        source,
        sink,
        fake_sleeper,
        config=MonitorConfig(poll_interval_seconds=10.0, alert_on_repeat=True),
    )

    await monitor.sync_once()
    await monitor.sync_once()

    assert len(sink.sent) == 2


async def test_tokens_held_before_monitoring_never_alert(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    source = FakeSource(scan(f.buy_transaction(signature="sig-new")))
    sink = FakeSink()
    monitor, _, _ = await build_monitor(
        tmp_path, source, sink, fake_sleeper, baseline=(f.TOKEN_MINT,)
    )

    await monitor.sync_once()

    assert sink.sent == []


async def test_already_processed_transactions_are_skipped(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    source = FakeSource(scan(f.buy_transaction(signature="sig-old")))
    sink = FakeSink()
    monitor, state, _ = await build_monitor(tmp_path, source, sink, fake_sleeper)
    state.mark_processed("sig-old")

    await monitor.sync_once()

    assert sink.sent == []


async def test_the_cursor_advances_even_without_acquisitions(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    boring = f.make_transaction(signature="sig-boring")
    source = FakeSource(scan(boring))
    monitor, state, _ = await build_monitor(tmp_path, source, FakeSink(), fake_sleeper)

    await monitor.sync_once()

    assert state.cursor_signature == "sig-boring"
    assert monitor.stats.transactions_scanned == 1


# -- Failure handling --------------------------------------------------------
async def test_notification_errors_do_not_mark_the_mint_as_seen(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    source = FakeSource(
        scan(f.buy_transaction(signature="sig-1")),
        scan(f.buy_transaction(signature="sig-2")),
    )
    sink = FakeSink(error=NotificationError("telegram down", retryable=True))
    monitor, state, _ = await build_monitor(tmp_path, source, sink, fake_sleeper)

    with pytest.raises(NotificationError):
        await monitor.sync_once()

    assert state.is_seen(f.TOKEN_MINT) is False
    assert state.alerts_sent == 0


async def test_unexpected_sink_errors_are_contained(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    source = FakeSource(scan(f.buy_transaction()))
    sink = FakeSink(error=ValueError("boom"))
    monitor, state, _ = await build_monitor(tmp_path, source, sink, fake_sleeper)

    await monitor.sync_once()

    assert monitor.stats.alerts_sent == 0
    assert state.alerts_sent == 0
    assert monitor.stats.consecutive_errors == 0


async def test_source_failures_surface_and_leave_the_cursor_untouched(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    source = FakeSource(HeliusError("boom", retryable=True))
    monitor, state, _ = await build_monitor(tmp_path, source, FakeSink(), fake_sleeper)

    with pytest.raises(HeliusError):
        await monitor.sync_once()

    assert state.cursor_signature == "cursor-1"


async def test_truncated_catch_up_is_logged_but_processed(
    tmp_path: Path, fake_sleeper: FakeSleeper, caplog: pytest.LogCaptureFixture
) -> None:
    source = FakeSource(HistoryScan((f.buy_transaction(signature="sig-1"),), 5, True))
    monitor, _, _ = await build_monitor(tmp_path, source, FakeSink(), fake_sleeper)

    with caplog.at_level("WARNING"):
        await monitor.sync_once()

    assert any("truncated" in record.message for record in caplog.records)


# -- Loop lifecycle ----------------------------------------------------------
async def test_start_and_stop_persist_state(tmp_path: Path, fake_sleeper: FakeSleeper) -> None:
    source = FakeSource(scan(f.buy_transaction(signature="sig-1")))
    monitor, _, _ = await build_monitor(tmp_path, source, FakeSink(), fake_sleeper)

    monitor.start()
    await asyncio.sleep(0)
    running = monitor.is_running
    await monitor.stop()

    assert running is True
    assert monitor.is_running is False
    reloaded = WatchStore(tmp_path / "state.json")
    reloaded.load()
    assert reloaded.require(CHAT_ID).cursor_signature == "sig-1"


async def test_stopping_a_monitor_that_never_ran_is_safe(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    monitor, _, _ = await build_monitor(tmp_path, FakeSource(), FakeSink(), fake_sleeper)

    await monitor.stop()

    assert monitor.is_running is False


async def _run_iterations(monitor: WalletMonitor, iterations: int = 1) -> None:
    """Run ``run_forever`` for a fixed number of cycles, then cancel it."""
    task = asyncio.create_task(monitor.run_forever())
    for _ in range(iterations):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_loop_keeps_polling_after_failures(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    source = FakeSource(
        HeliusError("flaky", retryable=True),
        scan(f.buy_transaction(signature="sig-1")),
    )
    monitor, _, _ = await build_monitor(tmp_path, source, FakeSink(), fake_sleeper)

    await _run_iterations(monitor)

    assert monitor.stats.errors == 1
    assert monitor.stats.consecutive_errors == 1
    assert "HeliusError" in (monitor.stats.last_error or "")


async def test_authentication_failures_back_off_hard(
    tmp_path: Path, fake_sleeper: FakeSleeper
) -> None:
    source = FakeSource(HeliusAuthError(401))
    monitor, _, _ = await build_monitor(tmp_path, source, FakeSink(), fake_sleeper)

    await _run_iterations(monitor)

    assert fake_sleeper.sleeps[0] >= 300.0


async def test_backoff_grows_between_failures(tmp_path: Path, fake_sleeper: FakeSleeper) -> None:
    source = FakeSource(*[HeliusError("flaky", retryable=True) for _ in range(4)])
    monitor, _, _ = await build_monitor(tmp_path, source, FakeSink(), fake_sleeper)

    await _run_iterations(monitor, iterations=3)

    sleeps = fake_sleeper.sleeps
    assert len(sleeps) >= 3
    assert sleeps[0] < sleeps[1] < sleeps[2]


# -- Watch manager -----------------------------------------------------------
def manager_for(store: WatchStore, monitors: list[WalletMonitor], **kwargs: object) -> WatchManager:
    def factory(state: WatchState) -> WalletMonitor:
        monitor = WalletMonitor(
            state=state,
            store=store,
            source=FakeSource(),
            detector=PurchaseDetector(),
            sink=FakeSink(),
            **kwargs,  # type: ignore[arg-type]
        )
        monitors.append(monitor)
        return monitor

    return WatchManager(store, factory)


async def test_manager_starts_and_stops_watches(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "state.json")
    await store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    monitors: list[WalletMonitor] = []
    manager = manager_for(store, monitors)

    await manager.start(CHAT_ID)
    assert manager.is_monitoring(CHAT_ID) is True
    assert store.require(CHAT_ID).monitoring is True

    assert await manager.stop(CHAT_ID) is True
    assert manager.is_monitoring(CHAT_ID) is False
    assert store.require(CHAT_ID).monitoring is False
    assert await manager.stop(CHAT_ID) is False
    assert monitors[0].is_running is False


async def test_manager_start_is_idempotent(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "state.json")
    await store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    monitors: list[WalletMonitor] = []
    manager = manager_for(store, monitors)

    await manager.start(CHAT_ID)
    await manager.start(CHAT_ID)

    assert len(monitors) == 1
    await manager.shutdown()


async def test_manager_resumes_persisted_watches(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "state.json")
    await store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    await store.set_monitoring(CHAT_ID, True)
    monitors: list[WalletMonitor] = []
    manager = manager_for(store, monitors)

    resumed = await manager.resume_all()

    assert resumed == (CHAT_ID,)
    assert manager.is_monitoring(CHAT_ID) is True
    await manager.shutdown()
    assert monitors[0].is_running is False


async def test_manager_status_reports_an_idle_configured_watch(tmp_path: Path) -> None:
    store = WatchStore(tmp_path / "state.json")
    state = await store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    state.prime([], "cursor-1")
    manager = manager_for(store, [])

    status = manager.status(CHAT_ID)

    assert status is not None
    assert status.wallet == f.WATCHED_WALLET
    assert status.monitoring is False
    assert status.primed is True
    assert status.polls == 0


def test_manager_status_is_none_for_unknown_chats(tmp_path: Path) -> None:
    manager = manager_for(WatchStore(tmp_path / "state.json"), [])

    assert manager.status(CHAT_ID) is None


async def test_manager_shutdown_is_idempotent(tmp_path: Path) -> None:
    manager = manager_for(WatchStore(tmp_path / "state.json"), [])

    await manager.shutdown()
    await manager.shutdown()

    assert manager.monitor_for(CHAT_ID) is None

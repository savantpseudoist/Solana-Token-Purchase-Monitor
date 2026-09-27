"""Tests for the framework-free command service."""

from __future__ import annotations

from pathlib import Path

import pytest

from solana_monitor.config import Settings
from solana_monitor.helius.client import HistoryScan
from solana_monitor.monitoring.detector import DetectorPolicy, PurchaseDetector
from solana_monitor.monitoring.manager import WatchManager
from solana_monitor.monitoring.monitor import WalletMonitor
from solana_monitor.monitoring.state import WatchState, WatchStore
from solana_monitor.telegram.authorization import CommandThrottle
from solana_monitor.telegram.commands import CommandService
from solana_monitor.telegram.formatting import Reply
from solana_monitor.telegram.registry import COMMANDS
from tests import factories as f
from tests.conftest import VALID_API_KEY, VALID_BOT_TOKEN, FakeClock

CHAT_ID = -1001234567890
ADMIN_ID = 4242
STRANGER_ID = 9999


class FakeHistory:
    def __init__(self, *scans: HistoryScan) -> None:
        self._scans = list(scans)
        self.calls: list[dict[str, object]] = []

    def queue(self, *scans: HistoryScan) -> None:
        self._scans.extend(scans)

    async def collect_history(self, address: str, **kwargs: object) -> HistoryScan:
        self.calls.append({"address": address, **kwargs})
        if not self._scans:
            return HistoryScan((), 1, False)
        return self._scans.pop(0)


class FakeHoldings:
    def __init__(self, holdings: frozenset[str] | None = frozenset()) -> None:
        self.holdings = holdings
        self.calls: list[str] = []

    async def get_owned_fungible_mints(self, address: str) -> frozenset[str] | None:
        self.calls.append(address)
        return self.holdings


class ServiceHarness:
    """Wires a service over a real store and a manager of stub monitors."""

    def __init__(self, settings: Settings, store: WatchStore, history: FakeHistory) -> None:
        self.settings = settings
        self.store = store
        self.history = history
        self.holdings = FakeHoldings()
        self.started: list[int] = []
        self.stopped: list[int] = []
        self._manager = WatchManager(store, self._build_monitor)
        self.service = CommandService(
            settings=settings,
            store=store,
            manager=self._manager,
            history=history,
            detector=PurchaseDetector(DetectorPolicy()),
            holdings=self.holdings,
            throttle=CommandThrottle(settings.telegram_commands_per_minute, clock=FakeClock()),
        )

    def _build_monitor(self, state: WatchState) -> WalletMonitor:
        monitor = WalletMonitor(
            state=state,
            store=self.store,
            source=self.history,
            detector=PurchaseDetector(DetectorPolicy()),
            sink=_NullSink(),
        )
        original_start = monitor.start
        original_stop = monitor.stop

        def start() -> None:
            self.started.append(state.chat_id)
            original_start()

        async def stop() -> None:
            self.stopped.append(state.chat_id)
            await original_stop()

        monitor.start = start  # type: ignore[method-assign]
        monitor.stop = stop  # type: ignore[method-assign]
        return monitor

    @property
    def manager(self) -> WatchManager:
        return self._manager

    async def run(
        self, command: str, args: tuple[str, ...] = (), *, user_id: int = ADMIN_ID
    ) -> Reply | None:
        return await self.service.execute(command, args, chat_id=CHAT_ID, user_id=user_id)

    async def shutdown(self) -> None:
        await self._manager.shutdown()


class _NullSink:
    async def send(self, acquisition: object) -> None:
        """Stub: commands never deliver alerts."""


def build_settings(**env: str) -> Settings:
    values: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": VALID_BOT_TOKEN,
        "HELIUS_API_KEY": VALID_API_KEY,
        "TELEGRAM_ADMIN_USER_IDS": str(ADMIN_ID),
        **env,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def build_harness(tmp_path: Path, **env: str) -> ServiceHarness:
    return ServiceHarness(
        build_settings(**env),
        WatchStore(tmp_path / "state.json"),
        FakeHistory(),
    )


# -- Authorisation -----------------------------------------------------------
async def test_non_admins_cannot_change_the_watch(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)

    reply = await harness.run("/set_wallet", (f.WATCHED_WALLET,), user_id=STRANGER_ID)

    assert reply is not None
    assert "administrators" in reply.text
    assert harness.store.get(CHAT_ID) is None


async def test_mutating_commands_are_refused_when_no_admin_is_configured(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, TELEGRAM_ADMIN_USER_IDS="")

    reply = await harness.run("/set_wallet", (f.WATCHED_WALLET,), user_id=ADMIN_ID)

    assert reply is not None
    assert "TELEGRAM_ADMIN_USER_IDS" in reply.text
    assert harness.store.get(CHAT_ID) is None


async def test_commands_from_unlisted_chats_are_ignored(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, TELEGRAM_ALLOWED_CHAT_IDS="-100999")

    reply = await harness.service.execute("/status", (), chat_id=CHAT_ID, user_id=ADMIN_ID)

    assert reply is not None
    assert "not configured for this bot" in reply.text


async def test_read_only_commands_work_without_admin_rights(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, TELEGRAM_ADMIN_USER_IDS="")
    await harness.store.set_wallet(CHAT_ID, f.WATCHED_WALLET)

    reply = await harness.run("/status", user_id=STRANGER_ID)

    assert reply is not None
    assert "Watch status" in reply.text


async def test_command_flooding_is_throttled(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, TELEGRAM_COMMANDS_PER_MINUTE="2")

    await harness.run("/help")
    await harness.run("/help")
    reply = await harness.run("/help")

    assert reply is not None
    assert "too quickly" in reply.text


async def test_unknown_commands_are_ignored(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)

    assert await harness.run("/definitely_not_a_command") is None


async def test_every_registered_command_is_implemented(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    args = {"/set_wallet": (f.WATCHED_WALLET,), "/analyze": ("1h",)}
    for spec in COMMANDS:
        reply = await harness.run(f"/{spec.name}", args.get(f"/{spec.name}", ()))
        assert reply is not None, f"{spec.name} has no implementation"
        assert reply.text, f"{spec.name} produced empty output"
    await harness.shutdown()


# -- /set_wallet -------------------------------------------------------------
async def test_set_wallet_validates_and_persists(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    harness.holdings.holdings = frozenset({f.SECOND_MINT})

    reply = await harness.run("/set_wallet", (f.WATCHED_WALLET,))

    assert reply is not None
    assert f.WATCHED_WALLET in reply.text
    assert "1" in reply.text  # one existing holding
    assert harness.store.require(CHAT_ID).wallet == f.WATCHED_WALLET
    await harness.shutdown()


async def test_set_wallet_without_arguments_shows_usage(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)

    reply = await harness.run("/set_wallet")

    assert reply is not None
    assert "Usage" in reply.text


async def test_set_wallet_rejects_invalid_addresses(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)

    reply = await harness.run("/set_wallet", ("not-a-wallet",))

    assert reply is not None
    assert "Invalid Solana address" in reply.text
    assert harness.store.get(CHAT_ID) is None


async def test_set_wallet_restarts_an_active_watch(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await harness.store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    await harness.manager.start(CHAT_ID)

    reply = await harness.run("/set_wallet", (f.OTHER_WALLET,))

    assert reply is not None
    assert "restarted" in reply.text
    assert harness.stopped == [CHAT_ID]
    assert harness.store.require(CHAT_ID).wallet == f.OTHER_WALLET
    await harness.shutdown()


# -- /start_monitoring and /stop_monitoring ----------------------------------
async def test_start_monitoring_requires_a_wallet(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)

    reply = await harness.run("/start_monitoring")

    assert reply is not None
    assert "/set_wallet" in reply.text
    assert harness.started == []


async def test_start_monitoring_starts_the_monitor(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await harness.store.set_wallet(CHAT_ID, f.WATCHED_WALLET)

    reply = await harness.run("/start_monitoring")

    assert reply is not None
    assert "Monitoring started" in reply.text
    assert harness.started == [CHAT_ID]

    repeat = await harness.run("/start_monitoring")
    assert repeat is not None
    assert "already active" in repeat.text
    await harness.shutdown()


async def test_stop_monitoring_is_idempotent(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await harness.store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    await harness.manager.start(CHAT_ID)

    stopped = await harness.run("/stop_monitoring")
    again = await harness.run("/stop_monitoring")

    assert stopped is not None
    assert "stopped" in stopped.text
    assert again is not None
    assert "not active" in again.text


# -- /analyze ----------------------------------------------------------------
async def test_analyze_requires_a_wallet(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)

    reply = await harness.run("/analyze", ("1h",))

    assert reply is not None
    assert "/set_wallet" in reply.text


@pytest.mark.parametrize("args", [(), ("2h",), ("yesterday",)])
async def test_analyze_rejects_unknown_periods(tmp_path: Path, args: tuple[str, ...]) -> None:
    harness = build_harness(tmp_path)
    await harness.store.set_wallet(CHAT_ID, f.WATCHED_WALLET)

    reply = await harness.run("/analyze", args)

    assert reply is not None
    assert "Choose a time period" in reply.text


async def test_analyze_uses_a_server_side_time_window(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await harness.store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    harness.history.queue(HistoryScan((f.buy_transaction(),), 1, False))

    reply = await harness.run("/analyze", ("1d",))

    assert reply is not None
    call = harness.history.calls[0]
    assert call["sort_order"] == "desc"
    assert isinstance(call["gte_time"], int)
    assert "Transactions scanned" in reply.text


async def test_analyze_deduplicates_repeat_purchases_of_one_mint(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await harness.store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    harness.history.queue(
        HistoryScan(
            (
                f.buy_transaction(signature="sig-1"),
                f.buy_transaction(signature="sig-2", amount=2_000_000),
            ),
            2,
            False,
        )
    )

    reply = await harness.run("/analyze", ("1h",))

    assert reply is not None
    assert "Unique tokens:</b> 1" in reply.text


async def test_analyze_reports_truncation(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await harness.store.set_wallet(CHAT_ID, f.WATCHED_WALLET)
    harness.history.queue(HistoryScan((f.buy_transaction(),), 10, True))

    reply = await harness.run("/analyze", ("1w",))

    assert reply is not None
    assert "truncated" in reply.text


async def test_analyze_handles_an_empty_window(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)
    await harness.store.set_wallet(CHAT_ID, f.WATCHED_WALLET)

    reply = await harness.run("/analyze", ("1h",))

    assert reply is not None
    assert "No new tokens" in reply.text


# -- /whoami and /help -------------------------------------------------------
async def test_whoami_reports_both_identifiers(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)

    reply = await harness.run("/whoami")

    assert reply is not None
    assert str(ADMIN_ID) in reply.text
    assert str(CHAT_ID) in reply.text


async def test_help_lists_commands_and_marks_admins(tmp_path: Path) -> None:
    harness = build_harness(tmp_path)

    reply = await harness.run("/help")

    assert reply is not None
    assert "/set_wallet" in reply.text
    assert "admin only" in reply.text


async def test_help_warns_when_no_admins_are_configured(tmp_path: Path) -> None:
    harness = build_harness(tmp_path, TELEGRAM_ADMIN_USER_IDS="")

    reply = await harness.run("/start")

    assert reply is not None
    assert "No administrators are configured" in reply.text

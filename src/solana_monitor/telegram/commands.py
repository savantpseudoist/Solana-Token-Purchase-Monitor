"""Framework-free command logic.

Everything here takes primitives (``chat_id``, ``user_id``, arguments) and
returns a :class:`~solana_monitor.telegram.formatting.Reply`, so the behaviour
that matters (authorisation, validation, analysis windows) is testable without a
Telegram connection.  The handlers in :mod:`solana_monitor.telegram.app` are thin
adapters.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

from solana_monitor.config import Settings
from solana_monitor.domain.addresses import validate_address
from solana_monitor.domain.errors import InvalidAddressError, MonitorError
from solana_monitor.domain.models import AnalysisResult, TokenAcquisition
from solana_monitor.helius.client import HistoryScan, SortOrder
from solana_monitor.monitoring.detector import DetectorPolicy, PurchaseDetector
from solana_monitor.monitoring.manager import WatchManager
from solana_monitor.monitoring.state import WatchState, WatchStore
from solana_monitor.telegram import formatting
from solana_monitor.telegram.authorization import CommandAuthorization, CommandThrottle
from solana_monitor.telegram.formatting import Reply
from solana_monitor.telegram.registry import COMMANDS, COMMANDS_BY_NAME

logger = logging.getLogger(__name__)

PERIODS: Final[dict[str, timedelta]] = {
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
}
_USAGE_HINT: Final = "Usage: {usage}"
_NO_WALLET_HINT: Final = "No wallet configured yet. Send /set_wallet <solana_address> first."


class HistoryReader(Protocol):
    """The part of the Helius client ``/analyze`` needs."""

    async def collect_history(
        self,
        address: str,
        *,
        gte_time: int | None = ...,
        sort_order: SortOrder = ...,
        start_after: str | None = ...,
        start_before: str | None = ...,
        page_size: int | None = ...,
        max_pages: int | None = ...,
    ) -> HistoryScan: ...


class HoldingsReader(Protocol):
    async def get_owned_fungible_mints(self, address: str) -> frozenset[str] | None: ...


class CommandService:
    """Executes bot commands for chats and users."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: WatchStore,
        manager: WatchManager,
        history: HistoryReader,
        detector: PurchaseDetector | None = None,
        holdings: HoldingsReader | None = None,
        authorization: CommandAuthorization | None = None,
        throttle: CommandThrottle | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._manager = manager
        self._history = history
        self._detector = detector or PurchaseDetector(DetectorPolicy.from_settings(settings))
        self._holdings = holdings
        self._authorization = authorization or CommandAuthorization.from_settings(settings)
        self._throttle = throttle or CommandThrottle(settings.telegram_commands_per_minute)

    @property
    def authorization(self) -> CommandAuthorization:
        return self._authorization

    async def execute(
        self, command: str, args: Sequence[str], *, chat_id: int, user_id: int
    ) -> Reply | None:
        """Run one command; ``None`` means "ignore this message"."""
        name = command.lstrip("/").lower()
        spec = COMMANDS_BY_NAME.get(name)
        if spec is None:
            return None
        try:
            self._authorization.check_chat(chat_id)
            self._authorization.check_command(
                user_id=user_id, command=name, requires_admin=spec.requires_admin
            )
        except MonitorError as error:
            return formatting.build_error(str(error))
        if not self._throttle.allow(user_id):
            return formatting.build_error(self._throttle.retry_hint())
        return await self._dispatch(name, args, chat_id=chat_id, user_id=user_id)

    async def _dispatch(
        self, name: str, args: Sequence[str], *, chat_id: int, user_id: int
    ) -> Reply:
        """Route to the implementation; every registry entry has exactly one."""
        match name:
            case "set_wallet":
                return await self._set_wallet(args, chat_id=chat_id)
            case "start_monitoring":
                return await self._start_monitoring(chat_id)
            case "stop_monitoring":
                return await self._stop_monitoring(chat_id)
            case "status":
                return await self._status(chat_id)
            case "analyze":
                return await self._analyze_command(args, chat_id=chat_id)
            case "whoami":
                return self._whoami(chat_id=chat_id, user_id=user_id)
            case "help" | "start":
                return self._help()
            case _:  # pragma: no cover - registry and service are in sync
                logger.error("command has no implementation", extra={"command": name})
                return formatting.build_error("This command is not available.")

    # -- Individual commands -------------------------------------------------
    async def _set_wallet(self, args: Sequence[str], *, chat_id: int) -> Reply:
        if not args:
            usage = _USAGE_HINT.format(usage="/set_wallet <solana_address>")
            return formatting.build_error(usage)
        try:
            wallet = validate_address(args[0], field="wallet address")
        except InvalidAddressError as error:
            return formatting.build_error(str(error))

        was_monitoring = self._manager.is_monitoring(chat_id)
        await self._store.set_wallet(chat_id, wallet)
        holdings = await self._holdings_count(wallet)
        if was_monitoring:
            await self._manager.restart(chat_id)
        return formatting.build_wallet_set(wallet, holdings=holdings, was_monitoring=was_monitoring)

    async def _start_monitoring(self, chat_id: int) -> Reply:
        state = self._store.get(chat_id)
        if state is None:
            return formatting.build_error(_NO_WALLET_HINT)
        if self._manager.is_monitoring(chat_id):
            return formatting.build_error("Monitoring is already active.")
        await self._manager.start(chat_id)
        holdings = await self._holdings_count(state.wallet)
        return formatting.build_monitoring_started(state.wallet, holdings=holdings)

    async def _stop_monitoring(self, chat_id: int) -> Reply:
        if not self._manager.is_monitoring(chat_id):
            return formatting.build_error("Monitoring is not active.")
        await self._manager.stop(chat_id)
        return formatting.build_monitoring_stopped()

    async def _status(self, chat_id: int) -> Reply:
        if self._store.get(chat_id) is None:
            return formatting.build_error(_NO_WALLET_HINT)
        status = self._manager.status(chat_id)
        if status is None:  # pragma: no cover - store and manager are kept in sync
            return formatting.build_error("No watch configured for this chat.")
        return formatting.build_status(status, self._settings)

    async def _analyze_command(self, args: Sequence[str], *, chat_id: int) -> Reply:
        state = self._store.get(chat_id)
        if state is None:
            return formatting.build_error(_NO_WALLET_HINT)
        period = args[0].lower() if args else ""
        window = PERIODS.get(period)
        if window is None:
            options = ", ".join(sorted(PERIODS))
            return formatting.build_error(f"Choose a time period: {options}.")
        result = await self._analyze(state, period=period, window=window)
        return formatting.build_analysis(result)

    def _whoami(self, *, chat_id: int, user_id: int) -> Reply:
        return formatting.build_whoami(user_id, chat_id)

    def _help(self) -> Reply:
        return formatting.build_help(
            COMMANDS, admins_configured=self._authorization.admins_configured
        )

    # -- Helpers -------------------------------------------------------------
    async def _holdings_count(self, wallet: str) -> int | None:
        """Number of currently held fungible tokens, or ``None`` if unknown."""
        if self._holdings is None:
            return None
        holdings = await self._holdings.get_owned_fungible_mints(wallet)
        return None if holdings is None else len(holdings)

    async def _analyze(
        self, state: WatchState, *, period: str, window: timedelta
    ) -> AnalysisResult:
        now = datetime.now(UTC)
        window_start = now - window
        scan = await self._history.collect_history(
            state.wallet,
            gte_time=int(window_start.timestamp()),
            sort_order="desc",
            page_size=self._settings.helius_page_size,
            max_pages=self._settings.analyze_max_pages,
        )
        acquisitions = [
            acquisition
            for transaction in scan.transactions
            for acquisition in self._detector.detect(transaction, state.wallet)
        ]
        unique = self._deduplicate(acquisitions)
        return AnalysisResult(
            wallet=state.wallet,
            period=period,
            window_start=window_start,
            generated_at=now,
            acquisitions=tuple(unique[: self._settings.analyze_max_tokens_listed]),
            transactions_scanned=len(scan),
            truncated=scan.truncated,
            unique_mints=len(unique),
        )

    @staticmethod
    def _deduplicate(acquisitions: Sequence[TokenAcquisition]) -> list[TokenAcquisition]:
        """Keep the earliest acquisition of each mint, newest first overall.

        The old implementation stored ``(mint, symbol, timestamp)`` tuples in a
        set, so buying the same token twice counted as two different tokens and
        the same purchase could be listed several times.
        """
        earliest: dict[str, TokenAcquisition] = {}
        for acquisition in acquisitions:
            known = earliest.get(acquisition.mint)
            if known is None or _timestamp_of(acquisition) < _timestamp_of(known):
                earliest[acquisition.mint] = acquisition
        return sorted(earliest.values(), key=_timestamp_of, reverse=True)


def _timestamp_of(acquisition: TokenAcquisition) -> float:
    return acquisition.timestamp.timestamp() if acquisition.timestamp else 0.0

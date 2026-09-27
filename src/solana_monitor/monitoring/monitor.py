"""The polling loop for one watched wallet.

The loop is a plain ``asyncio`` task with no framework dependency, so it can be
driven directly in tests.  Its contract:

* the first sync establishes a *baseline* (what the wallet already holds) and a
  cursor, so starting the bot never floods the chat with historical activity;
* later syncs fetch only what is newer than the cursor, in ascending order, and
  advance the cursor transaction by transaction so a mid-sync failure cannot skip
  activity;
* failures back off exponentially (and authentication failures back off hard
  because retrying a bad API key quickly is pointless);
* the task is cancellable and always leaves the state file consistent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol

from solana_monitor.config import Settings
from solana_monitor.domain.errors import HeliusAuthError, MonitorError, NotificationError
from solana_monitor.domain.models import (
    TokenAcquisition,
    TokenMetadata,
    Transaction,
    WatchStatusView,
)
from solana_monitor.helius.client import HistoryScan, SortOrder
from solana_monitor.monitoring.detector import PurchaseDetector
from solana_monitor.monitoring.state import WatchState, WatchStore
from solana_monitor.resilience import compute_backoff

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]

_AUTH_BACKOFF_SECONDS: Final = 300.0


class TransactionSource(Protocol):
    """The part of :class:`~solana_monitor.helius.client.HeliusClient` we need."""

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


class AlertSink(Protocol):
    """Where an alert goes (implemented by the Telegram layer)."""

    async def send(self, acquisition: TokenAcquisition) -> None: ...


class MetadataSource(Protocol):
    async def resolve(self, mint: str) -> TokenMetadata | None: ...


@dataclass(slots=True)
class MonitorStats:
    """Counters exposed to ``/status`` and to structured logs."""

    polls: int = 0
    transactions_scanned: int = 0
    acquisitions_detected: int = 0
    alerts_sent: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    primed: bool = False
    last_poll_at: datetime | None = None
    last_alert_at: datetime | None = None
    last_error: str | None = None
    started_at: datetime | None = field(default=None)

    def snapshot(self) -> dict[str, object]:
        """Structured-log friendly view of the counters."""
        return {
            "polls": self.polls,
            "transactions_scanned": self.transactions_scanned,
            "acquisitions_detected": self.acquisitions_detected,
            "alerts_sent": self.alerts_sent,
            "errors": self.errors,
            "consecutive_errors": self.consecutive_errors,
            "primed": self.primed,
            "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
            "last_alert_at": self.last_alert_at.isoformat() if self.last_alert_at else None,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class MonitorConfig:
    """Everything the loop needs that is not a collaborator."""

    poll_interval_seconds: float = 20.0
    max_backoff_seconds: float = 300.0
    max_catchup_pages: int = 10
    page_size: int = 100
    alert_on_repeat: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> MonitorConfig:
        return cls(
            poll_interval_seconds=settings.monitor_poll_interval_seconds,
            max_backoff_seconds=settings.monitor_max_backoff_seconds,
            max_catchup_pages=settings.monitor_max_catchup_pages,
            page_size=settings.helius_page_size,
            alert_on_repeat=settings.monitor_alert_on_repeat,
        )


class HoldingsSource(Protocol):
    async def get_owned_fungible_mints(self, address: str) -> frozenset[str] | None: ...


class WalletMonitor:
    """Polls one wallet and emits alerts for new acquisitions."""

    def __init__(
        self,
        *,
        state: WatchState,
        store: WatchStore,
        source: TransactionSource,
        detector: PurchaseDetector,
        sink: AlertSink,
        holdings: HoldingsSource | None = None,
        resolver: MetadataSource | None = None,
        config: MonitorConfig | None = None,
        sleep: Sleeper = asyncio.sleep,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._state = state
        self._store = store
        self._source = source
        self._detector = detector
        self._sink = sink
        self._holdings = holdings
        self._resolver = resolver
        self._config = config or MonitorConfig()
        self._sleep = sleep
        self._clock = clock
        self._stats = MonitorStats(started_at=clock())
        self._task: asyncio.Task[None] | None = None

    # -- Lifecycle ----------------------------------------------------------
    @property
    def chat_id(self) -> int:
        return self._state.chat_id

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def stats(self) -> MonitorStats:
        return self._stats

    def start(self) -> None:
        if self.is_running:
            return
        self._task = asyncio.create_task(self.run_forever(), name=f"monitor-{self._state.chat_id}")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._store.save()

    async def run_forever(self) -> None:
        logger.info(
            "monitoring started",
            extra={"chat_id": self._state.chat_id, "wallet": self._state.wallet},
        )
        try:
            while True:
                delay = self._config.poll_interval_seconds
                try:
                    await self.sync_once()
                    self._stats.consecutive_errors = 0
                except asyncio.CancelledError:
                    raise
                except HeliusAuthError as error:
                    delay = self._record_error(error, backoff_to=_AUTH_BACKOFF_SECONDS)
                    logger.error(
                        "helius rejected our credentials; polling will back off",
                        extra={"chat_id": self._state.chat_id, "error": str(error)},
                    )
                except MonitorError as error:
                    delay = self._record_error(error)
                    logger.warning(
                        "monitoring sync failed, will retry",
                        extra={"chat_id": self._state.chat_id, "error": str(error)},
                    )
                await self._sleep(delay)
        except asyncio.CancelledError:
            logger.info("monitoring stopped", extra={"chat_id": self._state.chat_id})
            raise

    # -- Synchronisation ----------------------------------------------------
    async def sync_once(self) -> None:
        """Run a single synchronisation pass (also used directly by tests)."""
        state = self._state
        self._stats.polls += 1
        self._stats.last_poll_at = self._clock()

        if state.cursor_signature is None:
            await self._prime()
        else:
            await self._consume_new_activity()

        await self._store.save()

    async def _prime(self) -> None:
        """Establish the baseline without alerting, then remember the cursor."""
        state = self._state
        scan = await self._source.collect_history(
            state.wallet,
            sort_order="desc",
            page_size=self._config.page_size,
            max_pages=1,
        )
        holdings = await self._load_holdings()
        recent_mints = {
            mint
            for transaction in scan.transactions
            for mint in transaction.net_token_changes(state.wallet)
        }
        baseline = holdings if holdings is not None else recent_mints
        cursor = scan.newest_signature
        state.prime(baseline, cursor)
        self._stats.primed = True
        self._stats.transactions_scanned += len(scan)
        logger.info(
            "watch primed",
            extra={
                "chat_id": state.chat_id,
                "baseline_size": len(baseline),
                "holdings_known": holdings is not None,
                "cursor": cursor,
            },
        )

    async def _load_holdings(self) -> frozenset[str] | None:
        """Current holdings, or ``None`` when unknown (no source, or the API failed)."""
        if self._holdings is None:
            return None
        return await self._holdings.get_owned_fungible_mints(self._state.wallet)

    async def _consume_new_activity(self) -> None:
        """Fetch everything newer than the cursor and alert on new acquisitions.

        The cursor only advances past a transaction once its alerts have been
        delivered, so a Telegram outage makes the next pass retry that transaction
        (at-least-once delivery) instead of silently skipping the purchase.
        """
        state = self._state
        scan = await self._source.collect_history(
            state.wallet,
            sort_order="asc",
            start_after=state.cursor_signature,
            page_size=self._config.page_size,
            max_pages=self._config.max_catchup_pages,
        )
        self._stats.transactions_scanned += len(scan)
        alerted = 0
        for transaction in scan.transactions:
            if state.is_processed(transaction.signature):
                continue
            try:
                alerted += await self._handle_transaction(transaction)
            except MonitorError:
                # Persist what we did complete, then let the loop back off and
                # retry from this transaction on the next pass.
                await self._store.save()
                raise
            state.mark_processed(transaction.signature)
            state.cursor_signature = transaction.signature
        if scan.truncated:
            logger.warning(
                "catch-up truncated; remaining activity is picked up next poll",
                extra={"chat_id": state.chat_id, "pages": scan.pages_fetched},
            )
        if scan.transactions:
            logger.info(
                "sync complete",
                extra={
                    "chat_id": state.chat_id,
                    "transactions": len(scan),
                    "alerts": alerted,
                    "cursor": state.cursor_signature,
                    **self._stats.snapshot(),
                },
            )

    async def _handle_transaction(self, transaction: Transaction) -> int:
        acquisitions = self._detector.detect(transaction, self._state.wallet)
        if not acquisitions:
            return 0
        self._stats.acquisitions_detected += len(acquisitions)
        alerts = 0
        for acquisition in acquisitions:
            if not self._should_alert(acquisition):
                continue
            await self._emit(acquisition)
            alerts += 1
        return alerts

    def _should_alert(self, acquisition: TokenAcquisition) -> bool:
        """Check duplicate suppression *without* recording the mint yet.

        The mint is only recorded once the alert has been delivered, so a failed
        delivery is retried on the next pass instead of being lost silently.
        """
        if self._config.alert_on_repeat:
            return True
        return not self._state.is_seen(acquisition.mint)

    async def _emit(self, acquisition: TokenAcquisition) -> None:
        metadata = await self._resolve(acquisition)
        try:
            await self._sink.send(acquisition.with_metadata(metadata))
        except NotificationError:
            # The mint stays unseen so the next pass retries the alert.
            raise
        except Exception:
            logger.exception(
                "alert delivery failed with an unexpected error",
                extra={"chat_id": self._state.chat_id, "mint": acquisition.mint},
            )
            return
        self._state.alerts_sent += 1
        self._stats.alerts_sent += 1
        self._stats.last_alert_at = self._clock()
        self._state.mark_seen(acquisition.mint)

    async def _resolve(self, acquisition: TokenAcquisition) -> TokenMetadata | None:
        """Look up metadata only when the transaction did not provide it."""
        if self._resolver is None:
            return None
        if acquisition.symbol and acquisition.decimals is not None:
            return None
        return await self._resolver.resolve(acquisition.mint)

    # -- Diagnostics --------------------------------------------------------
    def status(self) -> WatchStatusView:
        return WatchStatusView(
            chat_id=self._state.chat_id,
            wallet=self._state.wallet,
            monitoring=self.is_running,
            primed=self._stats.primed,
            alerts_sent=self._state.alerts_sent,
            acquisitions_detected=self._stats.acquisitions_detected,
            transactions_scanned=self._stats.transactions_scanned,
            polls=self._stats.polls,
            consecutive_errors=self._stats.consecutive_errors,
            last_poll_at=self._stats.last_poll_at,
            last_alert_at=self._stats.last_alert_at,
            last_error=self._stats.last_error,
            poll_interval_seconds=self._config.poll_interval_seconds,
            baseline_size=len(self._state.baseline_mints),
            started_at=self._stats.started_at,
        )

    def _record_error(self, error: BaseException, *, backoff_to: float | None = None) -> float:
        self._stats.errors += 1
        self._stats.consecutive_errors += 1
        self._stats.last_error = f"{type(error).__name__}: {error}"
        delay = compute_backoff(
            self._stats.consecutive_errors,
            base=self._config.poll_interval_seconds,
            maximum=self._config.max_backoff_seconds,
        )
        return max(delay, backoff_to or 0.0)

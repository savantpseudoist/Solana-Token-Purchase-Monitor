"""Ownership of running monitors, one per chat.

The manager is the only place that starts and stops :class:`WalletMonitor`
instances, which keeps the "is monitoring" flag in one place instead of being
mutated from Telegram handlers, background tasks and shutdown code at once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace

from solana_monitor.domain.models import WatchStatusView
from solana_monitor.monitoring.monitor import WalletMonitor
from solana_monitor.monitoring.state import WatchState, WatchStore

logger = logging.getLogger(__name__)

MonitorFactory = Callable[[WatchState], WalletMonitor]


class WatchManager:
    def __init__(self, store: WatchStore, monitor_factory: MonitorFactory) -> None:
        self._store = store
        self._factory = monitor_factory
        self._monitors: dict[int, WalletMonitor] = {}

    @property
    def store(self) -> WatchStore:
        return self._store

    def monitor_for(self, chat_id: int) -> WalletMonitor | None:
        return self._monitors.get(chat_id)

    def is_monitoring(self, chat_id: int) -> bool:
        monitor = self._monitors.get(chat_id)
        return monitor is not None and monitor.is_running

    def status(self, chat_id: int) -> WatchStatusView | None:
        monitor = self._monitors.get(chat_id)
        if monitor is not None:
            return monitor.status()
        state = self._store.get(chat_id)
        if state is None:
            return None
        return replace(
            WatchStatusView(
                chat_id=chat_id,
                wallet=state.wallet,
                monitoring=state.monitoring and monitor is not None,
                primed=state.primed,
                alerts_sent=state.alerts_sent,
                acquisitions_detected=0,
                transactions_scanned=0,
                polls=0,
                consecutive_errors=0,
                last_poll_at=None,
                last_alert_at=None,
                last_error=None,
                poll_interval_seconds=0.0,
                baseline_size=len(state.baseline_mints),
                started_at=None,
            )
        )

    async def start(self, chat_id: int) -> WatchState:
        """Start (or return) the monitor for ``chat_id``."""
        state = self._store.require(chat_id)
        existing = self._monitors.get(chat_id)
        if existing is not None and existing.is_running:
            return state
        monitor = self._factory(state)
        self._monitors[chat_id] = monitor
        monitor.start()
        if not state.monitoring:
            await self._store.set_monitoring(chat_id, True)
        logger.info("watch started", extra={"chat_id": chat_id, "wallet": state.wallet})
        return state

    async def stop(self, chat_id: int) -> bool:
        """Stop the monitor for ``chat_id``; ``False`` when none was running."""
        monitor = self._monitors.pop(chat_id, None)
        if monitor is None:
            if self._store.get(chat_id) is not None:
                await self._store.set_monitoring(chat_id, False)
            return False
        await monitor.stop()
        if self._store.get(chat_id) is not None:
            await self._store.set_monitoring(chat_id, False)
        logger.info("watch stopped", extra={"chat_id": chat_id})
        return True

    async def restart(self, chat_id: int) -> bool:
        """Stop then start, e.g. after the watched wallet changed."""
        was_running = await self.stop(chat_id)
        if self._store.get(chat_id) is None:
            return False
        await self.start(chat_id)
        return was_running

    async def resume_all(self) -> tuple[int, ...]:
        """Restart every watch that was monitoring when the process last exited."""
        resumed: list[int] = []
        for chat_id in self._store.monitoring_chat_ids:
            try:
                await self.start(chat_id)
            except Exception:
                logger.exception("could not resume watch", extra={"chat_id": chat_id})
                continue
            resumed.append(chat_id)
        if resumed:
            logger.info("resumed watches after restart", extra={"chats": resumed})
        return tuple(resumed)

    async def shutdown(self) -> None:
        """Stop every monitor and flush state."""
        chat_ids = list(self._monitors)
        for chat_id in chat_ids:
            monitor = self._monitors.pop(chat_id, None)
            if monitor is not None:
                await monitor.stop()
        await self._store.save()

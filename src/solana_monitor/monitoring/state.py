"""Persistent watch state: what has been alerted, and the polling cursor.

The original bot kept this in unbounded in-memory sets, so every restart
re-alerted the wallet's whole recent history and memory grew forever.  State is
now:

* **bounded** (FIFO caps on seen mints and processed signatures);
* **durable** (atomic JSON writes, so a crash cannot leave a truncated file);
* **per chat**, removing the global mutable state that made concurrent chats
  impossible to reason about.

Duplicate-suppression policy (also documented in the README):

* the first acquisition of a mint is always alerted;
* tokens already held when monitoring starts never alert (they are baseline);
* a re-purchase of an already-alerted mint is suppressed unless
  ``MONITOR_ALERT_ON_REPEAT`` is enabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from solana_monitor.domain.errors import StateError

logger = logging.getLogger(__name__)

STATE_VERSION: Final = 1


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BoundedOrderedSet:
    """A set that keeps insertion order and evicts the oldest entries when full."""

    __slots__ = ("_items", "_max_size", "_order")

    def __init__(self, max_size: int) -> None:
        if max_size < 1:
            msg = "max_size must be positive"
            raise ValueError(msg)
        self._max_size = max_size
        self._items: set[str] = set()
        self._order: list[str] = []

    def add(self, value: str) -> bool:
        """Insert ``value``; return ``True`` when it was not present before."""
        if value in self._items:
            return False
        self._items.add(value)
        self._order.append(value)
        while len(self._order) > self._max_size:
            self._items.discard(self._order.pop(0))
        return True

    def extend(self, values: Iterable[str]) -> None:
        for value in values:
            self.add(value)

    def clear(self) -> None:
        self._items.clear()
        self._order.clear()

    def to_list(self) -> list[str]:
        return list(self._order)

    def __contains__(self, value: object) -> bool:
        return value in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[str]:
        return iter(self._order)


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return _utc_now()
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return _utc_now()


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


@dataclass(slots=True)
class WatchState:
    """Everything remembered about one chat's watched wallet."""

    chat_id: int
    wallet: str
    monitoring: bool = False
    primed: bool = False
    cursor_signature: str | None = None
    seen_mints: BoundedOrderedSet = field(default_factory=lambda: BoundedOrderedSet(5000))
    processed_signatures: BoundedOrderedSet = field(default_factory=lambda: BoundedOrderedSet(2000))
    baseline_mints: set[str] = field(default_factory=set)
    alerts_sent: int = 0
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)

    def mark_seen(self, mint: str) -> bool:
        """Record a mint; return ``True`` the first time it is seen."""
        self.updated_at = _utc_now()
        return self.seen_mints.add(mint)

    def is_seen(self, mint: str) -> bool:
        return mint in self.seen_mints

    def mark_processed(self, signature: str) -> bool:
        """Record a signature; ``False`` means it had already been processed."""
        self.updated_at = _utc_now()
        return self.processed_signatures.add(signature)

    def is_processed(self, signature: str) -> bool:
        return signature in self.processed_signatures

    def prime(self, mints: Iterable[str], cursor_signature: str | None) -> None:
        """Establish a baseline of pre-existing holdings, without alerting."""
        for mint in mints:
            self.baseline_mints.add(mint)
            self.seen_mints.add(mint)
        self.cursor_signature = cursor_signature
        self.primed = True
        self.updated_at = _utc_now()

    def reset_for_wallet(self, wallet: str, now: datetime | None = None) -> None:
        """Point this watch at a new wallet, forgetting per-wallet history."""
        stamp = now or _utc_now()
        self.wallet = wallet
        self.monitoring = False
        self.primed = False
        self.cursor_signature = None
        self.seen_mints.clear()
        self.processed_signatures.clear()
        self.baseline_mints.clear()
        self.alerts_sent = 0
        self.updated_at = stamp

    # -- Serialisation ------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "wallet": self.wallet,
            "monitoring": self.monitoring,
            "primed": self.primed,
            "cursor_signature": self.cursor_signature,
            "seen_mints": self.seen_mints.to_list(),
            "processed_signatures": self.processed_signatures.to_list(),
            "baseline_mints": sorted(self.baseline_mints),
            "alerts_sent": self.alerts_sent,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, max_seen: int, max_processed: int) -> WatchState:
        state = cls(
            chat_id=int(data["chat_id"]),
            wallet=str(data["wallet"]),
            monitoring=bool(data.get("monitoring", False)),
            primed=bool(data.get("primed", False)),
            cursor_signature=_optional_str(data.get("cursor_signature")),
            seen_mints=BoundedOrderedSet(max_seen),
            processed_signatures=BoundedOrderedSet(max_processed),
            baseline_mints=set(_string_list(data.get("baseline_mints"))),
            alerts_sent=int(data.get("alerts_sent", 0)),
            created_at=_parse_datetime(data.get("created_at")),
            updated_at=_parse_datetime(data.get("updated_at")),
        )
        state.seen_mints.extend(_string_list(data.get("seen_mints")))
        state.processed_signatures.extend(_string_list(data.get("processed_signatures")))
        return state


class WatchStore:
    """Loads, owns and durably persists the :class:`WatchState` collection."""

    def __init__(
        self,
        path: Path,
        *,
        max_seen_mints: int = 5000,
        max_processed_signatures: int = 2000,
    ) -> None:
        self._path = path
        self._max_seen = max_seen_mints
        self._max_processed = max_processed_signatures
        self._states: dict[int, WatchState] = {}
        self._save_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        """Read the state file, tolerating a missing or corrupt file."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.info("no state file yet, starting fresh", extra={"path": str(self._path)})
            return
        except OSError as error:
            detail = f"cannot read state file: {error}"
            raise StateError(detail, context={"path": str(self._path)}) from error

        try:
            document = json.loads(raw)
            self._states = self._parse_document(document)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
            self._quarantine(str(error))
            self._states = {}
            return
        logger.info(
            "loaded watch state", extra={"path": str(self._path), "watches": len(self._states)}
        )

    def _parse_document(self, document: object) -> dict[int, WatchState]:
        if not isinstance(document, dict):
            msg = "state file must contain a JSON object"
            raise TypeError(msg)
        watches = document.get("watches")
        if not isinstance(watches, list):
            msg = "state file is missing the 'watches' list"
            raise TypeError(msg)
        states: dict[int, WatchState] = {}
        for entry in watches:
            if not isinstance(entry, dict):
                continue
            try:
                state = WatchState.from_dict(
                    entry, max_seen=self._max_seen, max_processed=self._max_processed
                )
            except (TypeError, ValueError, KeyError) as error:
                logger.warning("skipping malformed watch entry: %s", error)
                continue
            states[state.chat_id] = state
        return states

    def _quarantine(self, reason: str) -> None:
        source = self._path
        backup = self._path.with_suffix(f"{self._path.suffix}.corrupt")
        try:
            backup = source.replace(backup)
        except OSError:  # pragma: no cover - best effort only
            backup = source
        logger.error(
            "state file unreadable, moved aside and starting fresh",
            extra={"path": str(self._path), "backup": str(backup), "reason": reason},
        )

    # -- Queries ------------------------------------------------------------
    def get(self, chat_id: int) -> WatchState | None:
        return self._states.get(chat_id)

    def require(self, chat_id: int) -> WatchState:
        state = self._states.get(chat_id)
        if state is None:
            detail = "no wallet configured for this chat"
            raise StateError(detail, context={"chat_id": chat_id})
        return state

    def all(self) -> tuple[WatchState, ...]:
        return tuple(self._states.values())

    @property
    def monitoring_chat_ids(self) -> tuple[int, ...]:
        return tuple(chat_id for chat_id, state in self._states.items() if state.monitoring)

    # -- Mutations (all persisted) ------------------------------------------
    async def set_wallet(
        self, chat_id: int, wallet: str, *, now: datetime | None = None
    ) -> WatchState:
        state = self._states.get(chat_id)
        if state is None:
            state = WatchState(chat_id=chat_id, wallet=wallet, created_at=now or _utc_now())
            self._states[chat_id] = state
        else:
            state.reset_for_wallet(wallet, now)
        await self.save()
        return state

    async def set_monitoring(self, chat_id: int, monitoring: bool) -> WatchState:
        state = self.require(chat_id)
        state.monitoring = monitoring
        state.updated_at = _utc_now()
        await self.save()
        return state

    async def remove(self, chat_id: int) -> bool:
        removed = self._states.pop(chat_id, None) is not None
        if removed:
            await self.save()
        return removed

    async def save(self) -> None:
        """Write the whole state file atomically."""
        async with self._save_lock:
            payload = {
                "version": STATE_VERSION,
                "watches": [state.to_dict() for state in self._states.values()],
            }
            serialised = json.dumps(payload, indent=2, sort_keys=True)
            await asyncio.to_thread(self._write_atomic, serialised)

    def _write_atomic(self, serialised: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(serialised)
                    stream.flush()
                    os.fsync(stream.fileno())
                Path(temporary).replace(self._path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        except OSError as error:
            detail = f"cannot write state file: {error}"
            raise StateError(detail, context={"path": str(self._path)}) from error

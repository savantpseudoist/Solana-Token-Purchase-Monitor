"""Cached, best-effort token metadata lookups.

Token symbols and decimals are only needed for human-readable alerts.  Resolving
them costs DAS credits, so results (including "unknown") are cached with a TTL and
an upper bound on entries, and a failure is never allowed to break alerting: the
alert falls back to the raw amount.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Iterable
from typing import Final, Protocol

from solana_monitor.domain.errors import MonitorError
from solana_monitor.domain.models import TokenMetadata
from solana_monitor.helius.schemas import DasAssetPayload
from solana_monitor.resilience import Clock

logger = logging.getLogger(__name__)

_NEGATIVE_TTL_CAP: Final = 60.0
_DEFAULT_MAX_ENTRIES: Final = 2048


class AssetSource(Protocol):
    """The part of :class:`~solana_monitor.helius.client.HeliusClient` we depend on."""

    async def get_asset(self, mint: str) -> DasAssetPayload | None:
        """Return the DAS asset for ``mint``, or ``None`` when it is unknown."""
        ...


class TokenMetadataResolver:
    def __init__(
        self,
        source: AssetSource,
        *,
        ttl_seconds: float = 3600.0,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        clock: Clock,
    ) -> None:
        self._source = source
        self._ttl = max(0.0, ttl_seconds)
        self._max_entries = max(1, max_entries)
        self._clock = clock
        self._cache: OrderedDict[str, tuple[float, TokenMetadata | None]] = OrderedDict()

    async def resolve(self, mint: str) -> TokenMetadata | None:
        """Return cached metadata for ``mint``, fetching it at most once per TTL."""
        cached = self._cache.get(mint)
        now = self._clock()
        if cached is not None and cached[0] > now:
            self._cache.move_to_end(mint)
            return cached[1]

        metadata = await self._fetch(mint)
        self._store(mint, metadata, now)
        return metadata

    async def resolve_many(self, mints: Iterable[str]) -> dict[str, TokenMetadata]:
        """Resolve several mints, skipping those without metadata."""
        resolved: dict[str, TokenMetadata] = {}
        for mint in mints:
            metadata = await self.resolve(mint)
            if metadata is not None:
                resolved[mint] = metadata
        return resolved

    async def _fetch(self, mint: str) -> TokenMetadata | None:
        try:
            asset = await self._source.get_asset(mint)
        except MonitorError as error:
            logger.warning(
                "token metadata lookup failed; alerting without symbol",
                extra={"mint": mint, "error": type(error).__name__},
            )
            return None
        if asset is None:
            return None
        return asset.to_metadata()

    def _store(self, mint: str, metadata: TokenMetadata | None, now: float) -> None:
        ttl = self._ttl if metadata is not None else min(self._ttl, _NEGATIVE_TTL_CAP)
        if ttl <= 0:
            return
        self._cache[mint] = (now + ttl, metadata)
        self._cache.move_to_end(mint)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

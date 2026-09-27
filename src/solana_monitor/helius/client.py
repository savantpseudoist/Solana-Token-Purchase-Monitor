"""HTTP client for the Helius APIs.

Why this design:

* one client owns one ``aiohttp.ClientSession`` (connection reuse) and one rate
  limiter, so API quota is respected in a single place;
* retries, ``Retry-After`` handling and rate limiting are delegated to
  :mod:`solana_monitor.resilience`, keeping the transport thin;
* every URL is built from a *validated* base58 address, so no unvalidated user
  input ever reaches a URL path;
* malformed elements are skipped individually, so one bad transaction cannot
  discard a whole page;
* error messages never contain the API key: the credential travels as a query
  parameter and only the URL path is ever logged or raised.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import urlparse

import aiohttp
from pydantic import ValidationError

from solana_monitor.config import Settings
from solana_monitor.domain.addresses import validate_address
from solana_monitor.domain.errors import (
    HeliusAuthError,
    HeliusError,
    HeliusRateLimitError,
    HeliusResponseError,
    HeliusTransportError,
    MonitorError,
)
from solana_monitor.domain.models import Transaction
from solana_monitor.helius.schemas import (
    DasAssetPayload,
    EnhancedTransactionPayload,
    is_fungible_interface,
)
from solana_monitor.resilience import RateLimiter, parse_retry_after, retry_async

logger = logging.getLogger(__name__)

SortOrder = Literal["asc", "desc"]
FUNCTIONS_MARKER: Final = "solana-monitor"
_MAX_ERROR_BODY_CHARS: Final = 200
_MAX_HOLDING_PAGES: Final = 2


def _is_retryable(error: BaseException) -> bool:
    return isinstance(error, HeliusError) and error.retryable


@dataclass(frozen=True, slots=True)
class HistoryScan:
    """The result of walking a page range of an address' history."""

    transactions: tuple[Transaction, ...]
    pages_fetched: int
    truncated: bool

    @property
    def newest_signature(self) -> str | None:
        return self.transactions[0].signature if self.transactions else None

    @property
    def oldest_signature(self) -> str | None:
        return self.transactions[-1].signature if self.transactions else None

    def __len__(self) -> int:
        return len(self.transactions)


class HeliusClient:
    """Async Helius client for enhanced transactions and DAS lookups."""

    def __init__(
        self,
        settings: Settings,
        *,
        session: aiohttp.ClientSession | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        self._api_key = settings.helius_api_key.get_secret_value()
        self._base_url = settings.helius_api_base_url
        self._timeout = aiohttp.ClientTimeout(
            total=settings.helius_timeout_seconds,
            connect=min(10.0, settings.helius_timeout_seconds),
        )
        self._session = session
        self._owns_session = session is None
        self._limiter = limiter or RateLimiter(settings.helius_requests_per_second, sleep=sleep)
        self._sleep = sleep
        self._random = random_fn

    async def __aenter__(self) -> HeliusClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the session, but only when this client created it."""
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=300),
            )
            self._owns_session = True
        return self._session

    # -- Transport -----------------------------------------------------------
    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        path = urlparse(url).path

        async def attempt() -> Any:
            await self._limiter.acquire()
            try:
                async with self._ensure_session().request(
                    method, url, params=params, json=json_body
                ) as response:
                    body = await response.text()
                    return self._decode(response.status, body, dict(response.headers), path)
            except (aiohttp.ClientError, TimeoutError, OSError) as error:
                detail = f"Helius request to {path} failed: {type(error).__name__}"
                raise HeliusTransportError(detail, context={"path": path}) from error

        return await retry_async(
            attempt,
            attempts=self._settings.helius_max_retries + 1,
            base_delay=self._settings.helius_retry_base_seconds,
            max_delay=self._settings.helius_retry_max_seconds,
            should_retry=_is_retryable,
            delay_for=self._retry_delay,
            sleep=self._sleep,
            random_fn=self._random,
        )

    def _retry_delay(self, _attempt: int, error: BaseException) -> float | None:
        """Honour ``Retry-After`` when Helius sends one, otherwise use backoff."""
        if isinstance(error, HeliusError) and error.retry_after is not None:
            return min(max(error.retry_after, 0.0), self._settings.helius_retry_max_seconds)
        return None

    def _decode(self, status: int, body: str, headers: dict[str, str], path: str) -> Any:
        context = {"path": path}
        if status == 200:
            try:
                return json.loads(body)
            except json.JSONDecodeError as error:
                detail = "Helius returned a body that is not valid JSON"
                raise HeliusResponseError(detail, status=status, context=context) from error
        if status in (401, 403):
            raise HeliusAuthError(status, context=context)
        if status == 429:
            raise HeliusRateLimitError(
                retry_after=parse_retry_after(headers.get("Retry-After")), context=context
            )
        snippet = body.strip()[:_MAX_ERROR_BODY_CHARS]
        if status >= 500:
            detail = f"Helius server error (HTTP {status})"
            raise HeliusError(
                detail, status=status, retryable=True, context={**context, "body": snippet}
            )
        detail = f"Helius rejected the request (HTTP {status})"
        raise HeliusError(
            detail, status=status, retryable=False, context={**context, "body": snippet}
        )

    # -- Enhanced transactions ----------------------------------------------
    async def fetch_transactions(
        self,
        address: str,
        *,
        before_signature: str | None = None,
        after_signature: str | None = None,
        limit: int | None = None,
        gte_time: int | None = None,
        sort_order: SortOrder = "desc",
    ) -> tuple[Transaction, ...]:
        """Fetch one page of enhanced transactions for ``address``.

        Pagination uses the documented ``before-signature``/``after-signature``
        cursors; ``gte_time`` (Unix seconds) narrows the result to a time window
        server-side instead of walking the whole history.
        """
        validate_address(address, field="wallet address")
        params: dict[str, Any] = {
            "api-key": self._api_key,
            "limit": min(limit or self._settings.helius_page_size, 100),
            "commitment": self._settings.helius_commitment,
            "token-accounts": self._settings.helius_token_accounts,
            "sort-order": sort_order,
        }
        if before_signature:
            params["before-signature"] = before_signature
        if after_signature:
            params["after-signature"] = after_signature
        if gte_time is not None:
            params["gte-time"] = int(gte_time)

        url = f"{self._base_url}/v0/addresses/{address}/transactions"
        payload = await self._request("GET", url, params=params)
        return self._parse_transactions(payload)

    def _parse_transactions(self, payload: Any) -> tuple[Transaction, ...]:
        if isinstance(payload, dict):
            # Helius answers some paginated queries with an error object that
            # suggests continuing with a `before-signature` cursor.
            detail = payload.get("error") or "unexpected object instead of a list"
            message = f"Helius returned an error object: {str(detail)[:200]}"
            raise HeliusResponseError(message)
        if not isinstance(payload, list):
            message = "Helius returned an unexpected payload type"
            raise HeliusResponseError(message)

        transactions: list[Transaction] = []
        malformed = 0
        for item in payload:
            try:
                transactions.append(EnhancedTransactionPayload.model_validate(item).to_domain())
            except ValidationError as error:
                malformed += 1
                logger.warning(
                    "skipping malformed Helius transaction element",
                    extra={"errors": error.error_count()},
                )
        if malformed:
            logger.warning("skipped malformed transactions", extra={"count": malformed})
        return tuple(transactions)

    async def collect_history(
        self,
        address: str,
        *,
        gte_time: int | None = None,
        sort_order: SortOrder = "desc",
        start_after: str | None = None,
        start_before: str | None = None,
        page_size: int | None = None,
        max_pages: int | None = None,
    ) -> HistoryScan:
        """Walk up to ``max_pages`` pages, following the documented cursors."""
        size = min(page_size or self._settings.helius_page_size, 100)
        limit_pages = max_pages or self._settings.monitor_max_catchup_pages
        cursor_before = start_before
        cursor_after = start_after
        collected: list[Transaction] = []
        pages = 0
        truncated = False

        while pages < limit_pages:
            page = await self.fetch_transactions(
                address,
                before_signature=cursor_before,
                after_signature=cursor_after,
                limit=size,
                gte_time=gte_time,
                sort_order=sort_order,
            )
            pages += 1
            collected.extend(page)
            if len(page) < size:
                break
            if sort_order == "asc":
                cursor_after = page[-1].signature
                cursor_before = None
            else:
                cursor_before = page[-1].signature
                cursor_after = None
        else:
            truncated = True

        return HistoryScan(tuple(collected), pages, truncated)

    # -- DAS (JSON-RPC) ------------------------------------------------------
    async def rpc(self, method: str, params: dict[str, Any]) -> Any:
        """Perform a Helius DAS JSON-RPC call and return its ``result``."""
        body = {
            "jsonrpc": "2.0",
            "id": FUNCTIONS_MARKER,
            "method": method,
            "params": params,
        }
        url = f"{self._base_url}/"
        payload = await self._request(
            "POST", url, params={"api-key": self._api_key}, json_body=body
        )
        if not isinstance(payload, dict):
            message = f"Helius RPC {method} returned an unexpected payload type"
            raise HeliusResponseError(message)
        if payload.get("error") is not None:
            detail = f"Helius RPC {method} failed: {str(payload['error'])[:200]}"
            raise HeliusResponseError(detail)
        return payload.get("result")

    async def get_asset(self, mint: str) -> DasAssetPayload | None:
        """Fetch DAS metadata for a single mint, or ``None`` when unknown."""
        validate_address(mint, field="mint")
        result = await self.rpc("getAsset", {"id": mint})
        if not isinstance(result, dict):
            return None
        return DasAssetPayload.model_validate(result)

    async def get_owned_fungible_mints(self, address: str) -> frozenset[str] | None:
        """Mints currently held by ``address``.

        Returns ``None`` when holdings are unknown (the API failed) so callers can
        distinguish "no holdings" from "could not check".
        """
        validate_address(address, field="wallet address")
        mints: set[str] = set()
        try:
            for page in range(1, _MAX_HOLDING_PAGES + 1):
                result = await self.rpc(
                    "getAssetsByOwner",
                    {
                        "ownerAddress": address,
                        "page": page,
                        "limit": 1000,
                        "displayOptions": {"showFungible": True, "showNativeBalance": False},
                    },
                )
                if not isinstance(result, dict):
                    break
                items = result.get("items")
                if not isinstance(items, list):
                    break
                for item in items:
                    asset = DasAssetPayload.model_validate(item)
                    if not asset.mint_id or asset.balance is None:
                        continue
                    if asset.interface is not None and not is_fungible_interface(asset.interface):
                        continue
                    if asset.balance > 0:
                        mints.add(asset.mint_id)
                if len(items) < 1000:
                    break
        except (MonitorError, ValidationError) as error:
            logger.warning(
                "wallet holdings unavailable; falling back to first-poll baseline",
                extra={"error": type(error).__name__},
            )
            return None
        return frozenset(mints)

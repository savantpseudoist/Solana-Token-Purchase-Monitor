"""Resilience primitives: retry with backoff, rate limiting, header parsing.

These are deliberately small, dependency-free and injectable (clock, sleep and
randomness are parameters) so that retry and throttling behaviour can be tested
deterministically instead of by sleeping in tests.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

T = TypeVar("T")

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
RandomFn = Callable[[], float]


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header (delay-seconds or HTTP-date) into seconds."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return float(text)
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return max(0.0, (moment - reference).total_seconds())


def compute_backoff(
    attempt: int,
    *,
    base: float,
    maximum: float,
    jitter_ratio: float = 0.25,
    random_fn: RandomFn = random.random,
) -> float:
    """Exponential backoff with +/- ``jitter_ratio`` of additive jitter.

    ``attempt`` is 1-based: attempt 1 waits ``base``, attempt 2 waits ``2*base``,
    and so on, capped at ``maximum``.
    """
    raw = min(maximum, base * (2.0 ** max(0, attempt - 1)))
    if jitter_ratio <= 0:
        return raw
    spread = raw * jitter_ratio
    return max(0.0, raw + (random_fn() * 2 - 1) * spread)


class RateLimiter:
    """Async token bucket enforcing an average rate of ``rate_per_second``."""

    def __init__(
        self,
        rate_per_second: float,
        *,
        burst: float = 1.0,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0:
            msg = "rate_per_second must be positive"
            raise ValueError(msg)
        if burst <= 0:
            msg = "burst must be positive"
            raise ValueError(msg)
        self._rate = rate_per_second
        self._burst = burst
        self._clock = clock
        self._sleep = sleep
        self._tokens = burst
        self._updated_at = clock()

    async def acquire(self) -> float:
        """Wait until one request is allowed; return the seconds waited."""
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._updated_at) * self._rate)
        self._updated_at = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        waited = (1.0 - self._tokens) / self._rate
        await self._sleep(waited)
        self._tokens = 0.0
        self._updated_at = self._clock()
        return waited


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float,
    max_delay: float,
    should_retry: Callable[[BaseException], bool],
    delay_for: Callable[[int, BaseException], float | None] | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Sleeper = asyncio.sleep,
    random_fn: RandomFn = random.random,
) -> T:
    """Run ``operation`` until it succeeds or the retry budget is exhausted.

    ``should_retry`` decides whether an exception is worth retrying; anything it
    rejects (and :class:`asyncio.CancelledError`) propagates unchanged. When the
    budget is exhausted, the last error is raised so callers keep the original
    cause and status information.
    """
    if attempts < 1:
        msg = "attempts must be at least 1"
        raise ValueError(msg)

    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except BaseException as error:
            # Cancellation and non-retryable errors are re-raised unchanged.
            if isinstance(error, asyncio.CancelledError) or not should_retry(error):
                raise
            last_error = error
            if attempt >= attempts:
                break
            delay = delay_for(attempt, error) if delay_for is not None else None
            if delay is None:
                delay = compute_backoff(
                    attempt, base=base_delay, maximum=max_delay, random_fn=random_fn
                )
            if on_retry is not None:
                on_retry(attempt, error, delay)
            await sleep(delay)

    if last_error is None:  # pragma: no cover - defensive, loop always sets it
        msg = "retry loop finished without attempting the operation"
        raise RuntimeError(msg)
    raise last_error

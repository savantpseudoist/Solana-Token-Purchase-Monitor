"""Tests for retry, backoff and rate limiting."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from solana_monitor.resilience import RateLimiter, compute_backoff, parse_retry_after, retry_async
from tests.conftest import FakeClock, FakeSleeper


def never(_error: BaseException) -> bool:
    return False


def always(_error: BaseException) -> bool:
    return True


# -- parse_retry_after -------------------------------------------------------
def test_retry_after_missing_or_empty_is_unknown() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("   ") is None


def test_retry_after_seconds() -> None:
    assert parse_retry_after("30") == 30.0


def test_retry_after_http_date_is_relative_to_now() -> None:
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    header = (now + timedelta(seconds=45)).strftime("%a, %d %b %Y %H:%M:%S GMT")

    assert parse_retry_after(header, now=now) == pytest.approx(45.0, abs=1.0)


def test_retry_after_past_date_clamps_to_zero() -> None:
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)
    header = (now - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")

    assert parse_retry_after(header, now=now) == 0.0


def test_retry_after_garbage_is_unknown() -> None:
    assert parse_retry_after("soon") is None


# -- compute_backoff ---------------------------------------------------------
@pytest.mark.parametrize(
    ("attempt", "expected"), [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (10, 10.0)]
)
def test_backoff_grows_exponentially_and_is_capped(attempt: int, expected: float) -> None:
    assert compute_backoff(attempt, base=1.0, maximum=10.0, jitter_ratio=0.0) == expected


def test_backoff_jitter_stays_within_bounds() -> None:
    low = compute_backoff(1, base=2.0, maximum=10.0, jitter_ratio=0.25, random_fn=lambda: 0.0)
    high = compute_backoff(1, base=2.0, maximum=10.0, jitter_ratio=0.25, random_fn=lambda: 1.0)

    assert low == 1.5
    assert high == 2.5


# -- retry_async -------------------------------------------------------------
async def test_retry_returns_the_first_success() -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    result = await retry_async(
        operation, attempts=3, base_delay=0.1, max_delay=1.0, should_retry=never
    )

    assert result == "ok"
    assert calls == 1


async def test_retry_recovers_from_transient_failures(fake_sleeper: FakeSleeper) -> None:
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            msg = "transient"
            raise RuntimeError(msg)
        return "ok"

    result = await retry_async(
        operation,
        attempts=5,
        base_delay=1.0,
        max_delay=10.0,
        should_retry=always,
        sleep=fake_sleeper,
        random_fn=lambda: 0.5,
    )

    assert result == "ok"
    assert attempts == 3
    assert fake_sleeper.sleeps == [1.0, 2.0]


async def test_retry_reraises_the_last_error_after_exhaustion() -> None:
    async def operation() -> str:
        msg = "always down"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="always down"):
        await retry_async(operation, attempts=3, base_delay=0.1, max_delay=1.0, should_retry=always)


async def test_non_retryable_errors_propagate_immediately(fake_sleeper: FakeSleeper) -> None:
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        msg = "fatal"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="fatal"):
        await retry_async(
            operation,
            attempts=5,
            base_delay=1.0,
            max_delay=1.0,
            should_retry=never,
            sleep=fake_sleeper,
        )

    assert attempts == 1
    assert fake_sleeper.sleeps == []


async def test_cancellation_is_never_retried(fake_sleeper: FakeSleeper) -> None:
    async def operation() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await retry_async(
            operation,
            attempts=3,
            base_delay=1.0,
            max_delay=1.0,
            should_retry=always,
            sleep=fake_sleeper,
        )

    assert fake_sleeper.sleeps == []


async def test_delay_for_overrides_the_backoff(fake_sleeper: FakeSleeper) -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            msg = "rate limited"
            raise RuntimeError(msg)
        return "ok"

    await retry_async(
        operation,
        attempts=2,
        base_delay=10.0,
        max_delay=30.0,
        should_retry=always,
        delay_for=lambda _attempt, _error: 0.25,
        sleep=fake_sleeper,
    )

    assert fake_sleeper.sleeps == [0.25]


async def test_retry_reports_attempts_to_the_callback(fake_sleeper: FakeSleeper) -> None:
    seen: list[tuple[int, str, float]] = []

    async def operation() -> str:
        msg = "down"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        await retry_async(
            operation,
            attempts=2,
            base_delay=1.0,
            max_delay=1.0,
            should_retry=always,
            on_retry=lambda attempt, error, delay: seen.append((attempt, str(error), delay)),
            sleep=fake_sleeper,
            random_fn=lambda: 0.5,
        )

    assert seen == [(1, "down", 1.0)]


async def test_rate_limiter_spaces_requests(
    fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    limiter = RateLimiter(2.0, clock=fake_clock, sleep=fake_sleeper)

    assert await limiter.acquire() == 0.0
    assert await limiter.acquire() == 0.5
    assert await limiter.acquire() == 0.5
    assert fake_sleeper.sleeps == [0.5, 0.5]


async def test_rate_limiter_burst_allows_immediate_calls(
    fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    limiter = RateLimiter(10.0, burst=3.0, clock=fake_clock, sleep=fake_sleeper)

    for _ in range(3):
        assert await limiter.acquire() == 0.0
    assert await limiter.acquire() == pytest.approx(0.1)


def test_rate_limiter_rejects_nonsense_settings() -> None:
    with pytest.raises(ValueError, match="rate_per_second"):
        RateLimiter(0)
    with pytest.raises(ValueError, match="burst"):
        RateLimiter(1.0, burst=0)


async def test_retry_requires_at_least_one_attempt() -> None:
    async def operation() -> str:
        return "unused"

    with pytest.raises(ValueError, match="attempts"):
        await retry_async(operation, attempts=0, base_delay=1.0, max_delay=1.0, should_retry=never)

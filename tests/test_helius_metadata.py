"""Tests for the cached token metadata resolver."""

from __future__ import annotations

from solana_monitor.domain.errors import HeliusError
from solana_monitor.helius.metadata import TokenMetadataResolver
from solana_monitor.helius.schemas import DasAssetPayload
from tests import factories as f
from tests.conftest import FakeClock


class FakeAssetSource:
    def __init__(
        self, assets: dict[str, object] | None = None, error: Exception | None = None
    ) -> None:
        self.assets = assets or {}
        self.error = error
        self.calls: list[str] = []

    async def get_asset(self, mint: str):
        self.calls.append(mint)
        if self.error is not None:
            raise self.error
        return self.assets.get(mint)


def asset(symbol: str = "USDC", decimals: int = 6, mint: str = f.TOKEN_MINT) -> DasAssetPayload:
    return DasAssetPayload.model_validate(
        {
            "id": mint,
            "interface": "FungibleToken",
            "content": {"metadata": {"name": "USD Coin", "symbol": symbol}},
            "token_info": {"decimals": decimals},
        }
    )


def resolver(source: FakeAssetSource, clock: FakeClock, **kwargs: object) -> TokenMetadataResolver:
    return TokenMetadataResolver(source, clock=clock, **kwargs)  # type: ignore[arg-type]


async def test_metadata_is_fetched_and_cached(fake_clock: FakeClock) -> None:
    source = FakeAssetSource({f.TOKEN_MINT: asset()})
    cache = resolver(source, fake_clock, ttl_seconds=60.0)

    first = await cache.resolve(f.TOKEN_MINT)
    second = await cache.resolve(f.TOKEN_MINT)

    assert first is not None
    assert first.symbol == "USDC"
    assert first.decimals == 6
    assert source.calls == [f.TOKEN_MINT]
    assert second is first


async def test_entries_expire(fake_clock: FakeClock) -> None:
    source = FakeAssetSource({f.TOKEN_MINT: asset()})
    cache = resolver(source, fake_clock, ttl_seconds=60.0)

    await cache.resolve(f.TOKEN_MINT)
    fake_clock.advance(61)
    await cache.resolve(f.TOKEN_MINT)

    assert source.calls == [f.TOKEN_MINT, f.TOKEN_MINT]


async def test_unknown_mints_are_cached_too(fake_clock: FakeClock) -> None:
    source = FakeAssetSource()
    cache = resolver(source, fake_clock, ttl_seconds=3600.0)

    assert await cache.resolve(f.TOKEN_MINT) is None
    assert await cache.resolve(f.TOKEN_MINT) is None

    assert source.calls == [f.TOKEN_MINT]


async def test_failures_degrade_to_missing_metadata(fake_clock: FakeClock) -> None:
    source = FakeAssetSource(error=HeliusError("boom", retryable=False))
    cache = resolver(source, fake_clock, ttl_seconds=3600.0)

    assert await cache.resolve(f.TOKEN_MINT) is None
    assert await cache.resolve(f.TOKEN_MINT) is None
    assert len(source.calls) == 1


async def test_cache_is_bounded(fake_clock: FakeClock) -> None:
    source = FakeAssetSource({})
    cache = resolver(source, fake_clock, ttl_seconds=3600.0, max_entries=2)

    await cache.resolve("a")
    await cache.resolve("b")
    await cache.resolve("c")
    await cache.resolve("a")

    # `a` was evicted as the oldest entry, so it is fetched again.
    assert source.calls == ["a", "b", "c", "a"]


async def test_zero_ttl_disables_caching(fake_clock: FakeClock) -> None:
    source = FakeAssetSource({f.TOKEN_MINT: asset()})
    cache = resolver(source, fake_clock, ttl_seconds=0)

    await cache.resolve(f.TOKEN_MINT)
    await cache.resolve(f.TOKEN_MINT)

    assert source.calls == [f.TOKEN_MINT, f.TOKEN_MINT]


async def test_resolve_many_skips_unknown_mints(fake_clock: FakeClock) -> None:
    source = FakeAssetSource(
        {f.TOKEN_MINT: asset(), f.SECOND_MINT: asset("BONK", 5, f.SECOND_MINT)}
    )
    cache = resolver(source, fake_clock, ttl_seconds=60.0)

    resolved = await cache.resolve_many([f.TOKEN_MINT, f.SECOND_MINT, "missing"])

    assert set(resolved) == {f.TOKEN_MINT, f.SECOND_MINT}

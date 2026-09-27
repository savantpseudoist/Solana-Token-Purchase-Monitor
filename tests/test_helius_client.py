"""Tests for the Helius HTTP client (scripted, no network)."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest

from solana_monitor.config import Settings
from solana_monitor.domain.errors import (
    HeliusAuthError,
    HeliusError,
    HeliusResponseError,
    HeliusTransportError,
    InvalidAddressError,
)
from solana_monitor.helius.client import HeliusClient
from solana_monitor.resilience import RateLimiter
from tests import factories as f
from tests.conftest import FakeClock, FakeSleeper
from tests.support import FakeResponse, FakeSession, json_response

API_KEY = "00000000-1111-2222-3333-444444444444"


def build_client(
    session: FakeSession,
    *,
    settings: Settings,
    clock: FakeClock,
    sleeper: FakeSleeper,
    max_retries: int = 2,
) -> HeliusClient:
    return HeliusClient(
        settings.model_copy(update={"helius_max_retries": max_retries}),
        session=session,  # type: ignore[arg-type]
        limiter=RateLimiter(1000.0, clock=clock, sleep=sleeper),
        sleep=sleeper,
        random_fn=lambda: 0.5,
    )


def payloads(count: int = 1) -> list[dict[str, Any]]:
    return [f.helius_payload(signature=f"sig-{index}") for index in range(count)]


async def test_fetch_transactions_sends_documented_query_parameters(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(json_response(payloads()))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    transactions = await client.fetch_transactions(
        f.WATCHED_WALLET,
        before_signature="older-sig",
        after_signature="newer-sig",
        gte_time=1_700_000_000,
        sort_order="asc",
    )

    call = session.last_call
    assert call.method == "GET"
    assert (
        call.url == f"https://mainnet.helius-rpc.com/v0/addresses/{f.WATCHED_WALLET}/transactions"
    )
    assert call.params["api-key"] == API_KEY
    assert call.params["before-signature"] == "older-sig"
    assert call.params["after-signature"] == "newer-sig"
    assert call.params["gte-time"] == 1_700_000_000
    assert call.params["sort-order"] == "asc"
    assert call.params["commitment"] == "confirmed"
    assert call.params["token-accounts"] == "balanceChanged"
    assert call.params["limit"] == 100
    assert len(transactions) == 1


async def test_optional_parameters_are_omitted(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(json_response([]))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    await client.fetch_transactions(f.WATCHED_WALLET)

    assert "before-signature" not in session.last_call.params
    assert "after-signature" not in session.last_call.params
    assert "gte-time" not in session.last_call.params


async def test_invalid_addresses_never_reach_the_network(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession()
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    with pytest.raises(InvalidAddressError):
        await client.fetch_transactions("http://evil.example.com/../../admin")

    assert session.calls == []


async def test_rate_limiting_is_applied_per_request(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(json_response([]), json_response([]))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    await client.fetch_transactions(f.WATCHED_WALLET)
    await client.fetch_transactions(f.WATCHED_WALLET)

    assert fake_sleeper.sleeps  # the second call had to wait for the limiter


async def test_malformed_elements_are_skipped_but_valid_ones_survive(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    broken = f.helius_payload()
    del broken["signature"]
    session = FakeSession(json_response([f.helius_payload(), broken]))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    transactions = await client.fetch_transactions(f.WATCHED_WALLET)

    assert [transaction.signature for transaction in transactions] == [f.SIGNATURE]


async def test_error_object_payloads_are_reported(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(json_response({"error": "Failed to find events within the window"}))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    with pytest.raises(HeliusResponseError, match="error object"):
        await client.fetch_transactions(f.WATCHED_WALLET)


async def test_rate_limit_is_retried_after_the_advertised_delay(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(
        FakeResponse(status=429, body="{}", headers={"Retry-After": "7"}),
        json_response(payloads()),
    )
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    transactions = await client.fetch_transactions(f.WATCHED_WALLET)

    assert len(transactions) == 1
    assert 7.0 in fake_sleeper.sleeps


async def test_server_errors_are_retried_then_reported(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(*[FakeResponse(status=503, body="upstream down") for _ in range(3)])
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    with pytest.raises(HeliusError) as excinfo:
        await client.fetch_transactions(f.WATCHED_WALLET)

    assert excinfo.value.retryable is True
    assert excinfo.value.status == 503
    assert len(session.calls) == 3


async def test_authentication_failures_are_not_retried(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(FakeResponse(status=401, body="unauthorized"))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    with pytest.raises(HeliusAuthError) as excinfo:
        await client.fetch_transactions(f.WATCHED_WALLET)

    assert len(session.calls) == 1
    assert "HELIUS_API_KEY" in str(excinfo.value)


async def test_client_errors_are_not_retried(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(FakeResponse(status=400, body="bad request"))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    with pytest.raises(HeliusError) as excinfo:
        await client.fetch_transactions(f.WATCHED_WALLET)

    assert excinfo.value.retryable is False
    assert len(session.calls) == 1


async def test_non_json_bodies_are_reported(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(FakeResponse(status=200, body="<html>maintenance</html>"))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    with pytest.raises(HeliusResponseError, match="not valid JSON"):
        await client.fetch_transactions(f.WATCHED_WALLET)


async def test_transport_failures_are_wrapped_and_retried(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(
        aiohttp.ClientError("connection reset"),
        json_response(payloads()),
    )
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    transactions = await client.fetch_transactions(f.WATCHED_WALLET)

    assert len(transactions) == 1
    assert len(session.calls) == 2


async def test_persistent_timeouts_are_reported_as_transport_errors(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(*[TimeoutError() for _ in range(3)])
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    with pytest.raises(HeliusTransportError):
        await client.fetch_transactions(f.WATCHED_WALLET)


async def test_collect_history_stops_on_a_short_page(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(json_response(payloads(3)))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    scan = await client.collect_history(f.WATCHED_WALLET, page_size=10, max_pages=5)

    assert len(scan) == 3
    assert scan.pages_fetched == 1
    assert scan.truncated is False
    assert scan.newest_signature == "sig-0"
    assert scan.oldest_signature == "sig-2"


async def test_collect_history_paginates_with_the_documented_cursor(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(
        json_response(payloads(2)),
        json_response(payloads(1)),
    )
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    scan = await client.collect_history(f.WATCHED_WALLET, page_size=2, max_pages=5)

    assert len(scan) == 3
    assert scan.pages_fetched == 2
    assert "before-signature" not in session.calls[0].params
    assert session.calls[1].params["before-signature"] == "sig-1"
    assert "after-signature" not in session.calls[1].params


async def test_collect_history_moves_the_cursor_forward_in_ascending_order(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(
        json_response(payloads(2)),
        json_response(payloads(1)),
    )
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    await client.collect_history(
        f.WATCHED_WALLET, page_size=2, max_pages=5, sort_order="asc", start_after="cursor"
    )

    assert session.calls[0].params["after-signature"] == "cursor"
    assert session.calls[1].params["after-signature"] == "sig-1"
    assert "before-signature" not in session.calls[1].params


async def test_collect_history_reports_truncation(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(json_response(payloads(2)), json_response(payloads(2)))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    scan = await client.collect_history(f.WATCHED_WALLET, page_size=2, max_pages=2)

    assert scan.truncated is True
    assert scan.pages_fetched == 2
    assert len(scan) == 4


# -- DAS / JSON-RPC ----------------------------------------------------------
async def test_rpc_posts_json_rpc_and_returns_the_result(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(json_response({"jsonrpc": "2.0", "result": {"value": 1}}))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    result = await client.rpc("getAsset", {"id": f.TOKEN_MINT})

    assert result == {"value": 1}
    call = session.last_call
    assert call.method == "POST"
    assert call.json["method"] == "getAsset"
    assert call.json["params"] == {"id": f.TOKEN_MINT}
    assert call.params["api-key"] == API_KEY


async def test_rpc_errors_are_raised(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(json_response({"jsonrpc": "2.0", "error": {"message": "nope"}}))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    with pytest.raises(HeliusResponseError, match="getAsset failed"):
        await client.rpc("getAsset", {"id": f.TOKEN_MINT})


async def test_get_asset_validates_the_mint(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession()
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    with pytest.raises(InvalidAddressError):
        await client.get_asset("not-a-mint")

    assert session.calls == []


async def test_owned_holdings_filter_zero_balances_and_nfts(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(
        json_response(
            {
                "result": {
                    "items": [
                        {
                            "id": f.TOKEN_MINT,
                            "interface": "FungibleToken",
                            "token_info": {"decimals": 6, "balance": "1500000"},
                        },
                        {
                            "id": f.SECOND_MINT,
                            "interface": "FungibleToken",
                            "token_info": {"decimals": 6, "balance": "0"},
                        },
                        {
                            "id": "Nft11111111111111111111111111111111111111111",
                            "interface": "V1_NFT",
                            "token_info": {"decimals": 0, "balance": "1"},
                        },
                    ]
                }
            }
        )
    )
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    holdings = await client.get_owned_fungible_mints(f.WATCHED_WALLET)

    assert holdings == frozenset({f.TOKEN_MINT})


async def test_unknown_holdings_are_distinguishable_from_empty_holdings(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(FakeResponse(status=401, body="unauthorized"))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    assert await client.get_owned_fungible_mints(f.WATCHED_WALLET) is None

    session.queue(json_response({"result": {"items": []}}))
    assert await client.get_owned_fungible_mints(f.WATCHED_WALLET) == frozenset()


async def test_injected_sessions_are_not_closed_by_the_client(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession()
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    await client.close()

    assert session.closed is False


async def test_injected_sessions_stay_in_use(
    settings: Settings, fake_clock: FakeClock, fake_sleeper: FakeSleeper
) -> None:
    session = FakeSession(json_response(payloads()), json_response(payloads()))
    client = build_client(session, settings=settings, clock=fake_clock, sleeper=fake_sleeper)

    async with client:
        await client.fetch_transactions(f.WATCHED_WALLET)
        await client.fetch_transactions(f.WATCHED_WALLET)

    assert len(session.calls) == 2
    assert session.closed is False

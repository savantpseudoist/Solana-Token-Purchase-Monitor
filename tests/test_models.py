"""Tests for the domain value objects."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from solana_monitor.domain.models import AcquisitionKind, TokenAcquisition, TokenMetadata
from tests import factories as f


def test_net_token_changes_prefers_authoritative_balance_changes() -> None:
    transaction = f.make_transaction(
        token_balance_changes=[f.token_balance_change("1000000")],
        token_transfers=[f.token_transfer("999999999")],
    )

    assert transaction.net_token_changes(f.WATCHED_WALLET) == {f.TOKEN_MINT: Decimal(1_000_000)}


def test_net_token_changes_falls_back_to_transfers_when_account_data_is_missing() -> None:
    transaction = f.make_transaction(
        token_transfers=[
            f.token_transfer("1000000"),
            f.token_transfer("250000", from_account=f.WATCHED_WALLET, to_account=f.OTHER_WALLET),
            f.token_transfer("500", to_account=f.OTHER_WALLET),
        ]
    )

    assert transaction.net_token_changes(f.WATCHED_WALLET) == {f.TOKEN_MINT: Decimal(750_000)}


def test_net_token_changes_aggregates_multiple_entries_for_one_mint() -> None:
    transaction = f.make_transaction(
        token_balance_changes=[
            f.token_balance_change("1000"),
            f.token_balance_change("500"),
            f.token_balance_change("-1499"),
        ]
    )

    assert transaction.net_token_changes(f.WATCHED_WALLET) == {f.TOKEN_MINT: Decimal(1)}


def test_net_token_changes_omits_zero_deltas_and_other_accounts() -> None:
    transaction = f.make_transaction(
        token_balance_changes=[
            f.token_balance_change("1000"),
            f.token_balance_change("-1000"),
            f.token_balance_change("777", owner=f.OTHER_WALLET),
        ]
    )

    assert transaction.net_token_changes(f.WATCHED_WALLET) == {}


def test_net_token_changes_ignores_transfers_without_amounts() -> None:
    transaction = f.make_transaction(token_transfers=[f.token_transfer(None)])

    assert transaction.net_token_changes(f.WATCHED_WALLET) == {}


def test_sol_spent_excludes_the_transaction_fee() -> None:
    transaction = f.make_transaction(
        fee_lamports=5000,
        native_balance_changes={f.WATCHED_WALLET: -1_500_005_000},
    )

    assert transaction.sol_spent_lamports(f.WATCHED_WALLET) == 1_500_000_000


def test_sol_spent_ignores_fees_paid_by_somebody_else() -> None:
    transaction = f.make_transaction(
        fee_lamports=5000,
        fee_payer=f.OTHER_WALLET,
        native_balance_changes={f.WATCHED_WALLET: -1_500_000_000},
    )

    assert transaction.sol_spent_lamports(f.WATCHED_WALLET) == 1_500_000_000


def test_sol_spent_is_zero_when_the_wallet_only_received_sol() -> None:
    transaction = f.make_transaction(native_balance_changes={f.WATCHED_WALLET: 1_000_000_000})

    assert transaction.sol_spent_lamports(f.WATCHED_WALLET) == 0


def test_sol_spent_falls_back_to_native_transfers() -> None:
    transaction = f.make_transaction(
        native_transfers=[f.native_transfer(2_000_000_000)],
        fee_lamports=5000,
    )

    assert transaction.sol_spent_lamports(f.WATCHED_WALLET) == 2_000_000_000


def test_sol_spent_is_unknown_without_any_sol_data() -> None:
    transaction = f.make_transaction()

    assert transaction.sol_spent_lamports(f.WATCHED_WALLET) is None


def test_decimals_and_standard_lookup() -> None:
    transaction = f.make_transaction(
        token_balance_changes=[f.token_balance_change("1", decimals=9)],
        token_transfers=[f.token_transfer("1", token_standard="NonFungible")],
    )

    assert transaction.decimals_for(f.TOKEN_MINT) == 9
    assert transaction.token_standard_for(f.TOKEN_MINT) == "Fungible"
    assert transaction.decimals_for(f.SECOND_MINT) is None
    assert transaction.token_standard_for(f.SECOND_MINT) is None


def acquisition(**overrides: object) -> TokenAcquisition:
    base = TokenAcquisition(
        mint=f.TOKEN_MINT,
        amount_raw=Decimal("1500000"),
        decimals=6,
        symbol="TEST",
        kind=AcquisitionKind.PURCHASE,
        signature=f.SIGNATURE,
        slot=1,
        timestamp=datetime(2026, 9, 27, tzinfo=UTC),
        description="swap",
        sol_spent_lamports=1_000_000,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_amount_is_scaled_by_decimals() -> None:
    assert acquisition().amount == Decimal("1.5")


def test_amount_is_unknown_without_decimals() -> None:
    assert acquisition(decimals=None).amount is None


def test_is_purchase_reflects_the_kind() -> None:
    assert acquisition().is_purchase is True
    assert acquisition(kind=AcquisitionKind.RECEIPT).is_purchase is False


def test_with_metadata_fills_missing_fields_only() -> None:
    enriched = acquisition(symbol=None, decimals=None).with_metadata(
        TokenMetadata(
            mint=f.TOKEN_MINT,
            symbol="USDC",
            name="USD Coin",
            decimals=6,
            is_fungible=True,
        )
    )

    assert enriched.symbol == "USDC"
    assert enriched.decimals == 6


def test_with_metadata_keeps_present_values() -> None:
    original = acquisition(decimals=9, symbol="KNOWN")

    enriched = original.with_metadata(
        TokenMetadata(mint=f.TOKEN_MINT, symbol="OTHER", name=None, decimals=6, is_fungible=True)
    )

    assert enriched is original or (enriched.decimals == 9 and enriched.symbol == "KNOWN")


def test_with_metadata_is_a_noop_for_unknown_tokens() -> None:
    original = acquisition(symbol=None)

    assert original.with_metadata(None) is original

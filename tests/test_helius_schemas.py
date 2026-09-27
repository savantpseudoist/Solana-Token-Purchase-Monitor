"""Tests for the Helius wire-format schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from solana_monitor.helius.schemas import (
    DasAssetPayload,
    EnhancedTransactionPayload,
    is_fungible_interface,
    is_nft_interface,
    is_nft_token_standard,
)
from tests import factories as f


def parse(**overrides: object) -> EnhancedTransactionPayload:
    return EnhancedTransactionPayload.model_validate(f.helius_payload(**overrides))


def test_realistic_payload_maps_onto_the_domain_model() -> None:
    transaction = parse().to_domain()

    assert transaction.signature == f.SIGNATURE
    assert transaction.slot == 300_000_000
    assert transaction.timestamp == datetime(2026, 9, 27, 11, 0, tzinfo=UTC)
    assert transaction.transaction_type == "SWAP"
    assert transaction.source == "JUPITER"
    assert transaction.fee_lamports == 5000
    assert transaction.fee_payer == f.WATCHED_WALLET
    assert transaction.failed is False
    assert transaction.error is None
    assert transaction.native_balance_changes[f.WATCHED_WALLET] == -1_500_005_000
    assert transaction.token_balance_changes[0].decimals == 6
    assert transaction.token_balance_changes[0].amount_raw == Decimal("1000000000")
    assert transaction.token_transfers[0].to_account == f.WATCHED_WALLET
    assert transaction.net_token_changes(f.WATCHED_WALLET) == {f.TOKEN_MINT: Decimal("1000000000")}
    assert transaction.sol_spent_lamports(f.WATCHED_WALLET) == 1_500_000_000


def test_unknown_and_null_fields_are_tolerated() -> None:
    transaction = parse(
        nativeTransfers=None,
        tokenTransfers=None,
        accountData=None,
        events={"future": "shape"},
        brandNewField={"nested": True},
    ).to_domain()

    assert transaction.token_transfers == ()
    assert transaction.native_transfers == ()
    assert transaction.token_balance_changes == ()


def test_failed_transactions_are_flagged_with_a_summary() -> None:
    transaction = parse(transactionError={"InstructionError": [0, "Custom"]}).to_domain()

    assert transaction.failed is True
    assert transaction.error is not None
    assert "InstructionError" in transaction.error


def test_string_transaction_errors_are_preserved() -> None:
    transaction = parse(transactionError="BlockhashNotFound").to_domain()

    assert transaction.failed is True
    assert transaction.error == "BlockhashNotFound"


def test_empty_error_containers_do_not_mark_a_transaction_failed() -> None:
    assert parse(transactionError={}).to_domain().failed is False
    assert parse(transactionError=None).to_domain().failed is False
    assert parse(transactionError="").to_domain().failed is False


def test_missing_timestamp_yields_unknown_time() -> None:
    assert parse(timestamp=None).to_domain().timestamp is None
    assert parse(timestamp=0).to_domain().timestamp is None


def test_missing_signature_is_a_validation_error() -> None:
    payload = f.helius_payload()
    del payload["signature"]

    with pytest.raises(ValidationError):
        EnhancedTransactionPayload.model_validate(payload)


@pytest.mark.parametrize("amount", ["12345", 12345, 12345.0, Decimal("12345")])
def test_token_amounts_accept_strings_numbers_and_decimals(amount: object) -> None:
    transaction = parse(
        accountData=[
            {
                "account": f.WATCHED_WALLET,
                "tokenBalanceChanges": [
                    {
                        "userAccount": f.WATCHED_WALLET,
                        "mint": f.TOKEN_MINT,
                        "rawTokenAmount": {"tokenAmount": amount, "decimals": 6},
                    }
                ],
            }
        ]
    ).to_domain()

    assert transaction.token_balance_changes[0].amount_raw == Decimal("12345")


@pytest.mark.parametrize("amount", ["not-a-number", "", None, "NaN", True])
def test_unusable_token_amounts_are_dropped(amount: object) -> None:
    transaction = parse(
        accountData=[
            {
                "account": f.WATCHED_WALLET,
                "tokenBalanceChanges": [
                    {
                        "userAccount": f.WATCHED_WALLET,
                        "mint": f.TOKEN_MINT,
                        "rawTokenAmount": {"tokenAmount": amount, "decimals": 6},
                    }
                ],
            }
        ]
    ).to_domain()

    assert transaction.token_balance_changes == ()


def test_balance_changes_without_an_owner_are_dropped() -> None:
    transaction = parse(
        accountData=[
            {
                "account": f.WATCHED_WALLET,
                "tokenBalanceChanges": [{"mint": f.TOKEN_MINT}],
            }
        ]
    ).to_domain()

    assert transaction.token_balance_changes == ()


def test_transfers_without_a_mint_are_dropped() -> None:
    transaction = parse(
        tokenTransfers=[{"fromUserAccount": f.OTHER_WALLET, "tokenAmount": 5}],
    ).to_domain()

    assert transaction.token_transfers == ()


def test_token_accounts_are_used_as_fallback_endpoints() -> None:
    transaction = parse(
        tokenTransfers=[
            {"toTokenAccount": f.WATCHED_WALLET, "mint": f.TOKEN_MINT, "tokenAmount": 5}
        ],
        accountData=[],
    ).to_domain()

    assert transaction.token_transfers[0].to_account == f.WATCHED_WALLET


def test_das_asset_maps_to_token_metadata() -> None:
    asset = DasAssetPayload.model_validate(
        {
            "id": f.TOKEN_MINT,
            "interface": "FungibleToken",
            "content": {"metadata": {"name": "USD Coin", "symbol": "USDC"}},
            "token_info": {"decimals": 6, "balance": "1500000"},
        }
    )

    metadata = asset.to_metadata()

    assert metadata.mint == f.TOKEN_MINT
    assert metadata.symbol == "USDC"
    assert metadata.name == "USD Coin"
    assert metadata.decimals == 6
    assert metadata.is_fungible is True
    assert metadata.is_fungible is True
    assert asset.balance == Decimal("1500000")


def test_das_asset_falls_back_to_token_info_symbol() -> None:
    asset = DasAssetPayload.model_validate(
        {
            "id": f.TOKEN_MINT,
            "interface": "FungibleAsset",
            "token_info": {"decimals": 9, "symbol": "WIF"},
        }
    )

    assert asset.to_metadata().symbol == "WIF"
    assert asset.to_metadata().name is None


def test_das_nft_is_never_fungible() -> None:
    asset = DasAssetPayload.model_validate(
        {"id": f.TOKEN_MINT, "interface": "V1_NFT", "content": {"metadata": {"symbol": "NB"}}}
    )

    assert asset.is_nft is True
    assert asset.is_fungible is False


def test_das_asset_without_data_is_not_fungible() -> None:
    asset = DasAssetPayload.model_validate({"id": f.TOKEN_MINT})

    assert asset.is_fungible is False
    assert asset.balance is None
    assert asset.to_metadata().symbol is None


@pytest.mark.parametrize(
    ("interface", "fungible", "nft"),
    [
        ("FungibleToken", True, False),
        ("FungibleAsset", True, False),
        ("V1_NFT", False, True),
        ("ProgrammableNFT", False, True),
        ("MplCoreAsset", False, True),
        (None, False, False),
    ],
)
def test_interface_classification(interface: str | None, fungible: bool, nft: bool) -> None:
    assert is_fungible_interface(interface) is fungible
    assert is_nft_interface(interface) is nft


def test_token_standard_classification() -> None:
    assert is_nft_token_standard("NonFungible") is True
    assert is_nft_token_standard("ProgrammableNonFungibleEdition") is True
    assert is_nft_token_standard("Fungible") is False
    assert is_nft_token_standard(None) is False

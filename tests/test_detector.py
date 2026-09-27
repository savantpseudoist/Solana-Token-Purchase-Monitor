"""Tests for purchase detection rules."""

from __future__ import annotations

from decimal import Decimal

from solana_monitor.config import Settings
from solana_monitor.domain.constants import WRAPPED_SOL_MINT
from solana_monitor.domain.models import AcquisitionKind
from solana_monitor.monitoring.detector import DetectorPolicy, PurchaseDetector
from tests import factories as f

WALLET = f.WATCHED_WALLET


def detector(**policy: object) -> PurchaseDetector:
    return PurchaseDetector(DetectorPolicy(**policy))  # type: ignore[arg-type]


def test_sol_swap_is_a_purchase() -> None:
    acquisitions = detector().detect(f.buy_transaction(), WALLET)

    assert len(acquisitions) == 1
    acquisition = acquisitions[0]
    assert acquisition.mint == f.TOKEN_MINT
    assert acquisition.kind is AcquisitionKind.PURCHASE
    assert acquisition.decimals == f.DECIMALS
    assert acquisition.amount == Decimal(1)
    assert acquisition.sol_spent_lamports == 1_500_000_000
    assert acquisition.signature == f.SIGNATURE


def test_failed_transactions_are_ignored() -> None:
    failed = f.buy_transaction()
    failed = f.make_transaction(
        signature=failed.signature,
        failed=True,
        error="BlockhashNotFound",
        token_balance_changes=failed.token_balance_changes,
        native_balance_changes=failed.native_balance_changes,
    )

    assert detector().detect(failed, WALLET) == ()


def test_selling_a_token_produces_nothing() -> None:
    sell = f.make_transaction(
        token_balance_changes=[f.token_balance_change("-1000")],
        token_transfers=[f.token_transfer(1000, from_account=WALLET, to_account=f.OTHER_WALLET)],
    )

    assert detector().detect(sell, WALLET) == ()


def test_ignored_mints_are_skipped() -> None:
    buy = f.buy_transaction(mint=WRAPPED_SOL_MINT)

    assert detector(ignored_mints=frozenset({WRAPPED_SOL_MINT})).detect(buy, WALLET) == ()


def test_transfer_in_without_payment_is_a_receipt() -> None:
    airdrop = f.make_transaction(
        transaction_type="TRANSFER",
        fee_payer=None,
        token_balance_changes=[f.token_balance_change("5000")],
        token_transfers=[f.token_transfer("5000")],
    )

    purchases = detector().detect(airdrop, WALLET)
    receipts = detector(alert_on_receipts=False).detect(airdrop, WALLET)

    assert purchases[0].kind is AcquisitionKind.RECEIPT
    # No native data at all: the SOL spent is *unknown*, not zero.
    assert purchases[0].sol_spent_lamports is None
    assert receipts == ()


def test_buying_with_another_token_counts_as_a_purchase() -> None:
    swap = f.make_transaction(
        token_balance_changes=[
            f.token_balance_change("1000", mint=f.TOKEN_MINT),
            f.token_balance_change("-990000", mint=f.SECOND_MINT),
        ],
        token_transfers=[],
    )

    acquisitions = detector(alert_on_receipts=False).detect(swap, WALLET)

    assert [acquisition.kind for acquisition in acquisitions] == [AcquisitionKind.PURCHASE]


def test_nfts_are_excluded_by_token_standard() -> None:
    nft = f.make_transaction(
        token_balance_changes=[f.token_balance_change(1, decimals=0, token_standard="NonFungible")],
    )

    assert detector().detect(nft, WALLET) == ()


def test_single_indivisible_unit_is_treated_as_an_nft() -> None:
    nft = f.make_transaction(token_balance_changes=[f.token_balance_change(1, decimals=0)])

    assert detector().detect(nft, WALLET) == ()


def test_the_nft_heuristic_can_be_disabled() -> None:
    nft = f.make_transaction(token_balance_changes=[f.token_balance_change(1, decimals=0)])

    acquisitions = detector(drop_nft_heuristic_tokens=False).detect(nft, WALLET)

    assert len(acquisitions) == 1


def test_multiple_mints_in_one_transaction() -> None:
    swap = f.make_transaction(
        token_balance_changes=[
            f.token_balance_change("1000", mint=f.TOKEN_MINT),
            f.token_balance_change("2000", mint=f.SECOND_MINT, decimals=9),
        ],
    )

    acquisitions = detector().detect(swap, WALLET)

    assert [acquisition.mint for acquisition in acquisitions] == [f.TOKEN_MINT, f.SECOND_MINT]
    assert acquisitions[1].decimals == 9


def test_transactions_that_do_not_touch_the_wallet_are_ignored() -> None:
    other = f.make_transaction(
        token_balance_changes=[f.token_balance_change("1000", owner=f.OTHER_WALLET)]
    )

    assert detector().detect(other, WALLET) == ()


def test_policy_can_be_built_from_settings(settings: Settings) -> None:
    policy = DetectorPolicy.from_settings(settings)

    assert policy.ignored_mints == frozenset({WRAPPED_SOL_MINT})
    assert policy.alert_on_receipts is True

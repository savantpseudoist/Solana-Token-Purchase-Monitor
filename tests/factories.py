"""Factories for domain objects and wire payloads used across the suite.

Keeping construction in one place means tests describe *what* is unusual about a
case (a failed transaction, missing decimals) instead of repeating boilerplate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from solana_monitor.domain.models import (
    NativeTransfer,
    TokenBalanceChange,
    TokenTransfer,
    Transaction,
)

WATCHED_WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
OTHER_WALLET = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
TOKEN_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SECOND_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
DECIMALS = 6
TIMESTAMP = datetime(2026, 9, 27, 11, 0, 0, tzinfo=UTC)
SIGNATURE = "5" + "a" * 86


def token_balance_change(
    amount: Decimal | str | int,
    *,
    mint: str = TOKEN_MINT,
    owner: str = WATCHED_WALLET,
    token_account: str | None = None,
    decimals: int | None = DECIMALS,
    token_standard: str | None = "Fungible",
) -> TokenBalanceChange:
    return TokenBalanceChange(
        mint=mint,
        owner=owner,
        token_account=token_account,
        amount_raw=Decimal(str(amount)),
        decimals=decimals,
        token_standard=token_standard,
    )


def token_transfer(
    amount: Decimal | str | int | None,
    *,
    mint: str = TOKEN_MINT,
    from_account: str | None = OTHER_WALLET,
    to_account: str | None = WATCHED_WALLET,
    token_standard: str | None = None,
) -> TokenTransfer:
    return TokenTransfer(
        mint=mint,
        from_account=from_account,
        to_account=to_account,
        amount_raw=None if amount is None else Decimal(str(amount)),
        token_standard=token_standard,
    )


def native_transfer(
    amount_lamports: int,
    *,
    from_account: str | None = WATCHED_WALLET,
    to_account: str | None = OTHER_WALLET,
) -> NativeTransfer:
    return NativeTransfer(
        from_account=from_account,
        to_account=to_account,
        amount_lamports=amount_lamports,
    )


def make_transaction(
    *,
    signature: str = SIGNATURE,
    slot: int = 300_000_000,
    timestamp: datetime | None = TIMESTAMP,
    transaction_type: str = "SWAP",
    source: str | None = "JUPITER",
    description: str = "User swapped 1 SOL for 1000 tokens",
    fee_lamports: int = 5000,
    fee_payer: str | None = WATCHED_WALLET,
    failed: bool = False,
    error: str | None = None,
    native_balance_changes: Mapping[str, int] | None = None,
    token_balance_changes: Sequence[TokenBalanceChange] = (),
    token_transfers: Sequence[TokenTransfer] = (),
    native_transfers: Sequence[NativeTransfer] = (),
) -> Transaction:
    return Transaction(
        signature=signature,
        slot=slot,
        timestamp=timestamp,
        transaction_type=transaction_type,
        source=source,
        description=description,
        fee_lamports=fee_lamports,
        fee_payer=fee_payer,
        failed=failed,
        error=error,
        native_balance_changes=dict(native_balance_changes or {}),
        token_balance_changes=tuple(token_balance_changes),
        token_transfers=tuple(token_transfers),
        native_transfers=tuple(native_transfers),
    )


def buy_transaction(
    *,
    signature: str = SIGNATURE,
    mint: str = TOKEN_MINT,
    amount: Decimal | str | int = 1_000_000,
    decimals: int | None = DECIMALS,
    sol_spent_lamports: int = 1_500_000_000,
    fee_lamports: int = 5000,
    slot: int = 300_000_000,
    timestamp: datetime | None = TIMESTAMP,
) -> Transaction:
    """A plain, successful SOL -> token swap executed by the watched wallet."""
    return make_transaction(
        signature=signature,
        slot=slot,
        timestamp=timestamp,
        fee_lamports=fee_lamports,
        native_balance_changes={WATCHED_WALLET: -(sol_spent_lamports + fee_lamports)},
        token_balance_changes=[token_balance_change(amount, mint=mint, decimals=decimals)],
        token_transfers=[token_transfer(amount, mint=mint)],
    )


def helius_payload(**overrides: Any) -> dict[str, Any]:
    """A realistic Helius ``/v0/addresses/{address}/transactions`` element."""
    payload: dict[str, Any] = {
        "description": "User swapped 1.5 SOL for 1000 USDC on Jupiter",
        "type": "SWAP",
        "source": "JUPITER",
        "fee": 5000,
        "feePayer": WATCHED_WALLET,
        "signature": SIGNATURE,
        "slot": 300_000_000,
        "timestamp": int(TIMESTAMP.timestamp()),
        "nativeTransfers": [
            {
                "fromUserAccount": WATCHED_WALLET,
                "toUserAccount": OTHER_WALLET,
                "amount": 1_500_000_000,
            }
        ],
        "tokenTransfers": [
            {
                "fromUserAccount": OTHER_WALLET,
                "toUserAccount": WATCHED_WALLET,
                "fromTokenAccount": "pool-ata",
                "toTokenAccount": "wallet-ata",
                "tokenAmount": 1_000_000_000,
                "mint": TOKEN_MINT,
                "tokenStandard": "Fungible",
            }
        ],
        "accountData": [
            {
                "account": WATCHED_WALLET,
                "nativeBalanceChange": -1_500_005_000,
                "tokenBalanceChanges": [
                    {
                        "userAccount": WATCHED_WALLET,
                        "tokenAccount": "wallet-ata",
                        "rawTokenAmount": {"tokenAmount": "1000000000", "decimals": 6},
                        "mint": TOKEN_MINT,
                        "tokenStandard": "Fungible",
                    }
                ],
            },
            {
                "account": OTHER_WALLET,
                "nativeBalanceChange": 1_500_000_000,
                "tokenBalanceChanges": [],
            },
        ],
        "events": {},
        "instructions": [],
    }
    payload.update(overrides)
    return payload

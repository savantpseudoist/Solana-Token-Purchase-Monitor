"""Validated representations of the Helius wire format.

Helius payloads are external, untrusted input: they are parsed into these
pydantic models (which ignore unknown fields so new API fields never break the
bot) and only then converted into domain objects.  Anything malformed is rejected
per transaction, so one bad element cannot take down a whole poll.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from solana_monitor.domain.models import (
    NativeTransfer,
    TokenBalanceChange,
    TokenMetadata,
    TokenTransfer,
    Transaction,
)

FUNGIBLE_INTERFACES: Final = frozenset({"FungibleToken", "FungibleAsset"})
NFT_INTERFACES: Final = frozenset(
    {
        "V1_NFT",
        "V1_PRINT",
        "LEGACY_NFT",
        "ProgrammableNFT",
        "MplCoreAsset",
        "MplCoreCollection",
        "CompressedNFT",
        "NFT",
    }
)
NFT_TOKEN_STANDARDS: Final = frozenset(
    {
        "NonFungible",
        "NonFungibleEdition",
        "ProgrammableNonFungible",
        "ProgrammableNonFungibleEdition",
    }
)
_MAX_ERROR_CHARS: Final = 200


def is_fungible_interface(interface: str | None) -> bool:
    return interface in FUNGIBLE_INTERFACES


def is_nft_interface(interface: str | None) -> bool:
    return interface in NFT_INTERFACES


def is_nft_token_standard(token_standard: str | None) -> bool:
    return token_standard in NFT_TOKEN_STANDARDS


def _to_decimal(value: object) -> Decimal | None:
    """Coerce a JSON number or numeric string into a finite :class:`Decimal`."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return (
            Decimal(str(value))
            if value == value and value not in (float("inf"), float("-inf"))
            else None
        )
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            decoded = Decimal(text)
        except InvalidOperation:
            return None
        return decoded if decoded.is_finite() else None
    return None


class _Payload(BaseModel):
    """Base for wire models: ignore unknown fields, stay immutable."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


def _as_tuple(value: object) -> Any:
    """Treat ``null`` as an empty collection, as Helius sometimes sends it."""
    return () if value is None else value


class RawTokenAmountPayload(_Payload):
    token_amount: Decimal | None = Field(default=None, alias="tokenAmount")
    decimals: int | None = None

    @field_validator("token_amount", mode="before")
    @classmethod
    def _coerce_amount(cls, value: object) -> Decimal | None:
        return _to_decimal(value)


class TokenBalanceChangePayload(_Payload):
    user_account: str | None = Field(default=None, alias="userAccount")
    token_account: str | None = Field(default=None, alias="tokenAccount")
    mint: str = ""
    raw_token_amount: RawTokenAmountPayload | None = Field(default=None, alias="rawTokenAmount")
    token_standard: str | None = Field(default=None, alias="tokenStandard")


class AccountDataPayload(_Payload):
    account: str | None = None
    native_balance_change: int | None = Field(default=None, alias="nativeBalanceChange")
    token_balance_changes: tuple[TokenBalanceChangePayload, ...] = Field(
        default=(), alias="tokenBalanceChanges"
    )

    @field_validator("token_balance_changes", mode="before")
    @classmethod
    def _default_to_empty(cls, value: object) -> Any:
        return _as_tuple(value)


class TokenTransferPayload(_Payload):
    from_user_account: str | None = Field(default=None, alias="fromUserAccount")
    to_user_account: str | None = Field(default=None, alias="toUserAccount")
    from_token_account: str | None = Field(default=None, alias="fromTokenAccount")
    to_token_account: str | None = Field(default=None, alias="toTokenAccount")
    token_amount: Decimal | None = Field(default=None, alias="tokenAmount")
    mint: str = ""
    token_standard: str | None = Field(default=None, alias="tokenStandard")

    @field_validator("token_amount", mode="before")
    @classmethod
    def _coerce_amount(cls, value: object) -> Decimal | None:
        return _to_decimal(value)


class NativeTransferPayload(_Payload):
    from_user_account: str | None = Field(default=None, alias="fromUserAccount")
    to_user_account: str | None = Field(default=None, alias="toUserAccount")
    amount: int = 0


class EnhancedTransactionPayload(_Payload):
    """One element of ``GET /v0/addresses/{address}/transactions``."""

    signature: str
    slot: int = 0
    timestamp: float | None = None
    transaction_type: str = Field(default="UNKNOWN", alias="type")
    source: str | None = None
    description: str = ""
    fee: int = 0
    fee_payer: str | None = Field(default=None, alias="feePayer")
    native_transfers: tuple[NativeTransferPayload, ...] = Field(default=(), alias="nativeTransfers")
    token_transfers: tuple[TokenTransferPayload, ...] = Field(default=(), alias="tokenTransfers")
    account_data: tuple[AccountDataPayload, ...] = Field(default=(), alias="accountData")
    transaction_error: Any = Field(default=None, alias="transactionError")

    @field_validator("native_transfers", "token_transfers", "account_data", mode="before")
    @classmethod
    def _default_to_empty(cls, value: object) -> Any:
        return _as_tuple(value)

    @property
    def failed(self) -> bool:
        error = self.transaction_error
        if error is None or error is False:
            return False
        return not (isinstance(error, (dict, list, str, tuple)) and not error)

    @property
    def error_summary(self) -> str | None:
        """A short, log-safe description of ``transactionError``."""
        error = self.transaction_error
        if not self.failed:
            return None
        if isinstance(error, str):
            return error[:_MAX_ERROR_CHARS]
        try:
            return json.dumps(error, default=str)[:_MAX_ERROR_CHARS]
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return str(error)[:_MAX_ERROR_CHARS]

    @property
    def timestamp_utc(self) -> datetime | None:
        if not self.timestamp:
            return None
        return datetime.fromtimestamp(float(self.timestamp), tz=UTC)

    def to_domain(self) -> Transaction:
        """Convert to the domain model.

        Balance-change entries without a usable ``rawTokenAmount`` are skipped
        rather than guessed at with a zero delta.
        """
        native_changes = {
            entry.account: entry.native_balance_change
            for entry in self.account_data
            if entry.account is not None and entry.native_balance_change is not None
        }
        balance_changes: list[TokenBalanceChange] = []
        for entry in self.account_data:
            for change in entry.token_balance_changes:
                amount = change.raw_token_amount
                if change.user_account is None or amount is None or amount.token_amount is None:
                    continue
                balance_changes.append(
                    TokenBalanceChange(
                        mint=change.mint,
                        owner=change.user_account,
                        token_account=change.token_account,
                        amount_raw=amount.token_amount,
                        decimals=amount.decimals,
                        token_standard=change.token_standard,
                    )
                )
        return Transaction(
            signature=self.signature,
            slot=self.slot,
            timestamp=self.timestamp_utc,
            transaction_type=self.transaction_type or "UNKNOWN",
            source=self.source,
            description=self.description,
            fee_lamports=self.fee,
            fee_payer=self.fee_payer,
            failed=self.failed,
            error=self.error_summary,
            native_balance_changes=native_changes,
            token_balance_changes=tuple(balance_changes),
            token_transfers=tuple(
                TokenTransfer(
                    mint=transfer.mint,
                    from_account=transfer.from_user_account or transfer.from_token_account,
                    to_account=transfer.to_user_account or transfer.to_token_account,
                    amount_raw=transfer.token_amount,
                    token_standard=transfer.token_standard,
                )
                for transfer in self.token_transfers
                if transfer.mint
            ),
            native_transfers=tuple(
                NativeTransfer(
                    from_account=transfer.from_user_account,
                    to_account=transfer.to_user_account,
                    amount_lamports=transfer.amount,
                )
                for transfer in self.native_transfers
            ),
        )


class DasMetadataPayload(_Payload):
    name: str | None = None
    symbol: str | None = None


class DasContentPayload(_Payload):
    metadata: DasMetadataPayload | None = None


class DasTokenInfoPayload(_Payload):
    decimals: int | None = None
    balance: Decimal | None = None
    symbol: str | None = None

    @field_validator("balance", mode="before")
    @classmethod
    def _coerce_balance(cls, value: object) -> Decimal | None:
        return _to_decimal(value)


class DasAssetPayload(_Payload):
    """One element of a DAS ``getAsset``/``getAssetsByOwner`` response."""

    mint_id: str = Field(default="", alias="id")
    interface: str | None = None
    content: DasContentPayload | None = None
    token_info: DasTokenInfoPayload | None = Field(default=None, alias="token_info")

    @property
    def is_fungible(self) -> bool:
        if is_fungible_interface(self.interface):
            return True
        if is_nft_interface(self.interface):
            return False
        decimals = self.token_info.decimals if self.token_info else None
        return bool(decimals)

    @property
    def is_nft(self) -> bool:
        return is_nft_interface(self.interface)

    @property
    def balance(self) -> Decimal | None:
        return self.token_info.balance if self.token_info else None

    def to_metadata(self) -> TokenMetadata:
        metadata = self.content.metadata if self.content else None
        token_info = self.token_info
        return TokenMetadata(
            mint=self.mint_id,
            symbol=(metadata.symbol if metadata else None)
            or (token_info.symbol if token_info else None),
            name=(metadata.name if metadata else None),
            decimals=token_info.decimals if token_info else None,
            is_fungible=self.is_fungible,
        )

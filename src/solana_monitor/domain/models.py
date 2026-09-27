"""Domain models.

These types are the vocabulary shared by the detector, the monitor, the
persistence layer and the presentation layer.  They are immutable value objects
with no knowledge of HTTP, JSON or Telegram, which is what makes the business
rules testable without any I/O.

Token amounts are kept as :class:`~decimal.Decimal` (never ``float``) because raw
on-chain amounts routinely exceed the range ``float`` represents exactly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

ZERO: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class TokenBalanceChange:
    """Per-token balance delta reported by Helius for one owner account."""

    mint: str
    owner: str
    token_account: str | None
    amount_raw: Decimal
    decimals: int | None = None
    token_standard: str | None = None


@dataclass(frozen=True, slots=True)
class TokenTransfer:
    """A single SPL token movement inside a transaction."""

    mint: str
    from_account: str | None
    to_account: str | None
    amount_raw: Decimal | None
    token_standard: str | None = None


@dataclass(frozen=True, slots=True)
class NativeTransfer:
    """A single SOL (lamport) movement inside a transaction."""

    from_account: str | None
    to_account: str | None
    amount_lamports: int


@dataclass(frozen=True, slots=True)
class Transaction:
    """An enhanced (parsed) Solana transaction, reduced to what the bot needs."""

    signature: str
    slot: int
    timestamp: datetime | None
    transaction_type: str
    source: str | None
    description: str
    fee_lamports: int
    fee_payer: str | None
    failed: bool
    error: str | None
    native_balance_changes: Mapping[str, int]
    token_balance_changes: tuple[TokenBalanceChange, ...]
    token_transfers: tuple[TokenTransfer, ...]
    native_transfers: tuple[NativeTransfer, ...]

    def native_change(self, account: str) -> int:
        """Net lamport change for ``account`` (0 when the account is not listed)."""
        return self.native_balance_changes.get(account, 0)

    def net_token_changes(self, owner: str) -> dict[str, Decimal]:
        """Net token delta per mint for ``owner``, excluding zero deltas.

        Prefers Helius' authoritative ``tokenBalanceChanges`` and falls back to
        netting ``tokenTransfers`` when that data is absent.
        """
        deltas: dict[str, Decimal] = {}
        from_balance_changes = False
        for change in self.token_balance_changes:
            if change.owner != owner:
                continue
            from_balance_changes = True
            deltas[change.mint] = deltas.get(change.mint, ZERO) + change.amount_raw
        if not from_balance_changes:
            for transfer in self.token_transfers:
                if transfer.amount_raw is None:
                    continue
                if transfer.to_account == owner:
                    deltas[transfer.mint] = deltas.get(transfer.mint, ZERO) + transfer.amount_raw
                elif transfer.from_account == owner:
                    deltas[transfer.mint] = deltas.get(transfer.mint, ZERO) - transfer.amount_raw
        return {mint: delta for mint, delta in deltas.items() if delta != ZERO}

    def decimals_for(self, mint: str) -> int | None:
        """Token decimals reported for ``mint``, when Helius included them."""
        for change in self.token_balance_changes:
            if change.mint == mint and change.decimals is not None:
                return change.decimals
        return None

    def token_standard_for(self, mint: str) -> str | None:
        """Token standard reported for ``mint``, when Helius included it."""
        for change in self.token_balance_changes:
            if change.mint == mint and change.token_standard:
                return change.token_standard
        for transfer in self.token_transfers:
            if transfer.mint == mint and transfer.token_standard:
                return transfer.token_standard
        return None

    def sol_spent_lamports(self, account: str) -> int | None:
        """SOL paid out by ``account``, excluding the transaction fee.

        ``None`` means "unknown" (Helius returned neither balance changes nor
        native transfers); callers must distinguish that from "zero".

        Balance changes *include* the fee, native transfers do not, so the fee is
        only subtracted on the balance-change path.
        """
        fee = self.fee_lamports if self.fee_payer == account else 0
        if account in self.native_balance_changes:
            return max(0, -(self.native_balance_changes[account] + fee))
        if not self.native_transfers:
            return None
        received = sum(
            transfer.amount_lamports
            for transfer in self.native_transfers
            if transfer.to_account == account
        )
        sent = sum(
            transfer.amount_lamports
            for transfer in self.native_transfers
            if transfer.from_account == account
        )
        return max(0, sent - received)


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """Symbol/decimals as reported by the Helius DAS API."""

    mint: str
    symbol: str | None
    name: str | None
    decimals: int | None
    is_fungible: bool


class AcquisitionKind(StrEnum):
    """Why a token arrived in the watched wallet."""

    PURCHASE = "purchase"
    """The wallet gave up SOL or another token to receive this mint."""

    RECEIPT = "receipt"
    """The mint arrived without an obvious payment (airdrop/transfer in)."""


@dataclass(frozen=True, slots=True)
class TokenAcquisition:
    """A detection result: ``amount_raw`` of ``mint`` arrived in the wallet."""

    mint: str
    amount_raw: Decimal
    decimals: int | None
    symbol: str | None
    kind: AcquisitionKind
    signature: str
    slot: int
    timestamp: datetime | None
    description: str
    sol_spent_lamports: int | None
    token_standard: str | None = None

    @property
    def amount(self) -> Decimal | None:
        """Amount in whole tokens, or ``None`` when the decimals are unknown."""
        if self.decimals is None:
            return None
        return self.amount_raw.scaleb(-self.decimals)

    @property
    def is_purchase(self) -> bool:
        return self.kind is AcquisitionKind.PURCHASE

    def with_metadata(self, metadata: TokenMetadata | None) -> TokenAcquisition:
        """Return a copy with gaps filled from ``metadata``.

        Values already known from the transaction always win: metadata must never
        overwrite what the chain told us.
        """
        if metadata is None:
            return self
        return replace(
            self,
            symbol=self.symbol or metadata.symbol,
            decimals=self.decimals if self.decimals is not None else metadata.decimals,
        )


@dataclass(frozen=True, slots=True)
class WatchStatusView:
    """Read model describing one watch, used by ``/status``."""

    chat_id: int
    wallet: str | None
    monitoring: bool
    primed: bool
    alerts_sent: int
    acquisitions_detected: int
    transactions_scanned: int
    polls: int
    consecutive_errors: int
    last_poll_at: datetime | None
    last_alert_at: datetime | None
    last_error: str | None
    poll_interval_seconds: float
    baseline_size: int
    started_at: datetime | None


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Read model for ``/analyze``: acquisitions found inside a time window."""

    wallet: str
    period: str
    window_start: datetime
    generated_at: datetime
    acquisitions: tuple[TokenAcquisition, ...]
    transactions_scanned: int
    truncated: bool
    unique_mints: int

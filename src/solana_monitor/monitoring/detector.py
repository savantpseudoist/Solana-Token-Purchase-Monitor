"""Purchase detection.

Detection is deliberately based on *balance deltas* rather than on Helius'
``type`` field.  Token moves that qualify as a "purchase" (a token sold, a token
bought, a transfer in) are frequently typed ``SWAP``, ``TRANSFER`` or even
``UNKNOWN`` depending on the program Helius recognises, so trusting the type
missed real buys.  Looking at what actually changed in the wallet's balances is
program-agnostic and self-correcting.

Classification:

* ``PURCHASE`` - the wallet gave up SOL (after fees) or another token;
* ``RECEIPT``  - the mint arrived with no observable payment (airdrop, transfer).

Suppression (NFTs, ignored mints such as wrapped SOL) happens here so that no
other layer has to reimplement the rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from solana_monitor.config import Settings
from solana_monitor.domain.models import AcquisitionKind, TokenAcquisition, Transaction
from solana_monitor.helius.schemas import is_nft_token_standard

#: A mint with zero decimals can only be split into whole units; receiving exactly
#: one such unit is overwhelmingly an NFT rather than a tradeable token.
NFT_HEURISTIC_AMOUNT: Final = 1


@dataclass(frozen=True, slots=True)
class DetectorPolicy:
    """Tunable detection rules."""

    ignored_mints: frozenset[str] = frozenset()
    alert_on_receipts: bool = True
    drop_nft_heuristic_tokens: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> DetectorPolicy:
        return cls(
            ignored_mints=frozenset(settings.monitor_ignored_mints),
            alert_on_receipts=settings.monitor_alert_on_receipts,
        )


class PurchaseDetector:
    def __init__(self, policy: DetectorPolicy | None = None) -> None:
        self._policy = policy or DetectorPolicy()

    def detect(self, transaction: Transaction, wallet: str) -> tuple[TokenAcquisition, ...]:
        """Return every acquisition of ``transaction`` for ``wallet``."""
        if transaction.failed:
            return ()

        deltas = transaction.net_token_changes(wallet)
        if not deltas:
            return ()

        sol_spent = transaction.sol_spent_lamports(wallet)
        paid_with_another_token = any(delta < 0 for delta in deltas.values())
        is_purchase = bool(sol_spent) or paid_with_another_token

        acquisitions: list[TokenAcquisition] = []
        for mint, delta in deltas.items():
            if delta <= 0 or mint in self._policy.ignored_mints:
                continue
            decimals = transaction.decimals_for(mint)
            standard = transaction.token_standard_for(mint)
            if self._is_nft(delta, decimals, standard):
                continue
            if not is_purchase and not self._policy.alert_on_receipts:
                continue
            acquisitions.append(
                TokenAcquisition(
                    mint=mint,
                    amount_raw=delta,
                    decimals=decimals,
                    symbol=None,
                    kind=AcquisitionKind.PURCHASE if is_purchase else AcquisitionKind.RECEIPT,
                    signature=transaction.signature,
                    slot=transaction.slot,
                    timestamp=transaction.timestamp,
                    description=transaction.description,
                    sol_spent_lamports=sol_spent,
                    token_standard=standard,
                )
            )
        return tuple(acquisitions)

    def _is_nft(self, delta: object, decimals: int | None, standard: str | None) -> bool:
        if is_nft_token_standard(standard):
            return True
        if not self._policy.drop_nft_heuristic_tokens:
            return False
        return decimals == 0 and delta == NFT_HEURISTIC_AMOUNT

"""Solana address validation.

Addresses are base58-encoded 32-byte public keys.  Validation is deliberately
implemented here (rather than pulled in as a dependency) because it is a small,
well-specified algorithm and because every externally supplied address must be
checked before it is used in a URL path or a Telegram link.
"""

from __future__ import annotations

from typing import Final

from solana_monitor.domain.errors import InvalidAddressError

_ALPHABET: Final = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ALPHABET_INDEX: Final = {character: index for index, character in enumerate(_ALPHABET)}
_ADDRESS_BYTES: Final = 32
_MIN_TEXT_LENGTH: Final = 32
_MAX_TEXT_LENGTH: Final = 44


def decode_base58(value: str) -> bytes:
    """Decode a base58 string, raising :class:`ValueError` on invalid characters."""
    number = 0
    for character in value:
        digit = _ALPHABET_INDEX.get(character)
        if digit is None:
            msg = f"invalid base58 character {character!r}"
            raise ValueError(msg)
        number = number * 58 + digit
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeros = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeros + body


def validate_address(value: str, *, field: str = "address") -> str:
    """Return the canonical form of ``value`` or raise :class:`InvalidAddressError`."""
    candidate = (value or "").strip()
    if not candidate:
        raise InvalidAddressError(value, reason=f"{field} is empty")
    if not _MIN_TEXT_LENGTH <= len(candidate) <= _MAX_TEXT_LENGTH:
        raise InvalidAddressError(
            candidate,
            reason=f"{field} must be {_MIN_TEXT_LENGTH}-{_MAX_TEXT_LENGTH} characters long",
        )
    try:
        decoded = decode_base58(candidate)
    except ValueError as error:
        raise InvalidAddressError(candidate, reason=f"{field} is not valid base58") from error
    if len(decoded) != _ADDRESS_BYTES:
        raise InvalidAddressError(
            candidate,
            reason=f"{field} must decode to exactly {_ADDRESS_BYTES} bytes",
        )
    return candidate


def is_valid_address(value: object) -> bool:
    """Non-raising form of :func:`validate_address`, for URL building and filters."""
    if not isinstance(value, str):
        return False
    try:
        validate_address(value)
    except InvalidAddressError:
        return False
    return True

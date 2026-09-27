"""Tests for Solana address validation."""

from __future__ import annotations

import pytest

from solana_monitor.domain.addresses import decode_base58, is_valid_address, validate_address
from solana_monitor.domain.constants import WRAPPED_SOL_MINT
from solana_monitor.domain.errors import InvalidAddressError

SYSTEM_PROGRAM = "11111111111111111111111111111111"
WATCHED_WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"
TOKEN_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.mark.parametrize("address", [WRAPPED_SOL_MINT, SYSTEM_PROGRAM, WATCHED_WALLET, TOKEN_MINT])
def test_valid_addresses_are_accepted(address: str) -> None:
    assert validate_address(address) == address
    assert is_valid_address(address) is True


def test_surrounding_whitespace_is_trimmed() -> None:
    assert validate_address(f"  {WATCHED_WALLET}\n") == WATCHED_WALLET


@pytest.mark.parametrize(
    "address",
    [
        "",
        "   ",
        "too-short",
        "0OIl",  # characters that do not exist in the base58 alphabet
        "So11111111111111111111111111111111111111112x",  # 45 characters
        "z" * 44,  # valid base58, but not 32 bytes
    ],
)
def test_invalid_addresses_are_rejected(address: str) -> None:
    assert is_valid_address(address) is False
    with pytest.raises(InvalidAddressError):
        validate_address(address)


def test_error_explains_the_reason_and_length() -> None:
    with pytest.raises(InvalidAddressError) as excinfo:
        validate_address("tooshort")

    error = excinfo.value
    assert error.reason
    assert error.context["length"] == len("tooshort")
    assert "tooshort" in str(error)


def test_error_message_uses_the_field_name() -> None:
    with pytest.raises(InvalidAddressError, match="wallet"):
        validate_address("nope", field="wallet")


def test_is_valid_address_tolerates_non_strings() -> None:
    assert is_valid_address(None) is False
    assert is_valid_address(42) is False


def test_base58_round_trip_of_the_system_program() -> None:
    assert decode_base58(SYSTEM_PROGRAM) == b"\x00" * 32


def test_decode_rejects_invalid_characters() -> None:
    with pytest.raises(ValueError, match="invalid base58 character"):
        decode_base58("l")

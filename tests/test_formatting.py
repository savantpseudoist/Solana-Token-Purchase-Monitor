"""Tests for message formatting and escaping."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from solana_monitor.config import Settings
from solana_monitor.domain.models import (
    AcquisitionKind,
    AnalysisResult,
    TokenAcquisition,
    WatchStatusView,
)
from solana_monitor.telegram import formatting
from solana_monitor.telegram.registry import COMMANDS
from tests import factories as f
from tests.conftest import VALID_API_KEY, VALID_BOT_TOKEN


def settings(**env: str) -> Settings:
    values: dict[str, object] = {
        "TELEGRAM_BOT_TOKEN": VALID_BOT_TOKEN,
        "HELIUS_API_KEY": VALID_API_KEY,
        **env,
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def acquisition(**overrides: object) -> TokenAcquisition:
    base = TokenAcquisition(
        mint=f.TOKEN_MINT,
        amount_raw=Decimal("1500000"),
        decimals=6,
        symbol="USDC",
        kind=AcquisitionKind.PURCHASE,
        signature=f.SIGNATURE,
        slot=1,
        timestamp=datetime(2026, 9, 27, 11, 0, tzinfo=UTC),
        description="User swapped 1 SOL for 1000 USDC",
        sol_spent_lamports=1_000_000_000,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# -- Primitives --------------------------------------------------------------
@pytest.mark.parametrize(
    ("amount", "decimals", "expected"),
    [
        (Decimal("1.5"), 6, "1.5"),
        (Decimal("1000"), 6, "1,000"),
        (Decimal("1234.500000"), 6, "1,234.5"),
        (Decimal("0.000001"), 6, "0.000001"),
        (Decimal("1.23456789"), 6, "1.234568"),
        (None, 6, "unknown"),
        (Decimal("1500"), None, "unknown"),
    ],
)
def test_format_amount(amount: Decimal | None, decimals: int | None, expected: str) -> None:
    assert formatting.format_amount(amount, decimals) == expected


def test_format_sol() -> None:
    assert formatting.format_sol(1_500_000_000) == "1.5"
    assert formatting.format_sol(1000) == "0.000001"
    assert formatting.format_sol(0) == "0"
    assert formatting.format_sol(None) == "unknown"


def test_shorten_address() -> None:
    assert formatting.shorten_address(f.WATCHED_WALLET) == "9WzD...AWWM"
    assert formatting.shorten_address(None) == "—"
    assert formatting.shorten_address("short") == "short"


def test_format_timestamp_and_uptime() -> None:
    assert formatting.format_timestamp(None) == "unknown"
    moment = datetime(2026, 9, 27, 11, 30, tzinfo=UTC)
    assert formatting.format_timestamp(moment) == "2026-09-27 11:30:00 UTC"

    start = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
    assert formatting.format_uptime(start, moment) == "1d 11h"
    assert formatting.format_uptime(None) == "unknown"


def test_escape_neutralises_html() -> None:
    assert formatting.escape("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"


# -- Alerts ------------------------------------------------------------------
def test_alert_contains_the_essentials() -> None:
    reply = formatting.build_alert(acquisition(), wallet=f.WATCHED_WALLET, settings=settings())

    assert "New token purchase" in reply.text
    assert "USDC" in reply.text
    assert "Amount:</b> 1.5" in reply.text
    assert "Spent:</b> 1 SOL" in reply.text
    assert f.TOKEN_MINT in reply.text
    assert "2026-09-27 11:00:00 UTC" in reply.text
    assert reply.disable_preview is True


def test_alert_escapes_untrusted_symbol_and_description() -> None:
    hostile = acquisition(symbol="<b>SCAM</b>", description="<script>alert('x')</script>")

    reply = formatting.build_alert(hostile, wallet=f.WATCHED_WALLET, settings=settings())

    assert "<b>SCAM</b>" not in reply.text
    assert "&lt;b&gt;SCAM&lt;/b&gt;" in reply.text
    assert "<script>" not in reply.text


def test_alert_falls_back_to_a_short_mint_when_the_symbol_is_unknown() -> None:
    reply = formatting.build_alert(
        acquisition(symbol=None), wallet=f.WATCHED_WALLET, settings=settings()
    )

    assert f.TOKEN_MINT[:4] in reply.text


def test_alert_shows_unknown_amounts_honestly() -> None:
    reply = formatting.build_alert(
        acquisition(decimals=None), wallet=f.WATCHED_WALLET, settings=settings()
    )

    assert "Amount:</b> unknown" in reply.text


def test_receipts_are_labelled_differently() -> None:
    reply = formatting.build_alert(
        acquisition(kind=AcquisitionKind.RECEIPT),
        wallet=f.WATCHED_WALLET,
        settings=settings(),
    )

    assert "New token received" in reply.text


def test_alert_links_are_built_only_for_valid_mints() -> None:
    good = formatting.build_alert(acquisition(), wallet=f.WATCHED_WALLET, settings=settings())
    bad = formatting.build_alert(
        acquisition(mint="../../etc/passwd"), wallet=f.WATCHED_WALLET, settings=settings()
    )

    assert [link.url for link in good.links] == [
        f"https://dexscreener.com/solana/{f.TOKEN_MINT}",
        f"https://t.me/achilles_trojanbot?start={f.TOKEN_MINT}",
    ]
    assert bad.links == ()


def test_alert_links_can_be_disabled() -> None:
    reply = formatting.build_alert(
        acquisition(), wallet=f.WATCHED_WALLET, settings=settings(ALERTS_TROJAN_BOT_USERNAME="")
    )

    assert [link.text for link in reply.links] == ["Chart"]


# -- Status, analysis, help --------------------------------------------------
def status_view(**overrides: object) -> WatchStatusView:
    values: dict[str, object] = {
        "chat_id": -100,
        "wallet": f.WATCHED_WALLET,
        "monitoring": True,
        "primed": True,
        "alerts_sent": 3,
        "acquisitions_detected": 4,
        "transactions_scanned": 42,
        "polls": 7,
        "consecutive_errors": 0,
        "last_poll_at": datetime(2026, 9, 27, 11, 0, tzinfo=UTC),
        "last_alert_at": None,
        "last_error": None,
        "poll_interval_seconds": 20.0,
        "baseline_size": 5,
        "started_at": datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
        **overrides,
    }
    return WatchStatusView(**values)  # type: ignore[arg-type]


def test_status_reply_reports_health_and_counters() -> None:
    reply = formatting.build_status(status_view(), settings())

    assert "running" in reply.text
    assert "healthy" in reply.text
    assert "Alerts sent:</b> 3" in reply.text
    assert "Baseline tokens:</b> 5" in reply.text


def test_status_reply_surfaces_degradation() -> None:
    degraded = status_view(consecutive_errors=4, last_error="HeliusError: boom")

    reply = formatting.build_status(degraded, settings())

    assert "degraded" in reply.text
    assert "HeliusError" in reply.text


def test_analysis_reply_lists_purchases() -> None:
    result = AnalysisResult(
        wallet=f.WATCHED_WALLET,
        period="1h",
        window_start=datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
        generated_at=datetime(2026, 9, 27, 11, 0, tzinfo=UTC),
        acquisitions=(acquisition(),),
        transactions_scanned=10,
        truncated=False,
        unique_mints=1,
    )

    reply = formatting.build_analysis(result)

    assert "last 1h" in reply.text
    assert "USDC: 1.5" in reply.text
    assert "truncated" not in reply.text


def test_analysis_reply_flags_truncation() -> None:
    result = AnalysisResult(
        wallet=f.WATCHED_WALLET,
        period="1w",
        window_start=datetime(2026, 9, 27, 10, 0, tzinfo=UTC),
        generated_at=datetime(2026, 9, 27, 11, 0, tzinfo=UTC),
        acquisitions=(),
        transactions_scanned=1000,
        truncated=True,
        unique_mints=0,
    )

    reply = formatting.build_analysis(result)

    assert "truncated" in reply.text
    assert "No new tokens" in reply.text


def test_help_marks_admin_commands() -> None:
    reply = formatting.build_help(COMMANDS, admins_configured=True)

    assert "/analyze" in reply.text
    assert "(admin only)" in reply.text
    assert "No administrators" not in reply.text


def test_error_reply_is_escaped() -> None:
    reply = formatting.build_error("<script>alert(1)</script>")

    assert "<script>" not in reply.text
    assert "&lt;script&gt;" in reply.text

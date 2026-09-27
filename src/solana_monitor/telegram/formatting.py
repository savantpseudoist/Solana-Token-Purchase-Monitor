"""Rendering of alerts and command replies.

Everything is emitted as Telegram-flavoured HTML, and every value that originates
outside this codebase (token symbols, descriptions, addresses) is HTML-escaped.
The old bot used Markdown with unescaped, externally supplied text, which made
both formatting breakage and injection-style output possible.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from solana_monitor.config import Settings
from solana_monitor.domain.addresses import is_valid_address
from solana_monitor.domain.models import AnalysisResult, TokenAcquisition, WatchStatusView
from solana_monitor.telegram.registry import CommandSpec

_TIMESTAMP_FORMAT: Final = "%Y-%m-%d %H:%M:%S UTC"
_UNKNOWN: Final = "unknown"
_MAX_DESCRIPTION_CHARS: Final = 120
_DASH: Final = "—"


@dataclass(frozen=True, slots=True)
class InlineLink:
    """A URL button; the bot never renders a URL it did not validate."""

    text: str
    url: str


@dataclass(frozen=True, slots=True)
class Reply:
    """A framework-free answer ready to be sent to a chat."""

    text: str
    links: tuple[InlineLink, ...] = ()
    disable_preview: bool = True


def escape(value: object) -> str:
    """HTML-escape any externally supplied value."""
    return html.escape(str(value), quote=False)


def format_amount(amount: Decimal | None, decimals: int | None) -> str:
    """Format an amount that is *already* scaled to whole tokens.

    ``decimals`` only caps the printed precision; ``None`` means the decimals are
    unknown, in which case we say so rather than printing a misleading number.
    """
    if amount is None or decimals is None:
        return _UNKNOWN
    quantised = amount.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    text = f"{quantised:,f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_sol(lamports: int | None) -> str:
    """Format lamports as SOL at full precision, trimming trailing zeros."""
    if lamports is None:
        return _UNKNOWN
    sol = Decimal(lamports).scaleb(-9)
    text = f"{sol:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_timestamp(moment: datetime | None) -> str:
    if moment is None:
        return _UNKNOWN
    return moment.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def format_uptime(started_at: datetime | None, now: datetime | None = None) -> str:
    if started_at is None:
        return _UNKNOWN
    moment = now or datetime.now(UTC)
    seconds = int((moment - started_at).total_seconds())
    if seconds < 0:
        return "0m"
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def shorten_address(address: str | None, *, head: int = 4, tail: int = 4) -> str:
    if not address:
        return _DASH
    if len(address) <= head + tail + 1:
        return address
    return f"{address[:head]}...{address[-tail:]}"


def _label(symbol: str | None, mint: str) -> str:
    cleaned = (symbol or "").strip()
    return cleaned if cleaned else shorten_address(mint, head=4, tail=4)


def _alerts_links(acquisition: TokenAcquisition, settings: Settings) -> tuple[InlineLink, ...]:
    links: list[InlineLink] = []
    if is_valid_address(acquisition.mint):
        links.append(
            InlineLink(
                text="Chart", url=f"{settings.alerts_dexscreener_base_url}/{acquisition.mint}"
            )
        )
        if settings.alerts_trojan_bot_username:
            links.append(
                InlineLink(
                    text="Buy",
                    url=f"https://t.me/{settings.alerts_trojan_bot_username}?start={acquisition.mint}",
                )
            )
    return tuple(links)


def build_alert(acquisition: TokenAcquisition, *, wallet: str, settings: Settings) -> Reply:
    """Render a purchase alert."""
    headline = (
        "🟢 <b>New token purchase</b>"
        if acquisition.is_purchase
        else "📥 <b>New token received</b>"
    )
    amount = format_amount(acquisition.amount, acquisition.decimals)
    symbol = _label(acquisition.symbol, acquisition.mint)
    lines = [
        headline,
        "",
        f"<b>Token:</b> {escape(symbol)}",
        f"<b>Amount:</b> {escape(amount)}",
    ]
    if acquisition.is_purchase and acquisition.sol_spent_lamports:
        lines.append(f"<b>Spent:</b> {escape(format_sol(acquisition.sol_spent_lamports))} SOL")
    lines += [
        f"<b>Wallet:</b> <code>{escape(shorten_address(wallet))}</code>",
        f"<b>CA:</b> <code>{escape(acquisition.mint)}</code>",
        f"<b>Time:</b> {escape(format_timestamp(acquisition.timestamp))}",
    ]
    description = acquisition.description.strip()
    if description:
        lines.append(f"<i>{escape(description[:_MAX_DESCRIPTION_CHARS])}</i>")
    return Reply(
        text="\n".join(lines),
        links=_alerts_links(acquisition, settings),
        disable_preview=True,
    )


def build_wallet_set(wallet: str, *, holdings: int | None, was_monitoring: bool) -> Reply:
    """Reply to ``/set_wallet``."""
    holdings_text = _UNKNOWN if holdings is None else str(holdings)
    lines = [
        "✅ <b>Wallet updated</b>",
        "",
        f"<code>{escape(wallet)}</code>",
        "",
        f"Holding {escape(holdings_text)} fungible token(s) before monitoring starts.",
    ]
    if was_monitoring:
        lines.append("Monitoring was restarted on the new wallet.")
    else:
        lines.append("Send <code>/start_monitoring</code> to begin.")
    return Reply(text="\n".join(lines))


def build_monitoring_started(wallet: str, *, holdings: int | None) -> Reply:
    holdings_text = _UNKNOWN if holdings is None else str(holdings)
    return Reply(
        "\n".join(
            [
                "👀 <b>Monitoring started</b>",
                "",
                f"<code>{escape(wallet)}</code>",
                f"Baseline: {escape(holdings_text)} token(s) already held (never alerted).",
            ]
        )
    )


def build_monitoring_stopped() -> Reply:
    return Reply(text="🛑 <b>Monitoring stopped</b>")


def build_status(status: WatchStatusView, settings: Settings) -> Reply:
    """Reply to ``/status``."""
    monitoring = "🟢 running" if status.monitoring else "⚪️ stopped"
    health = "🟢 healthy" if status.consecutive_errors == 0 else "🟠 degraded"
    lines = [
        "<b>🤖 Watch status</b>",
        "",
        f"<b>Wallet:</b> <code>{escape(status.wallet or _DASH)}</code>",
        f"<b>Monitoring:</b> {monitoring}",
        f"<b>API:</b> {health}",
        f"<b>Poll interval:</b> {settings.monitor_poll_interval_seconds:g}s",
        f"<b>Polls:</b> {status.polls}",
        f"<b>Transactions scanned:</b> {status.transactions_scanned}",
        f"<b>Alerts sent:</b> {status.alerts_sent}",
        f"<b>Baseline tokens:</b> {status.baseline_size}",
        f"<b>Last poll:</b> {escape(format_timestamp(status.last_poll_at))}",
        f"<b>Last alert:</b> {escape(format_timestamp(status.last_alert_at))}",
    ]
    if status.last_error:
        lines.append(f"<b>Last error:</b> {escape(status.last_error[:200])}")
    return Reply(text="\n".join(lines))


def build_analysis(result: AnalysisResult) -> Reply:
    """Reply to ``/analyze``."""
    lines = [
        f"<b>📊 Purchases in the last {escape(result.period)}</b>",
        "",
        f"<b>Wallet:</b> <code>{escape(shorten_address(result.wallet))}</code>",
        f"<b>Transactions scanned:</b> {result.transactions_scanned}",
        f"<b>Unique tokens:</b> {result.unique_mints}",
        "",
    ]
    if not result.acquisitions:
        lines.append("No new tokens acquired in this window.")
    else:
        lines.append("<b>Most recent first:</b>")
        for acquisition in result.acquisitions:
            amount = format_amount(acquisition.amount, acquisition.decimals)
            symbol = _label(acquisition.symbol, acquisition.mint)
            kind = "purchase" if acquisition.is_purchase else "received"
            lines.append(
                f"• {escape(symbol)}: {escape(amount)} "
                f"({escape(format_timestamp(acquisition.timestamp))}, {kind})"
            )
    if result.truncated:
        lines.append("")
        lines.append("⚠️ History was truncated; older purchases in this window are not shown.")
    lines.append("")
    lines.append(f"<i>Generated {escape(format_timestamp(result.generated_at))}</i>")
    return Reply(text="\n".join(lines), disable_preview=True)


def build_help(commands: Sequence[CommandSpec], *, admins_configured: bool) -> Reply:
    lines = ["<b>🤖 Available commands</b>", ""]
    for spec in commands:
        suffix = " <i>(admin only)</i>" if spec.requires_admin else ""
        lines.append(f"<code>{escape(spec.usage)}</code> — {escape(spec.summary)}{suffix}")
    if not admins_configured:
        hint = (
            "<i>No administrators are configured yet: /set_wallet, "
            "/start_monitoring and /stop_monitoring are disabled. Send /whoami "
            "and set TELEGRAM_ADMIN_USER_IDS.</i>"
        )
        lines += ["", hint]
    return Reply(text="\n".join(lines))


def build_whoami(user_id: int, chat_id: int) -> Reply:
    hint = (
        "<i>Add the user ID to TELEGRAM_ADMIN_USER_IDS (comma separated) to "
        "allow changing the monitored wallet.</i>"
    )
    lines = [
        "<b>Your Telegram identifiers</b>",
        "",
        f"<b>User ID:</b> <code>{user_id}</code>",
        f"<b>Chat ID:</b> <code>{chat_id}</code>",
        "",
        hint,
    ]
    return Reply(text="\n".join(lines))


def build_error(message: str) -> Reply:
    return Reply(text=f"❌ {escape(message)}")

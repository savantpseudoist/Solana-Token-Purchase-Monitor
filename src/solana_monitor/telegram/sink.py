"""Delivery of alerts to Telegram, with retry and honest error semantics."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError

from solana_monitor.config import Settings
from solana_monitor.domain.errors import NotificationError
from solana_monitor.domain.models import TokenAcquisition
from solana_monitor.resilience import retry_async
from solana_monitor.telegram.formatting import Reply, build_alert

logger = logging.getLogger(__name__)

_SEND_TIMEOUT_SECONDS = 20.0
_MAX_SEND_ATTEMPTS = 4


class MessageSender(Protocol):
    """The slice of ``telegram.Bot`` used to deliver a message."""

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        disable_web_page_preview: bool | None = None,
        read_timeout: float | None = None,
    ) -> object: ...


def classify_notification_error(error: BaseException) -> NotificationError:
    """Translate a Telegram SDK error into ours, preserving retryability.

    ``BadRequest`` is checked before ``NetworkError`` because python-telegram-bot
    models it as a subclass, yet retrying a malformed request can never succeed.
    """
    if isinstance(error, Forbidden):
        detail = "the bot cannot post in this chat (blocked or removed?)"
        return NotificationError(detail, retryable=False)
    if isinstance(error, BadRequest):
        detail = f"Telegram rejected the request: {type(error).__name__}"
        return NotificationError(detail, retryable=False)
    if isinstance(error, NetworkError):
        detail = f"Telegram request failed: {type(error).__name__}"
        return NotificationError(detail, retryable=True)
    detail = f"Telegram request failed: {type(error).__name__}"
    return NotificationError(detail, retryable=False)


class TelegramAlertSink:
    """Sends purchase alerts for one chat, formatting them with escaping."""

    def __init__(
        self,
        *,
        sender: MessageSender,
        chat_id: int,
        wallet: str,
        settings: Settings,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._sender = sender
        self._chat_id = chat_id
        self._wallet = wallet
        self._settings = settings
        self._sleep = sleep

    @property
    def chat_id(self) -> int:
        return self._chat_id

    async def send(self, acquisition: TokenAcquisition) -> None:
        reply = build_alert(acquisition, wallet=self._wallet, settings=self._settings)
        await self.deliver(reply)

    async def deliver(self, reply: Reply) -> None:
        """Send a reply, retrying transient Telegram failures with backoff."""
        keyboard = (
            InlineKeyboardMarkup(
                [[InlineKeyboardButton(link.text, url=link.url) for link in reply.links]]
            )
            if reply.links
            else None
        )

        async def attempt() -> None:
            try:
                await self._sender.send_message(
                    chat_id=self._chat_id,
                    text=reply.text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=reply.disable_preview,
                    read_timeout=_SEND_TIMEOUT_SECONDS,
                )
            except Exception as error:
                raise classify_notification_error(error) from error

        try:
            await retry_async(
                attempt,
                attempts=_MAX_SEND_ATTEMPTS,
                base_delay=1.0,
                max_delay=10.0,
                should_retry=lambda error: (
                    isinstance(error, NotificationError) and bool(error.retryable)
                ),
                sleep=self._sleep,
            )
        except NotificationError as error:
            logger.warning(
                "could not deliver alert", extra={"chat_id": self._chat_id, "error": str(error)}
            )
            raise

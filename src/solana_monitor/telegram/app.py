"""Application lifecycle: the only module that talks to python-telegram-bot.

Handlers are thin adapters: they extract primitives from an ``Update``, call
:class:`~solana_monitor.telegram.commands.CommandService`, and send the resulting
:class:`~solana_monitor.telegram.formatting.Reply`.  Business rules stay in the
service, where they are testable offline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Final, cast

from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from solana_monitor.config import Settings
from solana_monitor.domain.errors import MonitorError
from solana_monitor.helius.client import HeliusClient
from solana_monitor.helius.metadata import TokenMetadataResolver
from solana_monitor.monitoring.detector import DetectorPolicy, PurchaseDetector
from solana_monitor.monitoring.manager import WatchManager
from solana_monitor.monitoring.monitor import MonitorConfig, WalletMonitor
from solana_monitor.monitoring.state import WatchState, WatchStore
from solana_monitor.telegram.commands import CommandService
from solana_monitor.telegram.formatting import Reply
from solana_monitor.telegram.registry import COMMANDS
from solana_monitor.telegram.sink import TelegramAlertSink

logger = logging.getLogger(__name__)

_TELEGRAM_TIMEOUT: Final = 30.0

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
ErrorHandler = Callable[[object, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
#: python-telegram-bot's own callback aliases, needed only at registration points,
#: where the library's callback context generics are wider than ours on purpose.
_PtbCallback = Callable[[Update, Any], Coroutine[Any, Any, Any]]
_PtbErrorCallback = Callable[[object, Any], Coroutine[Any, Any, None]]
_TelegramApplication = Application[Any, Any, Any, Any, Any, Any]
_UNEXPECTED_ERROR_HINT: Final = "Something went wrong on my side. The error has been logged."


async def _send_reply(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reply: Reply) -> None:
    """Send a framework-free :class:`Reply` as Telegram HTML."""
    keyboard = (
        InlineKeyboardMarkup(
            [[InlineKeyboardButton(link.text, url=link.url) for link in reply.links]]
        )
        if reply.links
        else None
    )
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=reply.text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=reply.disable_preview,
        )
    except Forbidden:
        logger.warning("bot is not allowed to post here", extra={"chat_id": chat_id})


def _identifiers(update: Update) -> tuple[int, int] | None:
    """Return ``(chat_id, user_id)`` for a message update, or ``None``."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return None
    return chat.id, user.id


def build_command_handler(service: CommandService, command: str) -> Handler:
    """Adapt one command name to a python-telegram-bot handler."""

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        identifiers = _identifiers(update)
        if identifiers is None:
            return
        chat_id, user_id = identifiers
        args = list(context.args or [])
        try:
            reply = await service.execute(command, args, chat_id=chat_id, user_id=user_id)
        except MonitorError as error:
            logger.warning(
                "command failed",
                extra={"command": command, "chat_id": chat_id, "error": str(error)},
            )
            reply = None
        if reply is not None:
            await _send_reply(context, chat_id, reply)

    return handler


def build_fallback_handler(service: CommandService) -> Handler:
    """Answer unknown ``/commands`` with a pointer to ``/help``."""

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        identifiers = _identifiers(update)
        if identifiers is None:
            return
        chat_id, _user_id = identifiers
        message = update.effective_message
        if message is None:  # pragma: no cover - guarded by _identifiers
            return
        name = (message.text or "").split(maxsplit=1)[0].lstrip("/").lower()
        logger.info("ignoring unknown command", extra={"command": name, "chat_id": chat_id})
        await _send_reply(
            context,
            chat_id,
            await service.execute("help", (), chat_id=chat_id, user_id=_user_id)
            or Reply(text="Type /help to see the available commands."),
        )

    return handler


def build_application(
    settings: Settings, service: CommandService, *, bot: Bot | None = None
) -> _TelegramApplication:
    """Create the Telegram application with every command registered.

    ``bot`` can be supplied when the caller needs the bot instance before the
    application exists (which is the case in :func:`serve`, because alert sinks
    need a sender).
    """
    builder: ApplicationBuilder[Any, Any, Any, Any, Any, Any] = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token.get_secret_value())
        .read_timeout(_TELEGRAM_TIMEOUT)
        .write_timeout(_TELEGRAM_TIMEOUT)
        .connect_timeout(_TELEGRAM_TIMEOUT)
        .rate_limiter(AIORateLimiter())
    )
    if bot is not None:
        builder = builder.bot(bot)
    application = builder.build()
    for spec in COMMANDS:
        handler = build_command_handler(service, spec.name)
        application.add_handler(CommandHandler(spec.name, cast("_PtbCallback", handler)))
    # Group 1 runs only when no group-0 handler already handled the update, so
    # unknown commands do not trigger a second reply.
    fallback = build_fallback_handler(service)
    application.add_handler(
        MessageHandler(filters.COMMAND, cast("_PtbCallback", fallback)), group=1
    )
    application.add_error_handler(cast("_PtbErrorCallback", build_error_handler()))
    return application


def build_error_handler() -> ErrorHandler:
    """Log unexpected handler failures without leaking internals to users."""

    async def handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = getattr(update, "effective_chat", None)
        logger.error(
            "unhandled error while processing an update",
            exc_info=context.error,
            extra={"chat_id": getattr(chat, "id", None)},
        )
        message = getattr(update, "effective_message", None)
        if message is None or context.error is None:
            return
        with contextlib.suppress(Exception):
            await message.reply_text(_UNEXPECTED_ERROR_HINT, parse_mode=ParseMode.HTML)

    return handler


# -- Composition root -------------------------------------------------------
def _monitor_factory(
    settings: Settings,
    store: WatchStore,
    client: HeliusClient,
    resolver: TokenMetadataResolver,
    detector: PurchaseDetector,
    bot: Bot,
) -> Callable[[WatchState], WalletMonitor]:
    """Build a monitor for a watch, bound to the bot that will deliver alerts."""

    def factory(state: WatchState) -> WalletMonitor:
        sink = TelegramAlertSink(
            sender=bot,
            chat_id=state.chat_id,
            wallet=state.wallet,
            settings=settings,
            sleep=asyncio.sleep,
        )
        return WalletMonitor(
            state=state,
            store=store,
            source=client,
            detector=detector,
            sink=sink,
            holdings=client,
            resolver=resolver,
            config=MonitorConfig.from_settings(settings),
        )

    return factory


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Stop the bot cleanly on SIGINT/SIGTERM (works on Windows too)."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, stop_event.set)


def _remove_signal_handlers() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.remove_signal_handler(sig)


async def _register_commands(application: _TelegramApplication) -> None:
    """Publish the command menu so Telegram's UI matches the registry."""
    await application.bot.set_my_commands(
        [BotCommand(spec.name, spec.summary) for spec in COMMANDS]
    )


async def serve(settings: Settings) -> None:
    """Run the bot until interrupted; wires every component together."""
    store = WatchStore(
        settings.state_file,
        max_seen_mints=settings.monitor_max_seen_mints,
        max_processed_signatures=settings.monitor_max_processed_signatures,
    )
    store.load()
    detector = PurchaseDetector(DetectorPolicy.from_settings(settings))
    stop_event = asyncio.Event()

    async with HeliusClient(settings) as client:
        resolver = TokenMetadataResolver(client, ttl_seconds=settings.helius_metadata_cache_seconds)
        bot = Bot(settings.telegram_bot_token.get_secret_value())
        manager = WatchManager(
            store, _monitor_factory(settings, store, client, resolver, detector, bot)
        )
        service = CommandService(
            settings=settings,
            store=store,
            manager=manager,
            history=client,
            detector=detector,
            holdings=client,
        )
        application = build_application(settings, service, bot=bot)
        updater = application.updater
        if updater is None:  # pragma: no cover - only if polling is disabled
            msg = "Telegram updater is unavailable"
            raise MonitorError(msg)

        _install_signal_handlers(stop_event)
        await application.initialize()
        await application.start()
        await updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
        await _register_commands(application)
        try:
            if settings.monitor_resume_on_start:
                await manager.resume_all()
            logger.info("bot is running")
            await stop_event.wait()
        finally:
            logger.info("shutting down")
            await manager.shutdown()
            for shutdown in (updater.stop, application.stop, application.shutdown):
                with contextlib.suppress(Exception):
                    await shutdown()
            _remove_signal_handlers()

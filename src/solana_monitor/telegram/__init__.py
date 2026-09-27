"""Telegram presentation and command handling.

The command *logic* in :mod:`solana_monitor.telegram.commands` is framework
free (it speaks in ``chat_id``/``user_id`` and returns a :class:`Reply`), and the
handlers in :mod:`solana_monitor.telegram.app` are thin adapters around
``python-telegram-bot``.  That split is what makes the interesting behaviour
testable without a Telegram connection.
"""

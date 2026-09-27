"""Solana Token Purchase Monitor.

A Telegram bot that watches Solana wallets and alerts on new token purchases
using the Helius Enhanced Transactions API.

The package is layered so that each concern can be tested in isolation:

``solana_monitor.domain``
    Framework-free models and validation rules.
``solana_monitor.helius``
    Transport and payload validation for the Helius HTTP APIs.
``solana_monitor.monitoring``
    Purchase detection, duplicate suppression, persistence and the polling loop.
``solana_monitor.telegram``
    Presentation and Telegram wiring (thin adapters over the command service).
``solana_monitor.cli``
    Process entry point that wires everything together.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:  # pragma: no cover - depends on how the package is installed
    __version__ = version("solana-token-purchase-monitor")
except PackageNotFoundError:  # pragma: no cover - running from a bare source tree
    __version__ = "0.0.0+local"

__all__ = ["__version__"]

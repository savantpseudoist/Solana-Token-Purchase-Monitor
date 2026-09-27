"""Process entry point: argument parsing, wiring and exit codes.

Deliberately thin.  Everything interesting lives in the layers below, so the CLI
only has to do three things: load and validate configuration, install logging,
and hand control to :func:`solana_monitor.telegram.app.serve`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from solana_monitor import __version__
from solana_monitor.config import Settings, load_settings
from solana_monitor.domain.errors import ConfigurationError, MonitorError
from solana_monitor.logging_setup import configure_logging

logger = logging.getLogger(__name__)

EXIT_OK: Final = 0
EXIT_RUNTIME_ERROR: Final = 1
EXIT_CONFIG_ERROR: Final = 2
EXIT_INTERRUPTED: Final = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solana-monitor",
        description="Telegram bot that alerts on new Solana token purchases.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to a .env file (default: .env in the working directory, if present).",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        default=None,
        help="Override LOG_LEVEL.",
    )
    parser.add_argument(
        "--log-format", choices=("text", "json"), default=None, help="Override LOG_FORMAT."
    )
    parser.add_argument("--state-file", type=Path, default=None, help="Override STATE_FILE.")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate the configuration and exit without contacting Telegram or Helius.",
    )
    return parser


def _overrides(args: argparse.Namespace) -> dict[str, object]:
    overrides: dict[str, object] = {}
    if args.log_level:
        overrides["log_level"] = args.log_level
    if args.log_format:
        overrides["log_format"] = args.log_format
    if args.state_file:
        overrides["state_file"] = args.state_file
    return overrides


def _report_configuration_warning(message: str) -> None:
    sys.stderr.write(f"configuration warning: {message}\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings: Settings = load_settings(args.env_file, overrides=_overrides(args))
    except ConfigurationError as error:
        sys.stderr.write(f"{error}\n")
        sys.stderr.write(
            "See .env.example for the full list of settings. "
            "Copy it to .env and fill in the required values.\n"
        )
        return EXIT_CONFIG_ERROR

    configure_logging(settings)
    for warning in settings.configuration_warnings():
        _report_configuration_warning(warning)

    if args.check_config:
        logger.info("configuration is valid", extra=settings.describe())
        return EXIT_OK

    from solana_monitor.telegram.app import serve  # imported late: heavy import

    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        logger.info("interrupted by the user")
        return EXIT_INTERRUPTED
    except MonitorError as error:
        logger.error("fatal error: %s", error)
        return EXIT_RUNTIME_ERROR
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""The single source of truth for the bot's command surface.

Command names, help text, admin requirements and Telegram's registered command
list are all derived from this table, so adding a command cannot leave the help
text, the Telegram menu or the authorisation rules out of sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    summary: str
    usage: str
    requires_admin: bool = False
    costly: bool = False
    """The command issues billable API requests, so it gets its own budget."""


COMMANDS: Final[tuple[CommandSpec, ...]] = (
    CommandSpec(
        name="set_wallet",
        summary="Choose the wallet to monitor",
        usage="/set_wallet <solana_address>",
        requires_admin=True,
    ),
    CommandSpec(
        name="start_monitoring",
        summary="Start alerting on new purchases",
        usage="/start_monitoring",
        requires_admin=True,
    ),
    CommandSpec(
        name="stop_monitoring",
        summary="Stop alerting",
        usage="/stop_monitoring",
        requires_admin=True,
    ),
    CommandSpec(
        name="status",
        summary="Show the current watch status and health",
        usage="/status",
    ),
    CommandSpec(
        name="analyze",
        summary="Summarise purchases in a time window",
        usage="/analyze <1h|1d|1w>",
        costly=True,
    ),
    CommandSpec(
        name="whoami",
        summary="Show your Telegram user ID (needed to configure admins)",
        usage="/whoami",
    ),
    CommandSpec(name="help", summary="Show this help", usage="/help"),
    CommandSpec(name="start", summary="Show this help", usage="/start"),
)

COMMANDS_BY_NAME: Final[dict[str, CommandSpec]] = {spec.name: spec for spec in COMMANDS}

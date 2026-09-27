"""Domain constants shared by configuration and business logic."""

from __future__ import annotations

from typing import Final

#: Wrapped SOL. Holding or receiving it is never a "token purchase" worth alerting on.
WRAPPED_SOL_MINT: Final = "So11111111111111111111111111111111111111112"

#: Default command throttling applied per Telegram user.
DEFAULT_COMMANDS_PER_MINUTE: Final = 12

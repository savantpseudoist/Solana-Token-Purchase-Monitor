"""Authorisation and abuse control for Telegram commands.

The original bot had no access control at all: anybody who could find the bot
could point it at a different wallet, which silently redirects alerts and burns
API credits.  The policy here is fail-closed and explicit:

* a chat allowlist (optional) decides *where* the bot answers at all;
* an admin allowlist decides *who* may run state-changing commands, and while
  that list is empty nobody may - the bot says how to fix it;
* every user is rate limited, so a stuck client (or an abuse attempt) cannot turn
  the bot into an API-credit cannon.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

from solana_monitor.config import Settings
from solana_monitor.domain.errors import AuthorizationError

_MAX_TRACKED_USERS = 1024

_NO_ADMIN_HINT = (
    "This bot has no administrators configured. Send /whoami to the bot and add "
    "your user ID to TELEGRAM_ADMIN_USER_IDS, then restart the bot."
)
_NOT_ALLOWED_HINT = "Only the bot administrators can change what is monitored."
_CHAT_HINT = "This chat is not configured for this bot."
_RATE_HINT = "You are sending commands too quickly. Please wait a moment."


@dataclass(frozen=True, slots=True)
class CommandAuthorization:
    """Pure, side-effect free access decisions."""

    admin_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    admins_configured: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> CommandAuthorization:
        return cls(
            admin_user_ids=frozenset(settings.telegram_admin_user_ids),
            allowed_chat_ids=frozenset(settings.telegram_allowed_chat_ids),
            admins_configured=bool(settings.telegram_admin_user_ids),
        )

    def is_chat_allowed(self, chat_id: int) -> bool:
        return not self.allowed_chat_ids or chat_id in self.allowed_chat_ids

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids

    def check_chat(self, chat_id: int) -> None:
        if not self.is_chat_allowed(chat_id):
            raise AuthorizationError(_CHAT_HINT, context={"chat_id": chat_id})

    def check_command(self, *, user_id: int, command: str, requires_admin: bool) -> None:
        if not requires_admin:
            return
        if not self.admins_configured:
            raise AuthorizationError(_NO_ADMIN_HINT, context={"command": command})
        if not self.is_admin(user_id):
            raise AuthorizationError(_NOT_ALLOWED_HINT, context={"command": command})


class CommandThrottle:
    """Sliding-window rate limiter, one bucket per user."""

    def __init__(
        self,
        per_minute: int,
        *,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = max(1, per_minute)
        self._window = window_seconds
        self._clock = clock
        self._buckets: OrderedDict[int, deque[float]] = OrderedDict()

    def allow(self, user_id: int) -> bool:
        now = self._clock()
        bucket = self._buckets.get(user_id)
        if bucket is None:
            bucket = deque()
            self._buckets[user_id] = bucket
        self._buckets.move_to_end(user_id)
        while bucket and now - bucket[0] >= self._window:
            bucket.popleft()
        if len(bucket) >= self._limit:
            return False
        bucket.append(now)
        while len(self._buckets) > _MAX_TRACKED_USERS:
            self._buckets.popitem(last=False)
        return True

    def retry_hint(self) -> str:
        return _RATE_HINT

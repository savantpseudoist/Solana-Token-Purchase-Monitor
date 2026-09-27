"""Test doubles for the external boundaries (HTTP, alerting, RPC)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any


class FakeResponse:
    """The subset of ``aiohttp.ClientResponse`` the client relies on."""

    def __init__(
        self, status: int = 200, body: str = "", headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body


def json_response(payload: Any, *, status: int = 200, headers: dict[str, str] | None = None):
    """Build a :class:`FakeResponse` whose body is ``payload`` serialised."""
    return FakeResponse(status=status, body=json.dumps(payload), headers=headers)


class _RequestContext:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    async def __aenter__(self) -> Any:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class FakeSession:
    """A scripted stand-in for ``aiohttp.ClientSession``.

    Outcomes are consumed in order; an exception instance is raised instead of
    being returned, which is how transport failures are simulated.
    """

    def __init__(self, *outcomes: Any) -> None:
        self._outcomes: list[Any] = list(outcomes)
        self.calls: list[SimpleNamespace] = []
        self.closed = False

    def queue(self, *outcomes: Any) -> None:
        self._outcomes.extend(outcomes)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: Any = None,
    ) -> _RequestContext:
        self.calls.append(
            SimpleNamespace(
                method=method, url=url, params=dict(params or {}), json=json, timeout=timeout
            )
        )
        if not self._outcomes:
            message = f"unexpected request: {method} {url}"
            raise AssertionError(message)
        return _RequestContext(self._outcomes.pop(0))

    async def close(self) -> None:
        self.closed = True

    @property
    def last_call(self) -> SimpleNamespace:
        return self.calls[-1]

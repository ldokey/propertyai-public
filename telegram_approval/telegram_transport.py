"""Role-neutral Telegram Bot API transport.

Credential selection and recipient authorization deliberately live above this
module.  Every request receives an explicit bot context from its caller.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Protocol
import urllib.parse
import urllib.request


@dataclass(frozen=True)
class TelegramBotContext:
    token: str

    def __post_init__(self) -> None:
        if not self.token or not self.token.strip():
            raise ValueError("Telegram bot token is empty")


class TelegramTransport(Protocol):
    def request(
        self, bot: TelegramBotContext, method: str, **values: Any
    ) -> dict[str, Any] | list[Any] | bool:
        """Call one Telegram Bot API method using only ``bot`` identity."""

    def download_file(
        self, bot: TelegramBotContext, file_path: str, *, maximum_bytes: int
    ) -> bytes:
        """Download one Bot API file using only ``bot`` identity."""


class TelegramHttpTransport:
    """Stateless HTTP implementation of :class:`TelegramTransport`."""

    def __init__(
        self,
        *,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        timeout_seconds: int = 40,
    ) -> None:
        self._urlopen = urlopen
        self._timeout_seconds = timeout_seconds

    def request(
        self, bot: TelegramBotContext, method: str, **values: Any
    ) -> dict[str, Any] | list[Any] | bool:
        data = urllib.parse.urlencode(values).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{bot.token}/{method}", data=data
        )
        with self._urlopen(request, timeout=self._timeout_seconds) as response:
            payload = json.loads(response.read())
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed")
        return payload["result"]

    def download_file(
        self, bot: TelegramBotContext, file_path: str, *, maximum_bytes: int
    ) -> bytes:
        url = f"https://api.telegram.org/file/bot{bot.token}/{file_path}"
        with self._urlopen(url, timeout=self._timeout_seconds) as response:
            return response.read(maximum_bytes + 1)


class CallableTelegramTransport:
    """Adapter for deterministic tests and existing explicit-token call sites."""

    def __init__(self, request: Callable[..., Any]) -> None:
        self._request = request

    def request(
        self, bot: TelegramBotContext, method: str, **values: Any
    ) -> dict[str, Any] | list[Any] | bool:
        return self._request(bot.token, method, **values)

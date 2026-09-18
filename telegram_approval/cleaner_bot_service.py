"""Cleaner-only Telegram routing boundary."""

from __future__ import annotations

from typing import Any, Callable, Iterable

from telegram_approval.cleaner_config import CleanerCredentialProvider, CleanerRuntimePaths
from telegram_approval.telegram_polling import TelegramPoller
from telegram_approval.telegram_transport import TelegramTransport


class CleanerTelegramRouter:
    def __init__(
        self,
        *,
        credential_provider: CleanerCredentialProvider,
        transport: TelegramTransport,
        handlers: Iterable[Callable[[dict[str, Any]], str | None]] = (),
    ) -> None:
        self._credential_provider = credential_provider
        self._transport = transport
        self._handlers = tuple(handlers)

    def send_message(self, chat_id: int, text: str, **values: Any) -> Any:
        return self._transport.request(
            self._credential_provider.bot_context(),
            "sendMessage",
            chat_id=chat_id,
            text=text,
            **values,
        )

    def route(self, update: dict[str, Any]) -> str:
        for handler in self._handlers:
            result = handler(update)
            if result:
                return result
        return "ignored"

    def poll_once(self, *, state_path=None) -> dict[str, Any]:
        return TelegramPoller(
            bot=self._credential_provider.bot_context(),
            transport=self._transport,
            state_path=state_path or CleanerRuntimePaths().state_path,
            handler=self.route,
        ).poll_once()

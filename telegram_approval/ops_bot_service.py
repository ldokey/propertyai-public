"""OPS-only Telegram routing boundary.

The router authorizes both messages and callbacks exclusively through the OPS
allowlist.  Callback business handling is injected by the role-specific runtime
so this module never imports Cleaner credentials, registry, recipients, or
runtime state.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from telegram_approval.ops_config import (
    OpsAllowlistProvider,
    OpsCredentialProvider,
    OpsRuntimePaths,
)
from telegram_approval.telegram_polling import TelegramPoller
from telegram_approval.telegram_transport import TelegramTransport


class OpsTelegramRouter:
    def __init__(
        self,
        *,
        credential_provider: OpsCredentialProvider,
        allowlist_provider: OpsAllowlistProvider,
        transport: TelegramTransport,
        handlers: Mapping[str, Callable[[dict[str, Any]], str]] | None = None,
        callback_handler: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self._credential_provider = credential_provider
        self._allowlist_provider = allowlist_provider
        self._transport = transport
        self._handlers = dict(handlers or {})
        self._callback_handler = callback_handler

    def send_message(self, chat_id: int, text: str, **values: Any) -> Any:
        if chat_id not in self._allowlist_provider.allowed_chat_ids():
            raise PermissionError("OPS Telegram chat is not allowlisted")
        return self._transport.request(
            self._credential_provider.bot_context(),
            "sendMessage",
            chat_id=chat_id,
            text=text,
            **values,
        )

    def route(self, update: dict[str, Any]) -> str:
        callback_query = update.get("callback_query")
        if callback_query:
            chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
            if chat_id not in self._allowlist_provider.allowed_chat_ids():
                return "ignored"
            return self._callback_handler(update) if self._callback_handler else "ignored"

        message = update.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        if chat_id not in self._allowlist_provider.allowed_chat_ids():
            return "ignored"
        text = message.get("text", "")
        if text == "/ops_briefing":
            return "ops_briefing_out_of_scope"
        handler = self._handlers.get(text)
        return handler(update) if handler else "ignored"

    def poll_once(self, *, state_path=None) -> dict[str, Any]:
        return TelegramPoller(
            bot=self._credential_provider.bot_context(),
            transport=self._transport,
            state_path=state_path or OpsRuntimePaths().state_path,
            handler=self.route,
        ).poll_once()

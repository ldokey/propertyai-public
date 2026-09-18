"""Composition helpers for explicit role-owned outbound routers."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from telegram_approval.cleaner_bot_service import CleanerTelegramRouter
from telegram_approval.cleaner_config import CleanerCredentialProvider
from telegram_approval.ops_bot_service import OpsTelegramRouter
from telegram_approval.ops_config import (
    OPS_ALLOWLIST_CONFIG,
    OpsAllowlistProvider,
    OpsCredentialProvider,
)
from telegram_approval.telegram_transport import CallableTelegramTransport


def cleaner_outbound_router(
    request: Callable[..., object], *, token_path: Path | None = None
) -> CleanerTelegramRouter:
    return CleanerTelegramRouter(
        credential_provider=CleanerCredentialProvider(token_path=token_path),
        transport=CallableTelegramTransport(request),
    )


def ops_outbound_router(
    request: Callable[..., object],
    *,
    token_path: Path | None = None,
    synthetic_allowed_chat_ids: Iterable[int] | None = None,
) -> OpsTelegramRouter:
    # The explicit allowlist override exists only for synthetic unit fixtures
    # that also supply a synthetic token path. Runtime callers use the frozen
    # PROPERTYAI_OPS_BRIEFING_ALLOWED_CHAT_IDS configuration.
    environment = None
    if synthetic_allowed_chat_ids is not None:
        if token_path is None:
            raise ValueError("synthetic allowlist requires a synthetic token path")
        environment = {
            OPS_ALLOWLIST_CONFIG: ",".join(str(item) for item in synthetic_allowed_chat_ids)
        }
    return OpsTelegramRouter(
        credential_provider=OpsCredentialProvider(token_path=token_path),
        allowlist_provider=OpsAllowlistProvider(environment=environment),
        transport=CallableTelegramTransport(request),
    )

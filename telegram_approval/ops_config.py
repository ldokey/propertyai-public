"""OPS Admin Telegram credential, allowlist, and runtime-state ownership."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping

from telegram_approval.telegram_transport import TelegramBotContext


OPS_TOKEN_PATH_CONFIG = "PROPERTYAI_OPS_ADMIN_TELEGRAM_TOKEN_PATH"
OPS_ALLOWLIST_CONFIG = "PROPERTYAI_OPS_BRIEFING_ALLOWED_CHAT_IDS"
ROOT = Path(__file__).resolve().parents[1]


class OpsCredentialProvider:
    """Load only the OPS Admin Bot credential path selected by its frozen key."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        token_path: Path | None = None,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self._token_path = token_path

    def credential_path(self) -> Path:
        if self._token_path is not None and hasattr(self._token_path, "read_text"):
            return self._token_path
        configured = self._token_path or self._environment.get(OPS_TOKEN_PATH_CONFIG)
        if not configured:
            raise RuntimeError(f"{OPS_TOKEN_PATH_CONFIG} is required")
        return Path(configured).expanduser()

    def bot_context(self) -> TelegramBotContext:
        return TelegramBotContext(self.credential_path().read_text().strip())


class OpsAllowlistProvider:
    """Resolve the OPS briefing allowlist without consulting Cleaner state."""

    def __init__(self, *, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def allowed_chat_ids(self) -> frozenset[int]:
        configured = self._environment.get(OPS_ALLOWLIST_CONFIG)
        if not configured:
            raise RuntimeError(f"{OPS_ALLOWLIST_CONFIG} is required")
        try:
            return frozenset(int(item.strip()) for item in configured.split(",") if item.strip())
        except ValueError as error:
            raise RuntimeError(f"{OPS_ALLOWLIST_CONFIG} contains a non-integer chat id") from error


@dataclass(frozen=True)
class OpsRuntimePaths:
    state_path: Path = ROOT / "telegram_approval" / "runtime" / "ops" / "state.json"
    callback_session_dir: Path = (
        ROOT / "telegram_approval" / "runtime" / "ops" / "callback-sessions"
    )
    request_dir: Path = ROOT / "telegram_approval" / "runtime" / "ops" / "requests"

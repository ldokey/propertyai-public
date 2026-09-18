"""Cleaner Telegram credential and runtime-state ownership."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping

from telegram_approval.telegram_transport import TelegramBotContext


CLEANER_TOKEN_PATH_CONFIG = "PROPERTYAI_CLEANER_TELEGRAM_TOKEN_PATH"
ROOT = Path(__file__).resolve().parents[1]


class CleanerCredentialProvider:
    """Load only the Cleaner Bot credential path selected by its frozen key."""

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
        configured = self._token_path or self._environment.get(CLEANER_TOKEN_PATH_CONFIG)
        if not configured:
            raise RuntimeError(f"{CLEANER_TOKEN_PATH_CONFIG} is required")
        return Path(configured).expanduser()

    def bot_context(self) -> TelegramBotContext:
        return TelegramBotContext(self.credential_path().read_text().strip())


@dataclass(frozen=True)
class CleanerRuntimePaths:
    state_path: Path = ROOT / "telegram_approval" / "runtime" / "cleaner" / "state.json"
    callback_session_dir: Path = (
        ROOT / "telegram_approval" / "runtime" / "cleaner" / "callback-sessions"
    )
    request_dir: Path = ROOT / "telegram_approval" / "runtime" / "cleaner" / "requests"
    door_code_request_dir: Path = (
        ROOT / "telegram_approval" / "runtime" / "cleaner" / "door-code-requests"
    )
    issue_session_dir: Path = (
        ROOT / "telegram_approval" / "runtime" / "cleaner" / "issue-uploads"
    )
    completion_session_dir: Path = (
        ROOT / "telegram_approval" / "runtime" / "cleaner" / "completion-uploads"
    )

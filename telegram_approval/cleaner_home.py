"""Korean-first navigation for the Cleaner Telegram surface.

This module is deliberately UI-only. Korean aliases are normalized to the
existing slash commands, while Cleaner authorization continues to use the
same ACTIVE Cleaner resolver as /jobs, /my, and /properties.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from telegram_approval.cleaner_jobs import resolve_active_cleaner
from telegram_approval.cleaner_registry import load_roster


JOBS_BUTTON = "🧹 신청 가능한 청소"
MY_BUTTON = "📅 내 청소"
PROPERTIES_BUTTON = "🏠 내 숙소"
HELP_BUTTON = "❓ 도움말"

JOBS_ALIASES = frozenset({JOBS_BUTTON, "신청 가능한 청소", "청소", "청소 찾기"})
MY_ALIASES = frozenset({MY_BUTTON, "내 청소", "내 일정"})
PROPERTIES_ALIASES = frozenset({PROPERTIES_BUTTON, "내 숙소", "숙소"})
HELP_ALIASES = frozenset({HELP_BUTTON, "도움말"})
HOME_ALIASES = frozenset({"/start", "메뉴", "홈", "시작"})

HOME_MESSAGE = "안녕하세요.\n청소 업무 메뉴입니다.\n원하는 메뉴를 눌러주세요."
HELP_MESSAGE = (
    "청소 업무 메뉴입니다.\n"
    f"{JOBS_BUTTON}: 현재 신청 가능한 청소를 확인합니다.\n"
    f"{MY_BUTTON}: 나에게 확정된 청소 일정을 확인합니다.\n"
    f"{PROPERTIES_BUTTON}: 이용 가능한 숙소를 확인하고 숙소 권한을 신청합니다.\n"
    "아래 버튼을 눌러 원하는 기능을 선택해주세요."
)
NAVIGATION_GUIDANCE_MESSAGE = "아래 메뉴에서 원하는 기능을 선택해주세요."
UNAUTHORIZED_MESSAGE = "청소 담당자 권한을 확인할 수 없습니다."
FAILURE_MESSAGE = "메뉴를 불러오지 못했습니다. 잠시 후 다시 시도해주세요."


def home_reply_markup() -> str:
    """Return a persistent Reply Keyboard for implemented capabilities only."""

    return json.dumps(
        {
            "keyboard": [
                [{"text": JOBS_BUTTON}, {"text": MY_BUTTON}],
                [{"text": PROPERTIES_BUTTON}, {"text": HELP_BUTTON}],
            ],
            "resize_keyboard": True,
        },
        ensure_ascii=False,
    )


def normalized_navigation_text(text: Any) -> str | None:
    """Map deterministic Korean aliases to existing command/navigation keys."""

    if not isinstance(text, str):
        return None
    normalized = text.strip()
    if normalized in JOBS_ALIASES:
        return "/jobs"
    if normalized in MY_ALIASES:
        return "/my"
    if normalized in PROPERTIES_ALIASES:
        return "/properties"
    if normalized in HELP_ALIASES:
        return "help"
    if normalized in HOME_ALIASES:
        return "home"
    return None


def alias_command_update(update: dict[str, Any]) -> dict[str, Any] | None:
    """Clone an alias message as the exact existing slash command input."""

    message = update.get("message")
    if not isinstance(message, dict):
        return None
    command = normalized_navigation_text(message.get("text"))
    if command not in {"/jobs", "/my", "/properties"}:
        return None
    normalized_update = dict(update)
    normalized_message = dict(message)
    normalized_message["text"] = command
    normalized_update["message"] = normalized_message
    return normalized_update


def handle_home_navigation(
    update: dict[str, Any],
    token: str,
    *,
    request_api: Callable | None = None,
    roster_loader: Callable[[], dict] | None = None,
) -> str | None:
    """Render Korean home/help UX for an authorized ACTIVE Cleaner.

    Business aliases (/jobs, /my, and /properties) are intentionally excluded here;
    the composition root rewrites them and invokes the existing handlers.
    """

    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not isinstance(text, str):
        return None
    normalized_text = text.strip()
    navigation = normalized_navigation_text(text)
    if navigation in {"/jobs", "/my", "/properties"}:
        return None
    if normalized_text.startswith("/") and navigation != "home":
        return None

    sender = message.get("from", {})
    chat = message.get("chat", {})
    if chat.get("type") != "private" or sender.get("is_bot"):
        return None
    request_api = request_api or _telegram_api
    roster_loader = roster_loader or load_roster

    try:
        cleaner = resolve_active_cleaner(
            roster_loader(),
            user_id=sender.get("id"),
            chat_id=chat.get("id"),
        )
    except Exception:
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=FAILURE_MESSAGE)
        return "cleaner_home_failed"
    if cleaner is None:
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=UNAUTHORIZED_MESSAGE)
        return "cleaner_home_unauthorized"

    if navigation == "home":
        response = HOME_MESSAGE
        result = "cleaner_home"
    elif navigation == "help":
        response = HELP_MESSAGE
        result = "cleaner_help"
    else:
        response = NAVIGATION_GUIDANCE_MESSAGE
        result = "cleaner_navigation_guidance"

    request_api(
        token,
        "sendMessage",
        chat_id=chat.get("id"),
        text=response,
        reply_markup=home_reply_markup(),
    )
    return result


def _telegram_api(token: str, method: str, **values):
    from telegram_approval.bot_service import api

    return api(token, method, **values)

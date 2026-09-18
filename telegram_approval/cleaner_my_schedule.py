#!/usr/bin/env python3
"""Read-only Cleaner ``/my`` schedule view for Phase 0A.

Cleaning 08 is the current projection, while canonical 19 durable Assignment
history is authoritative for an already-ended Cleaner binding.  Local Offer,
Application, and Property Access records are intentionally not consulted.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from typing import Callable
from pathlib import Path

from propertyai_core.notion_resources import cleaning_source_id
from telegram_approval.cleaner_jobs import (
    NOTION_TOKEN_PATH,
    NOTION_VERSION,
    load_property_mappings,
    resolve_active_cleaner,
)
from telegram_approval.cleaner_registry import load_roster
from telegram_approval.cleaner_config import CleanerRuntimePaths
from telegram_approval.send_approval import ACTION_SECRET_PATH
from telegram_approval.cleaner_unavailable import build_schedule_ui


KST = ZoneInfo("Asia/Seoul")
MAX_SOURCE_ROWS = 100
CURRENT_DISPLAY_LIMIT = 20
RECENT_HISTORY_DAYS = 30
RECENT_HISTORY_LIMIT = 10

EMPTY_MESSAGE = "현재 확정된 청소 일정이 없습니다."
FAILURE_MESSAGE = "내 청소 일정을 불러오지 못했습니다.\n잠시 후 다시 확인해 주세요."
UNAUTHORIZED_MESSAGE = "이 계정에서는 내 청소 일정을 확인할 수 없습니다."

ACTIVE_CLEANING_STATES = frozenset({"담당자 배정", "진행중"})
COMPLETED_CLEANING_STATES = frozenset({"완료 보고", "관리자 확인 완료"})
EFFECTIVE_ASSIGNMENT_STATES = frozenset({None, "HARD_BOOKED"})
CANCELLED_ASSIGNMENT_STATES = frozenset({None, "HARD_BOOKED", "CANCELLED"})


class NotionMyScheduleReader:
    """Bounded Notion query reader with no write-capable endpoint."""

    def __init__(self, *, token_path=NOTION_TOKEN_PATH, urlopen=urllib.request.urlopen):
        self._token_path = token_path
        self._urlopen = urlopen

    def query_for_cleaner(self, party_page_id: str) -> list[dict]:
        if not isinstance(party_page_id, str) or not party_page_id:
            raise RuntimeError("Cleaner Party identity is required")
        payload = {
            "filter": {
                "and": [
                    {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
                    {"property": "등록 상태", "select": {"equals": "APPROVED"}},
                    {"property": "담당 참여자/업체", "relation": {"contains": party_page_id}},
                ]
            },
            "page_size": MAX_SOURCE_ROWS,
        }
        request = urllib.request.Request(
            f"https://api.notion.com/v1/data_sources/{cleaning_source_id()}/query",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token_path.read_text().strip()}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        with self._urlopen(request, timeout=40) as response:
            page = json.loads(response.read())
        if not isinstance(page, dict) or not isinstance(page.get("results"), list):
            raise RuntimeError("My Schedule query response malformed")
        if not all(isinstance(item, dict) for item in page["results"]):
            raise RuntimeError("My Schedule query row malformed")
        if not isinstance(page.get("has_more"), bool):
            raise RuntimeError("My Schedule query pagination metadata malformed")
        if page["has_more"]:
            # Never silently turn a truncated source into a supposedly complete
            # personal schedule.  The source read itself remains strictly bounded.
            raise RuntimeError("My Schedule bounded source limit exceeded")
        if len(page["results"]) > MAX_SOURCE_ROWS:
            raise RuntimeError("My Schedule source exceeded bounded result limit")
        return page["results"]


def _select(page: dict, name: str) -> str | None:
    prop = page.get("properties", {}).get(name, {})
    selected = prop.get("select")
    return selected.get("name") if isinstance(selected, dict) else None


def _date_value(page: dict, name: str) -> str | None:
    prop = page.get("properties", {}).get(name, {})
    value = prop.get("date")
    return value.get("start") if isinstance(value, dict) else None


def _relations(page: dict, name: str) -> list[str] | None:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return None
    prop = properties.get(name)
    if not isinstance(prop, dict) or not isinstance(prop.get("relation"), list):
        return None
    values: list[str] = []
    for relation in prop["relation"]:
        if not isinstance(relation, dict):
            return None
        page_id = relation.get("id")
        if not isinstance(page_id, str) or not page_id:
            return None
        values.append(page_id)
    return values


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(KST)


def _service_day(page: dict) -> date | None:
    day_value = _date_value(page, "점검일")
    if day_value:
        try:
            return date.fromisoformat(day_value[:10])
        except ValueError:
            return None
    start_at = _parse_datetime(_date_value(page, "시작 예정"))
    return start_at.date() if start_at else None


def _safe_property_label(page: dict, property_mappings: dict[str, str]) -> str | None:
    property_ids = _relations(page, "연결 집")
    if property_ids is None or len(property_ids) != 1:
        return None
    label = property_mappings.get(property_ids[0])
    return label if isinstance(label, str) and label else None


def _resolve_schedule_cleaner(roster: dict, *, user_id: int, chat_id: int) -> dict | None:
    cleaner = resolve_active_cleaner(roster, user_id=user_id, chat_id=chat_id)
    if cleaner is None:
        return None
    party_page_id = cleaner.get("party_page_id")
    if not isinstance(party_page_id, str) or not party_page_id:
        return None
    party_owners = [
        item
        for item in roster.get("cleaners", [])
        if isinstance(item, dict)
        and item.get("role") == "CLEANER"
        and item.get("status") == "ACTIVE"
        and item.get("party_page_id") == party_page_id
    ]
    if len(party_owners) != 1 or party_owners[0] is not cleaner:
        return None
    return cleaner


def _classify_page(
    page: dict,
    *,
    party_page_id: str,
    today: date,
    property_mappings: dict[str, str],
) -> dict | None:
    page_id = page.get("id")
    if not isinstance(page_id, str) or not page_id:
        raise RuntimeError("Cleaning page identity malformed")
    if _select(page, "데이터 환경") != "PRODUCTION" or _select(page, "등록 상태") != "APPROVED":
        return None

    assignees = _relations(page, "담당 참여자/업체")
    if assignees is None:
        raise RuntimeError("Cleaning assignee relation malformed")
    # Multiple relations are ambiguous even when one is the current Cleaner.
    if assignees != [party_page_id]:
        return None

    label = _safe_property_label(page, property_mappings)
    service_day = _service_day(page)
    if label is None or service_day is None:
        return None

    state = _select(page, "상태")
    acceptance = _select(page, "배정 수락 상태")
    assignment_state = _select(page, "배정 상태")

    # Cancellation is only personal history when the durable row still proves
    # that this Cleaner had an accepted assignment.  A leftover assignee
    # relation alone is not sufficient evidence.
    if state == "취소":
        if acceptance != "수락" or assignment_state not in CANCELLED_ASSIGNMENT_STATES:
            return None
        ui_state = "CANCELLED"
    else:
        if acceptance != "수락" or assignment_state not in EFFECTIVE_ASSIGNMENT_STATES:
            return None
        if state == "담당자 배정":
            if service_day < today:
                return None
            ui_state = "TODAY" if service_day == today else "UPCOMING"
        elif state == "진행중":
            if service_day != today:
                return None
            ui_state = "TODAY"
        elif state in COMPLETED_CLEANING_STATES:
            if service_day > today:
                return None
            ui_state = "COMPLETED"
        else:
            # 재방문 필요, superseded/unknown states, and any new domain state
            # are not silently promoted into a confirmed personal schedule.
            return None

    start_at = _parse_datetime(_date_value(page, "시작 예정"))
    end_at = _parse_datetime(_date_value(page, "완료 목표"))
    return {
        "page_id": page_id,
        "property_label": label,
        "service_day": service_day,
        "start_at": start_at,
        "end_at": end_at,
        "ui_state": ui_state,
        "expected_cleaning_last_edited_time": page.get("last_edited_time"),
    }


def _operational_sort_time(item: dict) -> datetime:
    start_at = item.get("start_at")
    if isinstance(start_at, datetime):
        return start_at
    return datetime.combine(item["service_day"], time.min, tzinfo=KST)


def my_schedule(
    *,
    cleaner: dict,
    source,
    now: datetime | None = None,
    property_mappings: dict[str, str] | None = None,
) -> list[dict]:
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        raise RuntimeError("My Schedule clock must be timezone-aware")
    today = now.astimezone(KST).date()
    party_page_id = cleaner.get("party_page_id")
    if not isinstance(party_page_id, str) or not party_page_id:
        raise RuntimeError("Cleaner Party identity missing")
    mappings = property_mappings or load_property_mappings()
    pages = source.query_for_cleaner(party_page_id)
    if not isinstance(pages, list) or len(pages) > MAX_SOURCE_ROWS:
        raise RuntimeError("My Schedule source result malformed or unbounded")

    by_page: dict[str, dict] = {}
    for page in pages:
        item = _classify_page(
            page,
            party_page_id=party_page_id,
            today=today,
            property_mappings=mappings,
        )
        if item is None:
            continue
        prior = by_page.get(item["page_id"])
        if prior is not None:
            if prior != item:
                raise RuntimeError("Duplicate Cleaning rows conflict")
            continue
        by_page[item["page_id"]] = item

    current = [item for item in by_page.values() if item["ui_state"] in {"TODAY", "UPCOMING"}]
    current.sort(key=lambda item: (item["service_day"], _operational_sort_time(item), item["page_id"]))
    current = current[:CURRENT_DISPLAY_LIMIT]

    history_floor = today - timedelta(days=RECENT_HISTORY_DAYS)
    history = [
        item
        for item in by_page.values()
        if item["ui_state"] in {"COMPLETED", "CANCELLED"}
        and history_floor <= item["service_day"] <= today
    ]
    # Stable page-id order is the final tie breaker while date/start descend.
    history.sort(key=lambda item: item["page_id"])
    history.sort(key=lambda item: (item["service_day"], _operational_sort_time(item)), reverse=True)
    history = history[:RECENT_HISTORY_LIMIT]
    return current + history


def _time_range(item: dict) -> str | None:
    start_at = item.get("start_at")
    end_at = item.get("end_at")
    if not isinstance(start_at, datetime) or not isinstance(end_at, datetime):
        return None
    if start_at.date() != item["service_day"] or end_at.date() != item["service_day"]:
        return None
    if end_at <= start_at:
        return None
    return f"{start_at.strftime('%H:%M')}–{end_at.strftime('%H:%M')}"


def _render_line(item: dict, *, include_date: bool, include_terminal_state: bool) -> str:
    parts = []
    if include_date:
        parts.append(f"{item['service_day'].month}월 {item['service_day'].day}일")
    parts.append(item["property_label"])
    times = _time_range(item)
    if times:
        parts.append(times)
    if include_terminal_state:
        parts.append("완료" if item["ui_state"] == "COMPLETED" else "취소")
    human_status = item.get("human_status") or item.get("recent_status_text")
    if human_status:
        parts.append(human_status)
    return "• " + " · ".join(parts)


def render_my_schedule(items: list[dict]) -> str:
    if not items:
        return EMPTY_MESSAGE
    today_items = [item for item in items if item["ui_state"] == "TODAY"]
    upcoming = [item for item in items if item["ui_state"] == "UPCOMING"]
    history = [item for item in items if item["ui_state"] in {"COMPLETED", "CANCELLED"}]
    recent_changes = [item for item in items if item["ui_state"] == "UNAVAILABLE"]
    lines = ["📅 내 청소"]
    if today_items:
        lines.extend(["", "오늘"])
        lines.extend(_render_line(item, include_date=False, include_terminal_state=False) for item in today_items)
    if upcoming:
        lines.extend(["", "예정"])
        lines.extend(_render_line(item, include_date=True, include_terminal_state=False) for item in upcoming)
    if recent_changes:
        lines.extend(["", "최근 변경"])
        lines.extend(_render_line(item, include_date=True, include_terminal_state=False) for item in recent_changes)
    if history:
        lines.extend(["", "최근 완료/취소"])
        lines.extend(_render_line(item, include_date=True, include_terminal_state=True) for item in history)
    return "\n".join(lines)


def handle_my_schedule(
    update: dict,
    token: str,
    *,
    request_api: Callable | None = None,
    source=None,
    roster_loader: Callable[[], dict] | None = None,
    now: datetime | None = None,
    request_dir: Path | None = None,
    action_secret_path: Path | None = None,
    unavailable_ui_builder: Callable | None = None,
):
    message = update.get("message", {})
    if message.get("text") != "/my":
        return None
    sender = message.get("from", {})
    chat = message.get("chat", {})
    if chat.get("type") != "private" or sender.get("is_bot"):
        return "my_schedule_unauthorized"

    request_api = request_api or _telegram_api
    roster_loader = roster_loader or load_roster
    try:
        roster = roster_loader()
        cleaner = _resolve_schedule_cleaner(
            roster,
            user_id=sender.get("id"),
            chat_id=chat.get("id"),
        )
    except Exception:
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=FAILURE_MESSAGE)
        return "my_schedule_failed"
    if cleaner is None:
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=UNAUTHORIZED_MESSAGE)
        return "my_schedule_unauthorized"

    try:
        items = my_schedule(
            cleaner=cleaner,
            source=source or NotionMyScheduleReader(),
            now=now,
        )
    except Exception:
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=FAILURE_MESSAGE)
        return "my_schedule_failed"

    try:
        ui_builder = unavailable_ui_builder or build_schedule_ui
        items, markup = ui_builder(
            cleaner=cleaner,
            items=items,
            request_dir=request_dir or CleanerRuntimePaths().request_dir,
            secret_path=action_secret_path or ACTION_SECRET_PATH,
            now=now,
        )
    except Exception:
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=FAILURE_MESSAGE)
        return "my_schedule_failed"

    values = {"chat_id": chat.get("id"), "text": render_my_schedule(items)}
    if markup:
        values["reply_markup"] = json.dumps(markup, ensure_ascii=False)
    request_api(token, "sendMessage", **values)
    return "my_schedule_listed"


def _telegram_api(token: str, method: str, **values):
    from telegram_approval.bot_service import api

    return api(token, method, **values)

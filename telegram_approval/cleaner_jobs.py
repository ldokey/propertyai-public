#!/usr/bin/env python3
"""Read-only Cleaner ``/jobs`` discovery for the Phase 0A Legacy runtime."""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from propertyai_core.notion_resources import cleaning_source_id
from telegram_approval.cleaner_config import CleanerRuntimePaths
from telegram_approval.cleaner_registry import load_roster


ROOT = Path(__file__).resolve().parents[1]
NOTION_TOKEN_PATH = ROOT / "secrets" / "notion" / "token"
MAPPINGS_PATH = ROOT / "gmail_ingest" / "listing_mappings.json"
NOTION_VERSION = "2026-03-11"

EMPTY_MESSAGE = "현재 지원 가능한 청소가 없습니다."
FAILURE_MESSAGE = "청소 목록을 불러오지 못했습니다. 잠시 후 /jobs로 다시 확인해 주세요."
UNAUTHORIZED_MESSAGE = "이 계정에서는 청소 목록을 확인할 수 없습니다."

VISIBLE_STATES = frozenset({"예정", "담당자 배정"})
FINISHED_OR_CLOSED_STATES = frozenset({"진행중", "완료 보고", "관리자 확인 완료", "재방문 필요", "취소"})
BLOCKED_ASSIGNMENT_STATES = frozenset({"HARD_BOOKED", "CANCELLED"})
OPEN_ACCEPTANCE_STATES = frozenset({"미제안", "수락대기", "거절", "대체필요"})


class NotionCleaningReader:
    """Minimal Notion reader that exposes no write-capable HTTP method."""

    def __init__(self, *, token_path: Path = NOTION_TOKEN_PATH, urlopen=urllib.request.urlopen):
        self._token_path = token_path
        self._urlopen = urlopen

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        page_read = method == "GET" and re.fullmatch(r"/v1/pages/[^/]+", path) is not None
        data_source_query = (
            method == "POST"
            and re.fullmatch(r"/v1/data_sources/[^/]+/query", path) is not None
        )
        if not (page_read or data_source_query):
            raise RuntimeError("Cleaner jobs reader permits only page GET and data-source query POST")
        token = self._token_path.read_text().strip()
        request = urllib.request.Request(
            "https://api.notion.com" + path,
            data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        with self._urlopen(request, timeout=40) as response:
            return json.loads(response.read())

    def get_cleaning(self, page_id: str) -> dict:
        page = self._request("GET", f"/v1/pages/{page_id}")
        if not isinstance(page, dict) or not isinstance(page.get("properties"), dict):
            raise RuntimeError("Notion page response malformed")
        return page

    def query_open_applications(self) -> list[dict]:
        results: list[dict] = []
        cursor = None
        while True:
            payload: dict = {
                "filter": {"and": [
                    {"property": "배정 전략 Snapshot", "select": {"equals": "OPEN_APPLICATION"}},
                    {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
                    {"property": "등록 상태", "select": {"equals": "APPROVED"}},
                ]},
                "page_size": 100,
            }
            if cursor:
                payload["start_cursor"] = cursor
            page = self._request("POST", f"/v1/data_sources/{cleaning_source_id()}/query", payload)
            if not isinstance(page, dict):
                raise RuntimeError("Notion query response must be an object")
            if "results" not in page or not isinstance(page["results"], list):
                raise RuntimeError("Notion query results missing or malformed")
            if not all(isinstance(item, dict) for item in page["results"]):
                raise RuntimeError("Notion query result item malformed")
            if "has_more" not in page or not isinstance(page["has_more"], bool):
                raise RuntimeError("Notion query pagination metadata malformed")
            if "next_cursor" not in page:
                raise RuntimeError("Notion query pagination cursor metadata missing")
            cursor = page["next_cursor"]
            if page["has_more"]:
                if not isinstance(cursor, str) or not cursor:
                    raise RuntimeError("Notion query pagination cursor missing")
            elif cursor is not None:
                raise RuntimeError("Notion query terminal cursor must be null")
            results.extend(page["results"])
            if not page["has_more"]:
                return results


def _select(page: dict, name: str) -> str | None:
    return (page.get("properties", {}).get(name, {}).get("select") or {}).get("name")


def _date(page: dict, name: str) -> str | None:
    return (page.get("properties", {}).get(name, {}).get("date") or {}).get("start")


def _number(page: dict, name: str):
    return page.get("properties", {}).get(name, {}).get("number")


def _plain(page: dict, name: str) -> str:
    prop = page.get("properties", {}).get(name, {})
    values = prop.get(prop.get("type"), [])
    if not isinstance(values, list):
        return ""
    return "".join(item.get("plain_text", "") for item in values)


def _relations(page: dict, name: str) -> list[str] | None:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return None
    prop = properties.get(name)
    if not isinstance(prop, dict):
        return None
    values = prop.get("relation")
    if not isinstance(values, list):
        return None
    ids: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            return None
        page_id = item.get("id")
        if not isinstance(page_id, str) or not page_id:
            return None
        ids.append(page_id)
    return ids


def load_property_mappings(path: Path = MAPPINGS_PATH) -> dict[str, str]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("listings"), dict):
        raise RuntimeError("Legacy listing mappings malformed")
    by_property: dict[str, str] = {}
    for entry in raw["listings"].values():
        if not isinstance(entry, dict):
            raise RuntimeError("Legacy listing mapping entry malformed")
        page_id = entry.get("property_page_id")
        nickname = entry.get("nickname")
        if not isinstance(page_id, str) or not page_id or not isinstance(nickname, str) or not nickname:
            raise RuntimeError("Legacy listing mapping identity malformed")
        if page_id in by_property:
            raise RuntimeError("Legacy listing mapping property identity ambiguous")
        by_property[page_id] = nickname
    return by_property


def _canonical_property_nickname(
    page: dict,
    *,
    property_mappings: dict[str, str],
    authorized_properties: set[str],
) -> str | None:
    relation_ids = _relations(page, "연결 집")
    if relation_ids is None or len(relation_ids) != 1:
        return None
    nickname = property_mappings.get(relation_ids[0])
    if not isinstance(nickname, str) or not nickname or nickname not in authorized_properties:
        return None
    return nickname


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _validate_offer_record(record: dict) -> None:
    required_strings = ("cleaning_page_id", "property_nickname", "status", "expires_at")
    if any(not isinstance(record.get(name), str) or not record.get(name) for name in required_strings):
        raise RuntimeError("Cleaning assignment record string field malformed")
    for name in ("candidate_user_id", "candidate_chat_id"):
        if isinstance(record.get(name), bool) or not isinstance(record.get(name), int):
            raise RuntimeError("Cleaning assignment record Telegram identity malformed")
    if not isinstance(record.get("consumed"), bool) or not isinstance(record.get("test_mode"), bool):
        raise RuntimeError("Cleaning assignment record boolean field malformed")
    if _parse_datetime(record.get("expires_at")) is None:
        raise RuntimeError("Cleaning assignment record expiry malformed")


def _active_offer_record(record: dict, *, user_id: int, chat_id: int, now: datetime) -> bool:
    if record.get("action_type") != "CLEANING_ASSIGNMENT":
        return False
    _validate_offer_record(record)
    if record["test_mode"] is True:
        return False
    if record["candidate_user_id"] != user_id or record["candidate_chat_id"] != chat_id:
        return False
    if record["status"] != "PENDING" or record["consumed"] is not False:
        return False
    expires_at = _parse_datetime(record["expires_at"])
    return expires_at is not None and expires_at > now


def _fresh_common(page: dict) -> bool:
    assignees = _relations(page, "담당 참여자/업체")
    if assignees is None or assignees:
        return False
    if _select(page, "데이터 환경") != "PRODUCTION":
        return False
    if _select(page, "등록 상태") != "APPROVED":
        return False
    state = _select(page, "상태")
    if state not in VISIBLE_STATES or state in FINISHED_OR_CLOSED_STATES:
        return False
    if _select(page, "배정 상태") in BLOCKED_ASSIGNMENT_STATES:
        return False
    if _select(page, "배정 수락 상태") == "수락":
        return False
    return True


def _display_fields(
    page: dict,
    authorized_properties: set[str],
    property_mappings: dict[str, str],
) -> dict | None:
    nickname = _canonical_property_nickname(
        page, property_mappings=property_mappings, authorized_properties=authorized_properties
    )
    job_type = _select(page, "점검 유형")
    service_date = _date(page, "점검일") or _date(page, "시작 예정")
    start_at = _parse_datetime(_date(page, "시작 예정"))
    end_at = _parse_datetime(_date(page, "완료 목표"))
    fee = _number(page, "청소비 Snapshot")
    if (
        nickname is None
        or nickname not in authorized_properties
        or not job_type
        or not service_date
        or start_at is None
        or end_at is None
        or isinstance(fee, bool)
        or not isinstance(fee, (int, float))
    ):
        return None
    try:
        service_day = datetime.fromisoformat(service_date[:10])
    except ValueError:
        return None
    return {
        "page_id": page.get("id"),
        "nickname": nickname,
        "job_type": job_type,
        "service_day": service_day,
        "start_at": start_at,
        "end_at": end_at,
        "fee": int(fee),
    }


def resolve_active_cleaner(roster: dict, *, user_id: int, chat_id: int) -> dict | None:
    if not isinstance(roster, dict) or not isinstance(roster.get("cleaners"), list):
        raise RuntimeError("Cleaner roster malformed")
    entries = roster["cleaners"]
    if not all(isinstance(item, dict) for item in entries):
        raise RuntimeError("Cleaner roster entry malformed")
    collisions = [
        item for item in entries
        if item.get("telegram_user_id") == user_id or item.get("telegram_chat_id") == chat_id
    ]
    if len(collisions) != 1:
        return None
    selected = collisions[0]
    if (
        selected.get("role") != "CLEANER"
        or selected.get("status") != "ACTIVE"
        or selected.get("telegram_user_id") != user_id
        or selected.get("telegram_chat_id") != chat_id
    ):
        return None
    return selected


def open_application_fields(
    page: dict,
    *,
    cleaner: dict,
    property_mappings: dict[str, str] | None = None,
) -> dict | None:
    """Return the accepted 01A-safe fields only for an eligible open application."""
    if _select(page, "배정 전략 Snapshot") != "OPEN_APPLICATION":
        return None
    if not _fresh_common(page):
        return None
    if _select(page, "배정 수락 상태") not in OPEN_ACCEPTANCE_STATES:
        return None
    return _display_fields(
        page,
        set(cleaner.get("properties") or []),
        property_mappings or load_property_mappings(),
    )


def available_jobs(
    *,
    cleaner: dict,
    request_dir: Path,
    source,
    now: datetime | None = None,
) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    authorized_properties = set(cleaner.get("properties") or [])
    property_mappings = load_property_mappings()
    user_id = cleaner["telegram_user_id"]
    chat_id = cleaner["telegram_chat_id"]
    jobs_by_page: dict[str, dict] = {}

    if request_dir.exists():
        for path in sorted(request_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("Local jobs source record malformed") from exc
            if not isinstance(record, dict):
                raise RuntimeError("Local jobs source record must be an object")
            if not _active_offer_record(record, user_id=user_id, chat_id=chat_id, now=now):
                continue
            page_id = record.get("cleaning_page_id")
            if not isinstance(page_id, str) or not page_id:
                continue
            page = source.get_cleaning(page_id)
            if not _fresh_common(page) or _select(page, "배정 수락 상태") != "수락대기":
                continue
            fields = _display_fields(page, authorized_properties, property_mappings)
            if fields is None or fields["nickname"] != record.get("property_nickname"):
                continue
            fields["visibility"] = "MY_ACTIVE_OFFER"
            jobs_by_page[page_id] = fields

    for page in source.query_open_applications():
        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id or page_id in jobs_by_page:
            continue
        fields = open_application_fields(
            page, cleaner=cleaner, property_mappings=property_mappings
        )
        if fields is None:
            continue
        fields["visibility"] = "EXPLICIT_OPEN_APPLICATION"
        jobs_by_page[page_id] = fields

    return sorted(
        jobs_by_page.values(),
        key=lambda item: (item["service_day"], item["start_at"], item["page_id"]),
    )


def render_jobs(jobs: list[dict]) -> str:
    if not jobs:
        return EMPTY_MESSAGE
    lines = ["🧹 지원 가능한 청소", ""]
    for index, job in enumerate(jobs, start=1):
        label = "내게 온 제안" if job["visibility"] == "MY_ACTIVE_OFFER" else "공개 신청 가능"
        lines.extend([
            f"{index}. {job['nickname']} · {job['service_day'].month}/{job['service_day'].day} {job['job_type']}",
            f"   {job['start_at'].strftime('%H:%M')} → {job['end_at'].strftime('%H:%M')}",
            f"   ₩{job['fee']:,}",
            f"   {label}",
        ])
        if index != len(jobs):
            lines.append("")
    return "\n".join(lines)


def handle_jobs(
    update: dict,
    token: str,
    *,
    request_dir: Path | None = None,
    request_api: Callable | None = None,
    source=None,
    roster_loader: Callable[[], dict] | None = None,
    now: datetime | None = None,
):
    message = update.get("message", {})
    if message.get("text") != "/jobs":
        return None
    sender = message.get("from", {})
    chat = message.get("chat", {})
    if chat.get("type") != "private" or sender.get("is_bot"):
        return "jobs_unauthorized"
    request_api = request_api or _telegram_api
    roster_loader = roster_loader or load_roster
    request_dir = request_dir or CleanerRuntimePaths().request_dir

    try:
        cleaner = resolve_active_cleaner(
            roster_loader(),
            user_id=sender.get("id"),
            chat_id=chat.get("id"),
        )
    except Exception:
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=FAILURE_MESSAGE)
        return "jobs_failed"

    if cleaner is None:
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=UNAUTHORIZED_MESSAGE)
        return "jobs_unauthorized"

    try:
        jobs = available_jobs(
            cleaner=cleaner,
            request_dir=request_dir,
            source=source or NotionCleaningReader(),
            now=now,
        )
    except Exception:
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=FAILURE_MESSAGE)
        return "jobs_failed"

    values = {"chat_id": chat.get("id"), "text": render_jobs(jobs)}
    from telegram_approval.cleaner_application import application_reply_markup
    reply_markup = application_reply_markup(jobs)
    if reply_markup is not None:
        values["reply_markup"] = reply_markup
    request_api(token, "sendMessage", **values)
    return "jobs_listed"


def _telegram_api(token: str, method: str, **values):
    from telegram_approval.bot_service import api

    return api(token, method, **values)

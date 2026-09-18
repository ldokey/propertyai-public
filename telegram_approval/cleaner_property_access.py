#!/usr/bin/env python3
"""Property-level Cleaner access request/approval for PH0A-CLEAN-QUAL-01 V2.

Notion ``32_청소 인력 집 권한 DB_cleaner_property_access`` is the business
ledger.  ``cleaners.json`` remains a narrow Legacy runtime projection and is
mutated only after a durable APPROVED ledger readback.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from propertyai_core.global_writer import assert_current_production_writer
from telegram_approval.cleaner_jobs import (
    NOTION_TOKEN_PATH,
    NOTION_VERSION,
    load_property_mappings,
    resolve_active_cleaner,
)
from telegram_approval.cleaner_registry import ROSTER_PATH, atomic_private, load_roster


RENTAL_UNIT_SOURCE = "7fdd641c-9847-46ab-b91f-77e3a2063ec3"
PROPERTY_ACCESS_SOURCE = "b66af4a1-97e3-419a-8fd2-1d65c63e3d6d"
PROPERTY_REQUEST_CALLBACK_PREFIX = "cpa1:"
OPS_ACCESS_CALLBACK_PREFIX = "cpaops1:"
PROPERTY_ACCESS_CONTRACT_VERSION = "V1"
SAFE_NEUTRAL_PRIORITY = 999
OPERATIONAL_RENTAL_UNIT_STATES = frozenset({"모집중", "예약중", "입주중", "공실", "운영중"})

REQUEST_SUCCESS_MESSAGE = (
    "숙소 신청이 접수되었습니다.\n"
    "운영자 승인 후 이 숙소의 청소 제안을 받을 수 있습니다."
)
DUPLICATE_REQUEST_MESSAGE = "이미 신청한 숙소입니다."
ALREADY_APPROVED_MESSAGE = "이미 승인된 숙소입니다."
REJECTED_REQUEST_MESSAGE = "이번 숙소 신청은 승인되지 않았습니다."
APPROVED_MESSAGE = (
    "숙소 승인이 완료되었습니다.\n"
    "이제 이 숙소의 청소 대상이 될 수 있습니다."
)
PROPERTIES_UNAUTHORIZED_MESSAGE = "이 계정에서는 숙소 신청 정보를 확인할 수 없습니다."
PROPERTIES_FAILURE_MESSAGE = "숙소 목록을 불러오지 못했습니다. 잠시 후 /properties로 다시 확인해 주세요."
PROPERTIES_EMPTY_MESSAGE = "현재 신청 가능한 숙소가 없습니다."
REQUEST_FAILURE_MESSAGE = "숙소 신청 처리에 실패했습니다. 잠시 후 /properties에서 다시 확인해 주세요."

_PAGE_REF_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()


class PropertyAccessError(RuntimeError):
    pass


class PropertyAccessConflict(PropertyAccessError):
    pass


def _normalize_page_id(value: str) -> str:
    if not isinstance(value, str) or not value or not _PAGE_REF_RE.fullmatch(value):
        raise ValueError("invalid Notion page reference")
    compact = value.replace("-", "").lower()
    if len(compact) == 32 and all(char in "0123456789abcdef" for char in compact):
        return compact
    return value


def _same_page_id(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return _normalize_page_id(left) == _normalize_page_id(right)
    except ValueError:
        return False


def property_access_idempotency_key(party_page_id: str, property_page_id: str) -> str:
    return (
        "CLEANER_PROPERTY_ACCESS:"
        f"{_normalize_page_id(party_page_id)}:"
        f"{_normalize_page_id(property_page_id)}:"
        f"{PROPERTY_ACCESS_CONTRACT_VERSION}"
    )


def property_request_callback_data(rental_unit_page_id: str) -> str:
    data = PROPERTY_REQUEST_CALLBACK_PREFIX + _normalize_page_id(rental_unit_page_id)
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Telegram callback payload too large")
    return data


def ops_access_callback_data(access_page_id: str, decision: str) -> str:
    if decision not in {"approve", "reject"}:
        raise ValueError("invalid operator decision")
    data = f"{OPS_ACCESS_CALLBACK_PREFIX}{_normalize_page_id(access_page_id)}:{decision}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Telegram callback payload too large")
    return data


@contextmanager
def _property_key_lock(key: str):
    with _KEY_LOCKS_GUARD:
        lock = _KEY_LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield


def _select(page: dict, name: str) -> str | None:
    selected = page.get("properties", {}).get(name, {}).get("select")
    return selected.get("name") if isinstance(selected, dict) else None


def _checkbox(page: dict, name: str) -> bool | None:
    value = page.get("properties", {}).get(name, {}).get("checkbox")
    return value if isinstance(value, bool) else None


def _relations(page: dict, name: str) -> list[str] | None:
    prop = page.get("properties", {}).get(name)
    if not isinstance(prop, dict):
        return None
    values = prop.get("relation")
    if not isinstance(values, list):
        return None
    result: list[str] = []
    for item in values:
        if not isinstance(item, dict):
            return None
        page_id = item.get("id")
        if not isinstance(page_id, str) or not page_id:
            return None
        result.append(page_id)
    return result


def _single_relation(page: dict, name: str) -> str | None:
    values = _relations(page, name)
    return values[0] if values is not None and len(values) == 1 else None


def _single_text(prop: dict, field: str) -> str | None:
    values = prop.get(field)
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        return None
    plain = values[0].get("plain_text")
    if isinstance(plain, str):
        return plain
    text = values[0].get("text")
    if isinstance(text, dict) and isinstance(text.get("content"), str):
        return text["content"]
    return None


def _rich_value(page: dict, name: str) -> str | None:
    return _single_text(page.get("properties", {}).get(name, {}), "rich_text")


def _number(page: dict, name: str):
    return page.get("properties", {}).get(name, {}).get("number")


def _relation_property(page_id: str) -> dict:
    return {"relation": [{"id": page_id}]}


def _select_property(value: str) -> dict:
    return {"select": {"name": value}}


def _rich_text_property(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value}}]}


def _title_property(value: str) -> dict:
    return {"title": [{"text": {"content": value}}]}


def _date_property(value: str) -> dict:
    return {"date": {"start": value}}


def _number_property(value: int) -> dict:
    return {"number": value}


def _mapped_property_label(property_page_id: str, mappings: dict[str, str]) -> str | None:
    matches: list[str] = []
    for mapped_id, label in mappings.items():
        if _same_page_id(mapped_id, property_page_id):
            if not isinstance(label, str) or not label.strip():
                return None
            matches.append(label.strip())
    return matches[0] if len(matches) == 1 else None


def _eligible_rental_unit(page: dict, mappings: dict[str, str]) -> dict | None:
    if not isinstance(page, dict) or not isinstance(page.get("properties"), dict):
        raise PropertyAccessError("Rental Unit page malformed")
    page_id = page.get("id")
    if not isinstance(page_id, str) or not page_id:
        raise PropertyAccessError("Rental Unit id malformed")
    if _select(page, "데이터 환경") != "PRODUCTION":
        return None
    if _select(page, "등록 상태") != "APPROVED":
        return None
    if _select(page, "상태") not in OPERATIONAL_RENTAL_UNIT_STATES:
        return None
    if _select(page, "운영 모드") != "Airbnb":
        return None
    if _checkbox(page, "청소 운영 활성") is not True:
        return None
    property_ids = _relations(page, "연결 집")
    if property_ids is None or len(property_ids) != 1:
        raise PropertyAccessError("eligible Rental Unit must resolve exactly one Property")
    property_page_id = property_ids[0]
    label = _mapped_property_label(property_page_id, mappings)
    if label is None:
        raise PropertyAccessError("canonical safe Property label unavailable or ambiguous")
    return {
        "rental_unit_page_id": page_id,
        "property_page_id": property_page_id,
        "label": label,
    }


class NotionPropertyAccessSource:
    """Read-only Rental Unit / Party / Property source used by this slice."""

    def __init__(self, *, token_path: Path = NOTION_TOKEN_PATH, urlopen=urllib.request.urlopen):
        self._token_path = token_path
        self._urlopen = urlopen

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        page_read = method == "GET" and re.fullmatch(r"/v1/pages/[A-Za-z0-9-]+", path) is not None
        rental_query = method == "POST" and path == f"/v1/data_sources/{RENTAL_UNIT_SOURCE}/query"
        if not (page_read or rental_query):
            raise PropertyAccessError("Property access source endpoint rejected")
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
            value = json.loads(response.read())
        if not isinstance(value, dict):
            raise PropertyAccessError("Notion source response malformed")
        return value

    def get_page(self, page_id: str) -> dict:
        page_id = _normalize_page_id(page_id)
        page = self._request("GET", f"/v1/pages/{page_id}")
        if not isinstance(page.get("properties"), dict):
            raise PropertyAccessError("Notion page response malformed")
        return page

    def query_candidate_units(self) -> list[dict]:
        results: list[dict] = []
        cursor = None
        while True:
            body: dict = {
                "filter": {"and": [
                    {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
                    {"property": "등록 상태", "select": {"equals": "APPROVED"}},
                    {"property": "운영 모드", "select": {"equals": "Airbnb"}},
                    {"property": "청소 운영 활성", "checkbox": {"equals": True}},
                ]},
                "page_size": 100,
            }
            if cursor is not None:
                body["start_cursor"] = cursor
            page = self._request("POST", f"/v1/data_sources/{RENTAL_UNIT_SOURCE}/query", body)
            rows = page.get("results")
            if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
                raise PropertyAccessError("Rental Unit query response malformed")
            has_more = page.get("has_more")
            if not isinstance(has_more, bool) or "next_cursor" not in page:
                raise PropertyAccessError("Rental Unit query pagination malformed")
            results.extend(rows)
            if not has_more:
                if page["next_cursor"] is not None:
                    raise PropertyAccessError("Rental Unit terminal cursor malformed")
                return results
            cursor = page["next_cursor"]
            if not isinstance(cursor, str) or not cursor:
                raise PropertyAccessError("Rental Unit query cursor malformed")


class NotionPropertyAccessLedger:
    """Narrow writer for the Cleaner × Property business ledger only."""

    def __init__(self, *, token_path: Path = NOTION_TOKEN_PATH, urlopen=urllib.request.urlopen):
        self._token_path = token_path
        self._urlopen = urlopen

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        page_read = method == "GET" and re.fullmatch(r"/v1/pages/[A-Za-z0-9-]+", path) is not None
        access_query = method == "POST" and path == f"/v1/data_sources/{PROPERTY_ACCESS_SOURCE}/query"
        access_create = method == "POST" and path == "/v1/pages"
        access_update = method == "PATCH" and re.fullmatch(r"/v1/pages/[A-Za-z0-9-]+", path) is not None
        if not (page_read or access_query or access_create or access_update):
            raise PropertyAccessError("Property access ledger endpoint rejected")
        if access_create or access_update:
            assert_current_production_writer()
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
            value = json.loads(response.read())
        if not isinstance(value, dict):
            raise PropertyAccessError("Notion ledger response malformed")
        return value

    def get_access(self, page_id: str) -> dict:
        page = self._request("GET", f"/v1/pages/{_normalize_page_id(page_id)}")
        if not isinstance(page.get("properties"), dict):
            raise PropertyAccessError("Access readback malformed")
        return page

    def _query(self, filter_value: dict) -> list[dict]:
        results: list[dict] = []
        cursor = None
        while True:
            body: dict = {"filter": filter_value, "page_size": 100}
            if cursor is not None:
                body["start_cursor"] = cursor
            page = self._request("POST", f"/v1/data_sources/{PROPERTY_ACCESS_SOURCE}/query", body)
            rows = page.get("results")
            if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
                raise PropertyAccessError("Access query response malformed")
            has_more = page.get("has_more")
            if not isinstance(has_more, bool) or "next_cursor" not in page:
                raise PropertyAccessError("Access query pagination malformed")
            results.extend(rows)
            if not has_more:
                if page["next_cursor"] is not None:
                    raise PropertyAccessError("Access terminal cursor malformed")
                return results
            cursor = page["next_cursor"]
            if not isinstance(cursor, str) or not cursor:
                raise PropertyAccessError("Access query cursor malformed")

    def query_by_identity(self, party_page_id: str, property_page_id: str) -> list[dict]:
        return self._query({"and": [
            {"property": "인력", "relation": {"contains": party_page_id}},
            {"property": "집", "relation": {"contains": property_page_id}},
        ]})

    def query_by_idempotency(self, key: str) -> list[dict]:
        return self._query({"property": "Idempotency Key", "rich_text": {"equals": key}})

    def query_requested(self) -> list[dict]:
        return self._query({"and": [
            {"property": "신청 상태", "select": {"equals": "REQUESTED"}},
            {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
        ]})

    def query_approved_synced(self) -> list[dict]:
        """Read-only discovery for Cleaner-owned approval delivery work."""
        return self._query({"and": [
            {"property": "신청 상태", "select": {"equals": "APPROVED"}},
            {"property": "Runtime Sync Status", "select": {"equals": "SYNCED"}},
            {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
            {"property": "승인일", "date": {"is_not_empty": True}},
        ]})

    def query_rejected(self) -> list[dict]:
        """Read-only discovery for Cleaner-owned rejection delivery work."""
        return self._query({"and": [
            {"property": "신청 상태", "select": {"equals": "REJECTED"}},
            {"property": "Runtime Sync Status", "select": {"equals": "NOT_REQUIRED"}},
            {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
        ]})

    def create_request(
        self,
        *,
        party_page_id: str,
        property_page_id: str,
        source_message_ref: str,
        key: str,
        requested_at: datetime,
    ) -> dict:
        if requested_at.tzinfo is None:
            raise PropertyAccessError("Request time must be timezone-aware")
        body = {
            "parent": {"type": "data_source_id", "data_source_id": PROPERTY_ACCESS_SOURCE},
            "properties": {
                "권한명": _title_property("Cleaner 숙소 접근 신청"),
                "인력": _relation_property(party_page_id),
                "집": _relation_property(property_page_id),
                "신청 상태": _select_property("REQUESTED"),
                "신청일": _date_property(requested_at.isoformat()),
                "Idempotency Key": _rich_text_property(key),
                "Source Message Ref": _rich_text_property(source_message_ref),
                "데이터 환경": _select_property("PRODUCTION"),
                "Runtime Sync Status": _select_property("NOT_REQUIRED"),
            },
        }
        return self._request("POST", "/v1/pages", body)

    def approve(self, page_id: str, approved_at: datetime) -> dict:
        return self._update(page_id, {
            "신청 상태": _select_property("APPROVED"),
            "승인일": _date_property(approved_at.isoformat()),
            "Runtime Sync Status": _select_property("PENDING"),
            "우선순위": _number_property(SAFE_NEUTRAL_PRIORITY),
        })

    def reject(self, page_id: str) -> dict:
        return self._update(page_id, {
            "신청 상태": _select_property("REJECTED"),
            "Runtime Sync Status": _select_property("NOT_REQUIRED"),
        })

    def set_sync_status(self, page_id: str, status: str, *, synced_at: datetime | None = None) -> dict:
        if status not in {"PENDING", "SYNCED", "FAILED"}:
            raise PropertyAccessError("invalid runtime sync status")
        properties = {"Runtime Sync Status": _select_property(status)}
        if status == "SYNCED":
            if synced_at is None or synced_at.tzinfo is None:
                raise PropertyAccessError("SYNCED requires timezone-aware timestamp")
            properties["Last Synced At"] = _date_property(synced_at.isoformat())
        return self._update(page_id, properties)

    def _update(self, page_id: str, properties: dict) -> dict:
        return self._request(
            "PATCH",
            f"/v1/pages/{_normalize_page_id(page_id)}",
            {"properties": properties},
        )


def _access_row(page: dict) -> dict:
    if not isinstance(page, dict) or not isinstance(page.get("properties"), dict):
        raise PropertyAccessConflict("Access row malformed")
    page_id = page.get("id")
    party_page_id = _single_relation(page, "인력")
    property_page_id = _single_relation(page, "집")
    key = _rich_value(page, "Idempotency Key")
    if not all(isinstance(value, str) and value for value in (page_id, party_page_id, property_page_id, key)):
        raise PropertyAccessConflict("Access identity malformed")
    if _select(page, "데이터 환경") != "PRODUCTION":
        raise PropertyAccessConflict("Access environment mismatch")
    status = _select(page, "신청 상태")
    if status not in {"REQUESTED", "APPROVED", "REJECTED", "REVOKED"}:
        raise PropertyAccessConflict("Access status malformed")
    sync_status = _select(page, "Runtime Sync Status")
    if sync_status not in {"PENDING", "SYNCED", "FAILED", "NOT_REQUIRED"}:
        raise PropertyAccessConflict("Access sync status malformed")
    return {
        "page_id": page_id,
        "party_page_id": party_page_id,
        "property_page_id": property_page_id,
        "key": key,
        "status": status,
        "sync_status": sync_status,
        "priority": _number(page, "우선순위"),
    }


def _fresh_access_row(ledger, party_page_id: str, property_page_id: str) -> dict | None:
    key = property_access_idempotency_key(party_page_id, property_page_id)
    identity_rows = ledger.query_by_identity(party_page_id, property_page_id)
    key_rows = ledger.query_by_idempotency(key)
    if len(identity_rows) > 1 or len(key_rows) > 1:
        raise PropertyAccessConflict("multiple Access rows for one Cleaner × Property")

    parsed_by_id: dict[str, dict] = {}
    for page in [*identity_rows, *key_rows]:
        row = _access_row(page)
        if row["key"] != key:
            raise PropertyAccessConflict("Access idempotency key mismatch")
        if not _same_page_id(row["party_page_id"], party_page_id) or not _same_page_id(
            row["property_page_id"], property_page_id
        ):
            raise PropertyAccessConflict("Access relation mismatch")
        normalized = _normalize_page_id(row["page_id"])
        parsed_by_id[normalized] = row
    if len(parsed_by_id) > 1:
        raise PropertyAccessConflict("Access identity/key queries disagree")
    return next(iter(parsed_by_id.values()), None)


def discover_properties(*, cleaner: dict, source, ledger, mappings: dict[str, str] | None = None) -> list[dict]:
    party_page_id = cleaner.get("party_page_id")
    if not isinstance(party_page_id, str) or not party_page_id:
        raise PropertyAccessError("Cleaner Party relation missing")
    if mappings is None:
        mappings = load_property_mappings()
    by_property: dict[str, dict] = {}
    for page in source.query_candidate_units():
        candidate = _eligible_rental_unit(page, mappings)
        if candidate is None:
            # Re-check every result locally even though the Notion query is
            # filtered. Stale/non-operational rows are excluded; malformed
            # canonical Property relations still raise fail-closed above.
            continue
        property_key = _normalize_page_id(candidate["property_page_id"])
        existing = by_property.get(property_key)
        if existing is None or _normalize_page_id(candidate["rental_unit_page_id"]) < _normalize_page_id(
            existing["rental_unit_page_id"]
        ):
            by_property[property_key] = candidate

    result: list[dict] = []
    for candidate in by_property.values():
        access = _fresh_access_row(ledger, party_page_id, candidate["property_page_id"])
        status = "AVAILABLE" if access is None else access["status"]
        result.append({**candidate, "status": status})
    return sorted(result, key=lambda item: (item["label"].casefold(), _normalize_page_id(item["property_page_id"])))


def render_properties(candidates: list[dict]) -> str:
    if not candidates:
        return PROPERTIES_EMPTY_MESSAGE
    lines = ["🏠 가능한 숙소", ""]
    for index, candidate in enumerate(candidates):
        status = candidate["status"]
        if status == "APPROVED":
            icon, label = "✅", "승인됨"
        elif status == "REQUESTED":
            icon, label = "⏳", "승인 대기"
        elif status == "AVAILABLE":
            icon, label = "➕", "[신청]"
        else:
            icon, label = "❌", "승인되지 않음"
        lines.extend([f"{icon} {candidate['label']}", label])
        if index != len(candidates) - 1:
            lines.append("")
    return "\n".join(lines)


def properties_reply_markup(candidates: list[dict]) -> str | None:
    rows = []
    for candidate in candidates:
        if candidate["status"] != "AVAILABLE":
            continue
        rows.append([{
            "text": f"신청 · {candidate['label']}",
            "callback_data": property_request_callback_data(candidate["rental_unit_page_id"]),
        }])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False) if rows else None


def _source_message_ref(query: dict) -> str | None:
    callback_id = query.get("id")
    message_id = query.get("message", {}).get("message_id")
    if not isinstance(callback_id, str) or not callback_id or isinstance(message_id, bool) or not isinstance(message_id, int):
        return None
    return f"telegram_callback:{callback_id}:message:{message_id}"


def request_property_access(
    *,
    cleaner: dict,
    rental_unit_page_id: str,
    source_message_ref: str,
    source,
    ledger,
    mappings: dict[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    party_page_id = cleaner.get("party_page_id")
    if not isinstance(party_page_id, str) or not party_page_id:
        return "unauthorized"
    if mappings is None:
        mappings = load_property_mappings()
    try:
        page = source.get_page(rental_unit_page_id)
        if not _same_page_id(page.get("id"), rental_unit_page_id):
            return "unavailable"
        candidate = _eligible_rental_unit(page, mappings)
    except Exception:
        return "failed"
    if candidate is None:
        return "unavailable"
    property_page_id = candidate["property_page_id"]
    try:
        key = property_access_idempotency_key(party_page_id, property_page_id)
    except ValueError:
        return "failed"
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return "failed"

    with _property_key_lock(key):
        try:
            current = _fresh_access_row(ledger, party_page_id, property_page_id)
        except Exception:
            return "failed"
        if current is not None:
            if current["status"] == "REQUESTED":
                return "existing"
            if current["status"] == "APPROVED":
                return "approved"
            return "blocked"

        try:
            ledger.create_request(
                party_page_id=party_page_id,
                property_page_id=property_page_id,
                source_message_ref=source_message_ref,
                key=key,
                requested_at=now,
            )
            current = _fresh_access_row(ledger, party_page_id, property_page_id)
        except Exception:
            return "failed"
        if current is None or current["status"] != "REQUESTED" or current["sync_status"] != "NOT_REQUIRED":
            return "failed"

        # Telegram approval delivery is intentionally OPS-owned.  Cleaner stops
        # after the durable REQUESTED readback and never reads OPS credentials.
        return "created"


def _request_result_message(result: str) -> str:
    return {
        "created": REQUEST_SUCCESS_MESSAGE,
        "existing": DUPLICATE_REQUEST_MESSAGE,
        "approved": ALREADY_APPROVED_MESSAGE,
        "blocked": REJECTED_REQUEST_MESSAGE,
        "unavailable": REQUEST_FAILURE_MESSAGE,
        "unauthorized": REQUEST_FAILURE_MESSAGE,
        "failed": REQUEST_FAILURE_MESSAGE,
    }.get(result, REQUEST_FAILURE_MESSAGE)


def handle_properties(
    update: dict,
    token: str,
    *,
    request_api: Callable | None = None,
    source=None,
    ledger=None,
    roster_loader: Callable[[], dict] | None = None,
    mappings: dict[str, str] | None = None,
):
    message = update.get("message", {})
    if message.get("text") != "/properties":
        return None
    sender = message.get("from", {})
    chat = message.get("chat", {})
    if chat.get("type") != "private" or sender.get("is_bot"):
        return "properties_unauthorized"
    request_api = request_api or _telegram_api
    roster_loader = roster_loader or load_roster
    try:
        cleaner = resolve_active_cleaner(
            roster_loader(), user_id=sender.get("id"), chat_id=chat.get("id")
        )
    except Exception:
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=PROPERTIES_FAILURE_MESSAGE)
        return "properties_failed"
    if cleaner is None:
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=PROPERTIES_UNAUTHORIZED_MESSAGE)
        return "properties_unauthorized"
    try:
        candidates = discover_properties(
            cleaner=cleaner,
            source=source or NotionPropertyAccessSource(),
            ledger=ledger or NotionPropertyAccessLedger(),
            mappings=mappings,
        )
    except Exception:
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=PROPERTIES_FAILURE_MESSAGE)
        return "properties_failed"
    values = {"chat_id": chat["id"], "text": render_properties(candidates)}
    reply_markup = properties_reply_markup(candidates)
    if reply_markup is not None:
        values["reply_markup"] = reply_markup
    request_api(token, "sendMessage", **values)
    return "properties_listed"


def handle_property_request_callback(
    update: dict,
    token: str,
    *,
    request_api: Callable | None = None,
    source=None,
    ledger=None,
    roster_loader: Callable[[], dict] | None = None,
    mappings: dict[str, str] | None = None,
    now: datetime | None = None,
):
    query = update.get("callback_query")
    if not isinstance(query, dict):
        return None
    data = query.get("data")
    if not isinstance(data, str) or not data.startswith(PROPERTY_REQUEST_CALLBACK_PREFIX):
        return None
    request_api = request_api or _telegram_api
    callback_id = query.get("id")

    def acknowledge(text: str) -> None:
        if not isinstance(callback_id, str) or not callback_id:
            return
        try:
            request_api(token, "answerCallbackQuery", callback_query_id=callback_id, text=text)
        except Exception:
            pass

    try:
        rental_unit_page_id = _normalize_page_id(data[len(PROPERTY_REQUEST_CALLBACK_PREFIX):])
    except ValueError:
        acknowledge("신청할 수 없습니다.")
        return "property_request_unavailable"
    sender = query.get("from", {})
    chat = query.get("message", {}).get("chat", {})
    if chat.get("type") != "private" or sender.get("is_bot"):
        acknowledge("신청할 수 없습니다.")
        return "property_request_unauthorized"
    source_ref = _source_message_ref(query)
    if source_ref is None:
        acknowledge("신청 처리에 실패했습니다.")
        return "property_request_failed"
    roster_loader = roster_loader or load_roster
    try:
        cleaner = resolve_active_cleaner(
            roster_loader(), user_id=sender.get("id"), chat_id=chat.get("id")
        )
    except Exception:
        cleaner = None
    if cleaner is None or not isinstance(cleaner.get("party_page_id"), str) or not cleaner.get("party_page_id"):
        acknowledge("신청할 수 없습니다.")
        request_api(token, "sendMessage", chat_id=chat.get("id"), text=REQUEST_FAILURE_MESSAGE)
        return "property_request_unauthorized"

    acknowledge("신청 상태를 확인합니다.")
    result = request_property_access(
        cleaner=cleaner,
        rental_unit_page_id=rental_unit_page_id,
        source_message_ref=source_ref,
        source=source or NotionPropertyAccessSource(),
        ledger=ledger or NotionPropertyAccessLedger(),
        mappings=mappings,
        now=now,
    )
    request_api(token, "sendMessage", chat_id=chat["id"], text=_request_result_message(result))
    return f"property_request_{result}"


def _resolve_cleaner_by_party(roster: dict, party_page_id: str) -> dict:
    if not isinstance(roster, dict) or not isinstance(roster.get("cleaners"), list):
        raise PropertyAccessError("Cleaner roster malformed")
    entries = roster["cleaners"]
    if not all(isinstance(item, dict) for item in entries):
        raise PropertyAccessError("Cleaner roster entry malformed")
    matches = [item for item in entries if _same_page_id(item.get("party_page_id"), party_page_id)]
    if len(matches) != 1:
        raise PropertyAccessError("Cleaner Party projection identity ambiguous")
    cleaner = matches[0]
    if cleaner.get("role") != "CLEANER" or cleaner.get("status") != "ACTIVE":
        raise PropertyAccessError("Cleaner Party projection identity inactive")
    if isinstance(cleaner.get("telegram_chat_id"), bool) or not isinstance(cleaner.get("telegram_chat_id"), int):
        raise PropertyAccessError("Cleaner chat identity malformed")
    if not isinstance(cleaner.get("properties"), list) or not all(
        isinstance(item, str) and item for item in cleaner["properties"]
    ):
        raise PropertyAccessError("Cleaner properties projection malformed")
    if not isinstance(cleaner.get("priority_by_property"), dict):
        raise PropertyAccessError("Cleaner priority projection malformed")
    return cleaner


def _validate_fresh_relation_page(page: dict, expected_page_id: str, kind: str) -> None:
    if not isinstance(page, dict) or not isinstance(page.get("properties"), dict) or not _same_page_id(
        page.get("id"), expected_page_id
    ):
        raise PropertyAccessError(f"fresh {kind} relation read failed")


def _projection_present(roster: dict, party_page_id: str, property_label: str) -> bool:
    cleaner = _resolve_cleaner_by_party(roster, party_page_id)
    properties = cleaner["properties"]
    priority = cleaner["priority_by_property"].get(property_label)
    return properties.count(property_label) == 1 and isinstance(priority, (int, float)) and not isinstance(priority, bool)


def _project_property_to_roster(
    *,
    roster_path: Path,
    party_page_id: str,
    property_label: str,
    priority: int = SAFE_NEUTRAL_PRIORITY,
) -> dict:
    if roster_path == ROSTER_PATH:
        roster = load_roster()
    else:
        if not roster_path.exists():
            raise PropertyAccessError("Cleaner roster projection missing")
        roster = json.loads(roster_path.read_text())
    cleaner = _resolve_cleaner_by_party(roster, party_page_id)
    # Preserve order while enforcing the V2 projection invariant that a
    # canonical Property nickname appears exactly once.
    deduplicated = list(dict.fromkeys(cleaner["properties"]))
    if property_label not in deduplicated:
        deduplicated.append(property_label)
    cleaner["properties"] = deduplicated
    # Preserve an already-authorized explicit priority; fill only a missing
    # projection with the neutral Legacy value.
    existing_priority = cleaner["priority_by_property"].get(property_label)
    if not isinstance(existing_priority, (int, float)) or isinstance(existing_priority, bool):
        cleaner["priority_by_property"][property_label] = priority
    cleaner["updated_at"] = datetime.now(timezone.utc).isoformat()
    assert_current_production_writer()
    atomic_private(roster_path, roster)
    verify = json.loads(roster_path.read_text())
    if not _projection_present(verify, party_page_id, property_label):
        raise PropertyAccessError("Cleaner roster projection verification failed")
    return _resolve_cleaner_by_party(verify, party_page_id)


def approve_property_access(
    *,
    access_page_id: str,
    source,
    ledger,
    roster_loader: Callable[[], dict] | None = None,
    roster_path: Path = ROSTER_PATH,
    cleaner_notifier: Callable[[int, str], None] | None = None,
    mappings: dict[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    roster_loader = roster_loader or load_roster
    # Compatibility-only injection seam: approval delivery is asynchronous and
    # this callable is intentionally never invoked by the approval transaction.
    _ = cleaner_notifier
    if mappings is None:
        mappings = load_property_mappings()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return "failed"
    try:
        initial = _access_row(ledger.get_access(access_page_id))
        current = _fresh_access_row(ledger, initial["party_page_id"], initial["property_page_id"])
        if current is None or not _same_page_id(current["page_id"], access_page_id):
            raise PropertyAccessConflict("stale operator callback Access identity mismatch")
        property_page = source.get_page(current["property_page_id"])
        party_page = source.get_page(current["party_page_id"])
        _validate_fresh_relation_page(property_page, current["property_page_id"], "Property")
        _validate_fresh_relation_page(party_page, current["party_page_id"], "Cleaner Party")
        property_label = _mapped_property_label(current["property_page_id"], mappings)
        if property_label is None:
            raise PropertyAccessError("canonical Property nickname unavailable")
        roster_before = roster_loader()
        _resolve_cleaner_by_party(roster_before, current["party_page_id"])
    except Exception:
        return "failed"

    if current["status"] in {"REJECTED", "REVOKED"}:
        return "not_requestable"
    already_projected = False
    try:
        already_projected = _projection_present(roster_before, current["party_page_id"], property_label)
    except Exception:
        return "failed"
    if current["status"] == "APPROVED" and current["sync_status"] == "SYNCED" and already_projected:
        return "already_approved"

    try:
        if current["status"] == "REQUESTED":
            ledger.approve(current["page_id"], now)
            current = _access_row(ledger.get_access(current["page_id"]))
            if (
                current["status"] != "APPROVED"
                or current["sync_status"] != "PENDING"
                or current["priority"] != SAFE_NEUTRAL_PRIORITY
            ):
                raise PropertyAccessError("APPROVED durable readback failed")
        elif current["status"] == "APPROVED":
            ledger.set_sync_status(current["page_id"], "PENDING")
            current = _access_row(ledger.get_access(current["page_id"]))
            if current["sync_status"] != "PENDING":
                raise PropertyAccessError("PENDING durable readback failed")
        else:
            return "not_requestable"
    except Exception:
        return "failed"

    try:
        _project_property_to_roster(
            roster_path=roster_path,
            party_page_id=current["party_page_id"],
            property_label=property_label,
        )
    except Exception:
        try:
            ledger.set_sync_status(current["page_id"], "FAILED")
            failed = _access_row(ledger.get_access(current["page_id"]))
            if failed["status"] != "APPROVED" or failed["sync_status"] != "FAILED":
                return "failed"
        except Exception:
            return "failed"
        return "reconciliation_required"

    try:
        ledger.set_sync_status(current["page_id"], "SYNCED", synced_at=now)
        synced = _access_row(ledger.get_access(current["page_id"]))
        if synced["status"] != "APPROVED" or synced["sync_status"] != "SYNCED":
            raise PropertyAccessError("SYNCED durable readback failed")
    except Exception:
        return "reconciliation_required"

    # Approval business state is complete at durable APPROVED + SYNCED readback.
    # Cleaner Telegram delivery is owned asynchronously by the Cleaner runtime.
    return "approved_synced"


def reject_property_access(
    *,
    access_page_id: str,
    source,
    ledger,
    roster_loader: Callable[[], dict] | None = None,
    cleaner_notifier: Callable[[int, str], None] | None = None,
) -> str:
    roster_loader = roster_loader or load_roster
    # Compatibility-only injection seam: rejection delivery is asynchronous and
    # this callable is intentionally never invoked by the rejection transaction.
    _ = cleaner_notifier
    try:
        initial = _access_row(ledger.get_access(access_page_id))
        current = _fresh_access_row(ledger, initial["party_page_id"], initial["property_page_id"])
        if current is None or not _same_page_id(current["page_id"], access_page_id):
            raise PropertyAccessConflict("stale operator callback Access identity mismatch")
        _validate_fresh_relation_page(source.get_page(current["property_page_id"]), current["property_page_id"], "Property")
        _validate_fresh_relation_page(source.get_page(current["party_page_id"]), current["party_page_id"], "Cleaner Party")
        _resolve_cleaner_by_party(roster_loader(), current["party_page_id"])
    except Exception:
        return "failed"
    if current["status"] != "REQUESTED":
        return "not_requestable"
    try:
        ledger.reject(current["page_id"])
        rejected = _access_row(ledger.get_access(current["page_id"]))
        if rejected["status"] != "REJECTED" or rejected["sync_status"] != "NOT_REQUIRED":
            raise PropertyAccessError("REJECTED durable readback failed")
    except Exception:
        return "failed"

    # Rejection business state is complete at durable REJECTED + NOT_REQUIRED
    # readback. Cleaner Telegram delivery is owned asynchronously by the Cleaner
    # runtime and cannot change this business result.
    return "rejected"


def handle_ops_property_access_callback(
    update: dict,
    token: str,
    *,
    request_api: Callable | None = None,
    source=None,
    ledger=None,
    roster_loader: Callable[[], dict] | None = None,
    roster_path: Path = ROSTER_PATH,
    cleaner_notifier: Callable[[int, str], None] | None = None,
    mappings: dict[str, str] | None = None,
    now: datetime | None = None,
):
    """Handle a callback after ``OpsTelegramRouter`` allowlist authorization."""
    query = update.get("callback_query")
    if not isinstance(query, dict):
        return None
    data = query.get("data")
    if not isinstance(data, str) or not data.startswith(OPS_ACCESS_CALLBACK_PREFIX):
        return None
    request_api = request_api or _telegram_api
    raw = data[len(OPS_ACCESS_CALLBACK_PREFIX):]
    try:
        access_page_id, decision = raw.rsplit(":", 1)
        access_page_id = _normalize_page_id(access_page_id)
        if decision not in {"approve", "reject"}:
            raise ValueError
    except ValueError:
        return "property_access_ops_failed"
    callback_id = query.get("id")
    if isinstance(callback_id, str) and callback_id:
        try:
            request_api(token, "answerCallbackQuery", callback_query_id=callback_id, text="최신 상태를 확인합니다.")
        except Exception:
            pass
    source = source or NotionPropertyAccessSource()
    ledger = ledger or NotionPropertyAccessLedger()
    if decision == "approve":
        result = approve_property_access(
            access_page_id=access_page_id,
            source=source,
            ledger=ledger,
            roster_loader=roster_loader,
            roster_path=roster_path,
            mappings=mappings,
            now=now,
        )
    else:
        result = reject_property_access(
            access_page_id=access_page_id,
            source=source,
            ledger=ledger,
            roster_loader=roster_loader,
            cleaner_notifier=cleaner_notifier,
        )
    operator_message = {
        "approved_synced": "숙소 승인과 Runtime 반영이 완료되었습니다. Cleaner 알림은 별도 전송됩니다.",
        "already_approved": "이미 승인되어 Runtime 반영이 완료된 숙소입니다.",
        "reconciliation_required": "승인은 기록되었지만 Runtime 반영 확인이 필요합니다.",
        "rejected": "숙소 신청 거절 처리가 완료되었습니다. Cleaner 알림은 별도 전송됩니다.",
        "not_requestable": "현재 상태에서는 이 요청을 처리할 수 없습니다.",
        "failed": "숙소 권한 처리에 실패했습니다. 상태를 다시 확인해 주세요.",
    }.get(result, "숙소 권한 처리에 실패했습니다. 상태를 다시 확인해 주세요.")
    chat_id = query.get("message", {}).get("chat", {}).get("id")
    if isinstance(chat_id, int) and not isinstance(chat_id, bool):
        try:
            request_api(token, "sendMessage", chat_id=chat_id, text=operator_message)
        except Exception:
            pass
    return f"property_access_ops_{result}"


def _telegram_api(token: str, method: str, **values):
    from telegram_approval.bot_service import api

    return api(token, method, **values)

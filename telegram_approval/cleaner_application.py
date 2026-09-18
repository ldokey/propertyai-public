#!/usr/bin/env python3
"""Bounded Cleaner application ledger flow for Phase 0A PH0A-CLEAN-01B."""

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
    NotionCleaningReader,
    load_property_mappings,
    open_application_fields,
    resolve_active_cleaner,
)
from telegram_approval.cleaner_registry import load_roster


APPLICATION_SOURCE = "b0dcb1b2-c0d3-4a66-b65b-212581c121e2"
APPLICATION_CALLBACK_PREFIX = "ca1:"
APPLICATION_CONTRACT_VERSION = "V1"
APPLICATION_PROPERTY_WHITELIST = frozenset({
    "신청명",
    "Cleaning",
    "Cleaner",
    "Application Status",
    "Applied At",
    "Source Channel",
    "Source Message Ref",
    "Idempotency Key",
    "Data Environment",
})

SUCCESS_MESSAGE = "신청되었습니다.\n담당자로 확정된 것은 아닙니다.\n선정되면 별도 제안이 옵니다."
DUPLICATE_MESSAGE = "이미 신청한 청소입니다.\n담당자로 확정된 것은 아닙니다."
UNAVAILABLE_MESSAGE = "현재는 신청할 수 없는 청소입니다."
FAILURE_MESSAGE = "신청 처리에 실패했습니다.\n잠시 후 /jobs에서 다시 확인해 주세요."

_PAGE_REF_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()


class ApplicationLedgerError(RuntimeError):
    pass


class ApplicationIdempotencyConflict(ApplicationLedgerError):
    pass


def _normalize_page_id(value: str) -> str:
    if not isinstance(value, str) or not value or not _PAGE_REF_RE.fullmatch(value):
        raise ValueError("invalid Notion page reference")
    compact = value.replace("-", "").lower()
    if len(compact) == 32 and all(char in "0123456789abcdef" for char in compact):
        return compact
    return value


def application_idempotency_key(cleaning_page_id: str, cleaner_party_page_id: str) -> str:
    return (
        "CLEANING_JOB_APPLICATION:"
        f"{_normalize_page_id(cleaning_page_id)}:"
        f"{_normalize_page_id(cleaner_party_page_id)}:"
        f"{APPLICATION_CONTRACT_VERSION}"
    )


def application_callback_data(cleaning_page_id: str) -> str:
    data = APPLICATION_CALLBACK_PREFIX + _normalize_page_id(cleaning_page_id)
    if len(data.encode("utf-8")) > 64:
        raise ValueError("Telegram callback payload too large")
    return data


def application_reply_markup(jobs: list[dict]) -> str | None:
    rows = []
    for index, job in enumerate(jobs, start=1):
        if job.get("visibility") != "EXPLICIT_OPEN_APPLICATION":
            continue
        rows.append([{
            "text": f"신청 · {index}번",
            "callback_data": application_callback_data(job["page_id"]),
        }])
    if not rows:
        return None
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


@contextmanager
def _application_key_lock(key: str):
    with _KEY_LOCKS_GUARD:
        lock = _KEY_LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield


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


def _single_text_value(prop: dict, field: str) -> str | None:
    values = prop.get(field)
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        return None
    item = values[0]
    plain = item.get("plain_text")
    if isinstance(plain, str):
        return plain
    text = item.get("text")
    if isinstance(text, dict) and isinstance(text.get("content"), str):
        return text["content"]
    return None


def _single_relation_id(page: dict, name: str) -> str | None:
    prop = page.get("properties", {}).get(name, {})
    values = prop.get("relation")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        return None
    value = values[0].get("id")
    return value if isinstance(value, str) and value else None


def _select_value(page: dict, name: str) -> str | None:
    selected = page.get("properties", {}).get(name, {}).get("select")
    return selected.get("name") if isinstance(selected, dict) else None


def _rich_value(page: dict, name: str) -> str | None:
    prop = page.get("properties", {}).get(name, {})
    return _single_text_value(prop, "rich_text")


def _validate_application_row(
    page: dict,
    *,
    key: str,
    cleaning_page_id: str,
    cleaner_party_page_id: str,
) -> bool:
    if not isinstance(page, dict) or not isinstance(page.get("properties"), dict):
        return False
    cleaning = _single_relation_id(page, "Cleaning")
    cleaner = _single_relation_id(page, "Cleaner")
    try:
        cleaning_matches = _normalize_page_id(cleaning) == _normalize_page_id(cleaning_page_id) if cleaning else False
        cleaner_matches = _normalize_page_id(cleaner) == _normalize_page_id(cleaner_party_page_id) if cleaner else False
    except ValueError:
        return False
    return (
        _rich_value(page, "Idempotency Key") == key
        and cleaning_matches
        and cleaner_matches
        and _select_value(page, "Application Status") == "APPLIED"
        and _select_value(page, "Data Environment") == "PRODUCTION"
    )


def _validate_text_payload(prop: dict, field: str) -> bool:
    return isinstance(prop, dict) and bool(_single_text_value(prop, field))


def _validate_relation_payload(prop: dict) -> bool:
    if not isinstance(prop, dict) or set(prop) != {"relation"}:
        return False
    values = prop["relation"]
    return (
        isinstance(values, list)
        and len(values) == 1
        and isinstance(values[0], dict)
        and set(values[0]) == {"id"}
        and isinstance(values[0]["id"], str)
        and bool(values[0]["id"])
        and _PAGE_REF_RE.fullmatch(values[0]["id"]) is not None
    )


def _validate_fixed_select_payload(prop: dict, expected: str) -> bool:
    return (
        isinstance(prop, dict)
        and set(prop) == {"select"}
        and isinstance(prop["select"], dict)
        and prop["select"] == {"name": expected}
    )


def _validate_create_payload(body: dict) -> None:
    if not isinstance(body, dict) or set(body) != {"parent", "properties"}:
        raise ApplicationLedgerError("Application writer create body rejected")
    if body["parent"] != {"type": "data_source_id", "data_source_id": APPLICATION_SOURCE}:
        raise ApplicationLedgerError("Application writer parent rejected")
    properties = body["properties"]
    if not isinstance(properties, dict) or set(properties) != APPLICATION_PROPERTY_WHITELIST:
        raise ApplicationLedgerError("Application writer property whitelist rejected")
    if not _validate_text_payload(properties["신청명"], "title"):
        raise ApplicationLedgerError("Application title rejected")
    if not _validate_relation_payload(properties["Cleaning"]):
        raise ApplicationLedgerError("Application Cleaning relation rejected")
    if not _validate_relation_payload(properties["Cleaner"]):
        raise ApplicationLedgerError("Application Cleaner relation rejected")
    if not _validate_fixed_select_payload(properties["Application Status"], "APPLIED"):
        raise ApplicationLedgerError("Application status rejected")
    if not _validate_fixed_select_payload(properties["Source Channel"], "TELEGRAM"):
        raise ApplicationLedgerError("Application source channel rejected")
    if not _validate_fixed_select_payload(properties["Data Environment"], "PRODUCTION"):
        raise ApplicationLedgerError("Application environment rejected")
    if not _validate_text_payload(properties["Source Message Ref"], "rich_text"):
        raise ApplicationLedgerError("Application source reference rejected")
    if not _validate_text_payload(properties["Idempotency Key"], "rich_text"):
        raise ApplicationLedgerError("Application idempotency key rejected")
    applied = properties["Applied At"]
    if not isinstance(applied, dict) or set(applied) != {"date"} or not isinstance(applied["date"], dict):
        raise ApplicationLedgerError("Application timestamp rejected")
    if set(applied["date"]) != {"start"} or not isinstance(applied["date"]["start"], str):
        raise ApplicationLedgerError("Application timestamp rejected")
    try:
        parsed = datetime.fromisoformat(applied["date"]["start"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApplicationLedgerError("Application timestamp rejected") from exc
    if parsed.tzinfo is None:
        raise ApplicationLedgerError("Application timestamp rejected")


class NotionApplicationLedger:
    """Narrow Notion adapter: application-ledger query/create only."""

    def __init__(self, *, token_path: Path = NOTION_TOKEN_PATH, urlopen=urllib.request.urlopen):
        self._token_path = token_path
        self._urlopen = urlopen

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        page_read = method == "GET" and re.fullmatch(r"/v1/pages/[A-Za-z0-9-]+", path) is not None
        application_query = method == "POST" and path == f"/v1/data_sources/{APPLICATION_SOURCE}/query"
        application_create = method == "POST" and path == "/v1/pages"
        if not (page_read or application_query or application_create):
            raise ApplicationLedgerError("Application writer endpoint rejected")
        if page_read and body is not None:
            raise ApplicationLedgerError("Application page read body rejected")
        if application_query:
            if not isinstance(body, dict):
                raise ApplicationLedgerError("Application query body rejected")
            allowed_keys = {"filter", "page_size", "start_cursor"}
            if not set(body).issubset(allowed_keys) or "filter" not in body or "page_size" not in body:
                raise ApplicationLedgerError("Application query shape rejected")
            filter_value = body["filter"]
            expected_prefix = {"property": "Idempotency Key"}
            if not isinstance(filter_value, dict) or filter_value.get("property") != expected_prefix["property"]:
                raise ApplicationLedgerError("Application query property rejected")
            rich = filter_value.get("rich_text")
            if not isinstance(rich, dict) or set(rich) != {"equals"} or not isinstance(rich["equals"], str) or not rich["equals"]:
                raise ApplicationLedgerError("Application query value rejected")
            if body["page_size"] != 100:
                raise ApplicationLedgerError("Application query page size rejected")
            if "start_cursor" in body and (not isinstance(body["start_cursor"], str) or not body["start_cursor"]):
                raise ApplicationLedgerError("Application query cursor rejected")
        if application_create:
            _validate_create_payload(body)

        if application_create:
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
            return json.loads(response.read())

    def query_by_idempotency(self, key: str) -> list[dict]:
        results: list[dict] = []
        cursor = None
        while True:
            body = {
                "filter": {"property": "Idempotency Key", "rich_text": {"equals": key}},
                "page_size": 100,
            }
            if cursor is not None:
                body["start_cursor"] = cursor
            page = self._request("POST", f"/v1/data_sources/{APPLICATION_SOURCE}/query", body)
            if not isinstance(page, dict) or not isinstance(page.get("results"), list):
                raise ApplicationLedgerError("Application query response malformed")
            if not all(isinstance(item, dict) for item in page["results"]):
                raise ApplicationLedgerError("Application query result malformed")
            if not isinstance(page.get("has_more"), bool) or "next_cursor" not in page:
                raise ApplicationLedgerError("Application query pagination malformed")
            results.extend(page["results"])
            if not page["has_more"]:
                if page["next_cursor"] is not None:
                    raise ApplicationLedgerError("Application terminal cursor malformed")
                return results
            cursor = page["next_cursor"]
            if not isinstance(cursor, str) or not cursor:
                raise ApplicationLedgerError("Application query cursor malformed")

    def create_application(
        self,
        *,
        cleaning_page_id: str,
        cleaner_party_page_id: str,
        source_message_ref: str,
        key: str,
        applied_at: datetime,
    ) -> dict:
        if applied_at.tzinfo is None:
            raise ApplicationLedgerError("Application time must be timezone-aware")
        body = {
            "parent": {"type": "data_source_id", "data_source_id": APPLICATION_SOURCE},
            "properties": {
                "신청명": _title_property("청소 작업 신청"),
                "Cleaning": _relation_property(cleaning_page_id),
                "Cleaner": _relation_property(cleaner_party_page_id),
                "Application Status": _select_property("APPLIED"),
                "Applied At": _date_property(applied_at.isoformat()),
                "Source Channel": _select_property("TELEGRAM"),
                "Source Message Ref": _rich_text_property(source_message_ref),
                "Idempotency Key": _rich_text_property(key),
                "Data Environment": _select_property("PRODUCTION"),
            },
        }
        result = self._request("POST", "/v1/pages", body)
        return result if isinstance(result, dict) else {}


def _existing_application_state(
    rows: list[dict],
    *,
    key: str,
    cleaning_page_id: str,
    cleaner_party_page_id: str,
) -> str:
    if len(rows) > 1:
        raise ApplicationIdempotencyConflict("multiple application rows for one idempotency key")
    if not rows:
        return "absent"
    if not _validate_application_row(
        rows[0],
        key=key,
        cleaning_page_id=cleaning_page_id,
        cleaner_party_page_id=cleaner_party_page_id,
    ):
        raise ApplicationIdempotencyConflict("application idempotency row mismatch")
    return "existing"


def apply_for_cleaning(
    *,
    cleaner: dict,
    cleaning_page_id: str,
    source_message_ref: str,
    source,
    ledger,
    now: datetime | None = None,
) -> str:
    cleaner_party_page_id = cleaner.get("party_page_id")
    if not isinstance(cleaner_party_page_id, str) or not cleaner_party_page_id:
        return "unauthorized"
    try:
        key = application_idempotency_key(cleaning_page_id, cleaner_party_page_id)
    except ValueError:
        return "unavailable"
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return "failed"

    with _application_key_lock(key):
        try:
            page = source.get_cleaning(cleaning_page_id)
            if not isinstance(page, dict) or not isinstance(page.get("id"), str):
                return "failed"
            if _normalize_page_id(page["id"]) != _normalize_page_id(cleaning_page_id):
                return "unavailable"
            fields = open_application_fields(
                page,
                cleaner=cleaner,
                property_mappings=load_property_mappings(),
            )
            if fields is None:
                return "unavailable"
            rows = ledger.query_by_idempotency(key)
            if _existing_application_state(
                rows,
                key=key,
                cleaning_page_id=cleaning_page_id,
                cleaner_party_page_id=cleaner_party_page_id,
            ) == "existing":
                return "existing"
        except ApplicationIdempotencyConflict:
            return "failed"
        except Exception:
            return "failed"

        create_failed = False
        try:
            ledger.create_application(
                cleaning_page_id=cleaning_page_id,
                cleaner_party_page_id=cleaner_party_page_id,
                source_message_ref=source_message_ref,
                key=key,
                applied_at=now,
            )
        except Exception:
            create_failed = True

        try:
            rows = ledger.query_by_idempotency(key)
            state = _existing_application_state(
                rows,
                key=key,
                cleaning_page_id=cleaning_page_id,
                cleaner_party_page_id=cleaner_party_page_id,
            )
        except Exception:
            return "failed"
        if state == "existing":
            return "created"
        if create_failed:
            return "failed"
        return "failed"


def _source_message_ref(query: dict) -> str | None:
    callback_id = query.get("id")
    message_id = query.get("message", {}).get("message_id")
    if not isinstance(callback_id, str) or not callback_id or isinstance(message_id, bool) or not isinstance(message_id, int):
        return None
    return f"telegram_callback:{callback_id}:message:{message_id}"


def _send_result(request_api: Callable, token: str, chat_id: int, result: str) -> None:
    message = {
        "created": SUCCESS_MESSAGE,
        "existing": DUPLICATE_MESSAGE,
        "unavailable": UNAVAILABLE_MESSAGE,
        "unauthorized": FAILURE_MESSAGE,
        "failed": FAILURE_MESSAGE,
    }.get(result, FAILURE_MESSAGE)
    request_api(token, "sendMessage", chat_id=chat_id, text=message)


def handle_application_callback(
    update: dict,
    token: str,
    *,
    request_api: Callable | None = None,
    source=None,
    ledger=None,
    roster_loader: Callable[[], dict] | None = None,
    now: datetime | None = None,
):
    query = update.get("callback_query")
    if not isinstance(query, dict):
        return None
    data = query.get("data")
    if not isinstance(data, str) or not data.startswith(APPLICATION_CALLBACK_PREFIX):
        return None
    request_api = request_api or _telegram_api
    callback_id = query.get("id")

    def acknowledge(text: str) -> None:
        if not isinstance(callback_id, str) or not callback_id:
            return
        try:
            request_api(
                token,
                "answerCallbackQuery",
                callback_query_id=callback_id,
                text=text,
            )
        except Exception:
            pass

    raw_page_id = data[len(APPLICATION_CALLBACK_PREFIX):]
    try:
        cleaning_page_id = _normalize_page_id(raw_page_id)
    except ValueError:
        acknowledge("신청할 수 없습니다.")
        return "application_unavailable"

    sender = query.get("from", {})
    message = query.get("message", {})
    chat = message.get("chat", {})
    if (
        not isinstance(sender, dict)
        or not isinstance(chat, dict)
        or chat.get("type") != "private"
        or sender.get("is_bot")
    ):
        acknowledge("신청할 수 없습니다.")
        return "application_unauthorized"

    source_ref = _source_message_ref(query)
    if source_ref is None:
        acknowledge("신청 처리에 실패했습니다.")
        return "application_failed"

    roster_loader = roster_loader or load_roster
    try:
        cleaner = resolve_active_cleaner(
            roster_loader(),
            user_id=sender.get("id"),
            chat_id=chat.get("id"),
        )
    except Exception:
        cleaner = None
    if cleaner is None or not isinstance(cleaner.get("party_page_id"), str) or not cleaner.get("party_page_id"):
        acknowledge("신청할 수 없습니다.")
        _send_result(request_api, token, chat.get("id"), "unauthorized")
        return "application_unauthorized"

    acknowledge("신청 상태를 확인합니다.")
    result = apply_for_cleaning(
        cleaner=cleaner,
        cleaning_page_id=cleaning_page_id,
        source_message_ref=source_ref,
        source=source or NotionCleaningReader(),
        ledger=ledger or NotionApplicationLedger(),
        now=now,
    )
    _send_result(request_api, token, chat["id"], result)
    return f"application_{result}"


def _telegram_api(token: str, method: str, **values):
    from telegram_approval.bot_service import api

    return api(token, method, **values)

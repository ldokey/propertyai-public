"""Concrete destination-pure clients for Cleaner PostgreSQL outbox projections.

These clients deliberately do not import legacy booking orchestration helpers.  Each
client owns exactly one external destination and accepts only state/effect meaning
already sealed by the Product application layer.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from propertyai_core.global_writer import assert_current_production_writer
from propertyai_core.notion_resources import cleaning_source_id, reservation_source_id
from propertyai_core.runtime.cleaner_projection_adapters import ExternalResourceSnapshot
from propertyai_core.stage_b.identity import notion_identity
from telegram_approval.cleaner_bot_service import CleanerTelegramRouter
from telegram_approval.cleaner_config import CleanerCredentialProvider, CleanerRuntimePaths
from telegram_approval.cleaner_registry import load_roster
from telegram_approval.cleaner_reassignment import ACTION_TTL, ACTION_TYPE, decision_keyboard
from telegram_approval.ops_bot_service import OpsTelegramRouter
from telegram_approval.ops_config import OpsAllowlistProvider, OpsCredentialProvider
from telegram_approval.telegram_transport import TelegramHttpTransport


ROOT = Path(__file__).resolve().parents[2]
NOTION_VERSION = "2026-03-11"
NOTION_TOKEN_PATH = ROOT / "secrets" / "notion" / "token"
GOOGLE_TOKEN_PATH = ROOT / "secrets" / "google" / "token.json"
NOTION_TOKEN_PATH_ENV = "PROPERTYAI_CLEANER_NOTION_TOKEN_PATH"
GOOGLE_TOKEN_PATH_ENV = "PROPERTYAI_CLEANER_GOOGLE_TOKEN_PATH"
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
)
CALENDAR_DISPLAY_TARGET = "01_OPS_청소"
ACTION_SECRET_PATH = ROOT / "secrets" / "telegram" / "action-secret"


class ProjectionClientConfigurationError(RuntimeError):
    pass


def _projection_secret_path(
    *,
    explicit: Path | None,
    environment: Mapping[str, str],
    environment_key: str,
) -> Path:
    """Resolve one externally-bound secret without reading or copying its value."""

    raw = explicit if explicit is not None else environment.get(environment_key)
    if raw is None or not str(raw).strip():
        raise ProjectionClientConfigurationError(f"{environment_key}_REQUIRED")
    path = Path(raw)
    if not path.is_absolute():
        raise ProjectionClientConfigurationError(f"{environment_key}_NOT_ABSOLUTE")
    try:
        info = path.lstat()
        parent = path.parent.lstat()
        if path.resolve(strict=True) != path or path.parent.resolve(strict=True) != path.parent:
            raise ProjectionClientConfigurationError(f"{environment_key}_SYMLINK_FORBIDDEN")
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size == 0
        ):
            raise ProjectionClientConfigurationError(f"{environment_key}_METADATA_INVALID")
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise ProjectionClientConfigurationError(f"{environment_key}_DIRECTORY_INVALID")
    except OSError:
        raise ProjectionClientConfigurationError(f"{environment_key}_UNAVAILABLE") from None
    return path


def _text(value: str) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": value}}]}


def _title(value: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": value}}]}


def _select(value: str) -> dict[str, Any]:
    return {"select": {"name": value}}


def _date(value: str | None) -> dict[str, Any]:
    return {"date": None if value is None else {"start": value}}


def _relation(*page_ids: str) -> dict[str, Any]:
    return {"relation": [{"id": page_id} for page_id in page_ids if page_id]}


def _plain(page: Mapping[str, Any], property_name: str) -> str:
    prop = page.get("properties", {}).get(property_name, {})
    values = prop.get(prop.get("type"), [])
    if not isinstance(values, list):
        return ""
    return "".join(str(item.get("plain_text") or "") for item in values)


def _select_value(page: Mapping[str, Any], property_name: str) -> str | None:
    value = page.get("properties", {}).get(property_name, {}).get("select")
    return None if not isinstance(value, Mapping) else value.get("name")


def _date_value(page: Mapping[str, Any], property_name: str) -> str | None:
    value = page.get("properties", {}).get(property_name, {}).get("date")
    return None if not isinstance(value, Mapping) else value.get("start")


def _normalize_iso(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone(timezone.utc).isoformat()


class NotionCurrentStateClient:
    """Notion-only current-state projection using existing Reservation/Cleaning DBs."""

    def __init__(
        self,
        *,
        token_path: Path | None = None,
        environment: Mapping[str, str] | None = None,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        timeout_seconds: int = 30,
    ) -> None:
        self._token_path = token_path
        self._environment = os.environ if environment is None else environment
        self._urlopen = urlopen
        self._timeout_seconds = timeout_seconds

    def _token(self) -> str:
        token_path = _projection_secret_path(
            explicit=self._token_path,
            environment=self._environment,
            environment_key=NOTION_TOKEN_PATH_ENV,
        )
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ProjectionClientConfigurationError("NOTION_TOKEN_UNAVAILABLE") from error
        if not token:
            raise ProjectionClientConfigurationError("NOTION_TOKEN_EMPTY")
        return token

    def _request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            "https://api.notion.com" + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        with self._urlopen(request, timeout=self._timeout_seconds) as response:
            value = json.loads(response.read())
        if not isinstance(value, dict):
            raise RuntimeError("NOTION_RESPONSE_NOT_OBJECT")
        return value

    def _query_rich_text(self, source_id: str, property_name: str, value: str) -> list[dict[str, Any]]:
        payload = {
            "filter": {"property": property_name, "rich_text": {"equals": value}},
            "page_size": 20,
        }
        rows = self._request("POST", f"/v1/data_sources/{source_id}/query", payload).get("results", [])
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def _query_relation(self, source_id: str, property_name: str, page_id: str) -> list[dict[str, Any]]:
        payload = {
            "filter": {"property": property_name, "relation": {"contains": page_id}},
            "page_size": 50,
        }
        rows = self._request("POST", f"/v1/data_sources/{source_id}/query", payload).get("results", [])
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    @staticmethod
    def _dedupe(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            page_id = row.get("id")
            if isinstance(page_id, str) and page_id:
                by_id[page_id] = dict(row)
        return list(by_id.values())

    def _reservation_pages(self, identity: Mapping[str, Any]) -> list[dict[str, Any]]:
        reservation_id = str(identity.get("reservation_id") or "")
        reservation_code = str(identity.get("reservation_code") or "")
        rows: list[dict[str, Any]] = []
        if reservation_id:
            rows.extend(
                self._query_rich_text(
                    reservation_source_id(),
                    "Idempotency Key",
                    f"PG:RESERVATION:{reservation_id}",
                )
            )
        if reservation_code:
            rows.extend(self._query_rich_text(reservation_source_id(), "예약번호", reservation_code))
        return self._dedupe(rows)

    def _cleaning_pages(self, identity: Mapping[str, Any]) -> list[dict[str, Any]]:
        cleaning_id = str(identity.get("cleaning_id") or "")
        rows: list[dict[str, Any]] = []
        if cleaning_id:
            rows.extend(
                self._query_rich_text(
                    cleaning_source_id(),
                    "Idempotency Key",
                    f"PG:CLEANING:{cleaning_id}",
                )
            )
        if rows:
            return self._dedupe(rows)
        reservation_code = str(identity.get("reservation_code") or "")
        expected_start = _normalize_iso(
            str(identity.get("service_window_start_at"))
            if identity.get("service_window_start_at") is not None
            else None
        )
        if not reservation_code or expected_start is None:
            return []
        reservations = self._query_rich_text(reservation_source_id(), "예약번호", reservation_code)
        if len(reservations) != 1:
            return []
        related = self._query_relation(cleaning_source_id(), "관련 Reservation", reservations[0]["id"])
        return [
            page
            for page in related
            if _normalize_iso(_date_value(page, "시작 예정")) == expected_start
        ]

    def find(self, resource_kind: str, identity: Mapping[str, Any]) -> Sequence[ExternalResourceSnapshot]:
        if resource_kind == "RESERVATION":
            pages = self._reservation_pages(identity)
        elif resource_kind == "CLEANING":
            pages = self._cleaning_pages(identity)
        else:
            raise ProjectionClientConfigurationError("NOTION_RESOURCE_KIND_UNSUPPORTED")
        return [self._snapshot(resource_kind, page) for page in pages]

    def read(self, resource_kind: str, external_id: str) -> ExternalResourceSnapshot | None:
        try:
            page = self._request("GET", f"/v1/pages/{external_id}")
        except Exception as error:
            status = getattr(error, "code", None)
            if status == 404:
                return None
            raise
        if page.get("archived") is True or page.get("in_trash") is True:
            return None
        return self._snapshot(resource_kind, page)

    def _snapshot(self, resource_kind: str, page: Mapping[str, Any]) -> ExternalResourceSnapshot:
        page_id = str(page.get("id") or "")
        if not page_id:
            raise RuntimeError("NOTION_PAGE_ID_MISSING")
        if resource_kind == "RESERVATION":
            state = {
                "reservation_id": _plain(page, "Idempotency Key").removeprefix("PG:RESERVATION:"),
                "reservation_code": _plain(page, "예약번호"),
                "reservation_status": "CANCELLED" if _select_value(page, "상태") == "취소" else "CONFIRMED",
                "check_in_at": _normalize_iso(_date_value(page, "체크인")),
                "check_out_at": _normalize_iso(_date_value(page, "체크아웃")),
            }
        elif resource_kind == "CLEANING":
            status_value = _select_value(page, "상태")
            acceptance_value = _select_value(page, "배정 수락 상태")
            if status_value == "담당자 배정":
                cleaning_status = (
                    "OFFERING" if acceptance_value == "수락대기"
                    else "ASSIGNED" if acceptance_value == "수락"
                    else "PLANNED"
                )
            else:
                cleaning_status = {
                    "진행중": "IN_PROGRESS",
                    "완료": "COMPLETED",
                    "취소": "CANCELLED",
                }.get(status_value, "PLANNED")
            state = {
                "cleaning_id": _plain(page, "Idempotency Key").removeprefix("PG:CLEANING:"),
                "cleaning_code": _plain(page, "점검명"),
                "cleaning_status": cleaning_status,
                "service_window_start_at": _normalize_iso(_date_value(page, "시작 예정")),
                "service_deadline_at": _normalize_iso(_date_value(page, "완료 목표")),
            }
        else:
            raise ProjectionClientConfigurationError("NOTION_RESOURCE_KIND_UNSUPPORTED")
        return ExternalResourceSnapshot(
            page_id,
            state,
            external_version=(str(page.get("last_edited_time")) if page.get("last_edited_time") else None),
        )

    @staticmethod
    def _reservation_properties(desired: Mapping[str, Any]) -> dict[str, Any]:
        status = "취소" if desired["reservation_status"] == "CANCELLED" else "확정"
        return {
            "예약명": _title(str(desired["reservation_code"])),
            "예약번호": _text(str(desired["reservation_code"])),
            "상태": _select(status),
            "등록 상태": _select("APPROVED"),
            "데이터 환경": _select("PRODUCTION"),
            "Source Project Code": _text("PROPERTYAI_POSTGRES"),
            "Idempotency Key": _text(f"PG:RESERVATION:{desired['reservation_id']}"),
            "Sync Status": _select("SYNCED"),
            "체크인": _date(desired.get("check_in_at")),
            "체크아웃": _date(desired.get("check_out_at")),
        }

    def _reservation_page_id_for_cleaning(self, reservation_code: str) -> str:
        rows = self._query_rich_text(reservation_source_id(), "예약번호", reservation_code)
        if len(rows) != 1:
            raise RuntimeError("NOTION_CLEANING_RESERVATION_IDENTITY_AMBIGUOUS")
        return str(rows[0]["id"])

    def _cleaning_properties(
        self,
        desired: Mapping[str, Any],
        *,
        reservation_code: str | None,
    ) -> dict[str, Any]:
        status_map = {
            "PLANNED": ("담당자 배정", "미제안"),
            "OFFERING": ("담당자 배정", "수락대기"),
            "ASSIGNED": ("담당자 배정", "수락"),
            "IN_PROGRESS": ("진행중", "수락"),
            "COMPLETED": ("완료", "수락"),
            "CANCELLED": ("취소", "미제안"),
        }
        status, acceptance = status_map.get(str(desired["cleaning_status"]), ("담당자 배정", "미제안"))
        props: dict[str, Any] = {
            "점검명": _title(str(desired["cleaning_code"])),
            "점검 유형": _select("퇴실청소"),
            "시작 예정": _date(desired.get("service_window_start_at")),
            "완료 목표": _date(desired.get("service_deadline_at")),
            "상태": _select(status),
            "배정 수락 상태": _select(acceptance),
            "등록 상태": _select("APPROVED"),
            "Sync Status": _select("SYNCED"),
            "데이터 환경": _select("PRODUCTION"),
            "Calendar Code": _text("CAL.CLEANING"),
            "Source Project Code": _text("PROPERTYAI_POSTGRES"),
            "Idempotency Key": _text(f"PG:CLEANING:{desired['cleaning_id']}"),
        }
        if reservation_code:
            props["관련 Reservation"] = _relation(
                self._reservation_page_id_for_cleaning(reservation_code)
            )
        return props

    def create(
        self,
        resource_kind: str,
        identity: Mapping[str, Any],
        desired: Mapping[str, Any],
    ) -> ExternalResourceSnapshot:
        if resource_kind == "RESERVATION":
            source = reservation_source_id()
            properties = self._reservation_properties(desired)
        elif resource_kind == "CLEANING":
            source = cleaning_source_id()
            properties = self._cleaning_properties(
                desired,
                reservation_code=(str(identity.get("reservation_code")) if identity.get("reservation_code") else None),
            )
        else:
            raise ProjectionClientConfigurationError("NOTION_RESOURCE_KIND_UNSUPPORTED")
        assert_current_production_writer()
        page = self._request(
            "POST",
            "/v1/pages",
            {"parent": {"type": "data_source_id", "data_source_id": source}, "properties": properties},
        )
        assert_current_production_writer()
        return self._snapshot(resource_kind, page)

    def update(
        self,
        resource_kind: str,
        external_id: str,
        desired: Mapping[str, Any],
    ) -> ExternalResourceSnapshot:
        if resource_kind == "RESERVATION":
            properties = self._reservation_properties(desired)
        elif resource_kind == "CLEANING":
            # Relation identity is immutable at this layer; update accepted projection fields only.
            properties = self._cleaning_properties(desired, reservation_code=None)
        else:
            raise ProjectionClientConfigurationError("NOTION_RESOURCE_KIND_UNSUPPORTED")
        assert_current_production_writer()
        page = self._request("PATCH", f"/v1/pages/{external_id}", {"properties": properties})
        assert_current_production_writer()
        return self._snapshot(resource_kind, page)


class GoogleCleaningCalendarClient:
    """Calendar-only projection; provider/iCal calendars are never mutation targets."""

    def __init__(
        self,
        *,
        service: Any | None = None,
        token_path: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._service = service
        self._token_path = token_path
        self._environment = os.environ if environment is None else environment
        self._calendar_id: str | None = None

    def _calendar(self):
        if self._service is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            token_path = _projection_secret_path(
                explicit=self._token_path,
                environment=self._environment,
                environment_key=GOOGLE_TOKEN_PATH_ENV,
            )
            credentials = Credentials.from_authorized_user_file(token_path, GOOGLE_SCOPES)
            self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        return self._service

    def _target_calendar_id(self) -> str:
        if self._calendar_id is not None:
            return self._calendar_id
        items = self._calendar().calendarList().list(maxResults=250, showHidden=True).execute().get("items", [])
        matches = [item for item in items if item.get("summary") == CALENDAR_DISPLAY_TARGET]
        if len(matches) != 1 or not matches[0].get("id"):
            raise ProjectionClientConfigurationError("CAL_CLEANING_EXACT_TARGET_REQUIRED")
        calendar_id = str(matches[0]["id"])
        if "airbnb.com/calendar/ical/" in CALENDAR_DISPLAY_TARGET.casefold():
            raise ProjectionClientConfigurationError("PROVIDER_ICAL_MUTATION_TARGET_FORBIDDEN")
        self._calendar_id = calendar_id
        return calendar_id

    @staticmethod
    def _snapshot(item: Mapping[str, Any]) -> ExternalResourceSnapshot:
        external_id = str(item.get("id") or "")
        if not external_id:
            raise RuntimeError("CALENDAR_EVENT_ID_MISSING")
        private = item.get("extendedProperties", {}).get("private", {})
        if not isinstance(private, Mapping):
            private = {}
        state = {
            "cleaning_id": str(private.get("propertyaiCleaningId") or ""),
            "cleaning_code": str(private.get("propertyaiCleaningCode") or item.get("summary") or ""),
            "cleaning_status": str(private.get("propertyaiCleaningStatus") or ""),
            "service_window_start_at": _normalize_iso(
                str(item.get("start", {}).get("dateTime") or "") or None
            ),
            "service_deadline_at": _normalize_iso(
                str(item.get("end", {}).get("dateTime") or "") or None
            ),
        }
        return ExternalResourceSnapshot(
            external_id,
            state,
            external_uid=(str(item.get("iCalUID")) if item.get("iCalUID") else None),
            external_version=(str(item.get("etag")) if item.get("etag") else None),
        )

    def find(self, identity: Mapping[str, Any]) -> Sequence[ExternalResourceSnapshot]:
        cleaning_id = str(identity.get("cleaning_id") or "")
        if not cleaning_id:
            return []
        rows = self._calendar().events().list(
            calendarId=self._target_calendar_id(),
            privateExtendedProperty=f"propertyaiCleaningId={cleaning_id}",
            singleEvents=True,
            showDeleted=False,
            maxResults=10,
        ).execute().get("items", [])
        return [self._snapshot(item) for item in rows if isinstance(item, Mapping)]

    def read(self, external_id: str) -> ExternalResourceSnapshot | None:
        try:
            item = self._calendar().events().get(
                calendarId=self._target_calendar_id(), eventId=external_id
            ).execute()
        except Exception as error:
            if getattr(error, "status_code", None) == 404 or getattr(getattr(error, "resp", None), "status", None) == 404:
                return None
            raise
        if item.get("status") == "cancelled":
            return None
        return self._snapshot(item)

    @staticmethod
    def _body(identity: Mapping[str, Any], desired: Mapping[str, Any]) -> dict[str, Any]:
        cleaning_id = str(identity.get("cleaning_id") or desired.get("cleaning_id") or "")
        if not cleaning_id:
            raise ProjectionClientConfigurationError("CALENDAR_CLEANING_ID_REQUIRED")
        start = desired.get("service_window_start_at")
        end = desired.get("service_deadline_at")
        if not start or not end:
            raise ProjectionClientConfigurationError("CALENDAR_CLEANING_WINDOW_REQUIRED")
        return {
            "summary": str(desired["cleaning_code"]),
            "description": "PropertyAI PostgreSQL authoritative Cleaning projection",
            "start": {"dateTime": str(start), "timeZone": "Asia/Seoul"},
            "end": {"dateTime": str(end), "timeZone": "Asia/Seoul"},
            "transparency": "opaque",
            "visibility": "private",
            "extendedProperties": {
                "private": {
                    "propertyaiCleaningId": cleaning_id,
                    "propertyaiCleaningCode": str(desired["cleaning_code"]),
                    "propertyaiCleaningStatus": str(desired["cleaning_status"]),
                    "propertyaiResourceCode": "CAL.CLEANING",
                }
            },
        }

    def create(self, identity: Mapping[str, Any], desired: Mapping[str, Any]) -> ExternalResourceSnapshot:
        assert_current_production_writer()
        item = self._calendar().events().insert(
            calendarId=self._target_calendar_id(),
            body=self._body(identity, desired),
            sendUpdates="none",
        ).execute()
        assert_current_production_writer()
        return self._snapshot(item)

    def update(self, external_id: str, desired: Mapping[str, Any]) -> ExternalResourceSnapshot:
        identity = {"cleaning_id": desired.get("cleaning_id")}
        assert_current_production_writer()
        item = self._calendar().events().update(
            calendarId=self._target_calendar_id(),
            eventId=external_id,
            body=self._body(identity, desired),
            sendUpdates="none",
        ).execute()
        assert_current_production_writer()
        return self._snapshot(item)

    def retire(self, external_id: str) -> None:
        assert_current_production_writer()
        self._calendar().events().delete(
            calendarId=self._target_calendar_id(), eventId=external_id, sendUpdates="none"
        ).execute()
        assert_current_production_writer()


class TelegramSealedDeliveryClient:
    """Telegram-only delivery of an already-sealed Cleaner effect."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        transport: TelegramHttpTransport | None = None,
        request_dir: Path | None = None,
        action_secret_path: Path = ACTION_SECRET_PATH,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self._transport = transport or TelegramHttpTransport()
        self._request_dir = request_dir or CleanerRuntimePaths().request_dir
        self._action_secret_path = action_secret_path

    def _party_target(self, party_id: UUID) -> Mapping[str, Any]:
        matches = []
        for entry in load_roster().get("cleaners", []):
            if not isinstance(entry, Mapping) or entry.get("status") != "ACTIVE":
                continue
            page_id = entry.get("party_page_id")
            if not isinstance(page_id, str) or not page_id:
                continue
            try:
                derived = notion_identity("party", page_id)
            except ValueError:
                continue
            if derived == party_id:
                matches.append(entry)
        if len(matches) != 1:
            raise ProjectionClientConfigurationError("TELEGRAM_PARTY_RECIPIENT_EXACT_MATCH_REQUIRED")
        user_id = matches[0].get("telegram_user_id")
        chat_id = matches[0].get("telegram_chat_id")
        if not isinstance(user_id, int) or user_id <= 0 or not isinstance(chat_id, int) or chat_id <= 0:
            raise ProjectionClientConfigurationError("TELEGRAM_PARTY_RECIPIENT_PROVIDER_IDENTITY_INVALID")
        return matches[0]

    def resolve_recipient(self, effect: Mapping[str, Any]) -> str:
        recipient = effect.get("recipient")
        if not isinstance(recipient, Mapping):
            raise ProjectionClientConfigurationError("TELEGRAM_RECIPIENT_MISSING")
        if recipient.get("kind") == "PARTY":
            try:
                party_id = UUID(str(recipient.get("identity") or ""))
            except ValueError as error:
                raise ProjectionClientConfigurationError("TELEGRAM_PARTY_ID_INVALID") from error
            target = self._party_target(party_id)
            return f"telegram:PARTY:user:{target['telegram_user_id']}:chat:{target['telegram_chat_id']}"
        if recipient.get("kind") == "OPS" and recipient.get("identity") == "PROPERTYAI_OPS":
            allowed = sorted(OpsAllowlistProvider(environment=self._environment).allowed_chat_ids())
            if len(allowed) != 1:
                raise ProjectionClientConfigurationError("TELEGRAM_OPS_EXACT_RECIPIENT_REQUIRED")
            return f"telegram:OPS:chat:{allowed[0]}"
        raise ProjectionClientConfigurationError("TELEGRAM_RECIPIENT_NOT_ACCEPTED")

    @staticmethod
    def _chat_id(recipient_identity: str) -> int:
        try:
            return int(recipient_identity.rsplit(":", 1)[1])
        except (ValueError, IndexError) as error:
            raise ProjectionClientConfigurationError("TELEGRAM_RESOLVED_CHAT_ID_INVALID") from error

    def _action_signature(self, action_id: str, operation: str) -> str:
        try:
            key = self._action_secret_path.read_text(encoding="utf-8").strip().encode()
        except OSError as error:
            raise ProjectionClientConfigurationError("TELEGRAM_ACTION_SECRET_UNAVAILABLE") from error
        if not key:
            raise ProjectionClientConfigurationError("TELEGRAM_ACTION_SECRET_EMPTY")
        return hmac.new(key, f"{action_id}:{operation}".encode(), hashlib.sha256).hexdigest()[:16]

    def _atomic_action(self, path: Path, record: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _assignment_offer_action(
        self,
        effect: Mapping[str, Any],
        *,
        recipient_identity: str,
        delivery_identity: str,
    ) -> tuple[str, dict[str, Any]]:
        body = effect.get("body")
        if not isinstance(body, Mapping):
            raise ProjectionClientConfigurationError("TELEGRAM_EFFECT_BODY_MISSING")
        party_id = UUID(str(effect["recipient"]["identity"]))
        target = self._party_target(party_id)
        required = ("campaign_id", "offer_candidate_id", "cleaning_id", "acceptance_cutoff_at")
        if any(not body.get(name) for name in required):
            raise ProjectionClientConfigurationError("TELEGRAM_ASSIGNMENT_EFFECT_INCOMPLETE")
        action_id = "w07-" + delivery_identity[:20]
        path = self._request_dir / f"{action_id}.json"
        record = {
            "schema_version": 1,
            "action_id": action_id,
            "action_type": "CLEANING_ASSIGNMENT",
            "status": "PENDING",
            "consumed": False,
            "authority_route": "POSTGRES",
            "delivery_identity": delivery_identity,
            "candidate_user_id": target["telegram_user_id"],
            "candidate_chat_id": target["telegram_chat_id"],
            "pg_cleaner_party_id": str(party_id),
            "pg_campaign_id": str(body["campaign_id"]),
            "pg_offer_candidate_id": str(body["offer_candidate_id"]),
            "pg_cleaning_id": str(body["cleaning_id"]),
            "acceptance_cutoff_at": str(body["acceptance_cutoff_at"]),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("delivery_identity") != delivery_identity:
                raise ProjectionClientConfigurationError("TELEGRAM_ACTION_IDENTITY_CONFLICT")
            record = existing
        else:
            self._atomic_action(path, record)
        keyboard = {
            "inline_keyboard": [[
                {
                    "text": "✅ 수락",
                    "callback_data": f"a:{action_id}:approve:{self._action_signature(action_id, 'approve')}",
                },
                {
                    "text": "❌ 거절",
                    "callback_data": f"a:{action_id}:reject:{self._action_signature(action_id, 'reject')}",
                },
            ]]
        }
        return action_id, keyboard

    def _reassignment_decision_action(
        self,
        effect: Mapping[str, Any],
        *,
        delivery_identity: str,
    ) -> tuple[str, str]:
        body = effect.get("body")
        if not isinstance(body, Mapping):
            raise ProjectionClientConfigurationError("TELEGRAM_EFFECT_BODY_MISSING")
        request_id = body.get("reassignment_request_id")
        cleaner_party_id = body.get("cleaner_party_id")
        cleaning_id = body.get("cleaning_id")
        if not request_id or not cleaner_party_id or not cleaning_id:
            raise ProjectionClientConfigurationError("TELEGRAM_REASSIGNMENT_EFFECT_INCOMPLETE")
        action_id = "w07-r-" + delivery_identity[:18]
        path = self._request_dir / f"{action_id}.json"
        now = datetime.now(timezone.utc)
        record = {
            "schema_version": 1,
            "action_id": action_id,
            "action_type": ACTION_TYPE,
            "status": "PENDING",
            "consumed": False,
            "decision_committed": False,
            "authority_route": "POSTGRES",
            "delivery_identity": delivery_identity,
            "pg_reassignment_request_id": str(request_id),
            "pg_cleaner_party_id": str(cleaner_party_id),
            "pg_cleaning_id": str(cleaning_id),
            "created_at": now.isoformat(),
            "expires_at": (now + ACTION_TTL).isoformat(),
            "notification_status": "ATTEMPTING",
        }
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("delivery_identity") != delivery_identity:
                raise ProjectionClientConfigurationError("TELEGRAM_ACTION_IDENTITY_CONFLICT")
            record = existing
            if record.get("decision_committed") is True:
                raise ProjectionClientConfigurationError("TELEGRAM_REASSIGNMENT_ALREADY_DECIDED")
            if record.get("notification_status") == "DELIVERED":
                return action_id, decision_keyboard(action_id, secret_path=self._action_secret_path)
            record["notification_status"] = "ATTEMPTING"
        self._atomic_action(path, record)
        return action_id, decision_keyboard(action_id, secret_path=self._action_secret_path)

    @staticmethod
    def _render_text(effect: Mapping[str, Any]) -> str:
        kind = str(effect.get("effect_kind") or "")
        body = effect.get("body") if isinstance(effect.get("body"), Mapping) else {}
        labels = {
            "CLEANING_ASSIGNMENT_OFFER_OPENED": "새 청소 배정 제안이 도착했습니다.",
            "CLEANING_ASSIGNMENT_ACCEPTED": "청소 배정 수락이 확정되었습니다.",
            "CLEANING_ASSIGNMENT_DECLINED": "청소 배정 거절이 반영되었습니다.",
            "CLEANER_UNAVAILABLE_CONFIRMED": "청소 불가 요청이 확정되었습니다.",
            "CLEANER_REASSIGNMENT_REQUESTED": "Cleaner 재배정 결정 요청이 등록되었습니다.",
            "ORIGINAL_CLEANER_REASSIGNED": "원래 Cleaner 재배정이 확정되었습니다.",
            "CLEANER_REPLACEMENT_CONTINUES": "대체 Cleaner 절차 계속이 확정되었습니다.",
            "CLEANING_COMPLETED": "청소 완료가 확정되었습니다.",
        }
        if kind not in labels:
            raise ProjectionClientConfigurationError("TELEGRAM_EFFECT_KIND_UNSUPPORTED")
        factual = []
        for key in ("cleaning_id", "assignment_id", "reassignment_request_id", "acceptance_cutoff_at"):
            if body.get(key):
                factual.append(f"{key}={body[key]}")
        # Deliberately never render door/access fields from generic effect payloads.
        return labels[kind] + (("\n" + "\n".join(factual)) if factual else "")

    def send(
        self,
        effect: Mapping[str, Any],
        *,
        recipient_identity: str,
        delivery_identity: str,
    ) -> str:
        chat_id = self._chat_id(recipient_identity)
        kind = str(effect.get("effect_kind") or "")
        values: dict[str, Any] = {}
        action_path: Path | None = None
        if kind == "CLEANING_ASSIGNMENT_OFFER_OPENED":
            action_id, keyboard = self._assignment_offer_action(
                effect,
                recipient_identity=recipient_identity,
                delivery_identity=delivery_identity,
            )
            values["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
            action_path = self._request_dir / f"{action_id}.json"
        elif kind == "CLEANER_REASSIGNMENT_REQUESTED":
            if not recipient_identity.startswith("telegram:OPS:"):
                raise ProjectionClientConfigurationError("TELEGRAM_REASSIGNMENT_OPS_RECIPIENT_REQUIRED")
            action_id, keyboard = self._reassignment_decision_action(
                effect, delivery_identity=delivery_identity
            )
            values["reply_markup"] = keyboard
            action_path = self._request_dir / f"{action_id}.json"

        transport = self._transport
        if recipient_identity.startswith("telegram:PARTY:"):
            router = CleanerTelegramRouter(
                credential_provider=CleanerCredentialProvider(environment=self._environment),
                transport=transport,
            )
        elif recipient_identity.startswith("telegram:OPS:"):
            router = OpsTelegramRouter(
                credential_provider=OpsCredentialProvider(environment=self._environment),
                allowlist_provider=OpsAllowlistProvider(environment=self._environment),
                transport=transport,
            )
        else:
            raise ProjectionClientConfigurationError("TELEGRAM_RESOLVED_RECIPIENT_KIND_INVALID")

        assert_current_production_writer()
        sent = router.send_message(chat_id, self._render_text(effect), **values)
        assert_current_production_writer()
        if not isinstance(sent, Mapping) or not isinstance(sent.get("message_id"), int):
            raise RuntimeError("TELEGRAM_MESSAGE_ID_MISSING")
        message_id = str(sent["message_id"])
        if action_path is not None:
            record = json.loads(action_path.read_text(encoding="utf-8"))
            delivered_at = datetime.now(timezone.utc).isoformat()
            record["telegram_message_id"] = message_id
            record["delivery_status"] = "DELIVERED"
            record["delivered_at"] = delivered_at
            if record.get("action_type") == ACTION_TYPE:
                record["notification_status"] = "DELIVERED"
                record["notification_delivered_at"] = delivered_at
                record["notification_deliveries"] = {
                    str(chat_id): {
                        "status": "DELIVERED",
                        "attempted_at": delivered_at,
                        "telegram_message_id": int(message_id),
                    }
                }
            self._atomic_action(action_path, record)
        return message_id


__all__ = [
    "CALENDAR_DISPLAY_TARGET",
    "GoogleCleaningCalendarClient",
    "NotionCurrentStateClient",
    "ProjectionClientConfigurationError",
    "TelegramSealedDeliveryClient",
]

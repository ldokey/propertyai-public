#!/usr/bin/env python3
"""Idempotent local bridge from parsed Gmail events to operations systems."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

try:
    from gmail_ingest.airbnb_parser import parse_airbnb_event
    from gmail_ingest.mime_utils import decoded_message_text
    from gmail_ingest.poll_once import header, load_credentials
    from gmail_ingest.runtime_config import load_gmail_ingest_config
except ModuleNotFoundError:
    from airbnb_parser import parse_airbnb_event
    from mime_utils import decoded_message_text
    from poll_once import header, load_credentials
    from runtime_config import load_gmail_ingest_config


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
QUEUE_DIR = ROOT / "gmail_ingest" / "runtime" / "reconcile_queue"
PENDING_CHANGES_DIR = ROOT / "gmail_ingest" / "runtime" / "pending_changes"
MAPPINGS_PATH = ROOT / "gmail_ingest" / "listing_mappings.json"
NOTION_TOKEN_PATH = ROOT / "secrets" / "notion" / "token"
GOOGLE_TOKEN_PATH = ROOT / "secrets" / "google" / "token.json"
NOTION_VERSION = "2026-03-11"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]
PROCESS_MODE_LIVE = "LIVE"
PROCESS_MODE_CORE_REPLAY = "CORE_REPLAY"
PROCESS_MODES = {PROCESS_MODE_LIVE, PROCESS_MODE_CORE_REPLAY}
FIRST_PG_SOURCE_QUEUE_ITEM = "QUEUE_ITEM"
FIRST_PG_SOURCE_FRESH_GMAIL_EVENT = "FRESH_GMAIL_EVENT"


class ExternalEffectUncertain(RuntimeError):
    """An external mutation may have applied without a trustworthy acknowledgement."""


class FirstPostgresCommandSelectionError(RuntimeError):
    """The first PostgreSQL command selection no longer binds to one exact source event."""


@dataclass(frozen=True)
class FirstPostgresCommandSelection:
    """Exact immutable identity for the one future first PostgreSQL Cleaner command."""

    queue_item_name: str | None
    source_message_hash: str
    reservation_code: str
    listing_id: str
    source_evidence_kind: str = FIRST_PG_SOURCE_QUEUE_ITEM

    def __post_init__(self) -> None:
        if self.source_evidence_kind == FIRST_PG_SOURCE_QUEUE_ITEM:
            if (
                not isinstance(self.queue_item_name, str)
                or not self.queue_item_name
                or self.queue_item_name != Path(self.queue_item_name).name
                or self.queue_item_name in {".", ".."}
            ):
                raise FirstPostgresCommandSelectionError("FIRST_PG_QUEUE_ITEM_NAME_INVALID")
        elif self.source_evidence_kind == FIRST_PG_SOURCE_FRESH_GMAIL_EVENT:
            if self.queue_item_name is not None:
                raise FirstPostgresCommandSelectionError(
                    "FIRST_PG_FRESH_SOURCE_QUEUE_ITEM_FORBIDDEN"
                )
        else:
            raise FirstPostgresCommandSelectionError("FIRST_PG_SOURCE_EVIDENCE_KIND_INVALID")
        if re.fullmatch(r"[0-9a-f]{64}", self.source_message_hash) is None:
            raise FirstPostgresCommandSelectionError("FIRST_PG_SOURCE_MESSAGE_HASH_INVALID")
        if not self.reservation_code or self.reservation_code != self.reservation_code.strip():
            raise FirstPostgresCommandSelectionError("FIRST_PG_RESERVATION_CODE_INVALID")
        if not self.listing_id or self.listing_id != self.listing_id.strip():
            raise FirstPostgresCommandSelectionError("FIRST_PG_LISTING_ID_INVALID")

    @classmethod
    def from_fresh_gmail_event(
        cls,
        *,
        source_message_hash: str,
        reservation_code: str,
        listing_id: str,
    ) -> "FirstPostgresCommandSelection":
        """Bind the first command to one exact fresh Gmail source event, without a queue item."""

        return cls(
            queue_item_name=None,
            source_message_hash=source_message_hash,
            reservation_code=reservation_code,
            listing_id=listing_id,
            source_evidence_kind=FIRST_PG_SOURCE_FRESH_GMAIL_EVENT,
        )


sys.path.insert(0, str(ROOT / "outputs" / "cleaning_automation"))
from send_door_code_prompt import send_prompt  # noqa: E402
from propertyai_core.notion_resources import cleaning_source_id, reservation_source_id  # noqa: E402
from telegram_approval.lifecycle_requests import (  # noqa: E402
    send_cancellation_approval,
    send_change_applied,
    send_change_review,
)
from telegram_approval.cleaning_assignment import send_assignment  # noqa: E402
from telegram_approval.cleaner_registry import assignment_targets  # noqa: E402
from propertyai_core.global_writer import (  # noqa: E402
    ProductionWriterError,
    assert_current_production_writer,
    mutation_scope,
)
from propertyai_core.config.cleaner_authority import CleanerAuthorityConfig  # noqa: E402
from propertyai_core.runtime.cleaner_pg_composition import (  # noqa: E402
    build_cleaner_postgres_application,
)


def atomic_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def persist_authoritative_result(path: Path, value: dict) -> None:
    """Persist authority-sensitive local state only under freshly current fencing."""
    assert_current_production_writer()
    atomic_private_json(path, value)


CANCELLED_PRE_INGEST_REASON = (
    "CANCELLED_PRE_INGEST_CONFIRMED_BY_REAL_AIRBNB_CANCELLATION_SOURCE_AND_ICAL"
)


def _require_reconciliation(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _same_reconciliation_audit(record: dict, expected: dict) -> bool:
    return all(record.get(key) == value for key, value in expected.items())


def resolve_provider_ical_evidence(reservation_code: str) -> list[dict]:
    """Read exact booking evidence from Airbnb iCal subscriptions in Google Calendar."""
    _require_reconciliation(bool(reservation_code), "provider iCal reservation code required")

    credentials = Credentials.from_authorized_user_file(GOOGLE_TOKEN_PATH, GOOGLE_SCOPES)
    calendar = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    provider_calendars = [
        item["id"]
        for item in calendar.calendarList().list(maxResults=250, showHidden=True).execute().get("items", [])
        if "airbnb.com/calendar/ical/" in str(item.get("summary") or "").casefold()
    ]
    _require_reconciliation(bool(provider_calendars), "provider Airbnb iCal calendar missing")

    exact_code = re.compile(
        rf"(?<![A-Z0-9]){re.escape(reservation_code)}(?![A-Z0-9])",
        re.IGNORECASE,
    )
    evidence = []
    for calendar_id in provider_calendars:
        items = calendar.events().list(
            calendarId=calendar_id,
            q=reservation_code,
            showDeleted=True,
            singleEvents=True,
            maxResults=50,
        ).execute().get("items", [])
        for item in items:
            searchable = "\n".join(
                str(item.get(field) or "") for field in ("summary", "description", "location")
            )
            match = exact_code.search(searchable)
            if not match:
                continue
            evidence.append({
                "reservation_code": match.group(0),
                "iCalUID": item.get("iCalUID"),
                "status": item.get("status"),
                "start": item.get("start"),
                "end": item.get("end"),
                "event_id": item.get("id"),
            })
    return evidence


def _validated_provider_ical_evidence(
    reservation_code: str,
    provider_ical_uid: str,
    provider_ical_status: str,
    resolver: Callable[[str], list[dict]],
) -> dict:
    resolved = resolver(reservation_code)
    _require_reconciliation(isinstance(resolved, list), "provider iCal resolver must return a list")
    _require_reconciliation(len(resolved) == 1, "provider iCal exact match count must be one")
    evidence = resolved[0]
    _require_reconciliation(isinstance(evidence, dict), "provider iCal evidence must be an object")
    _require_reconciliation(
        evidence.get("reservation_code") == reservation_code,
        "provider iCal reservation code mismatch",
    )
    resolved_uid = str(evidence.get("iCalUID") or "").strip()
    _require_reconciliation(bool(resolved_uid), "resolved provider iCal UID required")
    _require_reconciliation(resolved_uid == provider_ical_uid, "provider iCal UID mismatch")
    resolved_status = str(evidence.get("status") or "").casefold()
    _require_reconciliation(
        resolved_status == provider_ical_status.casefold(),
        "provider iCal status mismatch",
    )
    _require_reconciliation(resolved_status == "cancelled", "resolved provider iCal status must be cancelled")
    return evidence


def reconcile_cancelled_pre_ingest(
    confirmation_path: Path,
    cancellation_path: Path,
    *,
    reservation_code: str,
    expected_confirmation_source_hash: str,
    expected_cancellation_source_hash: str,
    provider_ical_uid: str,
    provider_ical_status: str,
    provider_ical_evidence_resolver: Callable[[str], list[dict]] = resolve_provider_ical_evidence,
    change_id: str,
    canonical_reservation_created: bool,
    reconciled_at: str | None = None,
    apply: bool = False,
) -> dict:
    """Reconcile one exact booking cancelled before canonical Reservation ingest.

    This operation is intentionally outside process_pending(). It validates both exact
    queue records and both fresh Gmail source events before the first write. If two
    writes are needed, the confirmation is made REVIEW_REQUIRED first so a later
    cancellation-record write failure cannot make the booking LIVE eligible again.
    """
    confirmation_path = Path(confirmation_path)
    cancellation_path = Path(cancellation_path)
    _require_reconciliation(confirmation_path != cancellation_path, "queue paths must be distinct")
    _require_reconciliation(confirmation_path.is_file(), "confirmation queue path missing")
    _require_reconciliation(cancellation_path.is_file(), "cancellation queue path missing")
    _require_reconciliation(bool(reservation_code), "reservation code required")
    _require_reconciliation(bool(change_id), "change id required")
    _require_reconciliation(bool(provider_ical_uid), "provider iCal UID required")
    _require_reconciliation(
        provider_ical_status.casefold() == "cancelled",
        "provider iCal status must be cancelled",
    )
    _require_reconciliation(
        canonical_reservation_created is False,
        "canonical reservation must be proven absent before reconciliation",
    )
    if reconciled_at is not None:
        datetime.fromisoformat(reconciled_at.replace("Z", "+00:00"))

    confirmation = json.loads(confirmation_path.read_text())
    cancellation = json.loads(cancellation_path.read_text())

    _require_reconciliation(
        confirmation.get("source_message_hash") == expected_confirmation_source_hash,
        "confirmation source hash mismatch",
    )
    _require_reconciliation(
        cancellation.get("source_message_hash") == expected_cancellation_source_hash,
        "cancellation source hash mismatch",
    )
    _require_reconciliation(confirmation.get("event_type") == "BOOKING_CONFIRMED", "confirmation event type mismatch")
    _require_reconciliation(confirmation.get("external_writes") == 0, "confirmation external writes must be zero")
    _require_reconciliation(cancellation.get("external_writes") == 0, "cancellation external writes must be zero")
    _require_reconciliation(
        cancellation.get("status") == "REVIEW_REQUIRED",
        "cancellation queue status must be REVIEW_REQUIRED",
    )
    _require_reconciliation(
        cancellation.get("event_type") in ("BOOKING_UPDATED", "BOOKING_CANCELLED"),
        "cancellation persisted event type is not reconcilable",
    )
    if confirmation.get("external_booking_ref_hash") and cancellation.get("external_booking_ref_hash"):
        _require_reconciliation(
            confirmation["external_booking_ref_hash"] == cancellation["external_booking_ref_hash"],
            "queue booking identity mismatch",
        )

    confirmation_event, confirmation_code, _ = fresh_source(expected_confirmation_source_hash)
    cancellation_event, cancellation_code, _ = fresh_source(expected_cancellation_source_hash)
    _require_reconciliation(confirmation_event.get("event_type") == "BOOKING_CONFIRMED", "fresh confirmation is not BOOKING_CONFIRMED")
    _require_reconciliation(cancellation_event.get("event_type") == "BOOKING_CANCELLED", "fresh cancellation is not BOOKING_CANCELLED")
    _require_reconciliation(
        confirmation_event.get("source_message_hash") == expected_confirmation_source_hash,
        "fresh confirmation source identity mismatch",
    )
    _require_reconciliation(
        cancellation_event.get("source_message_hash") == expected_cancellation_source_hash,
        "fresh cancellation source identity mismatch",
    )
    _require_reconciliation(confirmation_code == reservation_code, "confirmation reservation code mismatch")
    _require_reconciliation(cancellation_code == reservation_code, "cancellation reservation code mismatch")
    _validated_provider_ical_evidence(
        reservation_code,
        provider_ical_uid,
        provider_ical_status,
        provider_ical_evidence_resolver,
    )

    audit_identity = {
        "reconciliation_reason": "CANCELLED_PRE_INGEST",
        "reconciliation_source_message_hash": expected_cancellation_source_hash,
        "reconciliation_source_event_type": "BOOKING_CANCELLED",
        "reconciliation_provider_ical_uid": provider_ical_uid,
        "reconciliation_provider_ical_status": "cancelled",
        "reconciliation_change_id": change_id,
        "canonical_reservation_created": False,
        "external_writes": 0,
    }
    confirmation_reconciled = (
        confirmation.get("status") == "REVIEW_REQUIRED"
        and confirmation.get("reason") == CANCELLED_PRE_INGEST_REASON
        and _same_reconciliation_audit(confirmation, audit_identity)
    )
    _require_reconciliation(
        confirmation.get("status") == "RETRY_REQUIRED" or confirmation_reconciled,
        "confirmation queue status is not eligible for bounded reconciliation",
    )

    cancellation_audit_identity = {
        **audit_identity,
        "reconciliation_previous_event_type": "BOOKING_UPDATED",
    }
    cancellation_reconciled = (
        cancellation.get("event_type") == "BOOKING_CANCELLED"
        and cancellation.get("status") == "REVIEW_REQUIRED"
        and cancellation.get("reason") == CANCELLED_PRE_INGEST_REASON
        and _same_reconciliation_audit(cancellation, cancellation_audit_identity)
    )

    if confirmation_reconciled and cancellation_reconciled:
        return {
            "outcome": "already_reconciled",
            "apply": apply,
            "reservation_code": reservation_code,
            "confirmation_status": "REVIEW_REQUIRED",
            "cancellation_event_type": "BOOKING_CANCELLED",
            "reconciled_at": confirmation.get("reconciled_at"),
            "external_writes": 0,
        }

    existing_reconciled_at = confirmation.get("reconciled_at") if confirmation_reconciled else None
    if cancellation_reconciled and not existing_reconciled_at:
        existing_reconciled_at = cancellation.get("reconciled_at")
    effective_reconciled_at = existing_reconciled_at or reconciled_at
    if effective_reconciled_at is None and apply:
        effective_reconciled_at = datetime.now(timezone.utc).isoformat()

    confirmation_target = {
        **confirmation,
        "status": "REVIEW_REQUIRED",
        "reason": CANCELLED_PRE_INGEST_REASON,
        "classification": "CANCELLED_PRE_INGEST",
        **audit_identity,
        "reconciled_at": effective_reconciled_at,
    }
    cancellation_target = {
        **cancellation,
        "event_type": "BOOKING_CANCELLED",
        "status": "REVIEW_REQUIRED",
        "reason": CANCELLED_PRE_INGEST_REASON,
        "classification": "CANCELLED_PRE_INGEST_SOURCE_RECONCILED",
        **cancellation_audit_identity,
        "reconciled_at": effective_reconciled_at,
    }

    plan = {
        "outcome": "planned" if not apply else "reconciled",
        "apply": apply,
        "reservation_code": reservation_code,
        "confirmation_status": "REVIEW_REQUIRED",
        "cancellation_event_type": "BOOKING_CANCELLED",
        "reconciled_at": effective_reconciled_at,
        "external_writes": 0,
    }
    if not apply:
        return plan

    # Failure-safe ordering: the first durable mutation removes the confirmation
    # from process_pending eligibility. A later source-record write failure therefore
    # leaves a detectable REVIEW_REQUIRED state instead of reviving LIVE processing.
    with mutation_scope(
        "W01",
        unit_id=f"cancelled-pre-ingest:{reservation_code}",
        operation_class="GMAIL_RECONCILIATION_ITEM",
        target=f"reservation:{reservation_code}",
    ):
        if not confirmation_reconciled:
            atomic_private_json(confirmation_path, confirmation_target)
        if not cancellation_reconciled:
            atomic_private_json(cancellation_path, cancellation_target)
    return plan


def _notion(method: str, path: str, body: dict | None = None) -> dict:
    if method != "GET" and not (method == "POST" and path.endswith("/query")):
        assert_current_production_writer()
    token = NOTION_TOKEN_PATH.read_text().strip()
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
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.loads(response.read())
    except Exception as error:
        if method != "GET" and not (method == "POST" and path.endswith("/query")):
            raise ExternalEffectUncertain(
                f"NOTION_MUTATION_UNCERTAIN:{method}:{path}"
            ) from error
        raise


def _title(value: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": value}}]}


def _text(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": value}}]}


def _select(value: str) -> dict:
    return {"select": {"name": value}}


def _date(value: str) -> dict:
    return {"date": {"start": value}}


def _relation(*page_ids: str) -> dict:
    return {"relation": [{"id": page_id} for page_id in page_ids]}


def fresh_source(source_hash: str) -> tuple[dict, str, str]:
    gmail = build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)
    config = load_gmail_ingest_config()
    pointers = gmail.users().messages().list(userId="me", q=config["query"], maxResults=100).execute().get("messages", [])
    for pointer in pointers:
        if hashlib.sha256(pointer["id"].encode()).hexdigest() != source_hash:
            continue
        item = gmail.users().messages().get(userId="me", id=pointer["id"], format="full").execute()
        body = decoded_message_text(item["payload"])
        received = datetime.fromtimestamp(int(item["internalDate"]) / 1000, timezone.utc).isoformat()
        event = parse_airbnb_event({
            "id": pointer["id"],
            "subject": header(item["payload"], "Subject"),
            "template": header(item["payload"], "X-Template"),
            "email_ts": received,
            "body": body,
        })
        match = re.search(r"Confirmation code\s+([A-Z0-9]{8,14})", body)
        if not match:
            match = re.search(r"/hosting/reservations/details/([A-Z0-9]{8,14})", body)
        if not match:
            raise ValueError("confirmation code missing during fresh source read")
        return event, match.group(1), received
    raise ValueError("fresh Gmail source not found")


def query_exact(data_source_id: str, property_name: str, value: str) -> list[dict]:
    payload = {
        "filter": {"property": property_name, "rich_text": {"equals": value}},
        "page_size": 10,
    }
    return _notion("POST", f"/v1/data_sources/{data_source_id}/query", payload).get("results", [])


def query_related_cleaning(reservation_page_id: str) -> list[dict]:
    payload = {
        "filter": {"property": "관련 Reservation", "relation": {"contains": reservation_page_id}},
        "page_size": 20,
    }
    return _notion("POST", f"/v1/data_sources/{cleaning_source_id()}/query", payload).get("results", [])


def _plain(page: dict, property_name: str) -> str:
    prop = page.get("properties", {}).get(property_name, {})
    values = prop.get(prop.get("type"), [])
    return "".join(item.get("plain_text", "") for item in values) if isinstance(values, list) else ""


def _number(page: dict, property_name: str):
    return page.get("properties", {}).get(property_name, {}).get("number")


def _date_value(page: dict, property_name: str):
    return (page.get("properties", {}).get(property_name, {}).get("date") or {}).get("start")


def append_change_review(reservation_page_id: str, event: dict) -> None:
    page = _notion("GET", f"/v1/pages/{reservation_page_id}")
    current = _plain(page, "Change History")
    line = (
        f"[{event['received_at']}] PLATFORM · Airbnb 예약 변경 확정 감지 · "
        f"세부 변경값 미포함 · source=sha256:{event['source_message_hash']}"
    )
    history = f"{current}\n{line}".strip()[-1900:]
    _notion("PATCH", f"/v1/pages/{reservation_page_id}", {"properties": {
        "등록 상태": _select("REVIEW_REQUIRED"),
        "Sync Status": _select("NEEDS_SYNC"),
        "Status Lock": {"checkbox": True},
        "Last Transition At": _date(event["received_at"]),
        "Last Transition Rule Code": _text("RSV.CHANGE.PLATFORM_REVIEW"),
        "Change History": _text(history),
    }})


def find_pending_change(event: dict) -> list[tuple[Path, dict]]:
    if not event.get("actor_ref_hash") or not event.get("listing_ref_hash"):
        return []
    accepted_at = datetime.fromisoformat(event["received_at"])
    matches = []
    for pending_path in PENDING_CHANGES_DIR.glob("*.json"):
        pending = json.loads(pending_path.read_text())
        if pending.get("status") != "PENDING_CONFIRMATION":
            continue
        if pending.get("actor_ref_hash") != event["actor_ref_hash"]:
            continue
        if pending.get("listing_ref_hash") != event["listing_ref_hash"]:
            continue
        requested_at = datetime.fromisoformat(pending["received_at"])
        if timedelta(0) <= accepted_at - requested_at <= timedelta(days=7):
            matches.append((pending_path, pending))
    return matches


def apply_exact_pending_change(reservation_page_id: str, event: dict,
                               pending_path: Path, pending: dict) -> str | None:
    requested = pending.get("requested_adults")
    original = pending.get("original_adults")
    if requested is None:
        return None
    page = _notion("GET", f"/v1/pages/{reservation_page_id}")
    current = _number(page, "성인 수")
    if original is not None and current is not None and int(current) != int(original):
        return None
    history = _plain(page, "Change History")
    line = (
        f"[{event['received_at']}] PLATFORM · Airbnb 변경 요청+확정 정확 매칭 · "
        f"성인 {original if original is not None else current}→{requested} · "
        f"source=sha256:{event['source_message_hash']}"
    )
    _notion("PATCH", f"/v1/pages/{reservation_page_id}", {"properties": {
        "성인 수": {"number": requested},
        "게스트 수": {"number": requested},
        "게스트 구성 원문 Snapshot": _text(f"{requested} adults"),
        "게스트 구성 해석 상태": _select("EXACT"),
        "등록 상태": _select("APPROVED"),
        "Sync Status": _select("NEEDS_SYNC"),
        "Status Lock": {"checkbox": False},
        "Last Transition At": _date(event["received_at"]),
        "Last Transition Rule Code": _text("RSV.CHANGE.PLATFORM_EXACT"),
        "Change History": _text(f"{history}\n{line}".strip()[-1900:]),
    }})
    pending.update({
        "status": "APPLIED",
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "reservation_page_id": reservation_page_id,
        "accepted_source_message_hash": event["source_message_hash"],
    })
    persist_authoritative_result(pending_path, pending)
    return f"성인 {original if original is not None else current}명 → {requested}명"


def reservation_summary(page: dict) -> dict:
    currency = ((page.get("properties", {}).get("통화", {}).get("select") or {}).get("name") or "")
    amount = _number(page, "표시 금액")
    guests = _number(page, "게스트 수")
    return {
        "nickname": _plain(page, "숙소 닉네임 Snapshot") or "확인 필요",
        "check_in": _date_value(page, "체크인") or "N/A",
        "check_out": _date_value(page, "체크아웃") or "N/A",
        "guests": f"{int(guests)}명" if guests is not None else "N/A",
        "amount": f"{currency} {amount:.2f}" if amount is not None else "N/A",
    }


def process_update_event(path: Path, record: dict, event: dict, reservation_code: str) -> str:
    matches = query_exact(reservation_source_id(), "예약번호", reservation_code)
    if len(matches) != 1:
        record.update({"status": "REVIEW_REQUIRED", "reason": "RESERVATION_EXACT_MATCH_REQUIRED"})
        atomic_private_json(path, record)
        return "review_required"
    reservation_page_id = matches[0]["id"]
    pending_matches = find_pending_change(event)
    if len(pending_matches) == 1:
        change_summary = apply_exact_pending_change(
            reservation_page_id, event, pending_matches[0][0], pending_matches[0][1]
        )
        if change_summary:
            notice = send_change_applied(
                source_message_hash=event["source_message_hash"],
                reservation_code=reservation_code,
                reservation_page_id=reservation_page_id,
                change_summary=change_summary,
            )
            record.update({
                "status": "COMPLETE",
                "classification": "BOOKING_UPDATE_EXACT_APPLIED",
                "notion_reservation_page_id": reservation_page_id,
                "change_summary": change_summary,
                "telegram_notice_message_id": notice.get("telegram_message_id"),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            })
            persist_authoritative_result(path, record)
            return "completed"
    append_change_review(reservation_page_id, event)
    notice = send_change_review(
        source_message_hash=event["source_message_hash"],
        reservation_code=reservation_code,
        reservation_page_id=reservation_page_id,
    )
    record.update({
        "status": "REVIEW_REQUIRED",
        "classification": "BOOKING_UPDATE_EXACT_VALUES_REQUIRED",
        "notion_reservation_page_id": reservation_page_id,
        "telegram_notice_message_id": notice.get("telegram_message_id"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    persist_authoritative_result(path, record)
    return "review_required"


def process_cancellation_event(path: Path, record: dict, event: dict, reservation_code: str,
                               mappings: dict) -> str:
    matches = query_exact(reservation_source_id(), "예약번호", reservation_code)
    if len(matches) != 1:
        record.update({"status": "REVIEW_REQUIRED", "reason": "RESERVATION_EXACT_MATCH_REQUIRED"})
        atomic_private_json(path, record)
        return "review_required"
    reservation = matches[0]
    listing_id = _plain(reservation, "External Listing ID")
    mapping = mappings.get(listing_id, {})
    cleaning = query_related_cleaning(reservation["id"])
    calendar_event_ids = [_plain(page, "Google Event ID") for page in cleaning]
    calendar_event_ids = [value for value in calendar_event_ids if value]
    approval = send_cancellation_approval(
        source_message_hash=event["source_message_hash"],
        reservation_code=reservation_code,
        reservation_page_id=reservation["id"],
        cleaning_page_ids=[page["id"] for page in cleaning],
        cleaning_calendar_id=mapping.get("cleaning_calendar_id"),
        calendar_event_ids=calendar_event_ids,
        summary=reservation_summary(reservation),
        cancellation_received_at=event["received_at"],
        refund_scope=event.get("refund_scope", "UNKNOWN"),
    )
    record.update({
        "status": "AWAITING_CANCELLATION_APPROVAL",
        "classification": "CANCELLATION_PLATFORM_NOTICE_APPROVAL_REQUIRED",
        "notion_reservation_page_id": reservation["id"],
        "approval_action_id": approval.get("action_id"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    persist_authoritative_result(path, record)
    return "awaiting_approval"


def create_reservation(event: dict, reservation_code: str, mapping: dict) -> str:
    nights = event.get("nights") or (
        datetime.fromisoformat(event["check_out"]) - datetime.fromisoformat(event["check_in"])
    ).days
    stable = f"airbnb:{reservation_code}:confirmed:{event['check_in']}:{event['check_out']}"
    properties = {
        "예약명": _title(f"{mapping['nickname']} · {event['check_in']} · {nights}박"),
        "예약번호": _text(reservation_code),
        "플랫폼/채널": _select("Airbnb"),
        "기록 유형": _select("Airbnb예약"),
        "상태": _select("확정"),
        "등록 상태": _select("APPROVED"),
        "데이터 환경": _select("PRODUCTION"),
        "입력 출처": _select("OTHER"),
        "Source Message Ref": _text("sha256:" + event["source_message_hash"]),
        "Source Project Code": _text("LOCAL.PROPERTYAI.GMAIL"),
        "Idempotency Key": _text(hashlib.sha256(stable.encode()).hexdigest()),
        "Sync Status": _select("NEEDS_SYNC"),
        "Calendar Code": _text(mapping["reservation_calendar_code"]),
        "Calendar Source": _relation(mapping["calendar_source_page_id"]),
        "Calendar Sync Status": _select("PENDING_ICAL"),
        "External Listing ID": _text(event["listing_id"]),
        "리스팅명 Snapshot": _text(mapping["listing_title"]),
        "숙소 닉네임 Snapshot": _text(mapping["nickname"]),
        "닉네임 일치 상태": _select("MATCHED"),
        "체크인": _date(event["check_in"]),
        "체크아웃": _date(event["check_out"]),
        "체크인 시간 Snapshot": _text(mapping["check_in_time"]),
        "체크아웃 시간 Snapshot": _text(mapping["check_out_time"]),
        "날짜 해석 상태": _select("YEAR_INFERRED"),
        "성인 수": {"number": event.get("adults")},
        "게스트 수": {"number": event.get("adults")},
        "게스트 구성 원문 Snapshot": _text(f"{event.get('adults')} adult"),
        "게스트 구성 해석 상태": _select("EXACT"),
        "통화": _select(event["currency"]),
        "표시 금액": {"number": float(event["guest_total"])},
        "표시 금액 의미": _select("GUEST_TOTAL"),
        "호스트 수령액": {"number": float(event["host_payout"])},
        "연결 운영상품": _relation(mapping["rental_unit_page_id"]),
        "연결 집": _relation(mapping["property_page_id"]),
        "출입구": _relation(mapping["door_access_point_id"]),
        "출입정보 저장 상태": _select("NOT_STORED"),
        "출입코드 생성 방식": _select("PLATFORM_AUTO"),
        "출입 코드 유효 시작": _date(event["check_in"]),
        "출입 코드 유효 종료": _date(event["check_out"]),
        "도어코드 기기 반영 필요": {"checkbox": True},
        "도어코드 기기 반영 상태": _select("PENDING"),
        "Status Lock": {"checkbox": False},
    }
    result = _notion("POST", "/v1/pages", {"parent": {"type": "data_source_id", "data_source_id": reservation_source_id()}, "properties": properties})
    return result["id"]


def create_cleaning(event: dict, reservation_page_id: str, mapping: dict) -> tuple[str, str]:
    key = f"CLEANING-{mapping['nickname']}-{event['check_out'].replace('-', '')}-{reservation_page_id.replace('-', '')[:16]}"
    existing = query_exact(cleaning_source_id(), "Idempotency Key", key)
    if existing:
        return existing[0]["id"], key
    start = f"{event['check_out']}T{mapping['check_out_time']}:00+09:00"
    end = f"{event['check_out']}T{mapping['cleaning_end_time']}:00+09:00"
    properties = {
        "점검명": _title(f"{mapping['nickname']} · 퇴실청소 · {event['check_out']}"),
        "점검 유형": _select("퇴실청소"),
        "점검일": _date(event["check_out"]),
        "체크아웃 시각": _date(start),
        "시작 예정": _date(start),
        "완료 목표": _date(end),
        "상태": _select("담당자 배정"),
        "배정 수락 상태": _select("수락대기"),
        "등록 상태": _select("TEMPORARY"),
        "Sync Status": _select("NEEDS_SYNC"),
        "데이터 환경": _select("PRODUCTION"),
        "관련 Reservation": _relation(reservation_page_id),
        "연결 운영상품": _relation(mapping["rental_unit_page_id"]),
        "연결 집": _relation(mapping["property_page_id"]),
        "Access Point": _relation(*mapping["access_point_ids"]),
        "다음 예약 확인 상태": _select("NOT_CHECKED"),
        "턴오버 위험": _select("미확인"),
        "도어코드 운영 방식 Snapshot": _select(mapping["door_operation_mode"]),
        "도어코드 작업 상태": _select("PENDING"),
        "청소비 Snapshot": {"number": mapping["cleaning_fee_krw"]},
        "청소 대금 상태": _select("미생성"),
        "청결도": _select("미확인"),
        "체크 결과": _select("미확인"),
        "현장 보고 상태": _select("미제출"),
        "사진 분석 상태": _select("NOT_UPLOADED"),
        "체크리스트 버전": _text(mapping["cleaning_checklist_version"]),
        "Calendar Code": _text("CAL.CLEANING"),
        "Source Message Ref": _text("sha256:" + event["source_message_hash"]),
        "Source Project Code": _text("LOCAL.PROPERTYAI.GMAIL"),
        "Idempotency Key": _text(key),
        "Status Lock": {"checkbox": False},
        "관리자 확인": {"checkbox": False},
        "정산 반영 여부": {"checkbox": False},
        "점검 메모": _text("Airbnb 확정 이메일을 로컬 Python 자동 브리지가 처리해 생성."),
    }
    result = _notion("POST", "/v1/pages", {"parent": {"type": "data_source_id", "data_source_id": cleaning_source_id()}, "properties": properties})
    return result["id"], key


def ensure_calendar_event(event: dict, reservation_page_id: str, mapping: dict, key: str) -> dict:
    credentials = Credentials.from_authorized_user_file(GOOGLE_TOKEN_PATH, GOOGLE_SCOPES)
    calendar = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    start = f"{event['check_out']}T{mapping['check_out_time']}:00+09:00"
    end = f"{event['check_out']}T{mapping['cleaning_end_time']}:00+09:00"
    day_end = f"{event['check_out']}T23:59:59+09:00"
    existing = calendar.events().list(
        calendarId=mapping["cleaning_calendar_id"], timeMin=f"{event['check_out']}T00:00:00+09:00",
        timeMax=day_end, singleEvents=True
    ).execute().get("items", [])
    for item in existing:
        if key in (item.get("description") or ""):
            return item
    body = {
        "summary": f"{mapping['nickname']} · 퇴실청소",
        "description": f"PropertyAI 자동 청소 일정\nIdempotency: {key}\nReservation: https://app.notion.com/p/{reservation_page_id.replace('-', '')}",
        "start": {"dateTime": start, "timeZone": "Asia/Seoul"},
        "end": {"dateTime": end, "timeZone": "Asia/Seoul"},
        "transparency": "opaque",
        "visibility": "private",
    }
    assert_current_production_writer()
    try:
        return calendar.events().insert(
            calendarId=mapping["cleaning_calendar_id"], body=body, sendUpdates="none"
        ).execute()
    except Exception as error:
        raise ExternalEffectUncertain("CALENDAR_INSERT_UNCERTAIN") from error


def finalize_cleaning(cleaning_page_id: str, calendar_event: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    properties = {
        "Google Event ID": _text(calendar_event["id"]),
        "iCal UID": _text(calendar_event.get("iCalUID", calendar_event["id"])),
        "Sync Status": _select("SYNCED"),
        "등록 상태": _select("APPROVED"),
        "Last Synced At": _date(now),
    }
    _notion("PATCH", f"/v1/pages/{cleaning_page_id}", {"properties": properties})


def telegram_request_exists(reservation_page_id: str) -> bool:
    directory = ROOT / "telegram_approval" / "runtime" / "door-code-requests"
    for path in directory.glob("*.json"):
        record = json.loads(path.read_text())
        if record.get("reservation_page_id") == reservation_page_id and record.get("status") not in ("EXPIRED", "CANCELLED_CONFIGURATION_TEST"):
            return True
    return False


def cleaning_assignment_request_exists(cleaning_page_id: str) -> bool:
    directory = ROOT / "telegram_approval" / "runtime" / "requests"
    for path in directory.glob("*.json"):
        request = json.loads(path.read_text())
        if (
            request.get("action_type") == "CLEANING_ASSIGNMENT"
            and request.get("cleaning_page_id") == cleaning_page_id
            and request.get("status") not in ("CANCELLED", "SUPERSEDED")
        ):
            return True
    return False


def _fresh_ambiguity_readback(
    *,
    event: dict | None,
    reservation_code: str | None,
    mapping: dict | None,
    record: dict,
) -> dict:
    """Best-effort fresh external readback after an ambiguous mutation result.

    This function is intentionally read-only. It never retries an external mutation.
    """
    snapshot: dict = {"performed": True}
    if not event or not reservation_code:
        snapshot["reservation"] = "UNAVAILABLE"
        return snapshot
    try:
        reservations = query_exact(reservation_source_id(), "예약번호", reservation_code)
        snapshot["reservation_match_count"] = len(reservations)
        reservation_page_id = reservations[0]["id"] if len(reservations) == 1 else None
        if reservation_page_id is None or not mapping or event.get("event_type") != "BOOKING_CONFIRMED":
            return snapshot
        cleaning_key = (
            f"CLEANING-{mapping['nickname']}-{event['check_out'].replace('-', '')}-"
            f"{reservation_page_id.replace('-', '')[:16]}"
        )
        cleaning = query_exact(cleaning_source_id(), "Idempotency Key", cleaning_key)
        snapshot["cleaning_match_count"] = len(cleaning)
        snapshot["cleaning_key"] = cleaning_key
        # Calendar readback uses the same deterministic key embedded by ensure_calendar_event.
        try:
            credentials = Credentials.from_authorized_user_file(GOOGLE_TOKEN_PATH, GOOGLE_SCOPES)
            calendar = build("calendar", "v3", credentials=credentials, cache_discovery=False)
            day_end = f"{event['check_out']}T23:59:59+09:00"
            existing = calendar.events().list(
                calendarId=mapping["cleaning_calendar_id"],
                timeMin=f"{event['check_out']}T00:00:00+09:00",
                timeMax=day_end,
                singleEvents=True,
            ).execute().get("items", [])
            matches = [item for item in existing if cleaning_key in (item.get("description") or "")]
            snapshot["calendar_match_count"] = len(matches)
        except Exception as error:
            snapshot["calendar_readback_error_type"] = type(error).__name__
    except Exception as error:
        snapshot["readback_error_type"] = type(error).__name__
    return snapshot


def _process_one_under_global_writer(path: Path, mappings: dict, *, mode: str = PROCESS_MODE_LIVE) -> str:
    if mode not in PROCESS_MODES:
        raise ValueError(f"unsupported booking bridge mode: {mode}")
    record = json.loads(path.read_text())
    if record.get("status") not in ("PENDING_NOTION_READ", "RETRY_REQUIRED"):
        return "skipped"
    event: dict | None = None
    reservation_code: str | None = None
    mapping: dict | None = None
    external_mutation_started = False
    try:
        event, reservation_code, _received_at = fresh_source(record["source_message_hash"])
        if mode == PROCESS_MODE_CORE_REPLAY and event["event_type"] != "BOOKING_CONFIRMED":
            return "skipped"
        if event["event_type"] == "BOOKING_UPDATED":
            external_mutation_started = True
            return process_update_event(path, record, event, reservation_code)
        if event["event_type"] == "BOOKING_CANCELLED":
            external_mutation_started = True
            return process_cancellation_event(path, record, event, reservation_code, mappings)
        if event["event_type"] != "BOOKING_CONFIRMED":
            record.update({"status": "REVIEW_REQUIRED", "reason": "UNSUPPORTED_BRIDGE_EVENT"})
            atomic_private_json(path, record)
            return "review_required"
        mapping = mappings.get(event["listing_id"])
        if not mapping:
            record.update({"status": "REVIEW_REQUIRED", "reason": "LISTING_MAPPING_MISSING"})
            atomic_private_json(path, record)
            return "review_required"

        reservation_matches = query_exact(reservation_source_id(), "예약번호", reservation_code)
        if reservation_matches:
            reservation_page_id = reservation_matches[0]["id"]
        else:
            external_mutation_started = True
            reservation_page_id = create_reservation(event, reservation_code, mapping)
        record["notion_reservation_page_id"] = reservation_page_id
        persist_authoritative_result(path, record)

        external_mutation_started = True
        cleaning_page_id, cleaning_key = create_cleaning(event, reservation_page_id, mapping)
        record["notion_cleaning_page_id"] = cleaning_page_id
        persist_authoritative_result(path, record)

        external_mutation_started = True
        calendar_event = ensure_calendar_event(event, reservation_page_id, mapping, cleaning_key)
        record["calendar_event_id"] = calendar_event["id"]
        persist_authoritative_result(path, record)
        external_mutation_started = True
        finalize_cleaning(cleaning_page_id, calendar_event)

        if mode == PROCESS_MODE_LIVE:
            if not cleaning_assignment_request_exists(cleaning_page_id):
                cleaning_page = _notion("GET", f"/v1/pages/{cleaning_page_id}")
                candidates, observers = assignment_targets(mapping["nickname"])
                external_mutation_started = True
                assignment = send_assignment(
                    cleaning_page_id=cleaning_page_id,
                    cleaning_name=f"{mapping['nickname']} · 퇴실청소 · {event['check_out']}",
                    property_nickname=mapping["nickname"],
                    address=mapping["address"],
                    start_at=f"{event['check_out']} {mapping['check_out_time']}",
                    end_at=f"{event['check_out']} {mapping['cleaning_end_time']}",
                    cleaning_fee_krw=mapping["cleaning_fee_krw"],
                    expected_last_edited_time=cleaning_page["last_edited_time"],
                    candidates=candidates or None,
                    default_candidate_party_page_id=mapping["operator_party_page_id"],
                    observers=observers,
                )
                record["cleaning_assignment_action_id"] = assignment.get("action_id")
                persist_authoritative_result(path, record)

            if not telegram_request_exists(reservation_page_id):
                external_mutation_started = True
                prompt = send_prompt(
                    reservation_ref=reservation_code,
                    reservation_page_id=reservation_page_id,
                    access_point_ref=mapping["door_access_point_id"],
                    smart_doorlock=mapping["smart_doorlock"],
                    base_cleaner_message=f"{mapping['nickname']} · {event['check_out']} 퇴실청소 안내",
                    property_nickname=mapping["nickname"],
                    check_in=f"{event['check_in']} {mapping['check_in_time']}",
                    check_out=f"{event['check_out']} {mapping['check_out_time']}",
                    guest_count=f"성인 {event['adults']}명",
                    currency=event["currency"], guest_total=event["guest_total"], host_payout=event["host_payout"],
                    test_mode=False,
                )
                record["door_code_request_id"] = prompt["request_id"]

        record.update({
            "status": "COMPLETE",
            "classification": "DUPLICATE_COMPLETED" if reservation_matches else "NEW_COMPLETED",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "external_writes": 0 if reservation_matches else 2,
        })
        persist_authoritative_result(path, record)
        return "completed"
    except ProductionWriterError:
        raise
    except Exception as exc:
        if external_mutation_started or isinstance(exc, ExternalEffectUncertain):
            readback = _fresh_ambiguity_readback(
                event=event,
                reservation_code=reservation_code,
                mapping=mapping,
                record=record,
            )
            record.update({
                "status": "RECONCILIATION_REQUIRED",
                "reason": "EXTERNAL_EFFECT_UNCERTAIN_NO_BLIND_RETRY",
                "last_error_type": type(exc).__name__,
                "last_attempt_at": datetime.now(timezone.utc).isoformat(),
                "ambiguity_readback": readback,
            })
            persist_authoritative_result(path, record)
            return "reconciliation_required"
        record.update({
            "status": "RETRY_REQUIRED",
            "last_error_type": type(exc).__name__,
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        })
        atomic_private_json(path, record)
        return "retry_required"



def _validate_first_postgres_command_selection(
    path: Path | None,
    mappings: dict,
    selection: FirstPostgresCommandSelection,
) -> tuple[dict, str, dict]:
    """Freshly prove that one frozen selection still names one canonical confirmation."""

    if selection.source_evidence_kind == FIRST_PG_SOURCE_QUEUE_ITEM:
        if path is None or path.name != selection.queue_item_name:
            raise FirstPostgresCommandSelectionError("FIRST_PG_QUEUE_ITEM_NAME_MISMATCH")
        queue_root = QUEUE_DIR.resolve(strict=True)
        try:
            if path.is_symlink():
                raise FirstPostgresCommandSelectionError("FIRST_PG_QUEUE_ITEM_SYMLINK_FORBIDDEN")
            resolved = path.resolve(strict=True)
        except FileNotFoundError as error:
            raise FirstPostgresCommandSelectionError("FIRST_PG_QUEUE_ITEM_MISSING") from error
        if resolved.parent != queue_root or not resolved.is_file():
            raise FirstPostgresCommandSelectionError("FIRST_PG_QUEUE_ITEM_OUTSIDE_QUEUE")
        try:
            record = json.loads(resolved.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise FirstPostgresCommandSelectionError("FIRST_PG_QUEUE_ITEM_INVALID") from error
        if not isinstance(record, dict):
            raise FirstPostgresCommandSelectionError("FIRST_PG_QUEUE_ITEM_INVALID")
        status = record.get("status")
        if status == "COMPLETE":
            raise FirstPostgresCommandSelectionError("FIRST_PG_QUEUE_ITEM_ALREADY_CONSUMED")
        if status not in ("PENDING_NOTION_READ", "RETRY_REQUIRED"):
            raise FirstPostgresCommandSelectionError("FIRST_PG_QUEUE_ITEM_NOT_EXECUTABLE")
        if record.get("source_message_hash") != selection.source_message_hash:
            raise FirstPostgresCommandSelectionError("FIRST_PG_SOURCE_MESSAGE_HASH_MISMATCH")
    elif path is not None:
        raise FirstPostgresCommandSelectionError("FIRST_PG_FRESH_SOURCE_QUEUE_ITEM_FORBIDDEN")

    event, reservation_code, _received_at = fresh_source(selection.source_message_hash)
    if not isinstance(event, dict):
        raise FirstPostgresCommandSelectionError("FIRST_PG_SOURCE_EVENT_INVALID")
    if event.get("event_type") != "BOOKING_CONFIRMED":
        raise FirstPostgresCommandSelectionError("FIRST_PG_EVENT_TYPE_NOT_BOOKING_CONFIRMED")
    if event.get("source_message_hash") != selection.source_message_hash:
        raise FirstPostgresCommandSelectionError("FIRST_PG_FRESH_SOURCE_HASH_MISMATCH")
    if reservation_code != selection.reservation_code:
        raise FirstPostgresCommandSelectionError("FIRST_PG_RESERVATION_CODE_MISMATCH")
    if event.get("listing_id") != selection.listing_id:
        raise FirstPostgresCommandSelectionError("FIRST_PG_LISTING_ID_MISMATCH")
    mapping = mappings.get(selection.listing_id)
    if not isinstance(mapping, dict):
        raise FirstPostgresCommandSelectionError("FIRST_PG_LISTING_MAPPING_MISSING")
    return event, reservation_code, mapping


def _process_one_postgres_under_global_writer(
    path: Path | None,
    mappings: dict,
    *,
    service,
    authority_epoch: int,
    mode: str = PROCESS_MODE_LIVE,
    selection: FirstPostgresCommandSelection | None = None,
) -> str:
    if mode not in PROCESS_MODES:
        raise ValueError(f"unsupported booking bridge mode: {mode}")
    record = None if path is None else json.loads(path.read_text())
    if record is None and selection is None:
        raise FirstPostgresCommandSelectionError("FIRST_PG_SOURCE_SELECTION_REQUIRED")
    if record is not None and record.get("status") not in (
        "PENDING_NOTION_READ",
        "RETRY_REQUIRED",
    ):
        return "skipped"
    selected = (
        _validate_first_postgres_command_selection(path, mappings, selection)
        if selection is not None
        else None
    )
    try:
        if selected is None:
            event, reservation_code, _received_at = fresh_source(record["source_message_hash"])
            mapping = mappings.get(event.get("listing_id"))
        else:
            event, reservation_code, mapping = selected
        if mode == PROCESS_MODE_CORE_REPLAY and event["event_type"] != "BOOKING_CONFIRMED":
            return "skipped"
        if not mapping:
            if record is None:
                raise FirstPostgresCommandSelectionError("FIRST_PG_LISTING_MAPPING_MISSING")
            record.update({
                "status": "REVIEW_REQUIRED",
                "reason": "LISTING_MAPPING_MISSING",
                "authority_route": "POSTGRES",
                "external_writes": 0,
            })
            persist_authoritative_result(path, record)
            return "review_required"
        from gmail_ingest.postgres_ingress import normalize_airbnb_reservation_command

        command = normalize_airbnb_reservation_command(
            event=event,
            reservation_code=reservation_code,
            mapping=mapping,
            authority_epoch=authority_epoch,
            decided_at=datetime.now(timezone.utc),
        )
        result = (
            service.ingest_reservation(command, require_absent=True)
            if selection is not None
            else service.ingest_reservation(command)
        )
        if record is None:
            return "completed"
        record.update({
            "status": "COMPLETE",
            "classification": "POSTGRES_AUTHORITY_APPLIED",
            "authority_route": "POSTGRES",
            "pg_reservation_id": str(result.reservation_id),
            "pg_reservation_source_version": result.source_version,
            "pg_cleaning_id": None if result.cleaning_id is None else str(result.cleaning_id),
            "pg_schedule_revision_id": (
                None if result.schedule_revision_id is None else str(result.schedule_revision_id)
            ),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "external_writes": 0,
            "legacy_fallback_executed": False,
        })
        persist_authoritative_result(path, record)
        return "completed"
    except ProductionWriterError:
        raise
    except Exception as exc:
        if record is None:
            raise
        # POSTGRES authority is fail-closed. Never invoke the legacy Notion/Calendar/
        # Telegram mutation chain as an availability fallback.
        record.update({
            "status": "RETRY_REQUIRED",
            "reason": "POSTGRES_AUTHORITY_FAIL_CLOSED_NO_LEGACY_FALLBACK",
            "authority_route": "POSTGRES",
            "last_error_type": type(exc).__name__,
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
            "external_writes": 0,
            "legacy_fallback_executed": False,
        })
        persist_authoritative_result(path, record)
        return "retry_required"


def process_one_postgres(
    path: Path,
    mappings: dict,
    *,
    service,
    authority_epoch: int,
    mode: str = PROCESS_MODE_LIVE,
) -> str:
    record = json.loads(path.read_text())
    if record.get("status") not in ("PENDING_NOTION_READ", "RETRY_REQUIRED"):
        return "skipped"
    unit_id = record.get("source_message_hash") or path.name
    with mutation_scope(
        "W01",
        unit_id=f"booking-pg:{unit_id}",
        operation_class="GMAIL_INGEST_POSTGRES_BUSINESS_ITEM",
        target=f"postgres-reconcile-queue:{path.name}",
    ):
        return _process_one_postgres_under_global_writer(
            path,
            mappings,
            service=service,
            authority_epoch=authority_epoch,
            mode=mode,
        )

def execute_first_postgres_command(
    selection: FirstPostgresCommandSelection,
    mappings: dict,
    *,
    service,
    authority_epoch: int,
) -> str:
    """Execute exactly one freshly selected canonical Airbnb confirmation under one W01 lease.

    This surface deliberately accepts no queue scan, arbitrary event type, generic command
    payload, or legacy fallback.  Selection is checked once before lease acquisition and
    again inside the lease immediately before the Product transaction path.
    """

    if not isinstance(authority_epoch, int) or isinstance(authority_epoch, bool) or authority_epoch < 0:
        raise FirstPostgresCommandSelectionError("FIRST_PG_AUTHORITY_EPOCH_INVALID")
    path = (
        QUEUE_DIR / selection.queue_item_name
        if selection.source_evidence_kind == FIRST_PG_SOURCE_QUEUE_ITEM
        else None
    )
    _validate_first_postgres_command_selection(path, mappings, selection)
    target = (
        f"postgres-reconcile-queue:{selection.queue_item_name}"
        if path is not None
        else f"fresh-gmail-source:{selection.source_message_hash}"
    )
    with mutation_scope(
        "W01",
        unit_id=f"first-booking-pg:{selection.source_message_hash}",
        operation_class="GMAIL_INGEST_FIRST_POSTGRES_BUSINESS_ITEM",
        target=target,
    ):
        return _process_one_postgres_under_global_writer(
            path,
            mappings,
            service=service,
            authority_epoch=authority_epoch,
            mode=PROCESS_MODE_LIVE,
            selection=selection,
        )


def process_one(path: Path, mappings: dict, *, mode: str = PROCESS_MODE_LIVE) -> str:
    if mode not in PROCESS_MODES:
        raise ValueError(f"unsupported booking bridge mode: {mode}")
    record = json.loads(path.read_text())
    if record.get("status") not in ("PENDING_NOTION_READ", "RETRY_REQUIRED"):
        return "skipped"
    unit_id = record.get("source_message_hash") or path.name
    with mutation_scope(
        "W01",
        unit_id=f"booking:{unit_id}",
        operation_class="GMAIL_INGEST_PROJECTION_ITEM",
        target=f"reconcile-queue:{path.name}",
    ):
        return _process_one_under_global_writer(path, mappings, mode=mode)


def process_pending(
    *,
    mode: str = PROCESS_MODE_LIVE,
    authority_config: CleanerAuthorityConfig | None = None,
    postgres_service=None,
) -> dict:
    mappings = json.loads(MAPPINGS_PATH.read_text())["listings"]
    authority = authority_config or CleanerAuthorityConfig.from_environment()
    counts = {
        "completed": 0,
        "review_required": 0,
        "awaiting_approval": 0,
        "retry_required": 0,
        "reconciliation_required": 0,
        "skipped": 0,
    }

    paths = sorted(QUEUE_DIR.glob("*.json"))
    if authority.uses_legacy:
        for path in paths:
            outcome = process_one(path, mappings, mode=mode)
            counts[outcome] += 1
        return counts

    if not authority.uses_postgres or authority.authority_epoch is None:
        raise RuntimeError("Cleaner authority configuration is not executable")

    if postgres_service is not None:
        for path in paths:
            outcome = process_one_postgres(
                path,
                mappings,
                service=postgres_service,
                authority_epoch=authority.authority_epoch,
                mode=mode,
            )
            counts[outcome] += 1
        return counts

    with build_cleaner_postgres_application(authority) as bundle:
        if bundle.service is None:
            raise RuntimeError("Cleaner PostgreSQL application service did not materialize")
        for path in paths:
            outcome = process_one_postgres(
                path,
                mappings,
                service=bundle.service,
                authority_epoch=authority.authority_epoch,
                mode=mode,
            )
            counts[outcome] += 1
    return counts


if __name__ == "__main__":
    print(json.dumps(process_pending(), ensure_ascii=False))

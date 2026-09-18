#!/usr/bin/env python3
"""Read-only end-to-end audit for one already-ingested reservation.

The secure booking reference and door code are verified only in memory.  The
saved report contains hashes, dates, statuses, and boolean comparisons but no
guest identity, raw email body, booking reference, or door code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from gmail_ingest import booking_bridge
from gmail_ingest.reservation_eligibility import is_canonical_confirmed_reservation


ROOT = Path(__file__).resolve().parents[1]
MAPPINGS_PATH = ROOT / "gmail_ingest" / "listing_mappings.json"
GOOGLE_TOKEN_PATH = ROOT / "secrets" / "google" / "token.json"
REQUEST_DIR = ROOT / "telegram_approval" / "runtime" / "requests"
DOOR_REQUEST_DIR = ROOT / "telegram_approval" / "runtime" / "door-code-requests"
OUTPUT_DIR = ROOT / "outputs" / "live_dry_runs"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]


def _atomic_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _plain(page: dict, name: str) -> str:
    prop = page.get("properties", {}).get(name, {})
    values = prop.get(prop.get("type"), [])
    return "".join(item.get("plain_text", "") for item in values) if isinstance(values, list) else ""


def _select(page: dict, name: str) -> str | None:
    return (page.get("properties", {}).get(name, {}).get("select") or {}).get("name")


def _number(page: dict, name: str):
    return page.get("properties", {}).get(name, {}).get("number")


def _date(page: dict, name: str) -> str | None:
    return (page.get("properties", {}).get(name, {}).get("date") or {}).get("start")


def _relations(page: dict, name: str) -> list[str]:
    return [item["id"] for item in page.get("properties", {}).get(name, {}).get("relation", [])]


def _same_instant(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return datetime.fromisoformat(left.replace("Z", "+00:00")) == datetime.fromisoformat(right.replace("Z", "+00:00"))


def _matching_records(directory: Path, **criteria) -> list[dict]:
    matches = []
    for path in directory.glob("*.json"):
        record = json.loads(path.read_text())
        if all(record.get(key) == value for key, value in criteria.items()):
            matches.append(record)
    return sorted(matches, key=lambda item: item.get("created_at", item.get("captured_at", "")))


def run(queue_path: Path) -> dict:
    started = datetime.now(timezone.utc)
    queue = json.loads(queue_path.read_text())
    if queue.get("status") != "COMPLETE":
        raise ValueError("reservation bridge record is not complete")

    event, secure_booking_reference, _received_at = booking_bridge.fresh_source(queue["source_message_hash"])
    mappings = json.loads(MAPPINGS_PATH.read_text())["listings"]
    mapping = mappings.get(event.get("listing_id"))
    if not mapping:
        raise ValueError("listing mapping is missing")

    reservation_id = queue["notion_reservation_page_id"]
    cleaning_id = queue["notion_cleaning_page_id"]
    reservation = booking_bridge._notion("GET", f"/v1/pages/{reservation_id}")
    cleaning = booking_bridge._notion("GET", f"/v1/pages/{cleaning_id}")

    credentials = Credentials.from_authorized_user_file(GOOGLE_TOKEN_PATH, GOOGLE_SCOPES)
    calendar = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    calendar_event = calendar.events().get(
        calendarId=mapping["cleaning_calendar_id"], eventId=queue["calendar_event_id"]
    ).execute()

    assignments = _matching_records(
        REQUEST_DIR, action_type="CLEANING_ASSIGNMENT", cleaning_page_id=cleaning_id, test_mode=False
    )
    door_requests = _matching_records(DOOR_REQUEST_DIR, reservation_page_id=reservation_id, test_mode=False)
    assignment = assignments[-1] if assignments else {}
    door_request = door_requests[-1] if door_requests else {}

    expected_start = f"{event['check_out']}T{mapping['check_out_time']}:00+09:00"
    expected_end = f"{event['check_out']}T{mapping['cleaning_end_time']}:00+09:00"
    calendar_start = (calendar_event.get("start") or {}).get("dateTime")
    calendar_end = (calendar_event.get("end") or {}).get("dateTime")
    stored_door_code = _plain(reservation, "출입 코드")

    checks = {
        "gmail_source_re_read": True,
        "gmail_event_is_booking_confirmed": event.get("event_type") == "BOOKING_CONFIRMED",
        "secure_booking_reference_matches_notion_in_memory": _plain(reservation, "예약번호") == secure_booking_reference,
        "listing_mapping_matches": _plain(reservation, "External Listing ID") == event.get("listing_id"),
        "reservation_is_production_approved_confirmed": is_canonical_confirmed_reservation(reservation),
        "reservation_dates_match_gmail": (
            _date(reservation, "체크인") == event.get("check_in")
            and _date(reservation, "체크아웃") == event.get("check_out")
        ),
        "reservation_guest_count_matches_gmail": int(_number(reservation, "게스트 수") or -1) == int(event.get("adults") or -2),
        "reservation_amounts_match_gmail": (
            float(_number(reservation, "표시 금액") or -1) == float(event.get("guest_total") or -2)
            and float(_number(reservation, "호스트 수령액") or -1) == float(event.get("host_payout") or -2)
        ),
        "cleaning_links_exact_reservation": _relations(cleaning, "관련 Reservation") == [reservation_id],
        "cleaning_is_production_approved_and_calendar_linked": (
            _select(cleaning, "데이터 환경") == "PRODUCTION"
            and _select(cleaning, "등록 상태") == "APPROVED"
            and _plain(cleaning, "Google Event ID") == queue.get("calendar_event_id")
        ),
        "cleaning_dates_match_checkout": (
            (_date(cleaning, "점검일") or "")[:10] == event.get("check_out")
            and _same_instant(_date(cleaning, "시작 예정"), expected_start)
            and _same_instant(_date(cleaning, "완료 목표"), expected_end)
        ),
        "cleaning_fee_matches_mapping": int(_number(cleaning, "청소비 Snapshot") or -1) == int(mapping["cleaning_fee_krw"]),
        "cleaning_assignment_was_single_use_executed": (
            len(assignments) == 1
            and assignment.get("status") == "EXECUTED"
            and assignment.get("consumed") is True
        ),
        "cleaning_payment_not_created": (
            _select(cleaning, "청소 대금 상태") == "미생성"
            and cleaning.get("properties", {}).get("정산 반영 여부", {}).get("checkbox") is False
        ),
        "calendar_event_matches_cleaning": (
            calendar_event.get("id") == queue.get("calendar_event_id")
            and _same_instant(calendar_start, expected_start)
            and _same_instant(calendar_end, expected_end)
            and calendar_event.get("visibility") == "private"
        ),
        "door_code_was_stored_and_delivered_exactly": (
            _select(reservation, "출입정보 저장 상태") == "STORED"
            and bool(stored_door_code)
            and door_request.get("status") == "DELIVERED"
            and door_request.get("notion_write_status") == "SYNCED"
            and door_request.get("input_preserved_exactly") is True
            and door_request.get("airbnb_phone_last4") == stored_door_code
        ),
        "raw_email_and_guest_identity_not_persisted": (
            event.get("raw_body_stored") is False and event.get("pii_stored") is False
        ),
    }
    passed = all(checks.values())
    report = {
        "schema_version": 1,
        "run_type": "LIVE_JJ_RESERVATION_END_TO_END_DRY_RUN",
        "run_at": started.isoformat(),
        "status": "PASS_NO_WRITE" if passed else "BLOCKED",
        "source": {
            "source_message_hash": queue["source_message_hash"],
            "booking_reference_hash": hashlib.sha256(secure_booking_reference.encode()).hexdigest(),
            "listing_id": event.get("listing_id"),
            "property_nickname": mapping["nickname"],
            "check_in": event.get("check_in"),
            "check_out": event.get("check_out"),
            "guest_count": event.get("adults"),
            "raw_body_stored": False,
            "guest_identity_stored": False,
        },
        "targets": {
            "reservation_page_id": reservation_id,
            "cleaning_page_id": cleaning_id,
            "calendar_event_id": queue["calendar_event_id"],
        },
        "checks": checks,
        "checks_passed": sum(checks.values()),
        "checks_total": len(checks),
        "current_state": {
            "reservation": _select(reservation, "상태"),
            "cleaning": _select(cleaning, "상태"),
            "assignment_acceptance": _select(cleaning, "배정 수락 상태"),
            "payment": _select(cleaning, "청소 대금 상태"),
            "door_code": "VERIFIED_REDACTED",
        },
        "future_schedule": [
            {"at": f"{event['check_out']} 08:30 KST", "action": "DAY_CONFIRM"},
            {"at": f"{event['check_out']} 09:00 KST", "action": "NO_RESPONSE_RECONFIRM_IF_NEEDED"},
            {"at": f"{event['check_out']} 09:20 KST", "action": "OPERATOR_ESCALATION_IF_NEEDED"},
            {"at": f"{event['check_out']} 10:20 KST", "action": "ARRIVAL_AND_REDACTED_DOOR_CODE_IF_ELIGIBLE"},
            {"at": f"{event['check_out']} 15:00 KST", "action": "COMPLETION_PHOTOS_REVIEW_TRANSFER_GATE"},
        ],
        "external_writes": 0,
        "telegram_messages_sent": 0,
        "drive_files_created": 0,
        "notion_pages_changed": 0,
        "calendar_events_changed": 0,
        "write_ready": False,
        "notes": [
            "Python performed deterministic reads and invariant checks.",
            "OpenClaw/local LLM may review this redacted report but cannot override the Python write gate.",
            "This report intentionally excludes guest identity, booking reference, and door code.",
        ],
    }
    target = OUTPUT_DIR / f"jj-{event['check_in']}-{event['check_out']}.json"
    _atomic_private(target, report)
    print(json.dumps({
        "status": report["status"],
        "checks_passed": report["checks_passed"],
        "checks_total": report["checks_total"],
        "external_writes": 0,
        "saved": str(target),
    }, ensure_ascii=False))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("queue_record", type=Path)
    args = parser.parse_args()
    result = run(args.queue_record)
    if result["status"] != "PASS_NO_WRITE":
        raise SystemExit(2)


if __name__ == "__main__":
    main()

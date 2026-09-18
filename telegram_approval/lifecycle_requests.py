#!/usr/bin/env python3
"""Telegram notifications and approval requests for reservation lifecycle events."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram_approval.outbound import ops_outbound_router
from propertyai_core.global_writer import assert_current_production_writer
from telegram_approval.send_approval import (
    OPERATOR_PATH,
    REQUEST_DIR,
    api,
    atomic_private,
    secret,
    signature,
)


TOKEN_PATH = None


RUNTIME = Path(__file__).resolve().parent / "runtime"
NOTICE_DIR = RUNTIME / "lifecycle-notices"


def _operator():
    return json.loads(OPERATOR_PATH.read_text())


def _send_operator(message: str, **values):
    operator = _operator()
    return ops_outbound_router(
        api,
        token_path=TOKEN_PATH,
        synthetic_allowed_chat_ids=(operator["telegram_chat_id"],) if TOKEN_PATH else None,
    ).send_message(operator["telegram_chat_id"], message, **values)


def _already_recorded(source_message_hash: str, action_type: str) -> bool:
    for path in REQUEST_DIR.glob("*.json"):
        record = json.loads(path.read_text())
        if record.get("source_message_hash") == source_message_hash and record.get("action_type") == action_type:
            return True
    return False


def send_change_review(*, source_message_hash: str, reservation_code: str, reservation_page_id: str) -> dict:
    NOTICE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    notice_path = NOTICE_DIR / f"{source_message_hash[:16]}.json"
    if notice_path.exists():
        existing = json.loads(notice_path.read_text())
        if existing.get("status") == "REVIEW_NOTICE_SENT":
            return existing
        raise RuntimeError("LIFECYCLE_NOTICE_RECONCILIATION_REQUIRED")
    now = datetime.now(timezone.utc)
    record = {
        "schema_version": 1,
        "event_type": "BOOKING_UPDATED",
        "source_message_hash": source_message_hash,
        "reservation_page_id": reservation_page_id,
        "telegram_message_id": None,
        "status": "PENDING_DELIVERY",
        "created_at": now.isoformat(),
    }
    assert_current_production_writer()
    atomic_private(notice_path, record)
    message = (
        "⚠️ Airbnb 예약 변경 확정 감지\n\n"
        f"예약번호: {reservation_code}\n"
        f"Notion: https://app.notion.com/p/{reservation_page_id.replace('-', '')}\n\n"
        "Airbnb 변경 확정 이메일에는 바뀐 날짜·인원·금액이 포함되지 않습니다. "
        "원장은 REVIEW_REQUIRED로 잠겼으며 Airbnb 일정 또는 후속 iCal 대조가 필요합니다."
    )
    assert_current_production_writer()
    try:
        sent = _send_operator(message)
    except Exception:
        assert_current_production_writer()
        raise
    assert_current_production_writer()
    record["telegram_message_id"] = sent["message_id"]
    record["status"] = "REVIEW_NOTICE_SENT"
    atomic_private(notice_path, record)
    return record


def send_change_applied(*, source_message_hash: str, reservation_code: str,
                        reservation_page_id: str, change_summary: str) -> dict:
    NOTICE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    notice_path = NOTICE_DIR / f"{source_message_hash[:16]}-applied.json"
    if notice_path.exists():
        existing = json.loads(notice_path.read_text())
        if existing.get("status") == "APPLIED_NOTICE_SENT":
            return existing
        raise RuntimeError("LIFECYCLE_NOTICE_RECONCILIATION_REQUIRED")
    now = datetime.now(timezone.utc)
    record = {
        "schema_version": 1,
        "event_type": "BOOKING_UPDATED",
        "source_message_hash": source_message_hash,
        "reservation_page_id": reservation_page_id,
        "telegram_message_id": None,
        "status": "PENDING_DELIVERY",
        "created_at": now.isoformat(),
    }
    assert_current_production_writer()
    atomic_private(notice_path, record)
    message = (
        "✅ Airbnb 예약 변경 자동 반영 완료\n\n"
        f"예약번호: {reservation_code}\n"
        f"변경: {change_summary}\n"
        f"Notion: https://app.notion.com/p/{reservation_page_id.replace('-', '')}\n\n"
        "변경 요청 메일과 Airbnb 변경 확정 메일이 정확히 결합된 값만 반영했습니다."
    )
    assert_current_production_writer()
    try:
        sent = _send_operator(message)
    except Exception:
        assert_current_production_writer()
        raise
    assert_current_production_writer()
    record["telegram_message_id"] = sent["message_id"]
    record["status"] = "APPLIED_NOTICE_SENT"
    atomic_private(notice_path, record)
    return record


def send_cancellation_approval(*, source_message_hash: str, reservation_code: str,
                               reservation_page_id: str, cleaning_page_ids: list[str],
                               cleaning_calendar_id: str | None,
                               calendar_event_ids: list[str], summary: dict,
                               cancellation_received_at: str,
                               refund_scope: str = "UNKNOWN") -> dict:
    action_type = "CANCEL_RESERVATION_WORKFLOW"
    for existing_path in REQUEST_DIR.glob("*.json"):
        existing = json.loads(existing_path.read_text())
        if existing.get("source_message_hash") == source_message_hash and existing.get("action_type") == action_type:
            return {
                "sent": False,
                "reason": "ALREADY_RECORDED",
                "action_id": existing.get("action_id"),
                "status": existing.get("status"),
            }
    action_id = secrets.token_urlsafe(8)
    key = secret()
    now = datetime.now(timezone.utc)
    operator = _operator()
    record = {
        "schema_version": 2,
        "action_id": action_id,
        "action_type": action_type,
        "source_message_hash": source_message_hash,
        "reservation_code": reservation_code,
        "reservation_page_id": reservation_page_id,
        "cleaning_page_ids": cleaning_page_ids,
        "cleaning_calendar_id": cleaning_calendar_id,
        "calendar_event_ids": calendar_event_ids,
        "cancellation_source": "PLATFORM_NOTICE",
        "cancellation_received_at": cancellation_received_at,
        "refund_scope": refund_scope,
        "summary": summary,
        "cleaner_delivery_chat_ids": [operator["telegram_chat_id"]] if cleaning_page_ids else [],
        "cleaner_target_mode": "OPERATOR_AS_CLEANER_TEST",
        "status": "PENDING",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
        "consumed": False,
        "test_mode": False,
        "external_writes_on_approval": 2 + len(cleaning_page_ids) + len(calendar_event_ids) + (1 if cleaning_page_ids else 0),
    }
    assert_current_production_writer()
    atomic_private(REQUEST_DIR / f"{action_id}.json", record)
    message = (
        "🛑 Airbnb 예약 취소 통지 감지\n\n"
        f"숙소: {summary.get('nickname', '확인 필요')}\n"
        f"예약번호: {reservation_code}\n"
        f"체크인: {summary.get('check_in', 'N/A')}\n"
        f"체크아웃: {summary.get('check_out', 'N/A')}\n"
        f"인원: {summary.get('guests', 'N/A')}\n"
        f"게스트 결제: {summary.get('amount', 'N/A')}\n\n"
        f"승인 시: Reservation 취소 + 연결 Cleaning {len(cleaning_page_ids)}건 취소 + "
        f"청소 Calendar {len(calendar_event_ids)}건 [취소] 표시 + 청소 담당자 취소 안내 + Finance 조정 확인필요 생성\n"
        "삭제하지 않고 이력을 보존합니다. 게스트 환불액과 호스트 정산액은 추정하지 않습니다.\n"
        "유효시간: 24시간"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ 취소 반영", "callback_data": f"a:{action_id}:approve:{signature(key, action_id, 'approve')}"},
        {"text": "❌ 보류", "callback_data": f"a:{action_id}:reject:{signature(key, action_id, 'reject')}"},
    ]]}
    assert_current_production_writer()
    try:
        sent = _send_operator(message, reply_markup=json.dumps(keyboard, ensure_ascii=False))
    except Exception:
        assert_current_production_writer()
        raise
    assert_current_production_writer()
    record["telegram_message_id"] = sent["message_id"]
    atomic_private(REQUEST_DIR / f"{action_id}.json", record)
    return {"sent": True, "action_id": action_id}

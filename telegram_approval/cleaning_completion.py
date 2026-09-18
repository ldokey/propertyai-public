#!/usr/bin/env python3
"""Cleaner completion and operator transfer-confirmation messages."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from telegram_approval.cleaner_config import CleanerRuntimePaths
from telegram_approval.cleaner_performance import canonical_offer_economics, payout_amount_from_record
from propertyai_core.global_writer import assert_current_production_writer
from telegram_approval.ops_config import OpsRuntimePaths
from telegram_approval.outbound import cleaner_outbound_router, ops_outbound_router
from telegram_approval.send_approval import (
    OPERATOR_PATH,
    api,
    atomic_private,
    secret,
    signature,
)


# Synthetic tests may override these two compatibility seams with one fake
# path. Runtime resolution uses the two frozen role-specific config names.
TOKEN_PATH = None
REQUEST_DIR = None


def _cleaner_request_dir():
    return REQUEST_DIR or CleanerRuntimePaths().request_dir


def _ops_request_dir():
    return REQUEST_DIR or OpsRuntimePaths().request_dir


_ECONOMIC_KEYS = (
    "replacement_urgency",
    "urgent_premium_krw",
    "total_agreed_fee_krw",
    "urgent_premium_policy_version",
    "assignment_page_id",
    "assignment_version",
    "accepted_assignment_action_id",
)


def _copy_economics(source: dict, target: dict) -> None:
    for key in _ECONOMIC_KEYS:
        if key in source:
            target[key] = source.get(key)


def _compensation_text(record: dict, *, transfer: bool = False) -> str:
    economics = canonical_offer_economics(record)
    if economics is None:
        amount = payout_amount_from_record(record)
        return f"이체 금액: ₩{amount:,}\n" if transfer else f"청소비: ₩{amount:,}\n"
    if economics.replacement_urgency == "URGENT":
        prefix = "이체 예정" if transfer else "정산 예정"
        return (
            f"기본 청소비: ₩{economics.base_fee_krw:,}\n"
            f"긴급 대체 추가금: ₩{economics.urgent_premium_krw:,}\n"
            f"{prefix} 총액: ₩{economics.total_agreed_fee_krw:,}\n"
        )
    return (
        f"이체 금액: ₩{economics.total_agreed_fee_krw:,}\n"
        if transfer else f"청소비: ₩{economics.base_fee_krw:,}\n"
    )


def _keyboard(action_id: str, approve_label: str, reject_label: str) -> str:
    key = secret()
    return json.dumps({"inline_keyboard": [[
        {"text": approve_label, "callback_data": f"a:{action_id}:approve:{signature(key, action_id, 'approve')}"},
        {"text": reject_label, "callback_data": f"a:{action_id}:reject:{signature(key, action_id, 'reject')}"},
    ]]}, ensure_ascii=False)


def send_completion_request(*, cleaning_page_id: str, property_nickname: str,
                            address: str, cleaning_date: str, cleaning_fee_krw: int,
                            candidate: dict, test_mode: bool = True,
                            expected_last_edited_time: str | None = None,
                            replacement_urgency: str = "NORMAL",
                            urgent_premium_krw: int = 0,
                            total_agreed_fee_krw: int | None = None,
                            urgent_premium_policy_version: str | None = None,
                            assignment_page_id: str | None = None,
                            assignment_version: str | None = None,
                            accepted_assignment_action_id: str | None = None) -> dict:
    if total_agreed_fee_krw is None and replacement_urgency == "NORMAL":
        total_agreed_fee_krw = cleaning_fee_krw
    economics = canonical_offer_economics({
        "cleaning_fee_krw": cleaning_fee_krw,
        "replacement_urgency": replacement_urgency,
        "urgent_premium_krw": urgent_premium_krw,
        "total_agreed_fee_krw": total_agreed_fee_krw,
        "urgent_premium_policy_version": urgent_premium_policy_version,
    })
    if economics is None:
        raise ValueError("completion economics could not be resolved")
    if not test_mode and not expected_last_edited_time:
        raise ValueError("production completion request requires the current Notion last_edited_time")
    now = datetime.now(timezone.utc)
    request_dir = _cleaner_request_dir()
    for path in request_dir.glob("*.json"):
        existing = json.loads(path.read_text())
        if (
            existing.get("action_type") == "CLEANING_COMPLETION"
            and existing.get("cleaning_page_id") == cleaning_page_id
            and existing.get("status") in {"PENDING", "DELIVERY_UNCERTAIN"}
            and not existing.get("consumed")
            and now <= datetime.fromisoformat(existing["expires_at"])
        ):
            return {"sent": False, "reason": "PENDING_REQUEST_EXISTS", "action_id": existing["action_id"]}
    action_id = secrets.token_urlsafe(8)
    record = {
        "schema_version": 1,
        "action_id": action_id,
        "action_type": "CLEANING_COMPLETION",
        "cleaning_page_id": cleaning_page_id,
        "property_nickname": property_nickname,
        "address": address,
        "cleaning_date": cleaning_date,
        "cleaning_fee_krw": economics.base_fee_krw,
        "replacement_urgency": economics.replacement_urgency,
        "urgent_premium_krw": economics.urgent_premium_krw,
        "total_agreed_fee_krw": economics.total_agreed_fee_krw,
        "urgent_premium_policy_version": economics.urgent_premium_policy_version,
        "assignment_page_id": assignment_page_id,
        "assignment_version": assignment_version,
        "accepted_assignment_action_id": accepted_assignment_action_id,
        "candidate_user_id": candidate["telegram_user_id"],
        "candidate_chat_id": candidate["telegram_chat_id"],
        "candidate_party_page_id": candidate.get("party_page_id"),
        "candidate_label": candidate.get("label", "청소 담당자"),
        "expected_last_edited_time": expected_last_edited_time,
        "status": "PENDING",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
        "consumed": False,
        "test_mode": test_mode,
        "execute_on_reject": False,
        "external_writes_on_approval": 0,
        "external_writes_on_reject": 0,
    }
    path = request_dir / f"{action_id}.json"
    atomic_private(path, record)
    text = (
        ("🧪 [TEST] 청소 완료 보고\n\n" if test_mode else "🧹 청소 완료 보고\n\n")
        + f"숙소: {property_nickname}\n"
        f"주소: {address}\n"
        f"청소일: {cleaning_date}\n"
        + _compensation_text(record)
        + "\n청소와 기본 점검을 마쳤으면 완료를 눌러 사진 제출을 시작해주세요.\n"
        "완료 사진과 운영자 검수 전에는 지급 확인으로 넘어가지 않습니다."
        + ("\n\nTEST: Notion·Calendar·정산은 변경되지 않습니다." if test_mode else "")
    )
    if not test_mode:
        assert_current_production_writer()
    try:
        sent = cleaner_outbound_router(api, token_path=TOKEN_PATH).send_message(
            candidate["telegram_chat_id"],
            text,
            reply_markup=_keyboard(action_id, "✅ 완료·특이사항 없음", "⏳ 아직 청소 중"),
        )
    except Exception as error:
        if not test_mode:
            assert_current_production_writer()
            record.update({"status": "DELIVERY_UNCERTAIN", "delivery_error_type": type(error).__name__})
            atomic_private(path, record)
        raise
    if not test_mode:
        assert_current_production_writer()
    record["telegram_message_id"] = sent["message_id"]
    atomic_private(path, record)
    return {"sent": True, "action_id": action_id, "telegram_message_id": sent["message_id"]}


def send_operator_review(completion_record: dict) -> dict:
    request_dir = _ops_request_dir()
    now = datetime.now(timezone.utc)
    for existing_path in request_dir.glob("*.json"):
        existing = json.loads(existing_path.read_text())
        if (
            existing.get("action_type") == "CLEANING_EVIDENCE_REVIEW"
            and existing.get("parent_action_id") == completion_record["action_id"]
            and existing.get("status") in {"PENDING", "DELIVERY_UNCERTAIN"}
            and not existing.get("consumed")
            and now <= datetime.fromisoformat(existing["expires_at"])
        ):
            return {"sent": False, "reason": "PENDING_REQUEST_EXISTS", "action_id": existing["action_id"]}
    operator = json.loads(OPERATOR_PATH.read_text())
    action_id = secrets.token_urlsafe(8)
    test_mode = completion_record.get("test_mode", True)
    execution = completion_record.get("execution_result", {})
    folder_url = execution.get("completion_folder_url")
    record = {
        "schema_version": 1, "action_id": action_id,
        "action_type": "CLEANING_EVIDENCE_REVIEW",
        "parent_action_id": completion_record["action_id"],
        "cleaning_page_id": completion_record["cleaning_page_id"],
        "property_nickname": completion_record["property_nickname"],
        "cleaning_date": completion_record["cleaning_date"],
        "cleaning_fee_krw": completion_record["cleaning_fee_krw"],
        "candidate_party_page_id": completion_record.get("candidate_party_page_id"),
        "candidate_label": completion_record.get("candidate_label", "청소 담당자"),
        "completion_folder_url": folder_url,
        "photo_count": execution.get("photo_count", 0),
        "expected_last_edited_time": execution.get("next_expected_last_edited_time"),
        "status": "PENDING", "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
        "consumed": False, "test_mode": test_mode,
        "execute_on_reject": not test_mode,
        "external_writes_on_approval": 0 if test_mode else 1,
        "external_writes_on_reject": 0 if test_mode else 1,
    }
    _copy_economics(completion_record, record)
    canonical_offer_economics(record)
    path = request_dir / f"{action_id}.json"
    atomic_private(path, record)
    text = (
        ("🧪 [TEST] 청소 완료 사진 운영자 검수\n\n" if test_mode else "🔎 청소 완료 사진 운영자 검수\n\n")
        + f"숙소: {record['property_nickname']}\n"
        f"청소일: {record['cleaning_date']}\n"
        f"담당자: {record['candidate_label']}\n"
        f"사진: {record['photo_count']}장\n"
        + (f"사진 폴더: {folder_url}\n" if folder_url else "")
        + "\n사진을 확인한 뒤 승인하거나 보완을 요청해주세요.\n"
        "검수 승인 후에만 이체 확인 버튼이 생성됩니다."
        + ("\n\nTEST: Notion·정산은 변경되지 않습니다." if test_mode else "")
    )
    if not test_mode:
        assert_current_production_writer()
    try:
        sent = ops_outbound_router(
            api,
            token_path=TOKEN_PATH,
            synthetic_allowed_chat_ids=(operator["telegram_chat_id"],) if TOKEN_PATH else None,
        ).send_message(
            operator["telegram_chat_id"],
            text,
            reply_markup=_keyboard(action_id, "✅ 검수 완료·이체 확인", "🔁 보완 요청"),
        )
    except Exception as error:
        if not test_mode:
            assert_current_production_writer()
            record.update({"status": "DELIVERY_UNCERTAIN", "delivery_error_type": type(error).__name__})
            atomic_private(path, record)
        raise
    if not test_mode:
        assert_current_production_writer()
    record["telegram_message_id"] = sent["message_id"]
    atomic_private(path, record)
    return {"sent": True, "action_id": action_id, "telegram_message_id": sent["message_id"]}


def send_transfer_confirmation(completion_record: dict) -> dict:
    request_dir = _ops_request_dir()
    now = datetime.now(timezone.utc)
    for existing_path in request_dir.glob("*.json"):
        existing = json.loads(existing_path.read_text())
        if (
            existing.get("action_type") == "CLEANING_PAYMENT_CONFIRMATION"
            and existing.get("parent_action_id") == completion_record["action_id"]
            and existing.get("status") in {"PENDING", "DELIVERY_UNCERTAIN"}
            and not existing.get("consumed")
            and now <= datetime.fromisoformat(existing["expires_at"])
        ):
            return {"sent": False, "reason": "PENDING_REQUEST_EXISTS", "action_id": existing["action_id"]}
    operator = json.loads(OPERATOR_PATH.read_text())
    action_id = secrets.token_urlsafe(8)
    test_mode = completion_record.get("test_mode", True)
    record = {
        "schema_version": 1,
        "action_id": action_id,
        "action_type": "CLEANING_PAYMENT_CONFIRMATION",
        "parent_action_id": completion_record["action_id"],
        "cleaning_page_id": completion_record["cleaning_page_id"],
        "property_nickname": completion_record["property_nickname"],
        "cleaning_date": completion_record["cleaning_date"],
        "cleaning_fee_krw": completion_record["cleaning_fee_krw"],
        "candidate_party_page_id": completion_record.get("candidate_party_page_id"),
        "candidate_label": completion_record.get("candidate_label", "청소 담당자"),
        "expected_last_edited_time": completion_record.get("execution_result", {}).get("next_expected_last_edited_time"),
        "status": "PENDING",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
        "consumed": False,
        "test_mode": test_mode,
        "execute_on_reject": False,
        "external_writes_on_approval": 0 if test_mode else 4,
        "external_writes_on_reject": 0,
    }
    _copy_economics(completion_record, record)
    canonical_offer_economics(record)
    path = request_dir / f"{action_id}.json"
    atomic_private(path, record)
    text = (
        ("🧪 [TEST] 청소비 이체 확인\n\n" if test_mode else "💸 청소비 이체 확인\n\n")
        + f"숙소: {record['property_nickname']}\n"
        f"청소일: {record['cleaning_date']}\n"
        f"담당자: {record['candidate_label']}\n"
        + _compensation_text(record, transfer=True)
        + "\n실제로 이체한 뒤에만 `이체 완료`를 눌러주세요.\n"
        "버튼 자체가 은행 이체를 실행하지는 않습니다."
        + ("\n\nTEST: 버튼을 눌러도 정산 DB는 변경되지 않습니다." if test_mode else "")
    )
    if not test_mode:
        assert_current_production_writer()
    try:
        sent = ops_outbound_router(
            api,
            token_path=TOKEN_PATH,
            synthetic_allowed_chat_ids=(operator["telegram_chat_id"],) if TOKEN_PATH else None,
        ).send_message(
            operator["telegram_chat_id"],
            text,
            reply_markup=_keyboard(action_id, "💸 이체 완료(TEST)" if test_mode else "💸 이체 완료", "⏸ 보류"),
        )
    except Exception as error:
        if not test_mode:
            assert_current_production_writer()
            record.update({"status": "DELIVERY_UNCERTAIN", "delivery_error_type": type(error).__name__})
            atomic_private(path, record)
        raise
    if not test_mode:
        assert_current_production_writer()
    record["telegram_message_id"] = sent["message_id"]
    atomic_private(path, record)
    return {"sent": True, "action_id": action_id, "telegram_message_id": sent["message_id"]}

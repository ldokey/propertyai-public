#!/usr/bin/env python3
"""Signed TEST messages for cleaner day-of arrival and start flow."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from telegram_approval.cleaner_config import CleanerRuntimePaths
from propertyai_core.global_writer import assert_current_production_writer
from telegram_approval.outbound import cleaner_outbound_router
from telegram_approval.send_approval import (
    api,
    atomic_private,
    secret,
    signature,
)


TOKEN_PATH = None
REQUEST_DIR = CleanerRuntimePaths().request_dir


STAGES = {
    "DAY_CONFIRM": {
        "action_type": "CLEANING_DAY_CONFIRM",
        "heading": "청소 당일 방문 최종 확인",
        "body": "오늘 방문 가능 여부를 확인해주세요.",
        "approve": "✅ 오늘 갑니다",
        "reject": "❌ 방문이 어렵습니다",
    },
    "ARRIVAL": {
        "action_type": "CLEANING_ARRIVAL",
        "heading": "숙소 도착 확인",
        "body": "숙소에 도착하면 출입 가능 여부를 알려주세요.",
        "approve": "📍 도착했어요",
        "reject": "⚠️ 문제가 있어요",
    },
    "START": {
        "action_type": "CLEANING_START",
        "heading": "현장 문제 확인·청소 시작",
        "body": (
            "게스트 미퇴실, 출입 실패, 누수·정전, 큰 파손, 심한 오염 등 "
            "당일 입실에 영향을 줄 문제가 있는지 먼저 확인해주세요."
        ),
        "approve": "✅ 문제 없음·청소 시작",
        "reject": "⚠️ 문제 있음·바로 보고",
    },
}


def _keyboard(action_id: str, approve_label: str, reject_label: str) -> str:
    key = secret()
    return json.dumps({"inline_keyboard": [[
        {"text": approve_label, "callback_data": f"a:{action_id}:approve:{signature(key, action_id, 'approve')}"},
        {"text": reject_label, "callback_data": f"a:{action_id}:reject:{signature(key, action_id, 'reject')}"},
    ]]}, ensure_ascii=False)


def send_operation_stage(*, stage: str, cleaning_page_id: str,
                         property_nickname: str, address: str,
                         start_at: str, end_at: str, candidate: dict,
                         test_mode: bool = True,
                         expected_last_edited_time: str | None = None,
                         parent_action_id: str | None = None,
                         door_code: str | None = None) -> dict:
    if stage not in STAGES:
        raise ValueError("unsupported cleaning operation stage")
    if not test_mode and not expected_last_edited_time:
        raise ValueError("production operation requires the current Notion last_edited_time")
    if stage == "ARRIVAL" and not test_mode and door_code is None:
        raise ValueError("production arrival message requires the stored reservation door code")
    config = STAGES[stage]
    now = datetime.now(timezone.utc)
    if not test_mode:
        for existing_path in REQUEST_DIR.glob("*.json"):
            existing = json.loads(existing_path.read_text())
            if (
                existing.get("action_type") == config["action_type"]
                and existing.get("cleaning_page_id") == cleaning_page_id
                and existing.get("parent_action_id") == parent_action_id
                and existing.get("status") in {"PENDING", "DELIVERY_UNCERTAIN"}
                and not existing.get("consumed")
                and now <= datetime.fromisoformat(existing["expires_at"])
            ):
                return {"sent": False, "reason": "PENDING_REQUEST_EXISTS", "action_id": existing["action_id"]}
    action_id = secrets.token_urlsafe(8)
    record = {
        "schema_version": 1,
        "action_id": action_id,
        "action_type": config["action_type"],
        "operation_stage": stage,
        "parent_action_id": parent_action_id,
        "cleaning_page_id": cleaning_page_id,
        "property_nickname": property_nickname,
        "address": address,
        "start_at": start_at,
        "end_at": end_at,
        "candidate_user_id": candidate["telegram_user_id"],
        "candidate_chat_id": candidate["telegram_chat_id"],
        "candidate_party_page_id": candidate.get("party_page_id"),
        "candidate_label": candidate.get("label", "청소 담당자"),
        "expected_last_edited_time": expected_last_edited_time,
        "door_code_included": door_code is not None,
        "status": "PENDING",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
        "consumed": False,
        "test_mode": test_mode,
        "execute_on_reject": not test_mode,
        "external_writes_on_approval": 0 if test_mode else 1,
        "external_writes_on_reject": 0 if test_mode else 1,
    }
    path = REQUEST_DIR / f"{action_id}.json"
    atomic_private(path, record)
    prefix = "🧪 [TEST] " if test_mode else "🧹 "
    code_line = f"\n도어락 번호: {door_code}" if door_code is not None else ""
    text = (
        f"{prefix}{config['heading']}\n\n"
        f"숙소: {property_nickname}\n"
        f"주소: {address}\n"
        f"청소 시작: {start_at}\n"
        f"완료 목표: {end_at}\n\n"
        f"{config['body']}"
        f"{code_line}"
        + ("\n\nTEST: Notion·Calendar·정산은 변경되지 않습니다." if test_mode else "")
    )
    if not test_mode:
        assert_current_production_writer()
    try:
        sent = cleaner_outbound_router(api, token_path=TOKEN_PATH).send_message(
            candidate["telegram_chat_id"],
            text,
            reply_markup=_keyboard(action_id, config["approve"], config["reject"]),
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


def send_next_stage(record: dict, *, test_mode: bool,
                    expected_last_edited_time: str | None = None) -> dict | None:
    next_stage = {"DAY_CONFIRM": "ARRIVAL", "ARRIVAL": "START"}.get(record.get("operation_stage"))
    if not next_stage:
        return None
    candidate = {
        "telegram_user_id": record["candidate_user_id"],
        "telegram_chat_id": record["candidate_chat_id"],
        "party_page_id": record.get("candidate_party_page_id"),
        "label": record.get("candidate_label", "청소 담당자"),
    }
    return send_operation_stage(
        stage=next_stage,
        cleaning_page_id=record["cleaning_page_id"],
        property_nickname=record["property_nickname"],
        address=record["address"],
        start_at=record["start_at"],
        end_at=record["end_at"],
        candidate=candidate,
        test_mode=test_mode,
        expected_last_edited_time=expected_last_edited_time,
        parent_action_id=record["action_id"],
    )


def send_next_test_stage(record: dict) -> dict | None:
    return send_next_stage(record, test_mode=True)

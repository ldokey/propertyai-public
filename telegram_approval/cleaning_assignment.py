#!/usr/bin/env python3
"""Create signed cleaner assignment proposals for the current pilot."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram_approval.cleaner_config import CleanerRuntimePaths
from telegram_approval.cleaner_performance import canonical_offer_economics
from propertyai_core.global_writer import assert_current_production_writer
from telegram_approval.outbound import cleaner_outbound_router, ops_outbound_router
from telegram_approval.send_approval import (
    OPERATOR_PATH,
    api,
    atomic_private,
    secret,
    signature,
)


TOKEN_PATH = None
REQUEST_DIR = CleanerRuntimePaths().request_dir
_ACTION_ID_ALLOCATION_ATTEMPTS = 16


def _atomic_private_new(path: Path, value: dict) -> None:
    """Publish one new action identity without ever replacing a peer.

    The existing ``atomic_private`` primitive remains the durable local write
    boundary required by the current W01 post-external recovery contract.  A
    hard-link publish then adds exclusive identity allocation: if the final
    action path already exists, link(2) fails instead of replacing it.
    """

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    allocation = path.parent / f".{path.name}.{secrets.token_hex(8)}.allocation"
    try:
        atomic_private(allocation, value)
        os.link(allocation, path)
    finally:
        allocation.unlink(missing_ok=True)


def send_assignment(*, cleaning_page_id: str, cleaning_name: str, property_nickname: str,
                    address: str, start_at: str, end_at: str, cleaning_fee_krw: int,
                    expected_last_edited_time: str, candidates: list[dict] | None = None,
                    proposal_round: int = 1,
                    expected_acceptance_status: str = "수락대기",
                    default_candidate_party_page_id: str | None = None,
                    observers: list[dict] | None = None,
                    test_mode: bool = False,
                    replacement_urgency: str = "NORMAL",
                    urgent_premium_krw: int = 0,
                    total_agreed_fee_krw: int | None = None,
                    urgent_premium_policy_version: str | None = None) -> dict:
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
        raise ValueError("assignment economics could not be resolved")
    for path in REQUEST_DIR.glob("*.json"):
        existing = json.loads(path.read_text())
        if (
            existing.get("action_type") == "CLEANING_ASSIGNMENT"
            and existing.get("cleaning_page_id") == cleaning_page_id
            and existing.get("status") == "PENDING"
            and not existing.get("consumed")
        ):
            return {"sent": False, "reason": "PENDING_REQUEST_EXISTS", "action_id": existing["action_id"]}

    operator = json.loads(OPERATOR_PATH.read_text())
    if candidates is None:
        candidates = [{
            "telegram_user_id": operator["telegram_user_id"],
            "telegram_chat_id": operator["telegram_chat_id"],
            "party_page_id": default_candidate_party_page_id,
            "label": "운영자 테스트 담당자",
        }]
    if not candidates:
        raise ValueError("at least one cleaner candidate is required")
    candidate, remaining_candidates = candidates[0], candidates[1:]
    now = datetime.now(timezone.utc)
    for _ in range(_ACTION_ID_ALLOCATION_ATTEMPTS):
        action_id = secrets.token_urlsafe(8)
        record = {
            "schema_version": 2,
            "action_id": action_id,
            "action_type": "CLEANING_ASSIGNMENT",
            "execute_on_reject": not test_mode,
            "cleaning_page_id": cleaning_page_id,
            "cleaning_name": cleaning_name,
            "property_nickname": property_nickname,
            "address": address,
            "start_at": start_at,
            "end_at": end_at,
            "cleaning_fee_krw": economics.base_fee_krw,
            "replacement_urgency": economics.replacement_urgency,
            "urgent_premium_krw": economics.urgent_premium_krw,
            "total_agreed_fee_krw": economics.total_agreed_fee_krw,
            "urgent_premium_policy_version": economics.urgent_premium_policy_version,
            "expected_last_edited_time": expected_last_edited_time,
            "expected_acceptance_status": expected_acceptance_status,
            "candidate_user_id": candidate["telegram_user_id"],
            "candidate_chat_id": candidate["telegram_chat_id"],
            "candidate_party_page_id": candidate.get("party_page_id"),
            "candidate_label": candidate.get("label", "청소 담당자"),
            "remaining_candidates": remaining_candidates,
            "observers": observers or [],
            "proposal_round": proposal_round,
            "status": "PENDING",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "consumed": False,
            "test_mode": test_mode,
            "actor_mode": "OPERATOR_AS_CLEANER_TEST",
            "external_writes_on_approval": 0 if test_mode else 1,
            "external_writes_on_reject": 0 if test_mode else 1,
            "payment_effects": 0,
        }
        path = REQUEST_DIR / f"{action_id}.json"
        try:
            _atomic_private_new(path, record)
        except FileExistsError:
            continue
        break
    else:
        raise RuntimeError("unable to allocate unique CLEANING_ASSIGNMENT action_id")
    compensation_text = (
        f"기본 청소비: ₩{economics.base_fee_krw:,}\n"
        f"긴급 대체 추가금: ₩{economics.urgent_premium_krw:,}\n"
        f"총 제안금액: ₩{economics.total_agreed_fee_krw:,}\n"
        if economics.replacement_urgency == "URGENT"
        else f"청소비: ₩{economics.base_fee_krw:,}\n"
    )
    text = (
        ("🧪 [TEST] JJ 청소 일정 제안\n\n" if test_mode else "🧹 JJ 청소 일정 제안\n\n")
        + f"숙소: {property_nickname}\n"
        f"청소 시작: {start_at}\n"
        f"완료 목표: {end_at}\n"
        + compensation_text
        + "\n수락하면 청소 담당자로 확정됩니다.\n"
        "24시간 내 수락하지 않으면 다음 담당자에게 제안이 넘어갑니다.\n"
        "이 단계에서는 지급예정·지급완료가 생성되지 않습니다."
        + ("\n\nTEST: 버튼을 눌러도 Notion·Calendar·정산은 변경되지 않습니다." if test_mode else "")
    )
    key = secret()
    keyboard = {"inline_keyboard": [[
        {"text": "✅ 수락", "callback_data": f"a:{action_id}:approve:{signature(key, action_id, 'approve')}"},
        {"text": "❌ 거절", "callback_data": f"a:{action_id}:reject:{signature(key, action_id, 'reject')}"},
    ]]}
    if not test_mode:
        assert_current_production_writer()
    try:
        sent = cleaner_outbound_router(api, token_path=TOKEN_PATH).send_message(
            candidate["telegram_chat_id"], text,
            reply_markup=json.dumps(keyboard, ensure_ascii=False),
        )
    except Exception:
        if not test_mode:
            assert_current_production_writer()
        raise
    if not test_mode:
        assert_current_production_writer()
    record["telegram_message_id"] = sent["message_id"]
    observer_messages = []
    for observer in observers or []:
        if observer["telegram_chat_id"] == candidate["telegram_chat_id"]:
            continue
        observer_text = (
            "👀 청소 제안 발송 확인용\n\n"
            f"제안 대상: {candidate.get('label', '청소 담당자')}\n"
            + text
        )
        if not test_mode:
            assert_current_production_writer()
        try:
            observer_sent = ops_outbound_router(
                api,
                token_path=TOKEN_PATH,
                synthetic_allowed_chat_ids=(observer["telegram_chat_id"],) if TOKEN_PATH else None,
            ).send_message(
                observer["telegram_chat_id"], observer_text,
            )
        except Exception:
            if not test_mode:
                assert_current_production_writer()
            raise
        if not test_mode:
            assert_current_production_writer()
        observer_messages.append({
            "chat_id": observer["telegram_chat_id"],
            "telegram_message_id": observer_sent["message_id"],
        })
    record["observer_messages"] = observer_messages
    if not test_mode:
        assert_current_production_writer()
    atomic_private(path, record)
    return {"sent": True, "action_id": action_id, "telegram_message_id": sent["message_id"]}


def send_next_assignment(record: dict, expected_last_edited_time: str) -> dict:
    remaining = record.get("remaining_candidates") or []
    if not remaining:
        return {"sent": False, "reason": "NO_REMAINING_CANDIDATE"}
    return send_assignment(
        cleaning_page_id=record["cleaning_page_id"],
        cleaning_name=record["cleaning_name"],
        property_nickname=record["property_nickname"],
        address=record["address"],
        start_at=record["start_at"],
        end_at=record["end_at"],
        cleaning_fee_krw=record["cleaning_fee_krw"],
        expected_last_edited_time=expected_last_edited_time,
        candidates=remaining,
        proposal_round=int(record.get("proposal_round", 1)) + 1,
        observers=record.get("observers") or [],
        test_mode=bool(record.get("test_mode", False)),
        replacement_urgency=record.get("replacement_urgency", "NORMAL"),
        urgent_premium_krw=record.get("urgent_premium_krw", 0),
        total_agreed_fee_krw=record.get("total_agreed_fee_krw", record["cleaning_fee_krw"]),
        urgent_premium_policy_version=record.get("urgent_premium_policy_version"),
    )

#!/usr/bin/env python3
import argparse
import hashlib
import json
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from telegram_approval.outbound import ops_outbound_router  # noqa: E402
from telegram_approval.send_approval import (  # noqa: E402
    OPERATOR_PATH,
    REQUEST_DIR,
    api,
    atomic_private,
    secret,
    signature,
)


TOKEN_PATH = None


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    parser.add_argument("dry_run", type=Path)
    parser.add_argument("test_approval", type=Path)
    args = parser.parse_args()

    candidate = json.loads(args.candidate.read_text())
    dry_run = json.loads(args.dry_run.read_text())
    test_approval = json.loads(args.test_approval.read_text())

    checks = {
        "candidate_blocked": candidate.get("write_ready") is False and candidate.get("external_writes") == 0,
        "test_approval_valid": test_approval.get("status") == "APPROVED_TEST" and test_approval.get("consumed") is True,
        "test_approval_matches": test_approval.get("changeset_hash") == sha256(args.candidate),
        "dry_run_matches": dry_run.get("source_candidate_sha256") == sha256(args.candidate),
        "dry_run_no_writes": dry_run.get("external_writes") == 0 and dry_run.get("write_ready") is False,
        "effect_count": dry_run.get("external_writes_if_separately_approved") == 3 and len(dry_run.get("effects", [])) == 3,
    }
    if not all(checks.values()):
        raise SystemExit(json.dumps({"status": "REVIEW_REQUIRED", "checks": checks}))

    action_id = secrets.token_urlsafe(8)
    key = secret()
    now = datetime.now(timezone.utc)
    record = {
        "schema_version": 1,
        "action_id": action_id,
        "operation": "CREATE_JJ_CLEANING_WORKFLOW",
        "candidate_path": str(args.candidate.resolve()),
        "candidate_hash": sha256(args.candidate),
        "dry_run_path": str(args.dry_run.resolve()),
        "dry_run_hash": sha256(args.dry_run),
        "test_approval_action_id": test_approval["action_id"],
        "status": "PENDING",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "consumed": False,
        "test_mode": False,
        "external_writes_on_approval": 3,
        "allowed_targets": ["NOTION.DB.CLEANING", "GCAL.CAL.CLEANING", "NOTION.CLEANING.SYNC_FINALIZE"],
        "execution_requires_fresh_recheck": True,
    }
    path = REQUEST_DIR / f"{action_id}.json"
    atomic_private(path, record)

    text = (
        "⚠️ JJ 청소 실제 생성 승인\n\n"
        "대상: JJ 퇴실청소\n"
        "일시: 2026-08-12 11:00–15:00 KST\n"
        "다음 체크인: 2026-08-14 15:00 KST\n"
        "담당자: 미배정 / 연락 없음\n\n"
        "승인 시 최대 3회 쓰기:\n"
        "1) Notion 청소 TEMPORARY 생성\n"
        "2) CAL.CLEANING 일정 생성\n"
        "3) Notion에 Calendar Event ID 반영\n\n"
        "청소비·지급·도어코드·담당자 메시지는 건드리지 않습니다.\n"
        "Notion 쿼리 한도 때문에 최종 중복 확인은 정확 제목·멱등키 검색으로 대체됐습니다.\n"
        "유효시간: 10분"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ 실제 3단계 실행 승인", "callback_data": f"a:{action_id}:approve:{signature(key, action_id, 'approve')}"},
        {"text": "❌ 취소", "callback_data": f"a:{action_id}:reject:{signature(key, action_id, 'reject')}"},
    ]]}
    operator = json.loads(OPERATOR_PATH.read_text())
    sent = ops_outbound_router(
        api,
        token_path=TOKEN_PATH,
        synthetic_allowed_chat_ids=(operator["telegram_chat_id"],) if TOKEN_PATH else None,
    ).send_message(
        operator["telegram_chat_id"],
        text,
        reply_markup=json.dumps(keyboard),
    )
    record["telegram_message_id"] = sent["message_id"]
    atomic_private(path, record)
    print(json.dumps({"sent": True, "action_id": action_id, "status": "PENDING", "external_writes_on_approval": 3}))


if __name__ == "__main__":
    main()

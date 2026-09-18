#!/usr/bin/env python3
import argparse
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from telegram_approval.send_approval import (  # noqa: E402
    OPERATOR_PATH,
    REQUEST_DIR,
    api,
    atomic_private,
    secret,
    signature,
)
from telegram_approval.outbound import ops_outbound_router


TOKEN_PATH = None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("changeset", type=Path)
    parser.add_argument("dry_run", type=Path)
    args = parser.parse_args()
    changeset = json.loads(args.changeset.read_text())
    dry_run = json.loads(args.dry_run.read_text())
    if dry_run.get("status") != "PASS_NO_WRITE" or dry_run.get("external_writes") != 0:
        raise SystemExit("A passing no-write dry-run is required")
    action_id = secrets.token_urlsafe(8)
    key = secret()
    now = datetime.now(timezone.utc)
    record = {
        "schema_version": 1,
        "action_id": action_id,
        "operation": "CREATE_NOTION_RESERVATION",
        "changeset_path": str(args.changeset.resolve()),
        "changeset_hash": hashlib.sha256(args.changeset.read_bytes()).hexdigest(),
        "dry_run_hash": hashlib.sha256(args.dry_run.read_bytes()).hexdigest(),
        "status": "PENDING",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "consumed": False,
        "test_mode": False,
        "external_writes_on_approval": 1,
        "allowed_target": "NOTION.DB.RESERVATION",
    }
    atomic_private(REQUEST_DIR / f"{action_id}.json", record)
    fields = changeset["proposed_notion_properties"]
    text = (
        "⚠️ PropertyAI 실제 쓰기 승인\n\n"
        "작업: Notion Reservation 1건 생성\n"
        "환경: PRODUCTION\n등록 상태: TEMPORARY\n"
        f"체크인: {fields['체크인']}\n체크아웃: {fields['체크아웃']}\n"
        f"성인: {fields['성인 수']}명\n게스트 결제: {fields['통화']} {fields['표시 금액']}\n\n"
        "Calendar·청소·Finance·메시지는 생성하지 않습니다.\n유효시간: 10분"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ 실제 생성 승인", "callback_data": f"a:{action_id}:approve:{signature(key, action_id, 'approve')}"},
        {"text": "❌ 취소", "callback_data": f"a:{action_id}:reject:{signature(key, action_id, 'reject')}"},
    ]]}
    operator = json.loads(OPERATOR_PATH.read_text())
    sent = ops_outbound_router(
        api,
        token_path=TOKEN_PATH,
        synthetic_allowed_chat_ids=(operator["telegram_chat_id"],) if TOKEN_PATH else None,
    ).send_message(
        operator["telegram_chat_id"], text, reply_markup=json.dumps(keyboard)
    )
    record["telegram_message_id"] = sent["message_id"]
    atomic_private(REQUEST_DIR / f"{action_id}.json", record)
    print(json.dumps({"sent": True, "action_id": action_id, "operation": record["operation"], "status": "PENDING"}))


if __name__ == "__main__":
    main()

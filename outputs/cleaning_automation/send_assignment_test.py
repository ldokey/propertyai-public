#!/usr/bin/env python3
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


def main():
    action_id = secrets.token_urlsafe(8)
    now = datetime.now(timezone.utc)
    record = {
        "schema_version": 1,
        "action_id": action_id,
        "operation": "TEST_CLEANING_ASSIGNMENT_ACCEPTANCE",
        "cleaning_page_id": "3b03f2c8-cf47-8116-bd3f-f8af4799bbb3",
        "participant_role": "OPERATOR_TEST_OBSERVER",
        "status": "PENDING",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
        "consumed": False,
        "test_mode": True,
        "external_writes_on_approval": 0,
        "contains_door_code": False,
        "contains_detailed_address": False,
    }
    path = REQUEST_DIR / f"{action_id}.json"
    atomic_private(path, record)

    key = secret()
    message = (
        "🧪 청소 배정 메시지 TEST\n\n"
        "숙소: JJ\n"
        "일시: 2026-08-12 11:00–15:00\n"
        "유형: 퇴실청소\n"
        "청소비: 미확정\n"
        "역할: 운영자 확인용 TEST 참여자\n\n"
        "주소 상세와 도어코드는 수락 전 제공되지 않습니다.\n"
        "버튼을 눌러도 Notion·Calendar 상태는 바뀌지 않습니다."
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ 수락(TEST)", "callback_data": f"a:{action_id}:approve:{signature(key, action_id, 'approve')}"},
        {"text": "❌ 거절(TEST)", "callback_data": f"a:{action_id}:reject:{signature(key, action_id, 'reject')}"},
    ]]}
    operator = json.loads(OPERATOR_PATH.read_text())
    sent = ops_outbound_router(
        api,
        token_path=TOKEN_PATH,
        synthetic_allowed_chat_ids=(operator["telegram_chat_id"],) if TOKEN_PATH else None,
    ).send_message(
        operator["telegram_chat_id"],
        message,
        reply_markup=json.dumps(keyboard),
    )
    record["telegram_message_id"] = sent["message_id"]
    atomic_private(path, record)
    print(json.dumps({"sent": True, "action_id": action_id, "test_mode": True, "external_writes": 0}))


if __name__ == "__main__":
    main()

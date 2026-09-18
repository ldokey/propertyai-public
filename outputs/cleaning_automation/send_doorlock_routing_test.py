#!/usr/bin/env python3
"""Send the paired operator a non-mutating door-lock routing test."""

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from telegram_approval.outbound import ops_outbound_router  # noqa: E402
from telegram_approval.send_approval import OPERATOR_PATH, api  # noqa: E402


TOKEN_PATH = None


def main() -> None:
    operator = json.loads(OPERATOR_PATH.read_text())
    message = (
        "✅ 도어락 메시지 분기 TEST\n\n"
        "숙소: JJ\n"
        "스마트 도어락 여부: true\n"
        "Airbnb 전화번호 뒤 4자리 입력 요청: 생략\n"
        "청소 담당자 비밀번호 메시지: 생략\n\n"
        "기본 규칙: false(일반 도어락)인 숙소만 입력값을 변경 없이 발송"
    )
    sent = ops_outbound_router(
        api,
        token_path=TOKEN_PATH,
        synthetic_allowed_chat_ids=(operator["telegram_chat_id"],) if TOKEN_PATH else None,
    ).send_message(
        operator["telegram_chat_id"],
        message,
    )
    print(json.dumps({"sent": True, "message_id": sent["message_id"], "contains_door_code": False}))


if __name__ == "__main__":
    main()

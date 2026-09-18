#!/usr/bin/env python3
import argparse
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from telegram_approval.outbound import ops_outbound_router
from telegram_approval.ops_config import OpsRuntimePaths
from telegram_approval.telegram_transport import TelegramBotContext, TelegramHttpTransport


# Explicit override seam for deterministic tests. Runtime resolution uses only
# PROPERTYAI_OPS_ADMIN_TELEGRAM_TOKEN_PATH when this remains unset.
TOKEN_PATH = None
OPERATOR_PATH = ROOT / "secrets" / "telegram" / "operator.json"
ACTION_SECRET_PATH = ROOT / "secrets" / "telegram" / "action-secret"
REQUEST_DIR = OpsRuntimePaths().request_dir
_TRANSPORT = TelegramHttpTransport(timeout_seconds=20)


def atomic_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def api(token, method, **values):
    return _TRANSPORT.request(TelegramBotContext(token), method, **values)


def secret():
    if not ACTION_SECRET_PATH.exists():
        ACTION_SECRET_PATH.write_text(secrets.token_urlsafe(48) + "\n")
        os.chmod(ACTION_SECRET_PATH, 0o600)
    return ACTION_SECRET_PATH.read_text().strip().encode()


def signature(key, action_id, decision):
    return hmac.new(key, f"{action_id}:{decision}".encode(), hashlib.sha256).hexdigest()[:16]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("changeset", type=Path)
    args = parser.parse_args()
    changeset = json.loads(args.changeset.read_text())
    if changeset.get("write_ready") is not False or changeset.get("external_writes") != 0:
        raise SystemExit("Only blocked, no-write ChangeSets may enter TEST approval")
    action_id = secrets.token_urlsafe(8)
    key = secret()
    now = datetime.now(timezone.utc)
    request_record = {
        "schema_version": 1,
        "action_id": action_id,
        "changeset_path": str(args.changeset.resolve()),
        "changeset_hash": hashlib.sha256(args.changeset.read_bytes()).hexdigest(),
        "classification": changeset["classification"],
        "status": "PENDING",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=15)).isoformat(),
        "consumed": False,
        "test_mode": True,
        "external_writes_on_approval": 0,
    }
    atomic_private(REQUEST_DIR / f"{action_id}.json", request_record)
    fields = changeset.get("proposed_notion_properties") or {}
    text = (
        "🧪 PropertyAI 예약 승인 테스트\n\n"
        f"판정: {changeset['classification']}\n"
        f"Listing ID: {fields.get('External Listing ID', 'N/A')}\n"
        f"체크인: {fields.get('체크인', 'N/A')}\n"
        f"체크아웃: {fields.get('체크아웃', 'N/A')}\n"
        f"성인: {fields.get('성인 수', 'N/A')}명\n"
        f"게스트 결제: {fields.get('통화', '')} {fields.get('표시 금액', 'N/A')}\n"
        f"호스트 수령: {fields.get('통화', '')} {fields.get('호스트 수령액', 'N/A')}\n\n"
        "승인해도 Notion·Calendar·Gmail 쓰기는 실행되지 않습니다.\n"
        "유효시간: 15분"
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ 승인(TEST)", "callback_data": f"a:{action_id}:approve:{signature(key, action_id, 'approve')}"},
        {"text": "❌ 거절", "callback_data": f"a:{action_id}:reject:{signature(key, action_id, 'reject')}"},
    ]]}
    operator = json.loads(OPERATOR_PATH.read_text())
    sent = ops_outbound_router(
        api,
        token_path=TOKEN_PATH,
        synthetic_allowed_chat_ids=(operator["telegram_chat_id"],) if TOKEN_PATH else None,
    ).send_message(
        operator["telegram_chat_id"], text, reply_markup=json.dumps(keyboard)
    )
    request_record["telegram_message_id"] = sent["message_id"]
    atomic_private(REQUEST_DIR / f"{action_id}.json", request_record)
    print(json.dumps({"sent": True, "action_id": action_id, "status": "PENDING", "test_mode": True, "external_writes": 0}))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Completion-photo sessions required before a Cleaning can be reported done."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram_approval.cleaner_config import CleanerRuntimePaths
from telegram_approval.outbound import cleaner_outbound_router
from telegram_approval.send_approval import api, atomic_private, secret, signature
from telegram_approval.telegram_transport import TelegramBotContext, TelegramHttpTransport


ROOT = Path(__file__).resolve().parents[1]
TOKEN_PATH = None
REQUEST_DIR = CleanerRuntimePaths().request_dir
COMPLETION_DIR = CleanerRuntimePaths().completion_session_dir
MINIMUM_PHOTOS = 4


def _keyboard(action_id):
    key = secret()
    return json.dumps({"inline_keyboard": [[
        {"text": "✅ 완료 사진 제출", "callback_data": f"a:{action_id}:approve:{signature(key, action_id, 'approve')}"},
        {"text": "취소", "callback_data": f"a:{action_id}:reject:{signature(key, action_id, 'reject')}"},
    ]]}, ensure_ascii=False)


def _session_path(session_id):
    return COMPLETION_DIR / session_id / "session.json"


def load_session(session_id):
    if not session_id:
        return None
    path = _session_path(session_id)
    return json.loads(path.read_text()) if path.exists() else None


def save_session(session):
    directory = COMPLETION_DIR / session["session_id"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    atomic_private(directory / "session.json", session)


def session_ready(record):
    session = load_session(record.get("completion_session_id"))
    return bool(
        session and session.get("status") == "OPEN"
        and len(session.get("photos", [])) >= MINIMUM_PHOTOS
    )


def finish_session(record, *, submitted):
    session = load_session(record.get("completion_session_id"))
    if not session:
        return None
    session["status"] = ("SUBMITTED_TEST" if record.get("test_mode") else "SUBMITTED") if submitted else "CANCELLED"
    session["closed_at"] = datetime.now(timezone.utc).isoformat()
    if submitted and not record.get("test_mode"):
        try:
            from drive_archive.issue_evidence import quarantine_session_photos
            session["local_quarantine"] = quarantine_session_photos(session)
        except Exception as exc:
            session["local_quarantine"] = {"moved": 0, "error_type": type(exc).__name__}
    save_session(session)
    return session


def start_completion_session(parent_record):
    now = datetime.now(timezone.utc)
    session_id = secrets.token_urlsafe(8)
    action_id = secrets.token_urlsafe(8)
    test_mode = parent_record.get("test_mode", True)
    shared = {
        "cleaning_page_id": parent_record["cleaning_page_id"],
        "property_nickname": parent_record["property_nickname"],
        "cleaning_date": parent_record["cleaning_date"],
        "cleaning_fee_krw": parent_record["cleaning_fee_krw"],
        "replacement_urgency": parent_record.get("replacement_urgency", "NORMAL"),
        "urgent_premium_krw": parent_record.get("urgent_premium_krw", 0),
        "total_agreed_fee_krw": parent_record.get(
            "total_agreed_fee_krw", parent_record["cleaning_fee_krw"]
        ),
        "urgent_premium_policy_version": parent_record.get("urgent_premium_policy_version"),
        "assignment_page_id": parent_record.get("assignment_page_id"),
        "assignment_version": parent_record.get("assignment_version"),
        "accepted_assignment_action_id": parent_record.get("accepted_assignment_action_id"),
        "candidate_user_id": parent_record["candidate_user_id"],
        "candidate_chat_id": parent_record["candidate_chat_id"],
        "candidate_party_page_id": parent_record.get("candidate_party_page_id"),
        "candidate_label": parent_record.get("candidate_label", "청소 담당자"),
        "expected_last_edited_time": parent_record.get("expected_last_edited_time"),
        "test_mode": test_mode,
    }
    session = {
        "schema_version": 1, "evidence_type": "completion",
        "session_id": session_id, "action_id": action_id,
        "parent_action_id": parent_record["action_id"],
        **shared,
        "status": "OPEN", "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=4)).isoformat(),
        "notes": [], "photos": [],
    }
    save_session(session)
    record = {
        "schema_version": 1, "action_id": action_id,
        "action_type": "CLEANING_COMPLETION_SUBMISSION",
        "parent_action_id": parent_record["action_id"],
        "completion_session_id": session_id,
        **shared,
        "status": "PENDING", "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=4)).isoformat(),
        "consumed": False, "execute_on_reject": False,
        "external_writes_on_approval": 0 if test_mode else 2,
        "external_systems_on_approval": [] if test_mode else ["Google Drive", "Notion"],
        "external_writes_on_reject": 0,
    }
    atomic_private(REQUEST_DIR / f"{action_id}.json", record)
    text = (
        ("🧪 [TEST] 청소 완료 사진 제출\n\n" if test_mode else "📷 청소 완료 사진 제출\n\n")
        + f"숙소: {record['property_nickname']}\n"
        f"청소일: {record['cleaning_date']}\n\n"
        "아래 완료 사진을 최소 4장 보내주세요.\n"
        "• 침실·거실 세팅\n• 주방\n• 욕실\n• 현관·최종 퇴실 상태\n\n"
        "사진을 모두 보낸 뒤 `완료 사진 제출`을 눌러주세요.\n"
        "사진 검증과 운영자 확인 전에는 지급 확인으로 넘어가지 않습니다."
        + ("\n\nTEST: Drive·Notion·정산은 변경되지 않습니다." if test_mode else "")
    )
    sent = cleaner_outbound_router(api, token_path=TOKEN_PATH).send_message(
        record["candidate_chat_id"], text, reply_markup=_keyboard(action_id)
    )
    record["telegram_message_id"] = sent["message_id"]
    session["prompt_message_id"] = sent["message_id"]
    atomic_private(REQUEST_DIR / f"{action_id}.json", record)
    save_session(session)
    return {"sent": True, "action_id": action_id, "session_id": session_id,
            "telegram_message_id": sent["message_id"]}


def _open_sessions(user_id, chat_id):
    now = datetime.now(timezone.utc)
    matches = []
    for path in COMPLETION_DIR.glob("*/session.json"):
        session = json.loads(path.read_text())
        if session.get("status") != "OPEN":
            continue
        if now > datetime.fromisoformat(session["expires_at"]):
            session["status"] = "EXPIRED"
            save_session(session)
            continue
        if session.get("candidate_user_id") == user_id and session.get("candidate_chat_id") == chat_id:
            matches.append(session)
    return matches


def capture_completion_message(update, token):
    message = update.get("message", {})
    sender, chat = message.get("from", {}), message.get("chat", {})
    if chat.get("type") != "private" or sender.get("is_bot"):
        return None
    sessions = _open_sessions(sender.get("id"), chat.get("id"))
    if len(sessions) != 1:
        return None
    session = sessions[0]
    photos = message.get("photo") or []
    text_value = message.get("text")
    reply_id = (message.get("reply_to_message") or {}).get("message_id")
    if text_value and not text_value.startswith("/"):
        if reply_id != session.get("prompt_message_id"):
            return None
        session["notes"].append({"message_id": message.get("message_id"), "text": text_value,
                                 "received_at": datetime.now(timezone.utc).isoformat()})
        save_session(session)
        api(token, "sendMessage", chat_id=chat["id"], text="📝 완료 메모를 저장했습니다.")
        return "completion_text_saved"
    if not photos:
        return None
    photo = max(photos, key=lambda item: item.get("file_size", 0))
    if photo.get("file_size", 0) > 20 * 1024 * 1024:
        api(token, "sendMessage", chat_id=chat["id"], text="사진이 20MB를 초과했습니다. 일반 화질로 다시 보내주세요.")
        return "completion_photo_rejected"
    if any(item.get("file_unique_id") == photo.get("file_unique_id") for item in session["photos"]):
        api(token, "sendMessage", chat_id=chat["id"], text="이미 저장된 사진입니다.")
        return "completion_photo_duplicate"
    remote = api(token, "getFile", file_id=photo["file_id"])
    suffix = Path(remote["file_path"]).suffix.lower() or ".jpg"
    filename = f"{len(session['photos']) + 1:02d}_{photo.get('file_unique_id', 'photo')}{suffix}"
    target = COMPLETION_DIR / session["session_id"] / filename
    data = TelegramHttpTransport(
        urlopen=urllib.request.urlopen, timeout_seconds=60
    ).download_file(
        TelegramBotContext(token), remote["file_path"], maximum_bytes=20 * 1024 * 1024
    )
    if len(data) > 20 * 1024 * 1024:
        api(token, "sendMessage", chat_id=chat["id"], text="사진이 20MB를 초과했습니다. 일반 화질로 다시 보내주세요.")
        return "completion_photo_rejected"
    target.write_bytes(data)
    os.chmod(target, 0o600)
    session["photos"].append({
        "message_id": message.get("message_id"), "file_unique_id": photo.get("file_unique_id"),
        "filename": filename, "path": str(target), "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), "received_at": datetime.now(timezone.utc).isoformat(),
    })
    save_session(session)
    count = len(session["photos"])
    remaining = max(0, MINIMUM_PHOTOS - count)
    suffix_text = f" · 최소 {remaining}장 더 필요" if remaining else " · 제출 가능"
    api(token, "sendMessage", chat_id=chat["id"], text=f"📷 완료 사진 {count}장을 저장했습니다{suffix_text}")
    return "completion_photo_saved"

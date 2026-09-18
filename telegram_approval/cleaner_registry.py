#!/usr/bin/env python3
"""Private Telegram cleaner registry and one-time onboarding invitations."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPERATOR_PATH = ROOT / "secrets" / "telegram" / "operator.json"
ROSTER_PATH = ROOT / "secrets" / "telegram" / "cleaners.json"
METADATA_PATH = ROOT / "telegram_approval" / "runtime" / "bot-metadata.json"
INVITE_DIR = ROOT / "telegram_approval" / "runtime" / "cleaner-invites"


def atomic_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load_roster() -> dict:
    if not ROSTER_PATH.exists():
        return {"schema_version": 1, "cleaners": []}
    return json.loads(ROSTER_PATH.read_text())


def register_operator(*, party_page_id: str, properties: list[str], acts_as_cleaner: bool = False) -> dict:
    operator = json.loads(OPERATOR_PATH.read_text())
    roster = load_roster()
    now = datetime.now(timezone.utc).isoformat()
    entry = next((item for item in roster["cleaners"] if item.get("role") == "OPERATOR_OBSERVER"), None)
    values = {
        "identity_id": entry.get("identity_id") if entry else secrets.token_urlsafe(8),
        "label": "운영자 겸 청소담당자(TEST)" if acts_as_cleaner else "운영자 확인용",
        "role": "OPERATOR_OBSERVER",
        "telegram_user_id": operator["telegram_user_id"],
        "telegram_chat_id": operator["telegram_chat_id"],
        "party_page_id": party_page_id,
        "properties": properties,
        "priority_by_property": {name: 999 for name in properties},
        "observer_copy": True,
        "fallback_candidate": True,
        "acts_as_cleaner": acts_as_cleaner,
        "status": "ACTIVE",
        "updated_at": now,
    }
    if entry:
        entry.update(values)
    else:
        values["created_at"] = now
        roster["cleaners"].append(values)
    atomic_private(ROSTER_PATH, roster)
    return values


def create_invite(*, label: str, party_page_id: str, properties: list[str],
                  priority: int, expires_hours: int = 24) -> dict:
    code = secrets.token_urlsafe(18)
    invite_id = secrets.token_urlsafe(8)
    now = datetime.now(timezone.utc)
    record = {
        "schema_version": 1,
        "invite_id": invite_id,
        "code_hash": hashlib.sha256(code.encode()).hexdigest(),
        "label": label,
        "party_page_id": party_page_id,
        "properties": properties,
        "priority": priority,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=expires_hours)).isoformat(),
        "consumed": False,
    }
    atomic_private(INVITE_DIR / f"{invite_id}.json", record)
    metadata = json.loads(METADATA_PATH.read_text())
    return {
        "invite_id": invite_id,
        "url": f"https://t.me/{metadata['bot_username']}?start=c_{code}",
        "expires_at": record["expires_at"],
    }


def consume_invite(code: str, *, telegram_user_id: int, telegram_chat_id: int) -> dict | None:
    now = datetime.now(timezone.utc)
    digest = hashlib.sha256(code.encode()).hexdigest()
    for path in sorted(INVITE_DIR.glob("*.json")):
        invite = json.loads(path.read_text())
        if (
            invite.get("consumed")
            or now > datetime.fromisoformat(invite["expires_at"])
            or invite.get("code_hash") != digest
        ):
            continue
        roster = load_roster()
        if any(
            item.get("telegram_user_id") == telegram_user_id and item.get("status") == "ACTIVE"
            for item in roster["cleaners"]
        ):
            return None
        entry = {
            "identity_id": secrets.token_urlsafe(8),
            "label": invite["label"],
            "role": "CLEANER",
            "telegram_user_id": telegram_user_id,
            "telegram_chat_id": telegram_chat_id,
            "party_page_id": invite["party_page_id"],
            "properties": invite["properties"],
            "priority_by_property": {name: invite["priority"] for name in invite["properties"]},
            "observer_copy": False,
            "fallback_candidate": False,
            "status": "ACTIVE",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        roster["cleaners"].append(entry)
        atomic_private(ROSTER_PATH, roster)
        invite.update({"consumed": True, "consumed_at": now.isoformat(), "identity_id": entry["identity_id"]})
        atomic_private(path, invite)
        return entry
    return None


def assignment_targets(property_nickname: str) -> tuple[list[dict], list[dict]]:
    roster = load_roster()
    eligible = [
        item for item in roster["cleaners"]
        if item.get("status") == "ACTIVE" and property_nickname in item.get("properties", [])
    ]
    real_workers = [item for item in eligible if item.get("role") == "CLEANER"]
    test_workers = [item for item in eligible if item.get("acts_as_cleaner")]
    workers = real_workers or test_workers
    workers.sort(key=lambda item: (item.get("priority_by_property", {}).get(property_nickname, 999), item["identity_id"]))
    fallback = [item for item in eligible if item.get("fallback_candidate")]
    actionable = workers or fallback
    observers = [item for item in eligible if item.get("observer_copy")]

    def public(item: dict) -> dict:
        return {
            "telegram_user_id": item["telegram_user_id"],
            "telegram_chat_id": item["telegram_chat_id"],
            "party_page_id": item.get("party_page_id"),
            "label": item.get("label", "청소 담당자"),
        }

    candidate_targets = [public(item) for item in actionable]
    candidate_chats = {item["telegram_chat_id"] for item in candidate_targets}
    observer_targets = [public(item) for item in observers if item["telegram_chat_id"] not in candidate_chats]
    return candidate_targets, observer_targets

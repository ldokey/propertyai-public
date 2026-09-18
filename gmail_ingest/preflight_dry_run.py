#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

try:
    from gmail_ingest.airbnb_parser import parse_airbnb_confirmation
    from gmail_ingest.mime_utils import decoded_message_text
    from gmail_ingest.runtime_config import load_gmail_ingest_config
except ModuleNotFoundError:
    from airbnb_parser import parse_airbnb_confirmation
    from mime_utils import decoded_message_text
    from runtime_config import load_gmail_ingest_config


ROOT = Path(__file__).resolve().parents[1]
TOKEN = ROOT / "secrets" / "google" / "token.json"
OUTPUT_DIR = ROOT / "gmail_ingest" / "runtime" / "dry_runs"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]
COMPARE_FIELDS = ("external_booking_ref_hash", "listing_id", "check_in", "check_out", "adults", "nights", "currency", "guest_total", "host_payout")


def atomic_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def header(payload, name):
    for item in payload.get("headers", []):
        if item.get("name", "").casefold() == name.casefold():
            return item.get("value", "")
    return ""


def fresh_gmail_event(source_hash):
    credentials = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        TOKEN.write_text(credentials.to_json())
        os.chmod(TOKEN, 0o600)
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    config = load_gmail_ingest_config()
    response = service.users().messages().list(userId="me", q=config["query"], maxResults=100).execute()
    for pointer in response.get("messages", []):
        if hashlib.sha256(pointer["id"].encode()).hexdigest() != source_hash:
            continue
        item = service.users().messages().get(userId="me", id=pointer["id"], format="full").execute()
        received = datetime.fromtimestamp(int(item["internalDate"]) / 1000, timezone.utc).isoformat()
        return parse_airbnb_confirmation({
            "id": pointer["id"],
            "subject": header(item["payload"], "Subject"),
            "email_ts": received,
            "body": decoded_message_text(item["payload"]),
        })
    raise ValueError("source Gmail message not found")


def run(changeset_path, approval_path, notion_path, original_event_path):
    now = datetime.now(timezone.utc)
    changeset = json.loads(changeset_path.read_text())
    approval = json.loads(approval_path.read_text())
    notion = json.loads(notion_path.read_text())
    original = json.loads(original_event_path.read_text())
    fresh = fresh_gmail_event(changeset["source_message_hash"])
    gmail_differences = {field: {"original": original.get(field), "fresh": fresh.get(field)} for field in COMPARE_FIELDS if original.get(field) != fresh.get(field)}
    notion_age = (now - datetime.fromisoformat(notion["queried_at"].replace("Z", "+00:00"))).total_seconds()
    checks = {
        "telegram_approval_is_valid_test": approval.get("status") == "APPROVED_TEST" and approval.get("consumed") is True,
        "approval_matches_changeset": approval.get("changeset_hash") == hashlib.sha256(changeset_path.read_bytes()).hexdigest(),
        "gmail_source_found": True,
        "gmail_fields_unchanged": not gmail_differences,
        "notion_snapshot_is_fresh": 0 <= notion_age <= 300,
        "notion_still_has_no_match": notion.get("matches") == [] and notion.get("has_more") is False,
        "changeset_is_new": changeset.get("classification") == "NEW",
        "external_writes_are_disabled": changeset.get("external_writes") == 0 and approval.get("external_writes_executed") == 0,
    }
    passed = all(checks.values())
    result = {
        "schema_version": 1,
        "run_at": now.isoformat(),
        "status": "PASS_NO_WRITE" if passed else "BLOCKED",
        "checks": checks,
        "gmail_differences": gmail_differences,
        "notion_snapshot_age_seconds": round(notion_age, 3),
        "proposed_operation": "CREATE_RESERVATION" if passed else None,
        "proposed_notion_properties": changeset.get("proposed_notion_properties") if passed else None,
        "secure_booking_reference_verified_in_memory": fresh.get("external_booking_ref_hash") == changeset.get("external_booking_ref_hash"),
        "raw_gmail_body_stored": False,
        "raw_booking_reference_stored": False,
        "write_ready": False,
        "remaining_blockers": ["explicit production write approval", "same-transaction Gmail and Notion recheck"],
        "external_writes": 0,
    }
    target = OUTPUT_DIR / f"{changeset['source_message_hash'][:16]}.json"
    atomic_private(target, result)
    print(json.dumps({"status": result["status"], "checks_passed": sum(checks.values()), "checks_total": len(checks), "write_ready": False, "external_writes": 0, "saved": str(target)}, ensure_ascii=False))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("changeset", type=Path)
    parser.add_argument("approval", type=Path)
    parser.add_argument("notion_snapshot", type=Path)
    parser.add_argument("original_event", type=Path)
    args = parser.parse_args()
    result = run(args.changeset, args.approval, args.notion_snapshot, args.original_event)
    if result["status"] != "PASS_NO_WRITE":
        raise SystemExit(2)


if __name__ == "__main__":
    main()

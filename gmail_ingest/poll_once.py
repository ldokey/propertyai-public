#!/usr/bin/env python3
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from propertyai_core.global_writer import publish_startup_runtime_identity

try:
    from gmail_ingest.airbnb_parser import parse_airbnb_event
    from gmail_ingest.mime_utils import decoded_message_text
    from gmail_ingest.runtime_config import load_gmail_ingest_config
except ModuleNotFoundError:
    from airbnb_parser import parse_airbnb_event
    from mime_utils import decoded_message_text
    from runtime_config import load_gmail_ingest_config


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "gmail_ingest" / "runtime" / "state.json"
SHADOW_DIR = ROOT / "gmail_ingest" / "runtime" / "shadow"
RECONCILE_QUEUE_DIR = ROOT / "gmail_ingest" / "runtime" / "reconcile_queue"
PENDING_CHANGES_DIR = ROOT / "gmail_ingest" / "runtime" / "pending_changes"
TOKEN_PATH = ROOT / "secrets" / "google" / "token.json"
# This token is shared with the booking bridge, which creates and reconciles
# cleaning calendar events.  Loading and then refreshing it with a Gmail-only
# scope would serialize a reduced token and silently remove Calendar access.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]


def atomic_private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def header(payload, name):
    name = name.casefold()
    for item in payload.get("headers", []):
        if item.get("name", "").casefold() == name:
            return item.get("value", "")
    return ""


def load_credentials():
    credentials = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        TOKEN_PATH.write_text(credentials.to_json())
        os.chmod(TOKEN_PATH, 0o600)
    if not credentials.valid:
        raise RuntimeError("Gmail OAuth token is invalid; run oauth_connect.py")
    return credentials


def poll():
    config = load_gmail_ingest_config()
    state = json.loads(STATE_PATH.read_text())
    processed = set(state.get("processed_message_hashes", []))
    service = build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)
    response = service.users().messages().list(
        userId="me", q=config["query"], maxResults=config["max_results_per_poll"]
    ).execute()
    discovered = parsed = review = observed = historical = skipped = 0
    newest_ms = 0
    newest_hash = state.get("last_message_hash")
    for pointer in reversed(response.get("messages", [])):
        message_hash = hashlib.sha256(pointer["id"].encode()).hexdigest()
        if message_hash in processed:
            skipped += 1
            continue
        discovered += 1
        item = service.users().messages().get(userId="me", id=pointer["id"], format="full").execute()
        payload = item["payload"]
        received_ms = int(item["internalDate"])
        received_at = datetime.fromtimestamp(received_ms / 1000, timezone.utc).isoformat()
        envelope = {
            "id": pointer["id"],
            "subject": header(payload, "Subject"),
            "template": header(payload, "X-Template"),
            "email_ts": received_at,
            "body": decoded_message_text(payload),
        }
        try:
            result = parse_airbnb_event(envelope)
        except Exception as exc:
            result = {
                "schema_version": 2,
                "source": "GMAIL_AIRBNB",
                "event_type": "UNKNOWN",
                "source_message_hash": message_hash,
                "received_at": received_at,
                "pii_stored": False,
                "raw_body_stored": False,
                "status": "REVIEW_REQUIRED",
                "error_type": type(exc).__name__,
                "external_writes": 0,
            }
        result["external_writes"] = 0
        lifecycle_cutoff = config.get("lifecycle_event_start_at")
        historical_lifecycle = (
            result.get("event_type") != "BOOKING_CONFIRMED"
            and lifecycle_cutoff
            and datetime.fromisoformat(received_at) < datetime.fromisoformat(lifecycle_cutoff)
        )
        if historical_lifecycle:
            result["status"] = "HISTORICAL_SKIPPED"
        timestamp = received_at.replace("-", "").replace(":", "").replace("+0000", "Z")
        atomic_private_json(SHADOW_DIR / f"{timestamp}-{message_hash[:12]}.json", result)
        if result["status"] == "OBSERVED_NO_WRITE" and result.get("event_type") == "BOOKING_CHANGE_REQUESTED":
            atomic_private_json(PENDING_CHANGES_DIR / f"{message_hash[:16]}.json", {
                "schema_version": 1,
                "status": "PENDING_CONFIRMATION",
                "source_message_hash": result["source_message_hash"],
                "actor_ref_hash": result.get("actor_ref_hash"),
                "listing_ref_hash": result.get("listing_ref_hash"),
                "alteration_ref_hash": result.get("alteration_ref_hash"),
                "original_adults": result.get("original_adults"),
                "requested_adults": result.get("requested_adults"),
                "received_at": result["received_at"],
                "pii_stored": False,
                "raw_body_stored": False,
            })
        if result["status"] == "PARSED":
            atomic_private_json(RECONCILE_QUEUE_DIR / f"{message_hash[:16]}.json", {
                "schema_version": 2,
                "status": "PENDING_NOTION_READ",
                "event_type": result["event_type"],
                "source_message_hash": result["source_message_hash"],
                "external_booking_ref_hash": result["external_booking_ref_hash"],
                "listing_id": result.get("listing_id"),
                "check_in": result.get("check_in"),
                "check_out": result.get("check_out"),
                "pii_stored": False,
                "raw_body_stored": False,
                "external_writes": 0,
            })
        processed.add(message_hash)
        parsed += result["status"] == "PARSED"
        review += result["status"] == "REVIEW_REQUIRED"
        observed += result["status"] == "OBSERVED_NO_WRITE"
        historical += result["status"] == "HISTORICAL_SKIPPED"
        if received_ms >= newest_ms:
            newest_ms, newest_hash = received_ms, message_hash

    state.update({
        "mode": config.get("mode", state.get("mode")),
        "last_poll_at": datetime.now(timezone.utc).isoformat(),
        "last_message_hash": newest_hash,
        "last_message_received_at": (
            datetime.fromtimestamp(newest_ms / 1000, timezone.utc).isoformat()
            if newest_ms else state.get("last_message_received_at")
        ),
        "processed_message_hashes": sorted(processed),
        "parsed": state.get("parsed", 0) + parsed,
        "review_required": state.get("review_required", 0) + review,
        "observed_no_write": state.get("observed_no_write", 0) + observed,
        "historical_skipped": state.get("historical_skipped", 0) + historical,
        "external_writes": 0,
    })
    atomic_private_json(STATE_PATH, state)
    summary = {
        "discovered": discovered,
        "parsed": parsed,
        "review_required": review,
        "observed_no_write": observed,
        "historical_skipped": historical,
        "skipped": skipped,
        "gmail_writes": 0,
        "downstream_writes_enabled": bool(config.get("automation_bridge_enabled")),
    }
    if config.get("automation_bridge_enabled"):
        try:
            from gmail_ingest.booking_bridge import process_pending
        except ModuleNotFoundError:
            from booking_bridge import process_pending
        summary["automation_bridge"] = process_pending()
    print(json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> None:
    # The Gmail poller is mutation-capable only when the automation bridge is on.
    # Publish runtime identity before Gmail/network work in that mode.
    config = load_gmail_ingest_config()
    if config.get("automation_bridge_enabled"):
        publish_startup_runtime_identity("W01")
    poll()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "gmail_ingest" / "runtime"


def atomic_private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def comparable(event):
    return {
        "listing_id": event.get("listing_id"),
        "check_in": event.get("check_in"),
        "check_out": event.get("check_out"),
        "adults": event.get("adults"),
        "currency": event.get("currency"),
        "guest_total": event.get("guest_total"),
        "host_payout": event.get("host_payout"),
    }


def classify(event, match_snapshot):
    matches = match_snapshot.get("matches", [])
    booking_matches = [row for row in matches if row.get("external_booking_ref_hash") == event.get("external_booking_ref_hash")]
    if booking_matches:
        changed = {key: {"before": booking_matches[0].get(key), "after": value}
                   for key, value in comparable(event).items()
                   if booking_matches[0].get(key) not in (None, value)}
        return ("UPDATE_CANDIDATE", changed) if changed else ("DUPLICATE", {})
    date_matches = [row for row in matches if row.get("listing_id") == event.get("listing_id")
                    and row.get("check_in") == event.get("check_in")
                    and row.get("check_out") == event.get("check_out")]
    if date_matches:
        return "REVIEW_REQUIRED", {"reason": "same listing and dates but booking reference is different or unavailable"}
    overlaps = [row for row in matches if row.get("listing_id") == event.get("listing_id") and
                row.get("check_in") < event.get("check_out") and event.get("check_in") < row.get("check_out")]
    if overlaps:
        return "REVIEW_REQUIRED", {"reason": "overlapping reservation exists"}
    return "NEW", {}


def build_changeset(event, match_snapshot):
    classification, differences = classify(event, match_snapshot)
    stable = f"{event['source_message_hash']}:{event['external_booking_ref_hash']}:{classification}"
    proposed = None
    if classification in ("NEW", "UPDATE_CANDIDATE"):
        proposed = {
            "플랫폼/채널": "Airbnb",
            "기록 유형": "Airbnb예약",
            "External Listing ID": event["listing_id"],
            "체크인": event["check_in"],
            "체크아웃": event["check_out"],
            "성인 수": event["adults"],
            "게스트 수": event["adults"],
            "통화": event["currency"],
            "표시 금액": event["guest_total"],
            "표시 금액 의미": "GUEST_TOTAL",
            "호스트 수령액": event["host_payout"],
            "상태": "확정",
            "등록 상태": "TEMPORARY",
            "데이터 환경": "PRODUCTION",
            "입력 출처": "OTHER",
            "날짜 해석 상태": "YEAR_INFERRED",
            "게스트 구성 해석 상태": "EXACT",
            "Idempotency Key": hashlib.sha256(stable.encode()).hexdigest(),
        }
    return {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "classification": classification,
        "source_message_hash": event["source_message_hash"],
        "external_booking_ref_hash": event["external_booking_ref_hash"],
        "notion_match_snapshot_hash": hashlib.sha256(json.dumps(match_snapshot, sort_keys=True).encode()).hexdigest(),
        "differences": differences,
        "proposed_notion_properties": proposed,
        "approval_required": classification in ("NEW", "UPDATE_CANDIDATE", "REVIEW_REQUIRED"),
        "write_ready": False,
        "write_blockers": ["operator approval", "fresh Gmail source re-read", "fresh Notion last_edited_time check"],
        "external_writes": 0,
    }


def main():
    parser = argparse.ArgumentParser(description="Create a no-write reservation ChangeSet")
    parser.add_argument("event", type=Path)
    parser.add_argument("matches", type=Path)
    args = parser.parse_args()
    event = json.loads(args.event.read_text())
    snapshot = json.loads(args.matches.read_text())
    changeset = build_changeset(event, snapshot)
    target = RUNTIME / "changesets" / f"{event['source_message_hash'][:16]}.json"
    atomic_private_json(target, changeset)
    print(json.dumps({"classification": changeset["classification"], "approval_required": changeset["approval_required"], "write_ready": False, "external_writes": 0, "saved": str(target)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

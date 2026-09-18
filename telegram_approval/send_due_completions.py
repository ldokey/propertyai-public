#!/usr/bin/env python3
"""Find due accepted cleanings and optionally send completion-report buttons."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from gmail_ingest.lifecycle_actions import _notion
from propertyai_core.global_writer import mutation_scope, publish_startup_runtime_identity
from propertyai_core.notion_resources import cleaning_source_id
from telegram_approval.assignment_history import (
    NotionAssignmentHistoryStore,
    accepted_compensation_snapshot,
    resolve_effective_accepted_assignment,
)
from telegram_approval.cleaner_registry import assignment_targets
from telegram_approval.cleaner_config import CleanerCredentialProvider, CleanerRuntimePaths
from telegram_approval.cleaning_completion import send_completion_request


ROOT = Path(__file__).resolve().parents[1]
MAPPINGS_PATH = ROOT / "gmail_ingest" / "listing_mappings.json"
REQUEST_DIR = CleanerRuntimePaths().request_dir


def _select(page, name):
    return (page.get("properties", {}).get(name, {}).get("select") or {}).get("name")


def _number(page, name):
    return page.get("properties", {}).get(name, {}).get("number")


def _date(page, name):
    return (page.get("properties", {}).get(name, {}).get("date") or {}).get("start")


def _relations(page, name):
    return [item["id"] for item in page.get("properties", {}).get(name, {}).get("relation", [])]


def _as_datetime(value):
    if not value:
        return None
    if len(value) == 10:
        return datetime.fromisoformat(value + "T23:59:59+09:00")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _accepted_compensation_for_completion(
    *, cleaning_page_id: str, cleaner_party_page_id: str, base_fee_krw: int, history_store=None
):
    evidence, history_page = resolve_effective_accepted_assignment(
        store=history_store or NotionAssignmentHistoryStore(),
        cleaning_page_id=cleaning_page_id,
        cleaner_party_page_id=cleaner_party_page_id,
    )
    economics = accepted_compensation_snapshot(
        history_page, legacy_base_fee_krw=base_fee_krw
    )
    if not economics:
        raise ValueError("accepted compensation snapshot is unavailable")
    return {
        **economics,
        "assignment_page_id": history_page.get("id"),
        "assignment_version": evidence.assignment_version,
        "accepted_assignment_action_id": evidence.action_id,
    }


def _pending_completion_exists(cleaning_page_id):
    now = datetime.now(timezone.utc)
    for path in REQUEST_DIR.glob("*.json"):
        record = json.loads(path.read_text())
        if (
            record.get("action_type") == "CLEANING_COMPLETION"
            and record.get("cleaning_page_id") == cleaning_page_id
            and record.get("status") in {"PENDING", "DELIVERY_UNCERTAIN"}
            and not record.get("consumed")
            and now <= datetime.fromisoformat(record["expires_at"])
        ):
            return True
    return False


def due_cleanings(now=None, *, history_store=None):
    now = now or datetime.now(ZoneInfo("Asia/Seoul"))
    pages = _notion("POST", f"/v1/data_sources/{cleaning_source_id()}/query", {
        "filter": {"and": [
            {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
            {"property": "배정 수락 상태", "select": {"equals": "수락"}},
        ]},
        "page_size": 100,
    }).get("results", [])
    mappings = json.loads(MAPPINGS_PATH.read_text())["listings"]
    by_property = {value["property_page_id"]: value for value in mappings.values()}
    due = []
    for page in pages:
        if _select(page, "상태") not in ("담당자 배정", "진행중"):
            continue
        target = _as_datetime(_date(page, "완료 목표"))
        if not target or target.astimezone(ZoneInfo("Asia/Seoul")) > now:
            continue
        if _pending_completion_exists(page["id"]):
            continue
        property_ids = _relations(page, "연결 집")
        mapping = next((by_property[value] for value in property_ids if value in by_property), None)
        if not mapping:
            continue
        candidates, _observers = assignment_targets(mapping["nickname"])
        assigned = set(_relations(page, "담당 참여자/업체"))
        candidate = next((item for item in candidates if item.get("party_page_id") in assigned), None)
        if not candidate:
            continue
        fee = _number(page, "청소비 Snapshot")
        if fee is None:
            continue
        economics = _accepted_compensation_for_completion(
            cleaning_page_id=page["id"],
            cleaner_party_page_id=candidate["party_page_id"],
            base_fee_krw=int(fee),
            history_store=history_store,
        )
        due.append({
            "page": page,
            "mapping": mapping,
            "candidate": candidate,
            "cleaning_date": (_date(page, "점검일") or target.date().isoformat())[:10],
            "fee": int(fee),
            "economics": economics,
        })
    return due


def _validate_send_role_config():
    # Resolve configuration without reading the credential file. Missing role
    # configuration must fail before scheduler discovery or request creation.
    CleanerCredentialProvider().credential_path()


def run(send=False):
    if send:
        from propertyai_core.runtime.cleaner_topology import assert_legacy_cleaner_writer_allowed
        assert_legacy_cleaner_writer_allowed("W05")
        _validate_send_role_config()
    due = due_cleanings()
    sent = []
    if send:
        for item in due:
            with mutation_scope(
                "W05",
                unit_id=f"{item['page']['id']}:COMPLETION",
                operation_class="CLEANING_COMPLETION_DISPATCH",
                target=f"cleaning:{item['page']['id']}",
            ):
                result = send_completion_request(
                    cleaning_page_id=item["page"]["id"],
                    property_nickname=item["mapping"]["nickname"],
                    address=item["mapping"]["address"],
                    cleaning_date=item["cleaning_date"],
                    cleaning_fee_krw=item["fee"],
                    candidate=item["candidate"],
                    test_mode=False,
                    expected_last_edited_time=item["page"]["last_edited_time"],
                    replacement_urgency=item["economics"]["replacement_urgency"],
                    urgent_premium_krw=item["economics"]["urgent_premium_krw"],
                    total_agreed_fee_krw=item["economics"]["total_agreed_fee_krw"],
                    urgent_premium_policy_version=item["economics"]["urgent_premium_policy_version"],
                    assignment_page_id=item["economics"].get("assignment_page_id"),
                    assignment_version=item["economics"].get("assignment_version"),
                    accepted_assignment_action_id=item["economics"].get("accepted_assignment_action_id"),
                )
                sent.append({"action_id": result.get("action_id"), "sent": result.get("sent")})
    return {"mode": "SEND" if send else "DRY_RUN", "due_count": len(due), "sent": sent}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()
    if args.send:
        publish_startup_runtime_identity("W05")
    print(json.dumps(run(send=args.send), ensure_ascii=False))


if __name__ == "__main__":
    main()

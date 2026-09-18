#!/usr/bin/env python3
"""Schedule accepted cleanings' day-of Telegram operation messages."""

from __future__ import annotations

import argparse
import json
import secrets
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from gmail_ingest.lifecycle_actions import _notion
from propertyai_core.notion_resources import cleaning_source_id
from propertyai_core.global_writer import (
    assert_current_production_writer,
    mutation_scope,
    publish_startup_runtime_identity,
)
from telegram_approval.cleaner_registry import assignment_targets
from telegram_approval.cleaning_operations import send_operation_stage
from telegram_approval.cleaner_config import CleanerCredentialProvider, CleanerRuntimePaths
from telegram_approval.outbound import cleaner_outbound_router, ops_outbound_router
from telegram_approval.send_approval import OPERATOR_PATH, api, atomic_private
from telegram_approval.ops_config import OpsAllowlistProvider, OpsCredentialProvider


ROOT = Path(__file__).resolve().parents[1]
MAPPINGS_PATH = ROOT / "gmail_ingest" / "listing_mappings.json"
KST = ZoneInfo("Asia/Seoul")
TOKEN_PATH = None
REQUEST_DIR = CleanerRuntimePaths().request_dir


def _select(page, name):
    return (page.get("properties", {}).get(name, {}).get("select") or {}).get("name")


def _date(page, name):
    return (page.get("properties", {}).get(name, {}).get("date") or {}).get("start")


def _relations(page, name):
    return [item["id"] for item in page.get("properties", {}).get(name, {}).get("relation", [])]


def _plain(page, name):
    prop = page.get("properties", {}).get(name, {})
    values = prop.get(prop.get("type"), [])
    return "".join(item.get("plain_text", "") for item in values) if isinstance(values, list) else ""


def _reservation_door_code(cleaning):
    reservation_ids = _relations(cleaning, "관련 Reservation")
    if len(reservation_ids) != 1:
        return None, "RESERVATION_RELATION_NOT_EXACTLY_ONE"
    reservation = _notion("GET", f"/v1/pages/{reservation_ids[0]}")
    if _select(reservation, "데이터 환경") != "PRODUCTION":
        return None, "RESERVATION_NOT_PRODUCTION"
    if _select(reservation, "등록 상태") != "APPROVED" or _select(reservation, "상태") != "확정":
        return None, "RESERVATION_NOT_ACTIVE"
    if _select(reservation, "출입정보 저장 상태") != "STORED":
        return None, "DOOR_CODE_NOT_STORED"
    value = _plain(reservation, "출입 코드")
    if value == "":
        return None, "DOOR_CODE_EMPTY"
    return value, None


def _records(cleaning_page_id, action_type):
    matches = []
    for path in REQUEST_DIR.glob("*.json"):
        record = json.loads(path.read_text())
        if (
            record.get("cleaning_page_id") == cleaning_page_id
            and record.get("action_type") == action_type
            and not record.get("test_mode")
        ):
            matches.append(record)
    return sorted(matches, key=lambda item: item.get("created_at", ""))


def _audit_message(*, action_type, cleaning_page_id, chat_id, text, recipient_role):
    existing = _records(cleaning_page_id, action_type)
    if existing:
        return {"sent": False, "reason": "ALREADY_RECORDED", "action_id": existing[-1]["action_id"]}
    action_id = secrets.token_urlsafe(8)
    now = datetime.now(timezone.utc).isoformat()
    record = {
        "schema_version": 1,
        "action_id": action_id,
        "action_type": action_type,
        "cleaning_page_id": cleaning_page_id,
        "status": "PENDING_DELIVERY",
        "created_at": now,
        "test_mode": False,
        "consumed": True,
        "external_writes_executed": 0,
    }
    path = REQUEST_DIR / f"{action_id}.json"
    atomic_private(path, record)
    assert_current_production_writer()
    try:
        if recipient_role == "CLEANER":
            sent = cleaner_outbound_router(api, token_path=TOKEN_PATH).send_message(chat_id, text)
        elif recipient_role == "OPS":
            sent = ops_outbound_router(
                api,
                token_path=TOKEN_PATH,
                synthetic_allowed_chat_ids=(chat_id,) if TOKEN_PATH else None,
            ).send_message(chat_id, text)
        else:
            raise ValueError("recipient_role must be CLEANER or OPS")
    except Exception as error:
        assert_current_production_writer()
        record.update({"status": "DELIVERY_UNCERTAIN", "delivery_error_type": type(error).__name__})
        atomic_private(path, record)
        raise
    assert_current_production_writer()
    record.update({"status": "DELIVERED", "telegram_message_id": sent["message_id"], "delivered_at": now})
    atomic_private(path, record)
    return {"sent": True, "action_id": action_id, "telegram_message_id": sent["message_id"]}


def _service_date(page):
    value = _date(page, "점검일") or _date(page, "시작 예정")
    return value[:10] if value else None


def _candidate_for(page, nickname):
    candidates, _observers = assignment_targets(nickname)
    assigned = set(_relations(page, "담당 참여자/업체"))
    return next((item for item in candidates if item.get("party_page_id") in assigned), None)


def due_events(now=None):
    now = now or datetime.now(KST)
    pages = _notion("POST", f"/v1/data_sources/{cleaning_source_id()}/query", {
        "filter": {"and": [
            {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
            {"property": "배정 수락 상태", "select": {"equals": "수락"}},
        ]},
        "page_size": 100,
    }).get("results", [])
    mappings = json.loads(MAPPINGS_PATH.read_text())["listings"]
    by_property = {value["property_page_id"]: value for value in mappings.values()}
    events = []
    for page in pages:
        if _select(page, "상태") not in ("담당자 배정", "진행중"):
            continue
        if _service_date(page) != now.date().isoformat():
            continue
        mapping = next((by_property[item] for item in _relations(page, "연결 집") if item in by_property), None)
        if not mapping:
            continue
        candidate = _candidate_for(page, mapping["nickname"])
        if not candidate:
            continue
        start_at = _date(page, "시작 예정") or f"{now.date().isoformat()}T11:00:00+09:00"
        end_at = _date(page, "완료 목표") or f"{now.date().isoformat()}T15:00:00+09:00"
        day_records = _records(page["id"], "CLEANING_DAY_CONFIRM")
        day = day_records[-1] if day_records else None
        local_time = now.timetz().replace(tzinfo=None)
        common = {"page": page, "mapping": mapping, "candidate": candidate, "start_at": start_at, "end_at": end_at}
        if local_time >= time(8, 30) and not day:
            events.append({"event": "DAY_CONFIRM", **common})
            continue
        if day and day.get("status") == "PENDING" and not day.get("consumed"):
            if local_time >= time(9, 20) and not _records(page["id"], "CLEANING_DAY_ESCALATION"):
                events.append({"event": "DAY_ESCALATION", **common})
            elif local_time >= time(9, 0) and not _records(page["id"], "CLEANING_DAY_RECONFIRM"):
                events.append({"event": "DAY_RECONFIRM", **common})
        confirmed = (
            day
            and day.get("status") == "EXECUTED"
            and day.get("execution_result", {}).get("cleaning_operation") == "DAY_VISIT_CONFIRMED"
        )
        if local_time >= time(10, 20) and confirmed and not _records(page["id"], "CLEANING_ARRIVAL"):
            door_code, block_reason = _reservation_door_code(page)
            if door_code is None:
                if not _records(page["id"], "CLEANING_DOOR_CODE_REVIEW"):
                    events.append({"event": "DOOR_CODE_REVIEW", "block_reason": block_reason, **common})
            else:
                events.append({"event": "ARRIVAL", "door_code": door_code, **common})
    return events


def _validate_send_role_config():
    # This scheduler can address both Cleaner and OPS recipients. Resolve all
    # role configuration before any audit record or outbound side effect.
    CleanerCredentialProvider().credential_path()
    OpsCredentialProvider().credential_path()
    OpsAllowlistProvider().allowed_chat_ids()


def run(send=False, now=None):
    if send:
        from propertyai_core.runtime.cleaner_topology import assert_legacy_cleaner_writer_allowed
        assert_legacy_cleaner_writer_allowed("W04")
        _validate_send_role_config()
    events = due_events(now=now)
    results = []
    if send:
        operator = json.loads(OPERATOR_PATH.read_text())
        for item in events:
            page, mapping, candidate = item["page"], item["mapping"], item["candidate"]
            with mutation_scope(
                "W04",
                unit_id=f"{page['id']}:{item['event']}",
                operation_class="CLEANING_OPERATIONS_DISPATCH",
                target=f"cleaning:{page['id']}",
            ):
                if item["event"] in ("DAY_CONFIRM", "ARRIVAL"):
                    result = send_operation_stage(
                        stage=item["event"],
                        cleaning_page_id=page["id"],
                        property_nickname=mapping["nickname"],
                        address=mapping["address"],
                        start_at=item["start_at"],
                        end_at=item["end_at"],
                        candidate=candidate,
                        test_mode=False,
                        expected_last_edited_time=page["last_edited_time"],
                        door_code=item.get("door_code"),
                    )
                elif item["event"] == "DAY_RECONFIRM":
                    result = _audit_message(
                        action_type="CLEANING_DAY_RECONFIRM",
                        cleaning_page_id=page["id"],
                        chat_id=candidate["telegram_chat_id"],
                        recipient_role="CLEANER",
                        text=(f"⏰ 오늘 청소 방문 여부를 아직 확인하지 못했습니다.\n숙소: {mapping['nickname']}\n"
                              "앞서 받은 메시지에서 `오늘 갑니다` 또는 `방문이 어렵습니다`를 선택해주세요."),
                    )
                elif item["event"] == "DAY_ESCALATION":
                    result = _audit_message(
                        action_type="CLEANING_DAY_ESCALATION",
                        cleaning_page_id=page["id"],
                        chat_id=operator["telegram_chat_id"],
                        recipient_role="OPS",
                        text=(f"⚠️ 09:20 청소 방문 미응답 경고\n숙소: {mapping['nickname']}\n"
                              f"주소: {mapping['address']}\n담당자 응답이 없어 확인 또는 대체 배정이 필요합니다."),
                    )
                else:
                    result = _audit_message(
                        action_type="CLEANING_DOOR_CODE_REVIEW",
                        cleaning_page_id=page["id"],
                        chat_id=operator["telegram_chat_id"],
                        recipient_role="OPS",
                        text=(f"⚠️ 10:20 도어락 코드 발송 보류\n숙소: {mapping['nickname']}\n"
                              f"주소: {mapping['address']}\n사유: {item.get('block_reason')}\n"
                              "예약 출입 코드 저장 상태와 Cleaning 연결을 확인해주세요."),
                    )
                results.append({"event": item["event"], **result})
    return {"mode": "SEND" if send else "DRY_RUN", "due_count": len(events), "events": [item["event"] for item in events], "results": results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()
    if args.send:
        publish_startup_runtime_identity("W04")
    print(json.dumps(run(send=args.send), ensure_ascii=False))


if __name__ == "__main__":
    main()

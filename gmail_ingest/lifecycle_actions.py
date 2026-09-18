#!/usr/bin/env python3
"""Approved, audit-preserving reservation lifecycle mutations."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from propertyai_core.global_writer import assert_current_production_writer
from telegram_approval.outbound import cleaner_outbound_router
from telegram_approval.send_approval import api as telegram_api
from telegram_approval.cleaner_performance import payout_amount_from_record
from telegram_approval.assignment_history import (
    AssignmentHistoryError,
    NotionAssignmentHistoryStore,
    end_effective_assignment_for_cancellation,
    resolve_effective_accepted_assignment,
)


ROOT = Path(__file__).resolve().parents[1]
NOTION_TOKEN_PATH = ROOT / "secrets" / "notion" / "token"
GOOGLE_TOKEN_PATH = ROOT / "secrets" / "google" / "token.json"
NOTION_VERSION = "2026-03-11"
FINANCE_SOURCE = "8f0231dc-00b1-4397-b005-72b818ab826c"
LABOR_SOURCE = "ec0273ee-2e61-4481-a2cb-1581218b7ac8"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar"]
TELEGRAM_TOKEN_PATH = None


def _notion(method, path, body=None):
    if method != "GET" and not (method == "POST" and path.endswith("/query")):
        assert_current_production_writer()
    request = urllib.request.Request(
        "https://api.notion.com" + path,
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN_PATH.read_text().strip()}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.loads(response.read())


def _plain(prop):
    values = prop.get(prop.get("type"), []) if prop else []
    if isinstance(values, list):
        return "".join(item.get("plain_text", "") for item in values)
    return ""


def _relations(page, property_name):
    return [item["id"] for item in page.get("properties", {}).get(property_name, {}).get("relation", [])]


def _append_history(page, line):
    current = _plain(page.get("properties", {}).get("Change History"))
    combined = f"{current}\n{line}".strip()
    return {"rich_text": [{"type": "text", "text": {"content": combined[-1900:]}}]}


def _finance_cancellation_review(record, reservation):
    key = f"RSV-FIN-CANCEL::{record['reservation_code']}"
    matches = _notion("POST", f"/v1/data_sources/{FINANCE_SOURCE}/query", {
        "filter": {"property": "Idempotency Key", "rich_text": {"equals": key}},
        "page_size": 5,
    }).get("results", [])
    if matches:
        return {"page_id": matches[0]["id"], "outcome": "EXISTING"}
    currency = (reservation.get("properties", {}).get("통화", {}).get("select") or {}).get("name")
    refund_scope = record.get("refund_scope", "UNKNOWN")
    memo = (
        f"Airbnb 예약 취소 조정 확인 필요. refund_scope={refund_scope}. "
        "게스트 환불액과 호스트 정산 영향은 동일하다고 추정하지 않으며 플랫폼 정산서 확인 후 금액을 입력한다."
    )
    properties = {
        "정산 항목명": {"title": [{"type": "text", "text": {"content": f"{record['reservation_code']} · 예약 취소 조정 확인"}}]},
        "상태": {"select": {"name": "확인필요"}},
        "기록 유형": {"select": {"name": "기타"}},
        "항목 유형": {"select": {"name": "숙박비"}},
        "결제수단": {"select": {"name": "플랫폼정산"}},
        "세무 성격": {"select": {"name": "CONTRA_REVENUE"}},
        "정산 세부 유형": {"select": {"name": "OTHER"}},
        "발생일": {"date": {"start": record.get("cancellation_received_at") or datetime.now(timezone.utc).isoformat()}},
        "데이터 환경": {"select": {"name": "PRODUCTION"}},
        "관련 Reservation": {"relation": [{"id": record["reservation_page_id"]}]},
        "Reservation Finance Group Key": {"rich_text": [{"type": "text", "text": {"content": f"RSV-FIN::{record['reservation_code']}"}}]},
        "Idempotency Key": {"rich_text": [{"type": "text", "text": {"content": key}}]},
        "Source Message Ref": {"rich_text": [{"type": "text", "text": {"content": "sha256:" + record["source_message_hash"]}}]},
        "Source Project Code": {"rich_text": [{"type": "text", "text": {"content": "LOCAL.PROPERTYAI.GMAIL"}}]},
        "External Transaction ID": {"rich_text": [{"type": "text", "text": {"content": record["reservation_code"]}}]},
        "원본 항목명 Snapshot": {"rich_text": [{"type": "text", "text": {"content": "Airbnb reservation cancellation notice"}}]},
        "거래번호/메모": {"rich_text": [{"type": "text", "text": {"content": memo}}]},
        "OPS Explicit Block Reason": {"rich_text": [{"type": "text", "text": {"content": "HOST_PAYOUT_OR_REFUND_AMOUNT_RECONCILIATION_REQUIRED"}}]},
        "Status Lock": {"checkbox": True},
    }
    if currency:
        properties["통화"] = {"select": {"name": currency}}
    houses = _relations(reservation, "연결 집")
    units = _relations(reservation, "연결 운영상품")
    if houses:
        properties["연결 집"] = {"relation": [{"id": value} for value in houses]}
    if units:
        properties["연결 운영상품"] = {"relation": [{"id": value} for value in units]}
    created = _notion("POST", "/v1/pages", {
        "parent": {"type": "data_source_id", "data_source_id": FINANCE_SOURCE},
        "properties": properties,
    })
    return {"page_id": created["id"], "outcome": "CREATED_REVIEW_REQUIRED"}


def _send_cleaner_cancellation(record):
    summary = record.get("summary", {})
    text = (
        "🚫 청소 일정 취소 안내\n\n"
        f"숙소: {summary.get('nickname', '확인 필요')}\n"
        f"체크인: {summary.get('check_in', 'N/A')}\n"
        f"체크아웃/기존 청소일: {summary.get('check_out', 'N/A')}\n"
        f"인원: {summary.get('guests', 'N/A')}\n\n"
        "게스트 예약이 취소되어 해당 청소 일정도 취소되었습니다. 별도 진행하지 마세요."
    )
    sent = []
    router = cleaner_outbound_router(telegram_api, token_path=TELEGRAM_TOKEN_PATH)
    for chat_id in record.get("cleaner_delivery_chat_ids", []):
        assert_current_production_writer()
        message = router.send_message(chat_id, text)
        sent.append({"chat_ref": "OPERATOR_AS_CLEANER_TEST", "message_id": message["message_id"]})
    return sent


def _select_name(page, property_name):
    return (page.get("properties", {}).get(property_name, {}).get("select") or {}).get("name")


def _number(page, property_name):
    return page.get("properties", {}).get(property_name, {}).get("number")


def _date_start(page, property_name):
    return (page.get("properties", {}).get(property_name, {}).get("date") or {}).get("start")


def _query_idempotency(data_source_id, key):
    return _notion("POST", f"/v1/data_sources/{data_source_id}/query", {
        "filter": {"property": "Idempotency Key", "rich_text": {"equals": key}},
        "page_size": 5,
    }).get("results", [])


def validate_cleaner_unavailable(record):
    """Fresh fail-closed read for an accepted Cleaner assignment before ending 19."""

    page_id = record["cleaning_page_id"]
    page = _notion("GET", f"/v1/pages/{page_id}")
    if _select_name(page, "데이터 환경") != "PRODUCTION":
        raise ValueError("cleaning environment does not allow unavailable")
    if _select_name(page, "등록 상태") != "APPROVED":
        raise ValueError("cleaning registration does not allow unavailable")
    if _select_name(page, "상태") != "담당자 배정":
        raise ValueError("cleaning has started, completed, or been cancelled")
    if _select_name(page, "배정 수락 상태") != "수락":
        raise ValueError("cleaning is no longer accepted by this assignee")
    assignment_state = _select_name(page, "배정 상태")
    if assignment_state not in (None, "HARD_BOOKED"):
        raise ValueError("cleaning assignment binding is no longer HARD_BOOKED")
    expected = record.get("expected_cleaning_last_edited_time")
    if not expected or page.get("last_edited_time") != expected:
        raise ValueError("cleaning page changed; stale unavailable action rejected")
    expected_party = record.get("cleaner_party_page_id")
    assigned = _relations(page, "담당 참여자/업체")
    if not expected_party or assigned != [expected_party]:
        raise ValueError("cleaning assignee changed; stale unavailable action rejected")
    reservation_ids = _relations(page, "관련 Reservation")
    if len(reservation_ids) != 1:
        raise ValueError("cleaning reservation binding is not exact")
    reservation = _notion("GET", f"/v1/pages/{reservation_ids[0]}")
    if _select_name(reservation, "데이터 환경") != "PRODUCTION":
        raise ValueError("reservation environment does not allow unavailable")
    if _select_name(reservation, "등록 상태") != "APPROVED":
        raise ValueError("reservation registration does not allow unavailable")
    if _select_name(reservation, "상태") != "확정":
        raise ValueError("reservation is no longer active")
    return {
        "cleaning": page,
        "reservation": reservation,
        "reservation_page_id": reservation_ids[0],
        "last_edited_time": page.get("last_edited_time"),
    }


def execute_cleaner_unavailable_projection(record):
    """Converge mutable Cleaning 08 after durable 19 termination.

    A PATCH response can be lost after Notion committed it.  A retry therefore
    recognizes the exact already-projected state instead of trying to resurrect
    or revalidate the ended Assignment as still active.
    """

    page = _notion("GET", f"/v1/pages/{record['cleaning_page_id']}")
    if (
        _select_name(page, "데이터 환경") == "PRODUCTION"
        and _select_name(page, "등록 상태") == "APPROVED"
        and _select_name(page, "상태") == "담당자 배정"
        and _select_name(page, "배정 수락 상태") == "대체필요"
        and _relations(page, "담당 참여자/업체") == []
    ):
        reservation_ids = _relations(page, "관련 Reservation")
        if len(reservation_ids) != 1:
            raise ValueError("projected cleaning reservation binding is not exact")
        reservation = _notion("GET", f"/v1/pages/{reservation_ids[0]}")
        if (
            _select_name(reservation, "데이터 환경") != "PRODUCTION"
            or _select_name(reservation, "등록 상태") != "APPROVED"
            or _select_name(reservation, "상태") != "확정"
        ):
            raise ValueError("projected cleaning reservation is no longer active")
        return {
            "cleaner_unavailable_projection": "REPLACEMENT_REQUIRED",
            "next_expected_last_edited_time": page.get("last_edited_time"),
            "reservation_page_id": reservation_ids[0],
            "projection_write": "ALREADY_CONVERGED",
        }

    current = validate_cleaner_unavailable(record)
    page = current["cleaning"]
    now = record.get("transition_at") or datetime.now(timezone.utc).isoformat()
    memo = _plain(page.get("properties", {}).get("점검 메모"))
    line = f"[{now}] 확정 담당자 진행 불가 · 대체 담당자 확인 필요"
    updated = _notion("PATCH", f"/v1/pages/{record['cleaning_page_id']}", {
        "properties": {
            "배정 수락 상태": {"select": {"name": "대체필요"}},
            "담당 참여자/업체": {"relation": []},
            "Sync Status": {"select": {"name": "NEEDS_SYNC"}},
            "점검 메모": {
                "rich_text": [{"type": "text", "text": {"content": f"{memo}\n{line}".strip()[-1900:]}}]
            },
        }
    })
    return {
        "cleaner_unavailable_projection": "REPLACEMENT_REQUIRED",
        "next_expected_last_edited_time": updated.get("last_edited_time"),
        "reservation_page_id": current["reservation_page_id"],
        "projection_write": "UPDATED",
    }


def execute_cleaning_assignment(record, decision):
    page_id = record["cleaning_page_id"]
    page = _notion("GET", f"/v1/pages/{page_id}")
    current = _select_name(page, "배정 수락 상태")
    if current != record.get("expected_acceptance_status", "수락대기"):
        raise ValueError("cleaning assignment state changed; stale action rejected")
    if record.get("expected_last_edited_time") and page.get("last_edited_time") != record["expected_last_edited_time"]:
        raise ValueError("cleaning page changed; stale action rejected")
    now = datetime.now(timezone.utc).isoformat()
    memo = _plain(page.get("properties", {}).get("점검 메모"))
    if decision == "approve":
        line = f"[{now}] Telegram 청소 제안 수락 · actor=OPERATOR_AS_CLEANER_TEST"
        properties = {
            "상태": {"select": {"name": "담당자 배정"}},
            "배정 수락 상태": {"select": {"name": "수락"}},
            "배정 상태": {"select": {"name": "HARD_BOOKED"}},
            "점검 메모": {"rich_text": [{"type": "text", "text": {"content": f"{memo}\n{line}".strip()[-1900:]}}]},
            "Sync Status": {"select": {"name": "NEEDS_SYNC"}},
            "청소 대금 상태": {"select": {"name": "미생성"}},
        }
        if record.get("candidate_party_page_id"):
            properties["담당 참여자/업체"] = {"relation": [{"id": record["candidate_party_page_id"]}]}
        outcome = "ACCEPTED"
    elif decision == "reject":
        has_next = bool(record.get("remaining_candidates"))
        reason = record.get("assignment_response_reason", "DECLINED")
        line = (
            f"[{now}] Telegram 청소 제안 {reason} · "
            + ("다음 우선순위 담당자에게 자동 제안" if has_next else "대체 담당자 필요")
        )
        properties = {
            "상태": {"select": {"name": "담당자 배정"}},
            "배정 수락 상태": {"select": {"name": "수락대기" if has_next else "대체필요"}},
            "점검 메모": {"rich_text": [{"type": "text", "text": {"content": f"{memo}\n{line}".strip()[-1900:]}}]},
            "Sync Status": {"select": {"name": "NEEDS_SYNC"}},
            "청소 대금 상태": {"select": {"name": "미생성"}},
        }
        outcome = "NEXT_CANDIDATE_REQUIRED" if has_next else "REPLACEMENT_REQUIRED"
    else:
        raise ValueError("unsupported assignment decision")
    updated = _notion("PATCH", f"/v1/pages/{page_id}", {"properties": properties})
    return {
        "cleaning_assignment": outcome,
        "payment_effects": 0,
        "next_expected_last_edited_time": updated.get("last_edited_time"),
    }


def execute_cleaning_completion(record, decision):
    if decision != "approve":
        return {"cleaning_completion": "NOT_COMPLETED_NO_WRITES"}
    page_id = record["cleaning_page_id"]
    page = _notion("GET", f"/v1/pages/{page_id}")
    if _select_name(page, "배정 수락 상태") != "수락":
        raise ValueError("cleaning is not accepted by an assignee")
    if _select_name(page, "상태") not in ("담당자 배정", "진행중"):
        raise ValueError("cleaning state does not allow completion report")
    expected = record.get("expected_last_edited_time")
    if not expected or page.get("last_edited_time") != expected:
        raise ValueError("cleaning page changed; stale completion action rejected")
    cleaning_date = _date_start(page, "점검일") or record.get("cleaning_date")
    today_kst = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    if not cleaning_date or cleaning_date[:10] > today_kst:
        raise ValueError("future cleaning cannot be completed")
    configured_fee = _number(page, "청소비 Snapshot")
    if configured_fee is None or int(configured_fee) != int(record["cleaning_fee_krw"]):
        raise ValueError("cleaning fee changed; stale completion action rejected")
    now = datetime.now(timezone.utc).isoformat()
    memo = _plain(page.get("properties", {}).get("점검 메모"))
    line = f"[{now}] Telegram 청소 완료·특이사항 없음 보고"
    updated = _notion("PATCH", f"/v1/pages/{page_id}", {"properties": {
        "상태": {"select": {"name": "완료 보고"}},
        "완료일": {"date": {"start": now}},
        "현장 보고 상태": {"select": {"name": "특이사항없음"}},
        "현장 보고 제출일": {"date": {"start": now}},
        "청소 대금 상태": {"select": {"name": "미생성"}},
        "정산 반영 여부": {"checkbox": False},
        "Sync Status": {"select": {"name": "NEEDS_SYNC"}},
        "점검 메모": {"rich_text": [{"type": "text", "text": {"content": f"{memo}\n{line}".strip()[-1900:]}}]},
    }})
    return {
        "cleaning_completion": "REPORTED",
        "payment_effects": 0,
        "next_expected_last_edited_time": updated.get("last_edited_time"),
    }


def execute_cleaning_completion_submission(record, decision):
    if decision != "approve":
        return {"cleaning_completion": "PHOTO_SUBMISSION_CANCELLED_NO_WRITES"}
    from drive_archive.issue_evidence import archive_completion_photos
    from telegram_approval.cleaning_completion_evidence import load_session, save_session

    session = load_session(record.get("completion_session_id"))
    if not session or session.get("status") != "OPEN" or len(session.get("photos", [])) < 4:
        raise ValueError("completion session requires at least four open photos")
    page_id = record["cleaning_page_id"]
    page = _notion("GET", f"/v1/pages/{page_id}")
    if _select_name(page, "배정 수락 상태") != "수락":
        raise ValueError("cleaning is not accepted by an assignee")
    if _select_name(page, "데이터 환경") != "PRODUCTION" or _select_name(page, "등록 상태") != "APPROVED":
        raise ValueError("completion photo submission requires an approved production cleaning")
    if _select_name(page, "상태") not in ("담당자 배정", "진행중"):
        raise ValueError("cleaning state does not allow completion submission")
    expected = record.get("expected_last_edited_time")
    if not expected or page.get("last_edited_time") != expected:
        raise ValueError("cleaning page changed; stale completion submission rejected")
    assigned = set(_relations(page, "담당 참여자/업체"))
    if record.get("candidate_party_page_id") and record["candidate_party_page_id"] not in assigned:
        raise ValueError("cleaning assignee changed; stale completion submission rejected")
    cleaning_date = _date_start(page, "점검일") or record.get("cleaning_date")
    today_kst = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    if not cleaning_date or cleaning_date[:10] > today_kst:
        raise ValueError("future cleaning cannot be completed")
    configured_fee = _number(page, "청소비 Snapshot")
    if configured_fee is None or int(configured_fee) != int(record["cleaning_fee_krw"]):
        raise ValueError("cleaning fee changed; stale completion submission rejected")

    evidence = archive_completion_photos(session, cleaning_date=cleaning_date)
    now = datetime.now(timezone.utc).isoformat()
    memo = _plain(page.get("properties", {}).get("점검 메모"))
    notes = "\n".join(item["text"] for item in session.get("notes", []))
    line = f"[{now}] Telegram 청소 완료 사진 제출 · Drive 검증 사진 {len(session['photos'])}장 · 운영자 검수 대기"
    existing_files = page.get("properties", {}).get("완료 사진", {}).get("files", [])
    known_urls = {
        (item.get("external") or {}).get("url")
        for item in existing_files if item.get("type") == "external"
    }
    drive_files = list(existing_files)
    for item in evidence["uploads"]:
        if item["drive_url"] not in known_urls:
            drive_files.append({
                "name": item["filename"], "type": "external",
                "external": {"url": item["drive_url"]},
            })
    file_ids = "\n".join(item["drive_file_id"] for item in evidence["uploads"])
    properties = {
        "상태": {"select": {"name": "완료 보고"}},
        "완료일": {"date": {"start": now}},
        "현장 보고 상태": {"select": {"name": "특이사항없음"}},
        "현장 보고 제출일": {"date": {"start": now}},
        "완료 사진": {"type": "files", "files": drive_files},
        "Drive File ID": {"rich_text": [{"type": "text", "text": {"content": file_ids[-1900:]}}]},
        "Drive Folder ID": {"rich_text": [{"type": "text", "text": {"content": evidence["cleaning_folder_id"]}}]},
        "Drive Folder URL": {"url": evidence["cleaning_folder_url"]},
        "사진 링크": {"url": evidence["completion_folder_url"]},
        "사진 분석 상태": {"select": {"name": "UPLOADED"}},
        "관리자 확인": {"checkbox": False},
        "청소 대금 상태": {"select": {"name": "미생성"}},
        "정산 반영 여부": {"checkbox": False},
        "Sync Status": {"select": {"name": "NEEDS_SYNC"}},
        "점검 메모": {"rich_text": [{"type": "text", "text": {"content": f"{memo}\n{line}".strip()[-1900:]}}]},
    }
    if notes:
        properties["사진 분석 요약"] = {"rich_text": [{"type": "text", "text": {"content": notes[-1900:]}}]}
    updated = _notion("PATCH", f"/v1/pages/{page_id}", {"properties": properties})
    session_evidence = session.setdefault("drive_evidence", evidence)
    session_evidence["notion_recorded"] = True
    session_evidence["notion_recorded_at"] = now
    save_session(session)
    return {
        "cleaning_completion": "PHOTOS_SUBMITTED_REVIEW_REQUIRED",
        "photo_count": len(session["photos"]),
        "drive_folder_id": evidence["cleaning_folder_id"],
        "completion_folder_url": evidence["completion_folder_url"],
        "next_expected_last_edited_time": updated.get("last_edited_time"),
    }


def execute_cleaning_evidence_review(record, decision):
    page_id = record["cleaning_page_id"]
    page = _notion("GET", f"/v1/pages/{page_id}")
    if _select_name(page, "상태") != "완료 보고":
        raise ValueError("completion report is required before operator review")
    if _select_name(page, "사진 분석 상태") != "UPLOADED":
        raise ValueError("verified completion photos are required before operator review")
    expected = record.get("expected_last_edited_time")
    if not expected or page.get("last_edited_time") != expected:
        raise ValueError("cleaning page changed; stale operator review rejected")
    now = datetime.now(timezone.utc).isoformat()
    memo = _plain(page.get("properties", {}).get("점검 메모"))
    if decision == "approve":
        outcome = "APPROVED_FOR_PAYMENT_CONFIRMATION"
        line = f"[{now}] 운영자 Telegram 완료 사진 검수 승인 · 이체 확인 가능"
        properties = {
            "상태": {"select": {"name": "관리자 확인 완료"}},
            "관리자 확인": {"checkbox": True},
            "Sync Status": {"select": {"name": "NEEDS_SYNC"}},
        }
    else:
        outcome = "REVISION_REQUIRED"
        line = f"[{now}] 운영자 Telegram 완료 사진 보완 요청 · 이체 확인 차단"
        properties = {
            "상태": {"select": {"name": "진행중"}},
            "현장 보고 상태": {"select": {"name": "보완요청"}},
            "관리자 확인": {"checkbox": False},
            "Sync Status": {"select": {"name": "NEEDS_SYNC"}},
        }
    properties["점검 메모"] = {"rich_text": [{"type": "text", "text": {"content": f"{memo}\n{line}".strip()[-1900:]}}]}
    updated = _notion("PATCH", f"/v1/pages/{page_id}", {"properties": properties})
    return {"cleaning_review": outcome, "next_expected_last_edited_time": updated.get("last_edited_time")}


def _assert_effective_assignment_for_work_start(record, history_store=None):
    """Fail closed if durable 19 no longer proves this Cleaner is effective."""

    cleaner_party_page_id = record.get("candidate_party_page_id")
    if not cleaner_party_page_id:
        raise ValueError("cleaning work-start assignment identity is missing")
    try:
        return resolve_effective_accepted_assignment(
            store=history_store or NotionAssignmentHistoryStore(),
            cleaning_page_id=record["cleaning_page_id"],
            cleaner_party_page_id=cleaner_party_page_id,
        )
    except AssignmentHistoryError as exc:
        raise ValueError("cleaning work-start assignment is no longer effective") from exc


def execute_cleaning_operation(record, decision):
    page_id = record["cleaning_page_id"]
    page = _notion("GET", f"/v1/pages/{page_id}")
    if _select_name(page, "배정 수락 상태") != "수락":
        raise ValueError("cleaning is not accepted by an assignee")
    if _select_name(page, "상태") not in ("담당자 배정", "진행중"):
        raise ValueError("cleaning state does not allow operation update")
    expected = record.get("expected_last_edited_time")
    if not expected or page.get("last_edited_time") != expected:
        raise ValueError("cleaning page changed; stale operation action rejected")
    assigned = set(_relations(page, "담당 참여자/업체"))
    if record.get("candidate_party_page_id") and record["candidate_party_page_id"] not in assigned:
        raise ValueError("cleaning assignee changed; stale operation action rejected")

    action_type = record.get("action_type")
    now = datetime.now(timezone.utc).isoformat()
    memo = _plain(page.get("properties", {}).get("점검 메모"))
    properties = {"Sync Status": {"select": {"name": "NEEDS_SYNC"}}}
    if action_type == "CLEANING_DAY_CONFIRM":
        if decision == "approve":
            line = f"[{now}] Telegram 청소 당일 방문 확인 · 오늘 갑니다"
            outcome = "DAY_VISIT_CONFIRMED"
        else:
            line = f"[{now}] Telegram 청소 당일 방문 불가 · 대체 담당자 필요"
            properties["배정 수락 상태"] = {"select": {"name": "대체필요"}}
            outcome = "REPLACEMENT_REQUIRED"
    elif action_type == "CLEANING_ARRIVAL":
        if decision == "approve":
            line = f"[{now}] Telegram 숙소 도착 확인"
            outcome = "ARRIVED"
        else:
            line = f"[{now}] Telegram 숙소 출입 문제 보고 · 운영자 확인 필요"
            properties["현장 보고 상태"] = {"select": {"name": "예외있음"}}
            properties["예외 상세"] = {"rich_text": [{"type": "text", "text": {"content": "숙소 도착·출입 문제 — Telegram에서 운영자 확인 필요"}}]}
            outcome = "ENTRY_PROBLEM_REPORTED"
    elif action_type == "CLEANING_START":
        if decision == "approve":
            line = f"[{now}] Telegram 현장 문제 없음 확인 · 청소 시작"
            properties["상태"] = {"select": {"name": "진행중"}}
            outcome = "IN_PROGRESS"
        else:
            line = f"[{now}] Telegram 현장 문제 보고 · 청소 전 운영자 확인 필요"
            properties["현장 보고 상태"] = {"select": {"name": "예외있음"}}
            properties["예외 상세"] = {"rich_text": [{"type": "text", "text": {"content": "청소 시작 전 현장 문제 — Telegram에서 운영자 확인 필요"}}]}
            outcome = "SITE_PROBLEM_REPORTED"
    else:
        raise ValueError("unsupported cleaning operation action")
    if action_type == "CLEANING_START" and decision == "approve":
        # Re-read durable Assignment authority immediately before the mutable 08
        # work-start projection. If Cleaner-unavailable already ended 19, a
        # stale work-start button cannot advance that Cleaner as effective.
        _assert_effective_assignment_for_work_start(record)
    properties["점검 메모"] = {"rich_text": [{"type": "text", "text": {"content": f"{memo}\n{line}".strip()[-1900:]}}]}
    updated = _notion("PATCH", f"/v1/pages/{page_id}", {"properties": properties})
    return {
        "cleaning_operation": outcome,
        "next_expected_last_edited_time": updated.get("last_edited_time"),
    }


def execute_cleaning_issue_submission(record, decision):
    if decision != "approve":
        return {"cleaning_issue": "CANCELLED_NO_WRITES"}
    from drive_archive.issue_evidence import archive_issue_photos
    from telegram_approval.cleaning_issue import load_session, save_session

    session = load_session(record.get("issue_session_id"))
    if not session or session.get("status") != "OPEN" or not session.get("photos"):
        raise ValueError("issue session is missing, closed, or has no photo")
    page_id = record["cleaning_page_id"]
    page = _notion("GET", f"/v1/pages/{page_id}")
    if _select_name(page, "배정 수락 상태") != "수락":
        raise ValueError("cleaning is not accepted by an assignee")
    if _select_name(page, "데이터 환경") != "PRODUCTION":
        raise ValueError("production issue action requires a PRODUCTION cleaning")
    if _select_name(page, "등록 상태") != "APPROVED":
        raise ValueError("production issue action requires an APPROVED cleaning")
    if _select_name(page, "상태") not in ("담당자 배정", "진행중"):
        raise ValueError("cleaning state does not allow issue report")
    expected = record.get("expected_last_edited_time")
    if not expected or page.get("last_edited_time") != expected:
        raise ValueError("cleaning page changed; stale issue action rejected")
    assigned = set(_relations(page, "담당 참여자/업체"))
    if record.get("candidate_party_page_id") and record["candidate_party_page_id"] not in assigned:
        raise ValueError("cleaning assignee changed; stale issue action rejected")
    evidence = archive_issue_photos(
        session,
        cleaning_date=_date_start(page, "점검일") or record.get("cleaning_date"),
    )
    save_session(session)
    now = datetime.now(timezone.utc).isoformat()
    notes = "\n".join(item["text"] for item in session.get("notes", [])) or "사진으로 현장 문제 보고"
    memo = _plain(page.get("properties", {}).get("점검 메모"))
    line = f"[{now}] Telegram 현장 문제 보고 제출 · Drive 검증 사진 {len(session['photos'])}장"
    existing_files = page.get("properties", {}).get("문제 사진", {}).get("files", [])
    known_urls = {
        (item.get("external") or {}).get("url")
        for item in existing_files if item.get("type") == "external"
    }
    drive_files = list(existing_files)
    for item in evidence["uploads"]:
        if item["drive_url"] not in known_urls:
            drive_files.append({
                "name": item["filename"],
                "type": "external",
                "external": {"url": item["drive_url"]},
            })
    file_ids = "\n".join(item["drive_file_id"] for item in evidence["uploads"])
    updated = _notion("PATCH", f"/v1/pages/{page_id}", {"properties": {
        "현장 보고 상태": {"select": {"name": "예외있음"}},
        "예외 상세": {"rich_text": [{"type": "text", "text": {"content": notes[-1900:]}}]},
        "현장 보고 제출일": {"date": {"start": now}},
        "문제 사진": {"type": "files", "files": drive_files},
        "Drive File ID": {"rich_text": [{"type": "text", "text": {"content": file_ids[-1900:]}}]},
        "Drive Folder ID": {"rich_text": [{"type": "text", "text": {"content": evidence["cleaning_folder_id"]}}]},
        "Drive Folder URL": {"url": evidence["cleaning_folder_url"]},
        "사진 링크": {"url": evidence["issue_folder_url"]},
        "사진 분석 상태": {"select": {"name": "UPLOADED"}},
        "Sync Status": {"select": {"name": "NEEDS_SYNC"}},
        "점검 메모": {"rich_text": [{"type": "text", "text": {"content": f"{memo}\n{line}".strip()[-1900:]}}]},
    }})
    session_evidence = session.setdefault("drive_evidence", evidence)
    session_evidence["notion_recorded"] = True
    session_evidence["notion_recorded_at"] = now
    save_session(session)
    return {"cleaning_issue": "SUBMITTED_DRIVE_FIRST", "photo_count": len(session["photos"]),
            "drive_file_ids": [item["drive_file_id"] for item in evidence["uploads"]],
            "drive_folder_id": evidence["cleaning_folder_id"],
            "next_expected_last_edited_time": updated.get("last_edited_time")}


def _create_cleaning_finance(record, cleaning, key, now):
    existing = _query_idempotency(FINANCE_SOURCE, key)
    if existing:
        return existing[0]["id"], "EXISTING"
    properties = {
        "정산 항목명": {"title": [{"type": "text", "text": {"content": f"{record['property_nickname']} · {record['cleaning_date']} · 청소비 지급"}}]},
        "상태": {"select": {"name": "완료"}},
        "기록 유형": {"select": {"name": "지급"}},
        "항목 유형": {"select": {"name": "청소비"}},
        "결제수단": {"select": {"name": "계좌이체"}},
        "세무 성격": {"select": {"name": "EXPENSE"}},
        "정산 세부 유형": {"select": {"name": "CLEANING_FEE"}},
        "금액": {"number": payout_amount_from_record(record)},
        "통화": {"select": {"name": "KRW"}},
        "발생일": {"date": {"start": record["cleaning_date"][:10]}},
        "결제/정산일": {"date": {"start": now}},
        "데이터 환경": {"select": {"name": "PRODUCTION"}},
        "수취자": {"rich_text": [{"type": "text", "text": {"content": record.get("candidate_label", "청소 담당자")}}]},
        "지급자": {"rich_text": [{"type": "text", "text": {"content": "운영자 Telegram 이체완료 확인"}}]},
        "거래번호/메모": {"rich_text": [{"type": "text", "text": {"content": f"Telegram action {record['action_id']}에서 실제 이체 완료 확인"}}]},
        "Idempotency Key": {"rich_text": [{"type": "text", "text": {"content": key}}]},
        "Source Message Ref": {"rich_text": [{"type": "text", "text": {"content": f"telegram:{record['action_id']}"}}]},
        "Source Project Code": {"rich_text": [{"type": "text", "text": {"content": "LOCAL.PROPERTYAI.TELEGRAM"}}]},
        "Status Lock": {"checkbox": False},
    }
    for source_name, target_name in (("관련 Reservation", "관련 Reservation"), ("연결 집", "연결 집"), ("연결 운영상품", "연결 운영상품")):
        values = _relations(cleaning, source_name)
        if values:
            properties[target_name] = {"relation": [{"id": value} for value in values]}
    created = _notion("POST", "/v1/pages", {
        "parent": {"type": "data_source_id", "data_source_id": FINANCE_SOURCE},
        "properties": properties,
    })
    return created["id"], "CREATED"


def _create_cleaning_labor(record, cleaning, finance_page_id, key, now):
    existing = _query_idempotency(LABOR_SOURCE, key)
    if existing:
        return existing[0]["id"], "EXISTING"
    properties = {
        "지급명": {"title": [{"type": "text", "text": {"content": f"{record['property_nickname']} · {record['cleaning_date']} · 청소"}}]},
        "담당자/업체명": {"rich_text": [{"type": "text", "text": {"content": record.get("candidate_label", "청소 담당자")}}]},
        "업무 내용": {"rich_text": [{"type": "text", "text": {"content": f"{record['property_nickname']} 퇴실청소"}}]},
        "역할": {"select": {"name": "청소"}},
        "금액": {"number": payout_amount_from_record(record)},
        "작업일": {"date": {"start": record["cleaning_date"][:10]}},
        "지급예정일": {"date": {"start": record["cleaning_date"][:10]}},
        "지급일": {"date": {"start": now}},
        "지급상태": {"select": {"name": "지급완료"}},
        "지급수단": {"select": {"name": "계좌이체"}},
        "증빙 여부": {"select": {"name": "간이증빙"}},
        "세무 확인 상태": {"select": {"name": "확인 필요"}},
        "원천/지급명세서 확인": {"select": {"name": "확인 필요"}},
        "메모": {"rich_text": [{"type": "text", "text": {"content": f"운영자 Telegram 이체 완료 확인 · action={record['action_id']}"}}]},
        "Idempotency Key": {"rich_text": [{"type": "text", "text": {"content": key}}]},
        "Source Message Ref": {"rich_text": [{"type": "text", "text": {"content": f"telegram:{record['action_id']}"}}]},
        "Source Project Code": {"rich_text": [{"type": "text", "text": {"content": "LOCAL.PROPERTYAI.TELEGRAM"}}]},
        "연결 정산/결제": {"relation": [{"id": finance_page_id}]},
        "연결 청소/점검": {"relation": [{"id": record["cleaning_page_id"]}]},
    }
    for source_name, target_name in (("연결 집", "연결 집"), ("연결 운영상품", "연결 운영상품")):
        values = _relations(cleaning, source_name)
        if values:
            properties[target_name] = {"relation": [{"id": value} for value in values]}
    if record.get("candidate_party_page_id"):
        properties["참여자/업체"] = {"relation": [{"id": record["candidate_party_page_id"]}]}
    created = _notion("POST", "/v1/pages", {
        "parent": {"type": "data_source_id", "data_source_id": LABOR_SOURCE},
        "properties": properties,
    })
    return created["id"], "CREATED"


def execute_cleaning_payment_confirmation(record, decision):
    if decision != "approve":
        return {"cleaning_payment": "HELD_NO_WRITES"}
    page_id = record["cleaning_page_id"]
    cleaning = _notion("GET", f"/v1/pages/{page_id}")
    if _select_name(cleaning, "상태") != "관리자 확인 완료":
        raise ValueError("operator-approved completion evidence is required before payment confirmation")
    if not cleaning.get("properties", {}).get("관리자 확인", {}).get("checkbox"):
        raise ValueError("operator review checkbox is required before payment confirmation")
    if _select_name(cleaning, "청소 대금 상태") == "지급완료":
        raise ValueError("cleaning payment is already complete")
    expected = record.get("expected_last_edited_time")
    if not expected or cleaning.get("last_edited_time") != expected:
        raise ValueError("cleaning page changed; stale payment action rejected")
    configured_fee = _number(cleaning, "청소비 Snapshot")
    if configured_fee is None or int(configured_fee) != int(record["cleaning_fee_krw"]):
        raise ValueError("cleaning fee changed; stale payment action rejected")
    assigned = set(_relations(cleaning, "담당 참여자/업체"))
    if record.get("candidate_party_page_id") and record["candidate_party_page_id"] not in assigned:
        raise ValueError("cleaning assignee changed; stale payment action rejected")
    payout_amount_krw = payout_amount_from_record(record)
    now = datetime.now(timezone.utc).isoformat()
    finance_key = f"CLEANING-FIN-PAY::{page_id}"
    labor_key = f"CLEANING-LABOR-PAY::{page_id}"
    finance_id, finance_outcome = _create_cleaning_finance(record, cleaning, finance_key, now)
    labor_id, labor_outcome = _create_cleaning_labor(record, cleaning, finance_id, labor_key, now)
    _notion("PATCH", f"/v1/pages/{finance_id}", {"properties": {
        "인건비/외주비": {"relation": [{"id": labor_id}]},
    }})
    memo = _plain(cleaning.get("properties", {}).get("점검 메모"))
    line = f"[{now}] 운영자 Telegram 실제 이체 완료 확인 · Finance={finance_id} · Labor={labor_id}"
    _notion("PATCH", f"/v1/pages/{page_id}", {"properties": {
        "청소 대금 상태": {"select": {"name": "지급완료"}},
        "지급 확인일": {"date": {"start": now}},
        "정산 반영 여부": {"checkbox": True},
        "Sync Status": {"select": {"name": "NEEDS_SYNC"}},
        "점검 메모": {"rich_text": [{"type": "text", "text": {"content": f"{memo}\n{line}".strip()[-1900:]}}]},
    }})
    return {
        "cleaning_payment": "PAID_CONFIRMED",
        "finance": {"page_id": finance_id, "outcome": finance_outcome},
        "labor": {"page_id": labor_id, "outcome": labor_outcome},
        "payout_amount_krw": payout_amount_krw,
    }


def execute_cancellation(record, *, history_store=None):
    now = datetime.now(timezone.utc).isoformat()
    history_store = history_store or NotionAssignmentHistoryStore()
    cancellation_at = datetime.fromisoformat(
        record["cancellation_received_at"].replace("Z", "+00:00")
    )
    if cancellation_at.tzinfo is None:
        raise ValueError("cancellation_received_at must be timezone-aware")
    reservation_id = record["reservation_page_id"]
    reservation = _notion("GET", f"/v1/pages/{reservation_id}")
    line = f"[{now}] PLATFORM_NOTICE · 예약 취소 승인 · source=sha256:{record['source_message_hash']}"
    _notion("PATCH", f"/v1/pages/{reservation_id}", {"properties": {
        "상태": {"select": {"name": "취소"}},
        "Cancelled At": {"date": {"start": now}},
        "Cancellation Source": {"select": {"name": record.get("cancellation_source", "PLATFORM_NOTICE")}},
        "Last Transition At": {"date": {"start": now}},
        "Last Transition Rule Code": {"rich_text": [{"type": "text", "text": {"content": "RSV.CANCEL.PLATFORM_APPROVED"}}]},
        "Change History": _append_history(reservation, line),
        "Sync Status": {"select": {"name": "NEEDS_SYNC"}},
        "Status Lock": {"checkbox": False},
    }})

    cleaning_updates = []
    for page_id in record.get("cleaning_page_ids", []):
        assignment_end = end_effective_assignment_for_cancellation(
            store=history_store,
            cleaning_page_id=page_id,
            ended_at=cancellation_at,
            end_key=f"reservation-cancel:{reservation_id}:{page_id}",
        )
        page = _notion("GET", f"/v1/pages/{page_id}")
        status = page.get("properties", {}).get("상태", {}).get("select") or {}
        if status.get("name") in ("완료 보고", "관리자 확인 완료"):
            cleaning_updates.append({"page_id": page_id, "outcome": "PRESERVED_ALREADY_COMPLETED"})
            continue
        memo = _plain(page.get("properties", {}).get("점검 메모"))
        memo = f"{memo}\n[{now}] 연결 예약 취소 승인으로 청소 취소".strip()[-1900:]
        _notion("PATCH", f"/v1/pages/{page_id}", {"properties": {
            "상태": {"select": {"name": "취소"}},
            "배정 수락 상태": {"select": {"name": "미제안"}},
            "점검 메모": {"rich_text": [{"type": "text", "text": {"content": memo}}]},
            "Sync Status": {"select": {"name": "SYNCED"}},
        }})
        cleaning_updates.append({"page_id": page_id, "outcome": "CANCELLED"})

    calendar_updates = []
    calendar_id = record.get("cleaning_calendar_id")
    if calendar_id:
        credentials = Credentials.from_authorized_user_file(GOOGLE_TOKEN_PATH, GOOGLE_SCOPES)
        calendar = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        for event_id in record.get("calendar_event_ids", []):
            event = calendar.events().get(calendarId=calendar_id, eventId=event_id).execute()
            summary = event.get("summary", "청소 일정")
            if not summary.startswith("[취소]"):
                summary = "[취소] " + summary
            description = (event.get("description") or "") + f"\n\n[{now}] 연결 예약 취소 승인으로 운영 취소"
            assert_current_production_writer()
            updated = calendar.events().patch(
                calendarId=calendar_id,
                eventId=event_id,
                body={"summary": summary, "description": description[-7000:], "transparency": "transparent"},
                sendUpdates="none",
            ).execute()
            calendar_updates.append({"event_id": updated["id"], "outcome": "MARKED_CANCELLED"})
    finance = _finance_cancellation_review(record, reservation)
    cleaner_notifications = _send_cleaner_cancellation(record)
    return {"reservation": "CANCELLED", "cleaning": cleaning_updates, "calendar": calendar_updates,
            "cleaner_notifications": cleaner_notifications, "finance": finance}


def execute_lifecycle_action(record, decision="approve", *, history_store=None):
    if record.get("action_type") == "CANCEL_RESERVATION_WORKFLOW":
        if decision != "approve":
            return {"cancellation": "REJECTED_NO_WRITES"}
        return execute_cancellation(record, history_store=history_store)
    if record.get("action_type") == "CLEANING_ASSIGNMENT":
        return execute_cleaning_assignment(record, decision)
    if record.get("action_type") == "CLEANING_COMPLETION":
        return execute_cleaning_completion(record, decision)
    if record.get("action_type") == "CLEANING_COMPLETION_SUBMISSION":
        return execute_cleaning_completion_submission(record, decision)
    if record.get("action_type") == "CLEANING_EVIDENCE_REVIEW":
        return execute_cleaning_evidence_review(record, decision)
    if record.get("action_type") == "CLEANING_PAYMENT_CONFIRMATION":
        return execute_cleaning_payment_confirmation(record, decision)
    if record.get("action_type") in ("CLEANING_DAY_CONFIRM", "CLEANING_ARRIVAL", "CLEANING_START"):
        return execute_cleaning_operation(record, decision)
    if record.get("action_type") == "CLEANING_ISSUE_SUBMISSION":
        return execute_cleaning_issue_submission(record, decision)
    raise ValueError("unsupported lifecycle action")

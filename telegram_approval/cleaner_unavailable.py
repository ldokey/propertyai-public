#!/usr/bin/env python3
"""Cleaner unavailable vertical on canonical Assignment authority.

Phase 2 owns the terminal Assignment fact and replacement projection. Phase 3
adds downstream performance classification/event and urgent replacement pricing
without making those secondary effects capable of rolling back the Assignment.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from gmail_ingest.lifecycle_actions import (
    _notion,
    execute_cleaner_unavailable_projection,
    validate_cleaner_unavailable,
)
from telegram_approval.assignment_history import (
    AMBIGUOUS,
    NOT_FOUND,
    REMOTE_NOTION_FAILURE,
    AssignmentHistoryError,
    NotionAssignmentHistoryStore,
    assignment_generation_lock,
    end_accepted_assignment,
    newer_accepted_assignment_exists,
    recent_unavailable_assignments,
    resolve_effective_accepted_assignment,
    resolve_ended_unavailable_assignment,
)
from telegram_approval.cleaner_config import CleanerRuntimePaths
from telegram_approval.cleaner_jobs import load_property_mappings, resolve_active_cleaner
from telegram_approval.cleaner_registry import assignment_targets, load_roster
from telegram_approval.cleaning_assignment import send_assignment
from telegram_approval.cleaner_performance import (
    APPLIED as PERFORMANCE_APPLIED,
    POLICY_RESOLUTION_REQUIRED,
    RECONCILIATION_REQUIRED as PERFORMANCE_RECONCILIATION_REQUIRED,
    NotionPerformanceEventStore,
    NotionPerformancePolicyStore,
    append_performance_event,
    build_event as build_performance_event,
    classify_unavailable,
    event_key as performance_event_key,
    quote_replacement_offer,
    resolve_replacement_urgency,
)
from telegram_approval.send_approval import ACTION_SECRET_PATH, signature


KST = ZoneInfo("Asia/Seoul")
REQUEST_DIR = CleanerRuntimePaths().request_dir
ACTION_TTL = timedelta(hours=24)  # stale Telegram UI protection only; never a business cutoff
RECENT_CHANGE_DAYS = 30
PERFORMANCE_POLICY_STORE_FACTORY = NotionPerformancePolicyStore
PERFORMANCE_EVENT_STORE_FACTORY = NotionPerformanceEventStore

UNAVAILABLE_COMPLETE = "UNAVAILABLE_COMPLETE"
PROJECTION_RECONCILIATION_REQUIRED = "PROJECTION_RECONCILIATION_REQUIRED"
REPLACEMENT_ROUTING_REQUIRED = "REPLACEMENT_ROUTING_REQUIRED"
REPLACEMENT_REQUIRED = "REPLACEMENT_REQUIRED"
OPERATOR_REQUIRED = "OPERATOR_REQUIRED"
REASON_RETRY_REQUIRED = "REASON_RETRY_REQUIRED"
NOTIFICATION_RETRY_REQUIRED = "NOTIFICATION_RETRY_REQUIRED"
REASSIGNMENT_REQUESTED = "REASSIGNMENT_REQUESTED"
REASSIGNMENT_STALE = "STALE_OR_NO_LONGER_ACTIONABLE"
REASSIGNMENT_AUTHORITY_RETRY_REQUIRED = "REASSIGNMENT_AUTHORITY_RETRY_REQUIRED"
CONFIRMATION_CANCELLED = "CONFIRMATION_CANCELLED"

REASON_CHOICES = {
    "reason_personal": "개인 일정",
    "reason_health": "건강/응급",
    "reason_time": "시간 문제",
    "reason_other": "기타",
}


class CleanerUnavailableError(RuntimeError):
    pass


def _atomic_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_secret(path: Path) -> bytes:
    value = path.read_text().strip().encode()
    if not value:
        raise CleanerUnavailableError("action signing key unavailable")
    return value


def _callback(action_id: str, operation: str, *, secret_path: Path) -> str:
    return f"u:{action_id}:{operation}:{signature(_read_secret(secret_path), action_id, operation)}"


def _request_path(action_id: str, request_dir: Path) -> Path:
    return request_dir / f"{action_id}.json"


def _load_action(action_id: str, request_dir: Path) -> tuple[Path, dict]:
    path = _request_path(action_id, request_dir)
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CleanerUnavailableError("unavailable action not found") from exc
    if not isinstance(record, dict) or record.get("action_id") != action_id:
        raise CleanerUnavailableError("unavailable action identity mismatch")
    return path, record


def _aware(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise CleanerUnavailableError(f"{label} malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CleanerUnavailableError(f"{label} must be timezone-aware")
    return parsed


def _active_cleaner(roster: dict, *, user_id: int, chat_id: int, party_page_id: str) -> dict:
    cleaner = resolve_active_cleaner(roster, user_id=user_id, chat_id=chat_id)
    if cleaner is None or cleaner.get("party_page_id") != party_page_id:
        raise CleanerUnavailableError("Cleaner authorization changed")
    owners = [
        row for row in roster.get("cleaners", [])
        if isinstance(row, dict)
        and row.get("role") == "CLEANER"
        and row.get("status") == "ACTIVE"
        and row.get("party_page_id") == party_page_id
    ]
    if len(owners) != 1 or owners[0] is not cleaner:
        raise CleanerUnavailableError("Cleaner Party identity is ambiguous")
    return cleaner


def _relations(page: dict, name: str) -> list[str]:
    relations = page.get("properties", {}).get(name, {}).get("relation")
    if not isinstance(relations, list):
        raise CleanerUnavailableError(f"{name} relation malformed")
    result = []
    for item in relations:
        page_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(page_id, str) or not page_id:
            raise CleanerUnavailableError(f"{name} relation malformed")
        result.append(page_id)
    return result


def _select(page: dict, name: str) -> str | None:
    selected = page.get("properties", {}).get(name, {}).get("select")
    return selected.get("name") if isinstance(selected, dict) else None


def _date(page: dict, name: str) -> str | None:
    value = page.get("properties", {}).get(name, {}).get("date")
    return value.get("start") if isinstance(value, dict) else None


def _safe_recent_item(
    page: dict, fact: dict, mappings: dict[str, str], *, newer_accepted: bool = False
) -> dict | None:
    if _select(page, "데이터 환경") != "PRODUCTION" or _select(page, "등록 상태") != "APPROVED":
        return None
    houses = _relations(page, "연결 집")
    if len(houses) != 1 or houses[0] not in mappings:
        return None
    day_value = _date(page, "점검일") or _date(page, "시작 예정")
    if not day_value:
        return None
    try:
        service_day = datetime.fromisoformat(day_value[:10]).date()
    except ValueError:
        return None
    def parsed(name: str):
        value = _date(page, name)
        if not value:
            return None
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return result.astimezone(KST) if result.tzinfo else None

    acceptance = _select(page, "배정 수락 상태")
    state = _select(page, "상태")
    assignees = _relations(page, "담당 참여자/업체")
    request_status = fact.get("reassignment_request_status")
    requested = request_status == REASSIGNMENT_REQUESTED
    replacement_accepted = acceptance == "수락" and assignees and fact["cleaner_party_page_id"] not in assignees
    if state == "취소":
        status_text = "예약 취소로 일정 취소"
        can_reassign = False
    elif request_status == "REASSIGNED_ORIGINAL":
        status_text = "원래 일정 재배정 완료"
        can_reassign = False
    elif request_status == "CONTINUE_REPLACEMENT":
        status_text = "대체 담당자 진행 유지"
        can_reassign = False
    elif newer_accepted or replacement_accepted:
        status_text = "대체 담당자 배정 완료"
        can_reassign = False
    elif requested:
        status_text = "재배정 요청 전달됨"
        can_reassign = False
    elif acceptance == "대체필요" and not assignees:
        status_text = "대체 담당자 확인 중"
        can_reassign = True
    else:
        status_text = "진행 불가 처리됨"
        can_reassign = state == "담당자 배정" and not replacement_accepted
    return {
        "page_id": fact["cleaning_page_id"],
        "property_label": mappings[houses[0]],
        "service_day": service_day,
        "start_at": parsed("시작 예정"),
        "end_at": parsed("완료 목표"),
        "ui_state": "UNAVAILABLE",
        "recent_status_text": status_text,
        "can_reassign": can_reassign,
        "expected_cleaning_last_edited_time": page.get("last_edited_time"),
        **fact,
    }


def recent_change_items(
    *, cleaner: dict, history_store=None, page_reader: Callable[[str], dict] | None = None,
    now: datetime | None = None, property_mappings: dict[str, str] | None = None,
) -> list[dict]:
    history_store = history_store or NotionAssignmentHistoryStore()
    page_reader = page_reader or (lambda page_id: _notion("GET", f"/v1/pages/{page_id}"))
    now = now or datetime.now(KST)
    if now.tzinfo is None:
        raise CleanerUnavailableError("recent-change clock must be timezone-aware")
    mappings = property_mappings or load_property_mappings()
    floor = now.astimezone(KST).date() - timedelta(days=RECENT_CHANGE_DAYS)
    items = []
    for fact in recent_unavailable_assignments(
        store=history_store, cleaner_party_page_id=cleaner["party_page_id"]
    ):
        ended_at = _aware(fact["ended_at"], "Assignment Ended At").astimezone(KST)
        if ended_at.date() < floor:
            continue
        newer_accepted = newer_accepted_assignment_exists(
            store=history_store,
            cleaning_page_id=fact["cleaning_page_id"],
            original_assignment_version=fact["assignment_version"],
            original_ended_at=fact["ended_at"],
        )
        item = _safe_recent_item(
            page_reader(fact["cleaning_page_id"]), fact, mappings,
            newer_accepted=newer_accepted,
        )
        if item is not None:
            item["ended_at_sort"] = ended_at
            items.append(item)
    items.sort(key=lambda item: (item["ended_at_sort"], item["page_id"]), reverse=True)
    return items[:10]


def _new_action_id(request_dir: Path) -> str:
    for _ in range(32):
        action_id = secrets.token_urlsafe(8)
        if not _request_path(action_id, request_dir).exists():
            return action_id
    raise CleanerUnavailableError("unable to allocate unavailable action")


def _find_reusable_action(request_dir: Path, *, action_type: str, cleaning_page_id: str,
                          cleaner_party_page_id: str, assignment_version: str | None,
                          expected_last_edit: str | None, now: datetime) -> dict | None:
    for path in request_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            record.get("action_type") == action_type
            and record.get("cleaning_page_id") == cleaning_page_id
            and record.get("cleaner_party_page_id") == cleaner_party_page_id
            and record.get("assignment_version") == assignment_version
            and record.get("expected_cleaning_last_edited_time") == expected_last_edit
            and record.get("status") in {
                "PENDING", "CONFIRMATION_OPEN", "UNAVAILABLE_COMPLETE", OPERATOR_REQUIRED,
                REASSIGNMENT_REQUESTED, REASSIGNMENT_AUTHORITY_RETRY_REQUIRED,
            }
        ):
            try:
                if now <= _aware(record["expires_at"], "expires_at"):
                    return record
            except CleanerUnavailableError:
                continue
    return None


def create_unavailable_action(
    item: dict, cleaner: dict, *, request_dir: Path = REQUEST_DIR,
    secret_path: Path = ACTION_SECRET_PATH, history_store=None, now: datetime | None = None,
) -> dict:
    del secret_path  # signature is generated by the caller; creation only binds authority.
    now = now or datetime.now(timezone.utc)
    history_store = history_store or NotionAssignmentHistoryStore()
    expected = item.get("expected_cleaning_last_edited_time")
    if not expected:
        raise CleanerUnavailableError("Cleaning last_edited_time is required for unavailable")
    evidence, row = resolve_effective_accepted_assignment(
        store=history_store,
        cleaning_page_id=item["page_id"],
        cleaner_party_page_id=cleaner["party_page_id"],
    )
    if (
        evidence.cleaner_telegram_user_id != cleaner["telegram_user_id"]
        or evidence.cleaner_telegram_chat_id != cleaner["telegram_chat_id"]
    ):
        raise CleanerUnavailableError("accepted Assignment Telegram identity mismatch")
    reusable = _find_reusable_action(
        request_dir, action_type="CLEANER_UNAVAILABLE", cleaning_page_id=item["page_id"],
        cleaner_party_page_id=cleaner["party_page_id"], assignment_version=evidence.assignment_version,
        expected_last_edit=expected, now=now,
    )
    if reusable:
        return reusable
    action_id = _new_action_id(request_dir)
    record = {
        "schema_version": 1,
        "action_id": action_id,
        "action_type": "CLEANER_UNAVAILABLE",
        "status": "PENDING",
        "cleaning_page_id": item["page_id"],
        "property_nickname": item["property_label"],
        "expected_cleaning_last_edited_time": expected,
        "cleaner_party_page_id": cleaner["party_page_id"],
        "telegram_user_id": cleaner["telegram_user_id"],
        "telegram_chat_id": cleaner["telegram_chat_id"],
        "history_page_id": row["id"],
        "accepted_assignment_action_id": evidence.action_id,
        "assignment_version": evidence.assignment_version,
        "acceptance_idempotency_key": evidence.operation_identity,
        "proposal_round": evidence.proposal_round,
        "created_at": now.isoformat(),
        "expires_at": (now + ACTION_TTL).isoformat(),
        "business_mutations": 0,
    }
    _atomic_private(_request_path(action_id, request_dir), record)
    return record


def create_reassignment_action(
    item: dict, cleaner: dict, *, request_dir: Path = REQUEST_DIR, now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    reusable = _find_reusable_action(
        request_dir, action_type="CLEANER_REASSIGNMENT_REQUEST",
        cleaning_page_id=item["page_id"], cleaner_party_page_id=cleaner["party_page_id"],
        assignment_version=item["assignment_version"],
        expected_last_edit=item.get("expected_cleaning_last_edited_time"), now=now,
    )
    if reusable:
        if not reusable.get("original_ended_at"):
            reusable["original_ended_at"] = _aware(
                item["ended_at"], "Assignment Ended At"
            ).isoformat()
            _atomic_private(_request_path(reusable["action_id"], request_dir), reusable)
        return reusable
    action_id = _new_action_id(request_dir)
    record = {
        "schema_version": 1,
        "action_id": action_id,
        "action_type": "CLEANER_REASSIGNMENT_REQUEST",
        "status": "PENDING",
        "cleaning_page_id": item["page_id"],
        "cleaner_party_page_id": cleaner["party_page_id"],
        "telegram_user_id": cleaner["telegram_user_id"],
        "telegram_chat_id": cleaner["telegram_chat_id"],
        "history_page_id": item["history_page_id"],
        "accepted_assignment_action_id": item["assignment_action_id"],
        "assignment_version": item["assignment_version"],
        "acceptance_idempotency_key": item["acceptance_idempotency_key"],
        "end_key": item["end_key"],
        "original_ended_at": _aware(item["ended_at"], "Assignment Ended At").isoformat(),
        "expected_cleaning_last_edited_time": item.get("expected_cleaning_last_edited_time"),
        "created_at": now.isoformat(),
        "expires_at": (now + ACTION_TTL).isoformat(),
        "business_mutations": 0,
    }
    _atomic_private(_request_path(action_id, request_dir), record)
    return record


def build_schedule_ui(
    *, cleaner: dict, items: list[dict], history_store=None,
    page_reader: Callable[[str], dict] | None = None, request_dir: Path = REQUEST_DIR,
    secret_path: Path = ACTION_SECRET_PATH, now: datetime | None = None,
    property_mappings: dict[str, str] | None = None,
) -> tuple[list[dict], dict | None]:
    now = now or datetime.now(timezone.utc)
    history_store = history_store or NotionAssignmentHistoryStore()
    rows = []

    # Durable 19 end facts override a stale active-looking Cleaning 08 projection.
    # Resolve them before creating any new executable unavailable action so /my
    # remains read-only and cannot resurrect an already-ended Cleaner binding.
    recent = recent_change_items(
        cleaner=cleaner, history_store=history_store, page_reader=page_reader,
        now=now, property_mappings=property_mappings,
    )
    ended_page_ids = {
        item["page_id"] for item in recent
        if item.get("reassignment_request_status") != "REASSIGNED_ORIGINAL"
    }
    augmented_items = []
    for original in items:
        item = dict(original)
        if item["ui_state"] in {"TODAY", "UPCOMING"} and item["page_id"] in ended_page_ids:
            continue
        augmented_items.append(item)
        if item["ui_state"] not in {"TODAY", "UPCOMING"}:
            continue
        item["human_status"] = "배정 확정"
        action = create_unavailable_action(
            item, cleaner, request_dir=request_dir, secret_path=secret_path,
            history_store=history_store, now=now,
        )
        rows.append([{
            "text": f"진행 불가 · {item['property_label']}",
            "callback_data": _callback(action["action_id"], "open", secret_path=secret_path),
        }])
    for item in recent:
        if not item.get("can_reassign"):
            continue
        action = create_reassignment_action(item, cleaner, request_dir=request_dir, now=now)
        rows.append([{
            "text": f"다시 가능해졌어요 · {item['property_label']}",
            "callback_data": _callback(action["action_id"], "reassign", secret_path=secret_path),
        }])
    return augmented_items + recent, ({"inline_keyboard": rows} if rows else None)


def _validate_callback(update: dict, record: dict, *, operation: str, supplied_signature: str,
                       secret_path: Path, roster_loader: Callable[[], dict],
                       now: datetime | None = None) -> tuple[int, int]:
    callback = update.get("callback_query") or {}
    sender = callback.get("from") or {}
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or sender.get("is_bot"):
        raise CleanerUnavailableError("unavailable callback requires a private Cleaner chat")
    user_id, chat_id = sender.get("id"), chat.get("id")
    if user_id != record.get("telegram_user_id") or chat_id != record.get("telegram_chat_id"):
        raise CleanerUnavailableError("unavailable callback identity mismatch")
    expected = signature(_read_secret(secret_path), record["action_id"], operation)
    import hmac
    if not hmac.compare_digest(expected, supplied_signature):
        raise CleanerUnavailableError("unavailable callback signature invalid")
    now = now or datetime.now(timezone.utc)
    if now > _aware(record["expires_at"], "expires_at"):
        raise CleanerUnavailableError("unavailable callback expired")
    _active_cleaner(
        roster_loader(), user_id=user_id, chat_id=chat_id,
        party_page_id=record["cleaner_party_page_id"],
    )
    return user_id, chat_id


def _assert_exact_history_binding(record: dict, history_store) -> None:
    evidence, row = resolve_effective_accepted_assignment(
        store=history_store,
        cleaning_page_id=record["cleaning_page_id"],
        cleaner_party_page_id=record["cleaner_party_page_id"],
    )
    if (
        row.get("id") != record["history_page_id"]
        or evidence.action_id != record["accepted_assignment_action_id"]
        or evidence.assignment_version != record["assignment_version"]
        or evidence.operation_identity != record["acceptance_idempotency_key"]
        or evidence.cleaner_telegram_user_id != record["telegram_user_id"]
        or evidence.cleaner_telegram_chat_id != record["telegram_chat_id"]
    ):
        raise CleanerUnavailableError("accepted Assignment changed; stale action rejected")


def _load_accepted_offer(record: dict, request_dir: Path) -> dict:
    _path, accepted = _load_action(record["accepted_assignment_action_id"], request_dir)
    if (
        accepted.get("action_type") != "CLEANING_ASSIGNMENT"
        or accepted.get("cleaning_page_id") != record["cleaning_page_id"]
        or accepted.get("candidate_party_page_id") != record["cleaner_party_page_id"]
        or accepted.get("action_id") != record["assignment_version"]
    ):
        raise CleanerUnavailableError("accepted Assignment request binding mismatch")
    return accepted


def _persist_transition_clock(path: Path, record: dict, now: datetime) -> datetime:
    if record.get("transition_at"):
        return _aware(record["transition_at"], "transition_at")
    record["transition_at"] = now.isoformat()
    record["end_key"] = f"cleaner-unavailable:{record['action_id']}"
    _atomic_private(path, record)
    return now


def _notify_operator_required(record: dict):
    """Reuse the existing audited OPS notification boundary after durable fact."""

    from telegram_approval.send_cleaning_operations import _audit_message
    from telegram_approval.send_approval import OPERATOR_PATH

    operator = json.loads(OPERATOR_PATH.read_text())
    return _audit_message(
        action_type="CLEANER_UNAVAILABLE_OPERATOR_REQUIRED",
        cleaning_page_id=record["cleaning_page_id"],
        chat_id=operator["telegram_chat_id"],
        recipient_role="OPS",
        text=(
            "⚠️ 대체 청소 담당자 확인 필요\n"
            f"숙소: {record.get('property_nickname', '확인 필요')}\n"
            "기존 확정 담당자의 진행 불가 처리는 완료되었고, 현재 자동 제안 가능한 후보가 없습니다."
        ),
    )


def _apply_unavailable_performance(
    record: dict, *, path: Path, accepted: dict, performance_policy_store=None,
    performance_event_store=None,
):
    occurred_at = _aware(record["transition_at"], "transition_at")
    classification = classify_unavailable(
        cleaning_date=accepted.get("start_at", ""), occurred_at=occurred_at
    )
    record["performance_classification"] = classification
    record["performance_event_key"] = performance_event_key(
        business_key=record["end_key"], event_type=classification
    )
    policy_store = performance_policy_store or PERFORMANCE_POLICY_STORE_FACTORY()
    try:
        policy = policy_store.resolve_at(occurred_at)
        urgency = resolve_replacement_urgency(
            classification=classification, occurred_at=occurred_at,
            cleaning_start_at=accepted.get("start_at", ""), policy=policy,
        )
        economics = quote_replacement_offer(
            base_fee_krw=int(accepted.get("cleaning_fee_krw", 0)),
            replacement_urgency=urgency, policy=policy,
        )
    except Exception as exc:
        record["performance_reconciliation_state"] = POLICY_RESOLUTION_REQUIRED
        record["performance_error_type"] = type(exc).__name__
        record["replacement_urgency"] = (
            "URGENT" if classification == "SAME_DAY_UNAVAILABLE" else None
        )
        _atomic_private(path, record)
        return None

    record["replacement_urgency"] = urgency
    record["performance_policy_version"] = policy.version
    record["replacement_base_fee_krw"] = economics.base_fee_krw
    record["replacement_urgent_premium_krw"] = economics.urgent_premium_krw
    record["replacement_total_agreed_fee_krw"] = economics.total_agreed_fee_krw
    record["replacement_urgent_premium_policy_version"] = economics.urgent_premium_policy_version
    event = build_performance_event(
        business_key=record["end_key"], event_type=classification,
        cleaner_party_page_id=record["cleaner_party_page_id"],
        cleaning_page_id=record["cleaning_page_id"],
        assignment_page_id=record.get("history_page_id"),
        assignment_version=record.get("assignment_version"),
        occurred_at=occurred_at, policy=policy,
        performance_classification=classification, replacement_urgency=urgency,
        data_environment="PRODUCTION",
    )
    try:
        event_store = performance_event_store or PERFORMANCE_EVENT_STORE_FACTORY()
        append_performance_event(store=event_store, event=event)
    except Exception as exc:
        # Assignment end is already authoritative. Event failure is downstream
        # reconciliation only and must never restore the Cleaner.
        record["performance_reconciliation_state"] = PERFORMANCE_RECONCILIATION_REQUIRED
        record["performance_error_type"] = type(exc).__name__
    else:
        record["performance_reconciliation_state"] = PERFORMANCE_APPLIED
        record.pop("performance_error_type", None)
    _atomic_private(path, record)
    return economics


def execute_unavailable(
    record: dict, *, path: Path, history_store, request_dir: Path,
    now: datetime | None = None, validator=validate_cleaner_unavailable,
    projector=execute_cleaner_unavailable_projection,
    candidate_resolver=assignment_targets, assignment_sender=send_assignment,
    operator_notifier: Callable[[dict], object] | None = None,
    performance_policy_store=None, performance_event_store=None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    terminal_result = (
        record.get("execution_result")
        if record.get("status") in {UNAVAILABLE_COMPLETE, OPERATOR_REQUIRED}
        else None
    )
    if terminal_result and record.get("performance_reconciliation_state") == PERFORMANCE_APPLIED:
        return terminal_result

    result_status = record.get("history_end_status", "RETRY_SAME_KEY")
    if not record.get("history_ended"):
        # All fresh Cleaning/Reservation/Assignment/work-start guards must pass
        # before one authoritative terminal timestamp is captured.  The history
        # primitive then performs its own locked re-read and exact end semantics.
        validator(record)
        _assert_exact_history_binding(record, history_store)
        transition_at = _persist_transition_clock(path, record, now)
        result = end_accepted_assignment(
            store=history_store,
            cleaning_page_id=record["cleaning_page_id"],
            cleaner_party_page_id=record["cleaner_party_page_id"],
            assignment_version=record["assignment_version"],
            action_id=record["accepted_assignment_action_id"],
            acceptance_idempotency_key=record["acceptance_idempotency_key"],
            ended_at=transition_at,
            end_reason="CLEANER_UNAVAILABLE",
            end_key=record["end_key"],
        )
        result_status = result.status
        record["history_end_status"] = result_status
        record["history_ended"] = True
        record["business_mutations"] = max(1, int(record.get("business_mutations", 0)))
        _atomic_private(path, record)

    if record.get("projection_status") != REPLACEMENT_REQUIRED:
        try:
            projection = projector(record)
        except Exception as exc:
            record["status"] = PROJECTION_RECONCILIATION_REQUIRED
            record["projection_error_type"] = type(exc).__name__
            record["execution_result"] = {
                "status": PROJECTION_RECONCILIATION_REQUIRED,
                "assignment_end": result_status,
            }
            _atomic_private(path, record)
            return record["execution_result"]
        record["projection_status"] = REPLACEMENT_REQUIRED
        record["projected_last_edited_time"] = projection.get("next_expected_last_edited_time")
        _atomic_private(path, record)
    else:
        projection = {"next_expected_last_edited_time": record.get("projected_last_edited_time")}

    accepted = _load_accepted_offer(record, request_dir)
    economics = _apply_unavailable_performance(
        record, path=path, accepted=accepted,
        performance_policy_store=performance_policy_store,
        performance_event_store=performance_event_store,
    )
    if terminal_result:
        return terminal_result
    if economics is None:
        record["status"] = REPLACEMENT_ROUTING_REQUIRED
        record["replacement_error_stage"] = "PERFORMANCE_POLICY_RESOLUTION"
        record["execution_result"] = {
            "status": REPLACEMENT_ROUTING_REQUIRED,
            "assignment_end": result_status,
            "projection": REPLACEMENT_REQUIRED,
        }
        _atomic_private(path, record)
        return record["execution_result"]
    try:
        candidates, observers = candidate_resolver(record["property_nickname"])
    except (OSError, ValueError, KeyError) as exc:
        record["status"] = REPLACEMENT_ROUTING_REQUIRED
        record["replacement_error_stage"] = "CANDIDATE_RESOLUTION"
        record["replacement_error_type"] = type(exc).__name__
        record["execution_result"] = {
            "status": REPLACEMENT_ROUTING_REQUIRED,
            "assignment_end": result_status,
            "projection": REPLACEMENT_REQUIRED,
        }
        _atomic_private(path, record)
        return record["execution_result"]
    fresh_candidates = [
        candidate for candidate in candidates
        if candidate.get("party_page_id") != record["cleaner_party_page_id"]
    ]
    record["fresh_candidate_count"] = len(fresh_candidates)
    record["original_cleaner_excluded"] = True
    if not fresh_candidates:
        record["status"] = OPERATOR_REQUIRED
        record["execution_result"] = {
            "status": OPERATOR_REQUIRED,
            "assignment_end": result_status,
            "projection": REPLACEMENT_REQUIRED,
            "replacement_offer": "NO_CANDIDATE",
        }
        _atomic_private(path, record)
        if operator_notifier is not None:
            try:
                operator_notifier(record)
            except Exception as exc:
                record["notification_status"] = NOTIFICATION_RETRY_REQUIRED
                record["notification_error_type"] = type(exc).__name__
                _atomic_private(path, record)
        return record["execution_result"]

    try:
        sent = assignment_sender(
            cleaning_page_id=record["cleaning_page_id"],
            cleaning_name=accepted.get("cleaning_name", record["property_nickname"]),
            property_nickname=record["property_nickname"],
            address=accepted.get("address", ""),
            start_at=accepted.get("start_at", ""),
            end_at=accepted.get("end_at", ""),
            cleaning_fee_krw=int(accepted.get("cleaning_fee_krw", 0)),
            expected_last_edited_time=projection.get("next_expected_last_edited_time"),
            expected_acceptance_status="대체필요",
            candidates=fresh_candidates,
            observers=observers,
            proposal_round=int(record.get("proposal_round", 1)) + 1,
            test_mode=False,
            replacement_urgency=economics.replacement_urgency,
            urgent_premium_krw=economics.urgent_premium_krw,
            total_agreed_fee_krw=economics.total_agreed_fee_krw,
            urgent_premium_policy_version=economics.urgent_premium_policy_version,
        )
    except Exception as exc:
        record["status"] = REPLACEMENT_ROUTING_REQUIRED
        record["replacement_error_type"] = type(exc).__name__
        record["execution_result"] = {
            "status": REPLACEMENT_ROUTING_REQUIRED,
            "assignment_end": result_status,
            "projection": REPLACEMENT_REQUIRED,
        }
        _atomic_private(path, record)
        return record["execution_result"]

    record["status"] = UNAVAILABLE_COMPLETE
    record["replacement_action_id"] = sent.get("action_id")
    record["execution_result"] = {
        "status": UNAVAILABLE_COMPLETE,
        "assignment_end": result_status,
        "projection": REPLACEMENT_REQUIRED,
        "replacement_offer": "STARTED" if sent.get("sent") else sent.get("reason", "EXISTING"),
        "replacement_action_id": sent.get("action_id"),
    }
    _atomic_private(path, record)
    return record["execution_result"]


def capture_reason(record: dict, *, path: Path, operation: str, history_store,
                   now: datetime | None = None) -> dict:
    if not record.get("history_ended"):
        raise CleanerUnavailableError("reason is only allowed after unavailable succeeds")
    if operation == "reason_skip":
        record["reason_status"] = "SKIPPED"
        _atomic_private(path, record)
        return {"status": "SKIPPED"}
    reason = REASON_CHOICES.get(operation)
    if reason is None:
        raise CleanerUnavailableError("unsupported unavailable reason")
    now = now or datetime.now(timezone.utc)
    if not record.get("reason_captured_at"):
        record["reason_captured_at"] = now.isoformat()
        _atomic_private(path, record)
    captured_at = _aware(record["reason_captured_at"], "reason_captured_at")
    try:
        history_store.capture_unavailable_reason(
            record["history_page_id"],
            cleaning_page_id=record["cleaning_page_id"],
            cleaner_party_page_id=record["cleaner_party_page_id"],
            assignment_version=record["assignment_version"],
            action_id=record["accepted_assignment_action_id"],
            idempotency_key=record["acceptance_idempotency_key"],
            end_key=record["end_key"], reason=reason, captured_at=captured_at,
        )
    except Exception as exc:
        record["reason_status"] = REASON_RETRY_REQUIRED
        record["reason_error_type"] = type(exc).__name__
        _atomic_private(path, record)
        return {"status": REASON_RETRY_REQUIRED}
    record["reason_status"] = "CAPTURED"
    record["unavailable_reason"] = reason
    _atomic_private(path, record)
    return {"status": "CAPTURED", "reason": reason}


def execute_reassignment_request(record: dict, *, path: Path, history_store,
                                 page_reader: Callable[[str], dict] | None = None,
                                 now: datetime | None = None) -> dict:
    page_reader = page_reader or (lambda page_id: _notion("GET", f"/v1/pages/{page_id}"))
    if record.get("status") == REASSIGNMENT_STALE:
        return {
            "status": REASSIGNMENT_STALE, "assignment_changes": 0,
            "offer_cancellations": 0, "notify": False,
        }
    if record.get("status") == REASSIGNMENT_REQUESTED:
        return {
            "status": REASSIGNMENT_REQUESTED, "assignment_changes": 0,
            "offer_cancellations": 0, "replayed": True,
        }

    page = page_reader(record["cleaning_page_id"])
    if _select(page, "데이터 환경") != "PRODUCTION" or _select(page, "등록 상태") != "APPROVED":
        raise CleanerUnavailableError("Cleaning is no longer eligible")
    if _select(page, "상태") != "담당자 배정":
        raise CleanerUnavailableError("Cleaning is no longer eligible")
    assignees = _relations(page, "담당 참여자/업체")
    if _select(page, "배정 수락 상태") == "수락" and record["cleaner_party_page_id"] not in assignees:
        raise CleanerUnavailableError("replacement assignment already accepted")
    if _select(page, "배정 수락 상태") not in {"대체필요", "수락"}:
        raise CleanerUnavailableError("Cleaning replacement state changed")

    request_key = f"reassignment-request:{record['assignment_version']}:{record['cleaner_party_page_id']}"
    now = now or datetime.now(timezone.utc)

    def terminal_stale(latest: dict, *, reason: str) -> dict:
        latest["status"] = REASSIGNMENT_STALE
        latest["invalidated_at"] = now.isoformat()
        latest["invalidation_reason"] = reason
        latest["business_mutations"] = int(latest.get("business_mutations", 0))
        _atomic_private(path, latest)
        return {
            "status": REASSIGNMENT_STALE, "assignment_changes": 0,
            "offer_cancellations": 0, "notify": True,
        }

    def retryable_authority_failure(latest: dict, exc: AssignmentHistoryError) -> dict:
        latest["status"] = REASSIGNMENT_AUTHORITY_RETRY_REQUIRED
        latest["authority_retry_error_code"] = exc.code
        latest["business_mutations"] = int(latest.get("business_mutations", 0))
        _atomic_private(path, latest)
        return {
            "status": REASSIGNMENT_AUTHORITY_RETRY_REQUIRED,
            "assignment_changes": 0, "offer_cancellations": 0, "notify": True,
        }

    # Reuse the existing assignment-generation serialization for this original
    # generation.  Predecessor actions may lack original_ended_at, so hydrate it
    # only from the exact immutable canonical 19 binding under the same lock.
    # The newer-ACCEPTED query then remains the final business authority
    # immediately before the durable request write.
    with assignment_generation_lock(
        cleaning_page_id=record["cleaning_page_id"],
        assignment_version=record["assignment_version"],
    ):
        latest = json.loads(path.read_text())
        if latest.get("action_id") != record.get("action_id"):
            raise CleanerUnavailableError("reassignment action identity changed")
        if latest.get("status") == REASSIGNMENT_STALE:
            return {
                "status": REASSIGNMENT_STALE, "assignment_changes": 0,
                "offer_cancellations": 0, "notify": False,
            }
        if latest.get("status") == REASSIGNMENT_REQUESTED:
            return {
                "status": REASSIGNMENT_REQUESTED, "assignment_changes": 0,
                "offer_cancellations": 0, "replayed": True,
            }

        original_ended_at = latest.get("original_ended_at")
        if not original_ended_at:
            try:
                original = resolve_ended_unavailable_assignment(
                    store=history_store,
                    history_page_id=latest["history_page_id"],
                    cleaning_page_id=latest["cleaning_page_id"],
                    cleaner_party_page_id=latest["cleaner_party_page_id"],
                    assignment_version=latest["assignment_version"],
                    action_id=latest["accepted_assignment_action_id"],
                    idempotency_key=latest["acceptance_idempotency_key"],
                    end_key=latest["end_key"],
                )
            except AssignmentHistoryError as exc:
                if exc.code == REMOTE_NOTION_FAILURE:
                    return retryable_authority_failure(latest, exc)
                reason = (
                    f"ORIGINAL_ASSIGNMENT_{exc.code}"
                    if exc.code in {NOT_FOUND, AMBIGUOUS}
                    else "ORIGINAL_ASSIGNMENT_INVALID"
                )
                return terminal_stale(latest, reason=reason)
            original_ended_at = original["ended_at"]
            latest["original_ended_at"] = original_ended_at
            latest.pop("authority_retry_error_code", None)
            _atomic_private(path, latest)
        original_ended_at = _aware(original_ended_at, "Assignment Ended At").isoformat()

        try:
            newer_accepted = newer_accepted_assignment_exists(
                store=history_store,
                cleaning_page_id=latest["cleaning_page_id"],
                original_assignment_version=latest["assignment_version"],
                original_ended_at=original_ended_at,
            )
        except AssignmentHistoryError as exc:
            if exc.code == REMOTE_NOTION_FAILURE:
                return retryable_authority_failure(latest, exc)
            return terminal_stale(latest, reason="NEWER_ACCEPTED_AUTHORITY_INVALID")
        if newer_accepted:
            return terminal_stale(latest, reason="NEWER_ACCEPTED_ASSIGNMENT_EXISTS")

        requested_at = _aware(
            latest.get("reassignment_requested_at") or now.isoformat(),
            "reassignment_requested_at",
        )
        history_store.record_reassignment_request(
            latest["history_page_id"],
            cleaning_page_id=latest["cleaning_page_id"],
            cleaner_party_page_id=latest["cleaner_party_page_id"],
            assignment_version=latest["assignment_version"],
            action_id=latest["accepted_assignment_action_id"],
            idempotency_key=latest["acceptance_idempotency_key"],
            end_key=latest["end_key"], request_key=request_key,
            requested_at=requested_at,
        )
        latest["reassignment_requested_at"] = requested_at.isoformat()
        latest["status"] = REASSIGNMENT_REQUESTED
        latest["request_key"] = request_key
        latest["business_mutations"] = max(1, int(latest.get("business_mutations", 0)))
        _atomic_private(path, latest)
    return {
        "status": REASSIGNMENT_REQUESTED, "assignment_changes": 0,
        "offer_cancellations": 0, "replayed": False,
    }


def _reason_keyboard(action_id: str, secret_path: Path) -> str:
    rows = []
    for operation, label in REASON_CHOICES.items():
        rows.append([{"text": label, "callback_data": _callback(action_id, operation, secret_path=secret_path)}])
    rows.append([{"text": "건너뛰기", "callback_data": _callback(action_id, "reason_skip", secret_path=secret_path)}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def handle_unavailable_callback(
    update: dict, token: str, *, request_api: Callable,
    request_dir: Path = REQUEST_DIR, secret_path: Path = ACTION_SECRET_PATH,
    roster_loader: Callable[[], dict] = load_roster, history_store=None,
    now: datetime | None = None, validator=validate_cleaner_unavailable,
    projector=execute_cleaner_unavailable_projection,
    candidate_resolver=assignment_targets, assignment_sender=send_assignment,
    page_reader: Callable[[str], dict] | None = None,
    operator_notifier: Callable[[dict], object] | None = None,
    reassignment_operator_notifier: Callable[[dict], object] | None = None,
    performance_policy_store=None, performance_event_store=None,
):
    callback = update.get("callback_query") or {}
    data = callback.get("data")
    if not isinstance(data, str) or not data.startswith("u:"):
        return None
    parts = data.split(":")
    if len(parts) != 4:
        return "unavailable_invalid_callback"
    _prefix, action_id, operation, supplied = parts
    try:
        path, record = _load_action(action_id, request_dir)
        _validate_callback(
            update, record, operation=operation, supplied_signature=supplied,
            secret_path=secret_path, roster_loader=roster_loader, now=now,
        )
        store = history_store or NotionAssignmentHistoryStore()
        chat_id = record["telegram_chat_id"]
        if operation == "open":
            if record.get("action_type") != "CLEANER_UNAVAILABLE":
                raise CleanerUnavailableError("action type mismatch")
            validator(record)
            _assert_exact_history_binding(record, store)
            record["status"] = "CONFIRMATION_OPEN"
            _atomic_private(path, record)
            keyboard = {"inline_keyboard": [[
                {"text": "네, 진행 불가", "callback_data": _callback(action_id, "confirm", secret_path=secret_path)},
                {"text": "일정 유지", "callback_data": _callback(action_id, "keep", secret_path=secret_path)},
            ]]}
            request_api(
                token, "sendMessage", chat_id=chat_id,
                text="정말 이 청소를 진행하기 어려우신가요?\n확정하면 현재 배정이 종료되고\n대체 담당자 확인이 시작됩니다.",
                reply_markup=json.dumps(keyboard, ensure_ascii=False),
            )
            return "unavailable_confirmation_open"
        if operation == "keep":
            if record.get("action_type") != "CLEANER_UNAVAILABLE":
                raise CleanerUnavailableError("action type mismatch")
            if record.get("history_ended"):
                raise CleanerUnavailableError("unavailable action already executed")
            if record.get("status") != CONFIRMATION_CANCELLED:
                if record.get("status") != "CONFIRMATION_OPEN":
                    raise CleanerUnavailableError("unavailable confirmation is not open")
                record["status"] = CONFIRMATION_CANCELLED
                _atomic_private(path, record)
            request_api(token, "sendMessage", chat_id=chat_id, text="기존 청소 일정을 유지합니다.")
            return "unavailable_keep_no_business_mutation"
        if operation == "confirm":
            if record.get("action_type") != "CLEANER_UNAVAILABLE":
                raise CleanerUnavailableError("action type mismatch")
            if record.get("status") == CONFIRMATION_CANCELLED:
                raise CleanerUnavailableError("unavailable confirmation was cancelled")
            result = execute_unavailable(
                record, path=path, history_store=store, request_dir=request_dir, now=now,
                validator=validator, projector=projector, candidate_resolver=candidate_resolver,
                assignment_sender=assignment_sender,
                operator_notifier=operator_notifier or _notify_operator_required,
                performance_policy_store=performance_policy_store,
                performance_event_store=performance_event_store,
            )
            try:
                if record.get("history_ended"):
                    request_api(
                        token, "sendMessage", chat_id=chat_id,
                        text=(
                            "진행 불가 처리가 완료되었습니다.\n"
                            "운영팀에서 대체 담당자를 확인하고 있습니다.\n"
                            "가능하면 사유를 알려주세요. 사유는 선택 사항입니다."
                        ),
                        reply_markup=_reason_keyboard(action_id, secret_path),
                    )
                else:
                    request_api(
                        token, "sendMessage", chat_id=chat_id,
                        text="현재 상태가 변경되어 진행 불가 처리를 완료하지 못했습니다. /my에서 최신 일정을 확인해 주세요.",
                    )
            except Exception as exc:
                latest = json.loads(path.read_text())
                latest["notification_status"] = NOTIFICATION_RETRY_REQUIRED
                latest["notification_error_type"] = type(exc).__name__
                _atomic_private(path, latest)
                raise
            return f"unavailable_{result['status'].lower()}"
        if operation in set(REASON_CHOICES) | {"reason_skip"}:
            if record.get("action_type") != "CLEANER_UNAVAILABLE":
                raise CleanerUnavailableError("action type mismatch")
            result = capture_reason(record, path=path, operation=operation, history_store=store, now=now)
            text = "사유를 기록했습니다." if result["status"] == "CAPTURED" else (
                "사유 입력을 건너뛰었습니다." if result["status"] == "SKIPPED" else "진행 불가 처리는 완료되었고, 사유 기록은 운영팀 확인이 필요합니다."
            )
            request_api(token, "sendMessage", chat_id=chat_id, text=text)
            return f"unavailable_reason_{result['status'].lower()}"
        if operation == "reassign":
            if record.get("action_type") != "CLEANER_REASSIGNMENT_REQUEST":
                raise CleanerUnavailableError("action type mismatch")
            result = execute_reassignment_request(
                record, path=path, history_store=store, page_reader=page_reader, now=now,
            )
            if result["status"] == REASSIGNMENT_STALE:
                if result.get("notify"):
                    request_api(
                        token, "sendMessage", chat_id=chat_id,
                        text=(
                            "대체 담당자가 이미 확정되었거나 원래 배정 이력을 확인할 수 없어 "
                            "재배정 요청을 보낼 수 없습니다. 현재 일정 상태를 다시 확인해주세요."
                        ),
                    )
                return "unavailable_stale_fail_closed"
            if result["status"] == REASSIGNMENT_AUTHORITY_RETRY_REQUIRED:
                if result.get("notify"):
                    request_api(
                        token, "sendMessage", chat_id=chat_id,
                        text=(
                            "배정 이력을 일시적으로 확인하지 못했습니다. 잠시 후 같은 버튼을 다시 눌러주세요."
                        ),
                    )
                return "unavailable_reassignment_authority_retry_required"
            from telegram_approval.cleaner_reassignment import create_reassignment_decision_action
            latest_request = json.loads(path.read_text())
            decision_action = create_reassignment_decision_action(
                latest_request, request_dir=request_dir, now=now,
            )
            decision_path = request_dir / f"{decision_action['action_id']}.json"
            # W03 owns only the durable Cleaner request. OPS notification delivery
            # belongs to W02, which exclusively holds OPS bot credentials.
            if decision_action.get("notification_status") is None:
                decision_action["notification_status"] = "PENDING"
                _atomic_private(decision_path, decision_action)
            # Retain the explicit injection seam for deterministic unit tests only.
            if reassignment_operator_notifier is not None and decision_action.get("notification_status") != "DELIVERED":
                try:
                    reassignment_operator_notifier(decision_action)
                except Exception as exc:
                    decision_action["notification_status"] = NOTIFICATION_RETRY_REQUIRED
                    decision_action["notification_error_type"] = type(exc).__name__
                    _atomic_private(decision_path, decision_action)
                else:
                    decision_action["notification_status"] = "DELIVERED"
                    decision_action.pop("notification_error_type", None)
                    _atomic_private(decision_path, decision_action)
            if not result.get("replayed"):
                request_api(
                    token, "sendMessage", chat_id=chat_id,
                    text="다시 가능하다는 요청을 운영팀에 전달했습니다. 현재 배정은 자동으로 복구되지 않습니다.",
                )
            return "unavailable_reassignment_requested"
        raise CleanerUnavailableError("unsupported unavailable callback operation")
    except (CleanerUnavailableError, AssignmentHistoryError, ValueError, OSError):
        chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
        if chat_id is not None:
            request_api(
                token, "sendMessage", chat_id=chat_id,
                text="현재 상태가 변경되어 이 요청을 처리할 수 없습니다. /my에서 최신 일정을 확인해 주세요.",
            )
        return "unavailable_stale_fail_closed"

#!/usr/bin/env python3
"""Phase-4 restricted original-Cleaner reassignment control.

This module is intentionally not a generic assignment or Offer-cancellation API.
It owns exactly two terminal operator decisions for one previously requested
original-Cleaner reassignment: restore that same historical Cleaner, or keep the
replacement path.  Canonical Assignment History 19 remains the Assignment
authority; local signed actions provide durable callback/idempotency evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from gmail_ingest.lifecycle_actions import _notion
from propertyai_core.global_writer import ProductionWriterError
from telegram_approval.assignment_history import (
    AssignmentHistoryError,
    NotionAssignmentHistoryStore,
    accepted_assignment_compensation_snapshot,
    cleaning_assignment_lock,
    newer_accepted_assignment_exists,
    record_reassigned_original_assignment,
    resolve_ended_unavailable_assignment,
)
from telegram_approval.cleaner_config import CleanerRuntimePaths
from telegram_approval.ops_config import OpsAllowlistProvider
from telegram_approval.outbound import ops_outbound_router
from telegram_approval.send_approval import ACTION_SECRET_PATH, api


REQUEST_DIR = CleanerRuntimePaths().request_dir
ACTION_TTL = timedelta(hours=24)
ACTION_TYPE = "CLEANER_REASSIGNMENT_DECISION"
SOURCE_ACTION_TYPE = "CLEANER_REASSIGNMENT_REQUEST"
REASSIGNMENT_REQUESTED = "REASSIGNMENT_REQUESTED"
REASSIGNED_ORIGINAL = "REASSIGNED_ORIGINAL"
CONTINUE_REPLACEMENT = "CONTINUE_REPLACEMENT"
SUPERSEDED = "SUPERSEDED"
SUPERSEDED_CAUSE = "ORIGINAL_CLEANER_REASSIGNED"

ASSIGNMENT_RECONCILIATION_REQUIRED = "ASSIGNMENT_RECONCILIATION_REQUIRED"
OFFER_CLEANUP_RECONCILIATION_REQUIRED = "OFFER_CLEANUP_RECONCILIATION_REQUIRED"
PROJECTION_RECONCILIATION_REQUIRED = "PROJECTION_RECONCILIATION_REQUIRED"
REQUEST_RESOLUTION_RECONCILIATION_REQUIRED = "REQUEST_RESOLUTION_RECONCILIATION_REQUIRED"
NOTIFICATION_RETRY_REQUIRED = "NOTIFICATION_RETRY_REQUIRED"
REASSIGNMENT_COMPLETE = "REASSIGNMENT_COMPLETE"
CONTINUE_REPLACEMENT_COMPLETE = "CONTINUE_REPLACEMENT_COMPLETE"
REASSIGNMENT_ECONOMICS_BLOCKED = "REASSIGNMENT_ECONOMICS_BLOCKED"
STALE = "STALE_OR_NO_LONGER_ACTIONABLE"


class CleanerReassignmentError(RuntimeError):
    pass


def _atomic_private(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CleanerReassignmentError("reassignment action unavailable") from exc
    if not isinstance(value, dict):
        raise CleanerReassignmentError("reassignment action malformed")
    return value


def _action_path(action_id: str, request_dir: Path) -> Path:
    if not isinstance(action_id, str) or not action_id:
        raise CleanerReassignmentError("reassignment action id missing")
    return request_dir / f"{action_id}.json"


def _aware(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CleanerReassignmentError(f"{label} missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CleanerReassignmentError(f"{label} malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CleanerReassignmentError(f"{label} must be timezone-aware")
    return parsed


def _select(page: dict, name: str) -> str | None:
    selected = page.get("properties", {}).get(name, {}).get("select")
    return selected.get("name") if isinstance(selected, dict) else None


def _relations(page: dict, name: str) -> list[str]:
    raw = page.get("properties", {}).get(name, {}).get("relation")
    if not isinstance(raw, list):
        raise CleanerReassignmentError(f"{name} relation malformed")
    result = []
    for item in raw:
        page_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(page_id, str) or not page_id:
            raise CleanerReassignmentError(f"{name} relation malformed")
        result.append(page_id)
    return result


def _rich_text(page: dict, name: str) -> str | None:
    raw = page.get("properties", {}).get(name, {}).get("rich_text")
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        return None
    item = raw[0]
    if isinstance(item.get("plain_text"), str):
        return item["plain_text"]
    text = item.get("text")
    return text.get("content") if isinstance(text, dict) else None


def _new_action_id(request_dir: Path) -> str:
    for _ in range(32):
        value = secrets.token_urlsafe(8)
        if not _action_path(value, request_dir).exists():
            return value
    raise CleanerReassignmentError("unable to allocate reassignment decision action")


def _source_request(action_id: str, request_dir: Path) -> dict:
    value = _load(_action_path(action_id, request_dir))
    if value.get("action_id") != action_id or value.get("action_type") != SOURCE_ACTION_TYPE:
        raise CleanerReassignmentError("reassignment request identity mismatch")
    return value


def _decision_source(record: dict, request_dir: Path) -> dict:
    if record.get("action_type") != ACTION_TYPE:
        raise CleanerReassignmentError("operator action type mismatch")
    source = _source_request(record.get("request_action_id"), request_dir)
    exact = {
        "request_key": "request_key",
        "cleaning_page_id": "cleaning_page_id",
        "cleaner_party_page_id": "cleaner_party_page_id",
        "history_page_id": "history_page_id",
        "accepted_assignment_action_id": "accepted_assignment_action_id",
        "assignment_version": "assignment_version",
        "acceptance_idempotency_key": "acceptance_idempotency_key",
        "end_key": "end_key",
        "original_ended_at": "original_ended_at",
        "telegram_user_id": "cleaner_telegram_user_id",
        "telegram_chat_id": "cleaner_telegram_chat_id",
    }
    for source_name, decision_name in exact.items():
        if source.get(source_name) != record.get(decision_name):
            raise CleanerReassignmentError(f"reassignment decision source mismatch: {source_name}")
    return source


def create_reassignment_decision_action(
    request_record: dict, *, request_dir: Path = REQUEST_DIR,
    now: datetime | None = None,
) -> dict:
    """Create/reuse one operator decision bound to the exact Cleaner request."""

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise CleanerReassignmentError("decision clock must be timezone-aware")
    if request_record.get("action_type") != SOURCE_ACTION_TYPE:
        raise CleanerReassignmentError("source is not a reassignment request")
    if request_record.get("status") != REASSIGNMENT_REQUESTED or not request_record.get("request_key"):
        raise CleanerReassignmentError("reassignment request is not open")
    required = (
        "action_id", "cleaning_page_id", "cleaner_party_page_id", "history_page_id",
        "accepted_assignment_action_id", "assignment_version", "acceptance_idempotency_key",
        "end_key", "original_ended_at", "telegram_user_id", "telegram_chat_id",
    )
    if any(request_record.get(name) in (None, "") for name in required):
        raise CleanerReassignmentError("reassignment request authority is incomplete")

    matches = []
    for path in request_dir.glob("*.json"):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if value.get("action_type") == ACTION_TYPE and value.get("request_action_id") == request_record["action_id"]:
            matches.append(value)
    if len(matches) > 1:
        raise CleanerReassignmentError("multiple decision actions bind one request")
    if matches:
        _decision_source(matches[0], request_dir)
        return matches[0]

    action_id = _new_action_id(request_dir)
    record = {
        "schema_version": 1,
        "action_id": action_id,
        "action_type": ACTION_TYPE,
        "status": "PENDING",
        "consumed": False,
        "decision_committed": False,
        "request_action_id": request_record["action_id"],
        "request_key": request_record["request_key"],
        "cleaning_page_id": request_record["cleaning_page_id"],
        "cleaner_party_page_id": request_record["cleaner_party_page_id"],
        "history_page_id": request_record["history_page_id"],
        "accepted_assignment_action_id": request_record["accepted_assignment_action_id"],
        "assignment_version": request_record["assignment_version"],
        "acceptance_idempotency_key": request_record["acceptance_idempotency_key"],
        "end_key": request_record["end_key"],
        "original_ended_at": request_record["original_ended_at"],
        "cleaner_telegram_user_id": request_record["telegram_user_id"],
        "cleaner_telegram_chat_id": request_record["telegram_chat_id"],
        "created_at": now.isoformat(),
        "expires_at": (now + ACTION_TTL).isoformat(),
        "business_mutations": 0,
    }
    _atomic_private(_action_path(action_id, request_dir), record)
    return record


def _read_secret(secret_path: Path) -> bytes:
    try:
        value = secret_path.read_text().strip().encode()
    except OSError as exc:
        raise CleanerReassignmentError("action signing key unavailable") from exc
    if not value:
        raise CleanerReassignmentError("action signing key unavailable")
    return value


def decision_callback(action_id: str, operation: str, *, secret_path: Path = ACTION_SECRET_PATH) -> str:
    if operation not in {"reassign_original", "continue_replacement"}:
        raise CleanerReassignmentError("unsupported operator reassignment operation")
    key = _read_secret(secret_path)
    digest = hmac.new(key, f"{action_id}:{operation}".encode(), hashlib.sha256).hexdigest()[:16]
    return f"r:{action_id}:{operation}:{digest}"


def decision_keyboard(action_id: str, *, secret_path: Path = ACTION_SECRET_PATH) -> str:
    return json.dumps({"inline_keyboard": [[
        {"text": "원래 Cleaner 재배정", "callback_data": decision_callback(action_id, "reassign_original", secret_path=secret_path)},
        {"text": "대체 계속", "callback_data": decision_callback(action_id, "continue_replacement", secret_path=secret_path)},
    ]]}, ensure_ascii=False)


def notify_reassignment_operator(
    record: dict, *, secret_path: Path = ACTION_SECRET_PATH,
    sender: Callable[[int, str], object] | None = None,
) -> list[object]:
    """Downstream OPS notification. Tests inject sender; no domain mutation depends on it."""

    text = (
        "원래 Cleaner가 다시 가능하다고 요청했습니다.\n"
        "현재 대체 배정 상태를 확인한 뒤 한 가지 결정을 선택하세요."
    )
    keyboard = decision_keyboard(record["action_id"], secret_path=secret_path)
    if sender is not None:
        return [sender(chat_id, text, reply_markup=keyboard) for chat_id in sorted(OpsAllowlistProvider().allowed_chat_ids())]
    router = ops_outbound_router(api)
    return [router.send_message(chat_id, text, reply_markup=keyboard) for chat_id in sorted(OpsAllowlistProvider().allowed_chat_ids())]


def original_reassignment_decision_committed(
    *, request_dir: Path = REQUEST_DIR, cleaning_page_id: str,
) -> bool:
    """Bounded local fence used only by replacement Offer ACCEPT."""

    for path in request_dir.glob("*.json"):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            value.get("action_type") == ACTION_TYPE
            and value.get("cleaning_page_id") == cleaning_page_id
            and value.get("decision_committed") is True
            and value.get("decision") == REASSIGNED_ORIGINAL
        ):
            return True
    return False


def supersede_pending_replacement_offers(
    *, request_dir: Path, cleaning_page_id: str,
    original_cleaner_party_page_id: str, decision_action_id: str,
    now: datetime | None = None, atomic_writer: Callable[[Path, dict], None] = _atomic_private,
) -> dict:
    """Terminalize only still-actionable replacement Offers for this Cleaning."""

    now = now or datetime.now(timezone.utc)
    superseded: list[str] = []
    failures: list[dict] = []
    for path in sorted(request_dir.glob("*.json")):
        try:
            offer = json.loads(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            offer.get("action_type") != "CLEANING_ASSIGNMENT"
            or offer.get("cleaning_page_id") != cleaning_page_id
            or offer.get("candidate_party_page_id") == original_cleaner_party_page_id
            or offer.get("status") != "PENDING"
            or offer.get("consumed") is not False
        ):
            continue
        try:
            if now > _aware(offer.get("expires_at"), "Offer expires_at"):
                continue
        except CleanerReassignmentError:
            # Malformed expiry is not truthfully a still-PENDING actionable Offer.
            continue
        offer["status"] = SUPERSEDED
        offer["consumed"] = True
        offer["consumed_at"] = now.isoformat()
        offer["superseded_cause"] = SUPERSEDED_CAUSE
        offer["superseded_by_decision_action_id"] = decision_action_id
        try:
            atomic_writer(path, offer)
        except Exception as exc:  # preserve per-Offer progress; reconciliation is explicit
            if isinstance(exc, ProductionWriterError):
                raise
            failures.append({"action_id": offer.get("action_id"), "error_type": type(exc).__name__})
        else:
            superseded.append(offer["action_id"])
    return {"superseded": superseded, "failures": failures}


def _validate_cleaning_and_reservation(
    record: dict, *, page_reader: Callable[[str], dict], require_reassignable: bool,
) -> dict:
    cleaning = page_reader(record["cleaning_page_id"])
    if _select(cleaning, "데이터 환경") != "PRODUCTION" or _select(cleaning, "등록 상태") != "APPROVED":
        raise CleanerReassignmentError("Cleaning is not approved production authority")
    reservation_ids = _relations(cleaning, "관련 Reservation")
    if len(reservation_ids) != 1:
        raise CleanerReassignmentError("Cleaning Reservation authority is ambiguous")
    reservation = page_reader(reservation_ids[0])
    if (
        _select(reservation, "데이터 환경") != "PRODUCTION"
        or _select(reservation, "등록 상태") != "APPROVED"
        or _select(reservation, "상태") != "확정"
    ):
        raise CleanerReassignmentError("Reservation is no longer active")
    if require_reassignable:
        state = _select(cleaning, "상태")
        acceptance = _select(cleaning, "배정 수락 상태")
        assignees = _relations(cleaning, "담당 참여자/업체")
        if state != "담당자 배정" or acceptance != "대체필요" or assignees:
            raise CleanerReassignmentError("Cleaning is no longer reassignable")
    return cleaning


def _reassignment_economics(original: dict, record: dict) -> dict:
    snapshot = accepted_assignment_compensation_snapshot(
        original["page"], action_id=record["accepted_assignment_action_id"]
    )
    if not snapshot:
        raise CleanerReassignmentError(REASSIGNMENT_ECONOMICS_BLOCKED)
    if snapshot.get("legacy_normal_fallback") is True:
        return {"legacy_base_fee_krw": snapshot["base_fee_krw"]}
    required = {
        "replacement_urgency", "base_fee_krw", "urgent_premium_krw",
        "total_agreed_fee_krw", "urgent_premium_policy_version",
    }
    if not required.issubset(snapshot):
        raise CleanerReassignmentError(REASSIGNMENT_ECONOMICS_BLOCKED)
    return {name: snapshot[name] for name in required}


def _decision_identities(record: dict) -> tuple[str, str]:
    version = f"reassign-{record['action_id']}"
    payload = "|".join((
        record["action_id"], record["request_action_id"], record["request_key"],
        record["cleaning_page_id"], record["cleaner_party_page_id"], version,
    ))
    key = "CLEANER_REASSIGNMENT_ACCEPTED:v1:" + hashlib.sha256(payload.encode()).hexdigest()
    return version, key


def _default_projection(record: dict, page_reader: Callable[[str], dict]) -> dict:
    page = page_reader(record["cleaning_page_id"])
    state = _select(page, "상태")
    acceptance = _select(page, "배정 수락 상태")
    assignees = _relations(page, "담당 참여자/업체")
    original = record["cleaner_party_page_id"]
    if state == "담당자 배정" and acceptance == "수락" and assignees == [original]:
        return {"projection": "ALREADY_REASSIGNED", "next_expected_last_edited_time": page.get("last_edited_time")}
    if state != "담당자 배정" or acceptance != "대체필요" or assignees:
        raise CleanerReassignmentError("Cleaning projection no longer permits original reassignment")
    now = datetime.now(timezone.utc).isoformat()
    memo_prop = page.get("properties", {}).get("점검 메모", {})
    rich = memo_prop.get("rich_text") if isinstance(memo_prop, dict) else None
    memo = "".join(
        item.get("plain_text", "") for item in rich
        if isinstance(item, dict)
    ) if isinstance(rich, list) else ""
    line = f"[{now}] 운영자 원래 Cleaner 재배정"
    updated = _notion("PATCH", f"/v1/pages/{record['cleaning_page_id']}", {"properties": {
        "상태": {"select": {"name": "담당자 배정"}},
        "배정 수락 상태": {"select": {"name": "수락"}},
        "담당 참여자/업체": {"relation": [{"id": original}]},
        "Sync Status": {"select": {"name": "NEEDS_SYNC"}},
        "청소 대금 상태": {"select": {"name": "미생성"}},
        "점검 메모": {"rich_text": [{"type": "text", "text": {"content": f"{memo}\n{line}".strip()[-1900:]}}]},
    }})
    return {"projection": "REASSIGNED", "next_expected_last_edited_time": updated.get("last_edited_time")}


def _persist_status(path: Path, record: dict, status: str, **extra) -> dict:
    record["status"] = status
    record.update(extra)
    _atomic_private(path, record)
    return record


def execute_reassignment_decision(
    action_id: str, decision: str, *, request_dir: Path = REQUEST_DIR,
    history_store=None, page_reader: Callable[[str], dict] | None = None,
    projection_writer: Callable[[dict, Callable[[str], dict]], dict] | None = None,
    now: datetime | None = None, post_notifier: Callable[[dict], object] | None = None,
    offer_atomic_writer: Callable[[Path, dict], None] = _atomic_private,
) -> dict:
    """Execute/reconcile exactly one frozen operator decision under one Cleaning lock."""

    if decision not in {REASSIGNED_ORIGINAL, CONTINUE_REPLACEMENT}:
        raise CleanerReassignmentError("unsupported reassignment decision")
    now = now or datetime.now(timezone.utc)
    page_reader = page_reader or (lambda page_id: _notion("GET", f"/v1/pages/{page_id}"))
    history_store = history_store or NotionAssignmentHistoryStore()
    projection_writer = projection_writer or _default_projection
    path = _action_path(action_id, request_dir)
    initial = _load(path)
    if initial.get("action_id") != action_id or initial.get("action_type") != ACTION_TYPE:
        raise CleanerReassignmentError("reassignment decision identity mismatch")

    with cleaning_assignment_lock(cleaning_page_id=initial["cleaning_page_id"]):
        record = _load(path)
        source = _decision_source(record, request_dir)
        committed = record.get("decision") if record.get("decision_committed") is True else None
        if committed and committed != decision:
            raise CleanerReassignmentError("reassignment request already resolved differently")

        original = resolve_ended_unavailable_assignment(
            store=history_store,
            history_page_id=record["history_page_id"],
            cleaning_page_id=record["cleaning_page_id"],
            cleaner_party_page_id=record["cleaner_party_page_id"],
            assignment_version=record["assignment_version"],
            action_id=record["accepted_assignment_action_id"],
            idempotency_key=record["acceptance_idempotency_key"],
            end_key=record["end_key"],
        )
        if original["ended_at"] != _aware(record["original_ended_at"], "original_ended_at").isoformat():
            raise CleanerReassignmentError("original Assignment end authority changed")
        if _select(original["page"], "Reassignment Request Status") not in {
            REASSIGNMENT_REQUESTED, decision,
        }:
            raise CleanerReassignmentError("reassignment request is no longer open")
        if _rich_text(original["page"], "Reassignment Request Key") != record["request_key"]:
            raise CleanerReassignmentError("reassignment request key changed")
        if source.get("status") not in {REASSIGNMENT_REQUESTED, REASSIGNED_ORIGINAL, CONTINUE_REPLACEMENT}:
            raise CleanerReassignmentError("source reassignment request is no longer actionable")

        # Once the decision is committed, retries reconcile downstream writes and
        # must not require mutable pre-decision 08 state again.
        if not committed:
            _validate_cleaning_and_reservation(
                record, page_reader=page_reader,
                require_reassignable=(decision == REASSIGNED_ORIGINAL),
            )
            if newer_accepted_assignment_exists(
                store=history_store,
                cleaning_page_id=record["cleaning_page_id"],
                original_assignment_version=record["assignment_version"],
                original_ended_at=original["ended_at"],
            ):
                raise CleanerReassignmentError("newer accepted Assignment already exists")
            record["decision"] = decision
            record["decision_committed"] = True
            record["decision_at"] = now.isoformat()
            record["consumed"] = True
            record["consumed_at"] = now.isoformat()
            if decision == REASSIGNED_ORIGINAL:
                try:
                    economics = _reassignment_economics(original, record)
                except (AssignmentHistoryError, CleanerReassignmentError):
                    _persist_status(path, record, REASSIGNMENT_ECONOMICS_BLOCKED)
                    raise CleanerReassignmentError(REASSIGNMENT_ECONOMICS_BLOCKED)
                version, key = _decision_identities(record)
                record["reassignment_assignment_version"] = version
                record["reassignment_idempotency_key"] = key
                record["reassignment_economics"] = economics
            _atomic_private(path, record)

        if decision == CONTINUE_REPLACEMENT:
            try:
                history_store.resolve_reassignment_request(
                    record["history_page_id"],
                    cleaning_page_id=record["cleaning_page_id"],
                    cleaner_party_page_id=record["cleaner_party_page_id"],
                    assignment_version=record["assignment_version"],
                    action_id=record["accepted_assignment_action_id"],
                    idempotency_key=record["acceptance_idempotency_key"],
                    end_key=record["end_key"], request_key=record["request_key"],
                    resolution=CONTINUE_REPLACEMENT,
                )
            except ProductionWriterError:
                raise
            except Exception as exc:
                return _persist_status(
                    path, record, REQUEST_RESOLUTION_RECONCILIATION_REQUIRED,
                    request_resolution_error_type=type(exc).__name__,
                )
            source["status"] = CONTINUE_REPLACEMENT
            _atomic_private(_action_path(source["action_id"], request_dir), source)
            _persist_status(path, record, CONTINUE_REPLACEMENT_COMPLETE)
        else:
            cleanup = supersede_pending_replacement_offers(
                request_dir=request_dir,
                cleaning_page_id=record["cleaning_page_id"],
                original_cleaner_party_page_id=record["cleaner_party_page_id"],
                decision_action_id=record["action_id"], now=now,
                atomic_writer=offer_atomic_writer,
            )
            record["superseded_offer_action_ids"] = sorted(set(
                record.get("superseded_offer_action_ids", []) + cleanup["superseded"]
            ))
            record["offer_cleanup_failures"] = cleanup["failures"]
            _atomic_private(path, record)

            try:
                assignment = record_reassigned_original_assignment(
                    store=history_store, action_id=record["action_id"]
                )
            except ProductionWriterError:
                raise
            except Exception as exc:
                return _persist_status(
                    path, record, ASSIGNMENT_RECONCILIATION_REQUIRED,
                    assignment_error_type=type(exc).__name__,
                )
            record["assignment_materialized"] = True
            record["reassigned_assignment_page_id"] = assignment.page_id
            _atomic_private(path, record)

            try:
                projection = projection_writer(record, page_reader)
            except ProductionWriterError:
                raise
            except Exception as exc:
                return _persist_status(
                    path, record, PROJECTION_RECONCILIATION_REQUIRED,
                    projection_error_type=type(exc).__name__,
                )
            record["projection_status"] = projection.get("projection", "REASSIGNED")
            record["projected_last_edited_time"] = projection.get("next_expected_last_edited_time")
            _atomic_private(path, record)

            try:
                history_store.resolve_reassignment_request(
                    record["history_page_id"],
                    cleaning_page_id=record["cleaning_page_id"],
                    cleaner_party_page_id=record["cleaner_party_page_id"],
                    assignment_version=record["assignment_version"],
                    action_id=record["accepted_assignment_action_id"],
                    idempotency_key=record["acceptance_idempotency_key"],
                    end_key=record["end_key"], request_key=record["request_key"],
                    resolution=REASSIGNED_ORIGINAL,
                )
            except ProductionWriterError:
                raise
            except Exception as exc:
                return _persist_status(
                    path, record, REQUEST_RESOLUTION_RECONCILIATION_REQUIRED,
                    request_resolution_error_type=type(exc).__name__,
                )
            source["status"] = REASSIGNED_ORIGINAL
            _atomic_private(_action_path(source["action_id"], request_dir), source)
            final_status = OFFER_CLEANUP_RECONCILIATION_REQUIRED if cleanup["failures"] else REASSIGNMENT_COMPLETE
            _persist_status(path, record, final_status)

        if post_notifier is not None:
            try:
                post_notifier(record)
            except Exception as exc:
                if isinstance(exc, ProductionWriterError):
                    raise
                record["notification_status"] = NOTIFICATION_RETRY_REQUIRED
                record["notification_error_type"] = type(exc).__name__
                _atomic_private(path, record)
            else:
                record["notification_status"] = "DELIVERED"
                record.pop("notification_error_type", None)
                _atomic_private(path, record)
        return record


def handle_ops_reassignment_callback(
    update: dict, token: str, *, request_api: Callable,
    request_dir: Path = REQUEST_DIR, secret_path: Path = ACTION_SECRET_PATH,
    history_store=None, page_reader: Callable[[str], dict] | None = None,
    projection_writer=None, now: datetime | None = None,
) -> str | None:
    callback = update.get("callback_query") or {}
    data = callback.get("data")
    if not isinstance(data, str) or not data.startswith("r:"):
        return None
    parts = data.split(":")
    if len(parts) != 4:
        return "reassignment_invalid_callback"
    _prefix, action_id, operation, supplied = parts
    operation_map = {
        "reassign_original": REASSIGNED_ORIGINAL,
        "continue_replacement": CONTINUE_REPLACEMENT,
    }
    decision = operation_map.get(operation)
    if decision is None:
        return "reassignment_invalid_callback"
    try:
        record = _load(_action_path(action_id, request_dir))
        if record.get("action_id") != action_id or record.get("action_type") != ACTION_TYPE:
            raise CleanerReassignmentError("reassignment action mismatch")
        expected = decision_callback(action_id, operation, secret_path=secret_path).rsplit(":", 1)[-1]
        if not hmac.compare_digest(expected, supplied):
            raise CleanerReassignmentError("reassignment signature mismatch")
        if not record.get("decision_committed") and (now or datetime.now(timezone.utc)) > _aware(record["expires_at"], "expires_at"):
            raise CleanerReassignmentError("reassignment action expired")
        result = execute_reassignment_decision(
            action_id, decision, request_dir=request_dir, history_store=history_store,
            page_reader=page_reader, projection_writer=projection_writer, now=now,
        )
        if result["status"] in {
            REASSIGNMENT_COMPLETE, CONTINUE_REPLACEMENT_COMPLETE,
            OFFER_CLEANUP_RECONCILIATION_REQUIRED,
        }:
            text = "원래 담당자 재배정이 처리되었습니다." if decision == REASSIGNED_ORIGINAL else "대체 담당자 진행을 유지합니다."
        else:
            text = "결정은 기록되었고 일부 후속 반영은 재처리가 필요합니다."
        request_api(token, "sendMessage", chat_id=callback["message"]["chat"]["id"], text=text)
        return f"reassignment_{result['status'].lower()}"
    except (CleanerReassignmentError, AssignmentHistoryError, ValueError, OSError):
        chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
        if chat_id is not None:
            request_api(token, "sendMessage", chat_id=chat_id, text="현재 상태가 변경되어 이 결정을 처리할 수 없습니다.")
        return "reassignment_stale_fail_closed"

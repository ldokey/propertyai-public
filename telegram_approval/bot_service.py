#!/usr/bin/env python3
"""Transitional Telegram domain handlers; not a credential router or poller.

Stage-1 keeps these functions for Cleaner behavior regression while disabling
the former mixed-identity executable. Role-specific routers and poller state
live in ``ops_bot_service`` and ``cleaner_bot_service``.
"""

import hashlib
import hmac
import json
import os
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from telegram_approval.cleaner_config import CleanerRuntimePaths  # noqa: E402
from telegram_approval.cleaner_performance import (  # noqa: E402
    APPLIED as PERFORMANCE_APPLIED,
    POLICY_RESOLUTION_REQUIRED,
    RECONCILIATION_REQUIRED as PERFORMANCE_RECONCILIATION_REQUIRED,
    URGENT_ACCEPTED,
    URGENT_COMPLETED,
    InMemoryPerformanceEventStore,
    InMemoryPerformancePolicyStore,
    NotionPerformanceEventStore,
    NotionPerformancePolicyStore,
    append_performance_event,
    build_event as build_performance_event,
    event_key as performance_event_key,
)
from propertyai_core.global_writer import ProductionWriterError, assert_current_production_writer  # noqa: E402
from telegram_approval.telegram_transport import TelegramBotContext, TelegramHttpTransport  # noqa: E402


OPERATOR_PATH = ROOT / "secrets" / "telegram" / "operator.json"
RUNTIME = ROOT / "telegram_approval" / "runtime"
PAIRING_PATH = RUNTIME / "pairing.json"
METADATA_PATH = RUNTIME / "bot-metadata.json"
ACTION_SECRET_PATH = ROOT / "secrets" / "telegram" / "action-secret"
REQUEST_DIR = CleanerRuntimePaths().request_dir
DOOR_CODE_REQUEST_DIR = CleanerRuntimePaths().door_code_request_dir
_TRANSPORT = TelegramHttpTransport()
PERFORMANCE_POLICY_STORE_FACTORY = NotionPerformancePolicyStore
PERFORMANCE_EVENT_STORE_FACTORY = NotionPerformanceEventStore

CLEANER_CALLBACK_ACTION_TYPES = frozenset({
    "CLEANING_ASSIGNMENT",
    "CLEANING_COMPLETION",
    "CLEANING_COMPLETION_SUBMISSION",
    "CLEANING_DAY_CONFIRM",
    "CLEANING_ARRIVAL",
    "CLEANING_START",
    "CLEANING_ISSUE_SUBMISSION",
})
OPS_CALLBACK_ACTION_TYPES = frozenset({
    None,  # legacy TEST approval records predate explicit action_type
    "CANCEL_RESERVATION_WORKFLOW",
    "CLEANING_EVIDENCE_REVIEW",
    "CLEANING_PAYMENT_CONFIRMATION",
})

sys.path.insert(0, str(ROOT / "outputs" / "cleaning_automation"))
from door_code_capture import build_cleaner_message  # noqa: E402
from notion_door_code_writer import (  # noqa: E402
    NotionDoorCodeWriteError,
    update_reservation_door_code,
)


def atomic_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _converge_phase3_urgent_event(
    record, path, *, event_type, business_key, occurred_at, assignment_page_id=None,
    policy_store=None, event_store=None,
):
    """Append one downstream urgent performance fact without owning its business mutation."""

    if record.get("test_mode") or record.get("replacement_urgency") != "URGENT":
        record["performance_event_status"] = "NOT_APPLICABLE"
        atomic_private(path, record)
        return "NOT_APPLICABLE"
    occurred = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
    if occurred.tzinfo is None or occurred.utcoffset() is None:
        raise ValueError("urgent performance occurrence must be timezone-aware")
    key = performance_event_key(business_key=business_key, event_type=event_type)
    record["performance_event_key"] = key
    record["performance_event_type"] = event_type
    try:
        policy = (policy_store or PERFORMANCE_POLICY_STORE_FACTORY()).resolve_at(occurred)
    except Exception as exc:
        record["performance_event_status"] = POLICY_RESOLUTION_REQUIRED
        record["performance_event_error_type"] = type(exc).__name__
        atomic_private(path, record)
        return POLICY_RESOLUTION_REQUIRED
    event = build_performance_event(
        business_key=business_key,
        event_type=event_type,
        cleaner_party_page_id=record["candidate_party_page_id"],
        cleaning_page_id=record["cleaning_page_id"],
        assignment_page_id=assignment_page_id or record.get("assignment_page_id"),
        assignment_version=record.get("assignment_version") or record.get("action_id"),
        occurred_at=occurred,
        policy=policy,
        replacement_urgency="URGENT",
        data_environment="PRODUCTION",
    )
    try:
        append_performance_event(
            store=event_store or PERFORMANCE_EVENT_STORE_FACTORY(), event=event
        )
    except ProductionWriterError:
        record["performance_event_status"] = PERFORMANCE_RECONCILIATION_REQUIRED
        record["performance_event_error_type"] = "ProductionWriterError"
        atomic_private(path, record)
        raise
    except Exception as exc:
        record["performance_event_status"] = PERFORMANCE_RECONCILIATION_REQUIRED
        record["performance_event_error_type"] = type(exc).__name__
        atomic_private(path, record)
        return PERFORMANCE_RECONCILIATION_REQUIRED
    record["performance_event_status"] = PERFORMANCE_APPLIED
    record["performance_policy_version"] = policy.version
    record.pop("performance_event_error_type", None)
    atomic_private(path, record)
    return PERFORMANCE_APPLIED


def _converge_urgent_accepted_event(record, path, *, policy_store=None, event_store=None):
    if not record.get("assignment_history_operation_identity"):
        raise ValueError("urgent accepted Event requires durable assignment operation identity")
    if not record.get("assignment_history_page_id"):
        raise ValueError("urgent accepted Event requires canonical Assignment history page")
    return _converge_phase3_urgent_event(
        record, path, event_type=URGENT_ACCEPTED,
        business_key=record["assignment_history_operation_identity"],
        occurred_at=record["executed_at"],
        assignment_page_id=record["assignment_history_page_id"],
        policy_store=policy_store, event_store=event_store,
    )


def _converge_urgent_completed_event(record, path, *, policy_store=None, event_store=None):
    return _converge_phase3_urgent_event(
        record, path, event_type=URGENT_COMPLETED,
        business_key=f"CLEANING_COMPLETED:{record['cleaning_page_id']}:{record['action_id']}",
        occurred_at=record["executed_at"],
        assignment_page_id=record.get("assignment_page_id"),
        policy_store=policy_store, event_store=event_store,
    )


def _is_receipt_backed_assignment_execution(record, decision, *, secret_path):
    """Allow replay to enter history recovery, never assignment execution."""

    if (
        decision != "approve"
        or record.get("action_type") != "CLEANING_ASSIGNMENT"
        or record.get("test_mode") is not False
        or record.get("consumed") is not True
        or record.get("status") != "EXECUTED"
        or not isinstance(record.get("assignment_execution_receipt"), dict)
    ):
        return False
    try:
        from telegram_approval.assignment_execution_receipt import (
            verify_assignment_execution_receipt,
        )

        verify_assignment_execution_receipt(record, secret_path=secret_path)
    except Exception:
        return False
    return True


def _recover_durable_assignment_execution(
    record, path, decision, *, secret_path
):
    """Project immutable factual evidence only under fresh writer authority."""

    if (
        decision != "approve"
        or record.get("action_type") != "CLEANING_ASSIGNMENT"
        or record.get("test_mode") is not False
        or record.get("consumed") is not True
        or record.get("status") != "APPROVED_PRODUCTION"
    ):
        return False
    try:
        from telegram_approval.assignment_execution_receipt import (
            acceptance_operation_identity,
            load_durable_successful_assignment_execution,
        )

        factual = load_durable_successful_assignment_execution(
            path, record, secret_path=secret_path
        )
    except Exception:
        return False

    # Everything below is authoritative result/domain projection.  The stale
    # effect owner never reaches it; only the fresh successor may publish it.
    assert_current_production_writer()
    record["assignment_execution_receipt"] = factual[
        "assignment_execution_receipt"
    ]
    record["execution_result"] = factual["execution_result"]
    record["executed_at"] = factual["executed_at"]
    record["status"] = "EXECUTED"
    record["external_writes_executed"] = record.get(
        "external_writes_on_approval", 0
    )
    record["pre_effect_operation_identity"] = factual[
        "pre_effect_operation_identity"
    ]
    record["assignment_history_operation_identity"] = (
        acceptance_operation_identity(record, secret_path=secret_path)
    )
    record["assignment_history_status"] = "PENDING"
    atomic_private(path, record)
    return True


def _materialize_assignment_history(record, path, *, store=None):
    """Converge one authenticated ACCEPTED generation into durable history."""

    from telegram_approval.assignment_history import (
        NotionAssignmentHistoryStore,
        record_accepted_assignment,
    )

    history_store = store if store is not None else NotionAssignmentHistoryStore()
    operation_identity = record.get("assignment_history_operation_identity")
    result = record_accepted_assignment(
        store=history_store,
        action_id=record["action_id"],
        idempotency_key=operation_identity,
    )
    record["assignment_history_status"] = "MATERIALIZED"
    record["assignment_history_result"] = result.status
    record["assignment_history_page_id"] = result.page_id
    record.pop("assignment_history_error_type", None)
    record.pop("assignment_history_error_code", None)
    atomic_private(path, record)
    return result


def _persist_assignment_history_failure(record, path, exc):
    """Keep factual execution successful while exposing history recovery state."""

    record["assignment_history_status"] = "RETRY_REQUIRED"
    record["assignment_history_error_type"] = type(exc).__name__
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        record["assignment_history_error_code"] = code
    else:
        record.pop("assignment_history_error_code", None)
    atomic_private(path, record)


def _converge_assignment_history(record, path, *, store=None):
    """History recovery is separate from factual Cleaning execution success."""

    try:
        _materialize_assignment_history(record, path, store=store)
    except ProductionWriterError:
        # A stale owner must stop immediately.  The already-persisted receipt and
        # PENDING operation identity remain sufficient for a fresh writer replay.
        raise
    except Exception as exc:
        try:
            _persist_assignment_history_failure(record, path, exc)
        except Exception:
            # Failure metadata is secondary.  Never rewrite EXECUTED or discard
            # the authenticated receipt merely because this local write failed.
            pass
        return type(exc).__name__
    return None


def api(token, method, **values):
    return _TRANSPORT.request(TelegramBotContext(token), method, **values)


def answer_callback(
    token, callback_query_id, text, *, show_alert=False, request_api=None
):
    """A callback toast is best-effort and must never block the update queue."""
    request_api = request_api or api
    try:
        request_api(
            token,
            "answerCallbackQuery",
            callback_query_id=callback_query_id,
            text=text,
            **({"show_alert": "true"} if show_alert else {}),
        )
        return True
    except urllib.error.HTTPError:
        return False


def pair(update, token, *, cleaner_only=False, request_api=None):
    request_api = request_api or api
    message = update.get("message", {})
    sender = message.get("from", {})
    chat = message.get("chat", {})
    text = message.get("text", "")
    if not text.startswith("/start ") or chat.get("type") != "private" or sender.get("is_bot"):
        return False
    code = text.split(maxsplit=1)[1].strip()
    if code.startswith("c_"):
        from telegram_approval.cleaner_registry import consume_invite
        cleaner = consume_invite(
            code[2:],
            telegram_user_id=sender["id"],
            telegram_chat_id=chat["id"],
        )
        if not cleaner:
            request_api(token, "sendMessage", chat_id=chat["id"], text="초대 링크가 유효하지 않거나 이미 사용되었습니다.")
            return False
        request_api(
            token,
            "sendMessage",
            chat_id=chat["id"],
            text=(
                "✅ PropertyAI 청소 담당자 등록 완료\n\n"
                f"현재 승인된 숙소: {len(cleaner['properties'])}곳\n\n"
                "숙소 신청:\n"
                "/properties"
            ),
        )
        return "cleaner_paired"
    if cleaner_only:
        return False
    pairing = json.loads(PAIRING_PATH.read_text()) if PAIRING_PATH.exists() else {}
    valid = (
        not pairing.get("consumed")
        and datetime.now(timezone.utc) <= datetime.fromisoformat(pairing.get("expires_at", "1970-01-01T00:00:00+00:00"))
        and hashlib.sha256(code.encode()).hexdigest() == pairing.get("code_hash")
        and not OPERATOR_PATH.exists()
    )
    if not valid:
        request_api(token, "sendMessage", chat_id=chat["id"], text="페어링 코드가 유효하지 않거나 만료되었습니다.")
        return False
    operator = {
        "schema_version": 1,
        "telegram_user_id": sender["id"],
        "telegram_chat_id": chat["id"],
        "paired_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_private(OPERATOR_PATH, operator)
    pairing["consumed"] = True
    pairing["consumed_at"] = datetime.now(timezone.utc).isoformat()
    atomic_private(PAIRING_PATH, pairing)
    metadata = json.loads(METADATA_PATH.read_text())
    metadata["operator_paired"] = True
    metadata["operator_ref_hash"] = hashlib.sha256(str(sender["id"]).encode()).hexdigest()
    metadata["operator_identity_stored_in_runtime"] = False
    atomic_private(METADATA_PATH, metadata)
    api(token, "sendMessage", chat_id=chat["id"], text="✅ PropertyAI 운영자 페어링 완료\n현재는 승인 테스트 모드이며 외부 시스템 쓰기는 비활성화되어 있습니다.")
    return True


def callback(
    update,
    token,
    *,
    request_dir=None,
    action_secret_path=None,
    allowed_action_types=None,
    preauthorized_identity=False,
    operator_path=None,
    request_api=None,
    assignment_history_store=None,
    _cleaning_assignment_lock_held=False,
):
    """Consume one callback only from the explicitly supplied role state."""
    query = update.get("callback_query")
    if not query:
        return None
    request_dir = request_dir or REQUEST_DIR
    action_secret_path = action_secret_path or ACTION_SECRET_PATH
    operator_path = operator_path or OPERATOR_PATH
    request_api = request_api or api

    def acknowledge(callback_query_id, text, *, show_alert=False):
        return answer_callback(
            token,
            callback_query_id,
            text,
            show_alert=show_alert,
            request_api=request_api,
        )
    parts = query.get("data", "").split(":")
    if len(parts) != 4 or parts[0] != "a" or parts[2] not in ("approve", "reject"):
        return "ignored"
    _, action_id, decision, supplied = parts
    path = request_dir / f"{action_id}.json"
    if not path.exists():
        acknowledge(query["id"], "요청을 찾을 수 없습니다.", show_alert=True)
        return "ignored"
    record = json.loads(path.read_text())
    action_type = record.get("action_type")
    if allowed_action_types is not None and action_type not in allowed_action_types:
        acknowledge(query["id"], "이 Bot에서 처리할 수 없는 요청입니다.", show_alert=True)
        return "ignored"

    sender = query.get("from", {})
    chat = query.get("message", {}).get("chat", {})
    if action_type in CLEANER_CALLBACK_ACTION_TYPES:
        expected_user_id = record.get("candidate_user_id")
        expected_chat_id = record.get("candidate_chat_id")
        authorized = (
            sender.get("id") == expected_user_id and chat.get("id") == expected_chat_id
        )
    elif preauthorized_identity:
        # OPS router already authorized this callback through OpsAllowlistProvider.
        authorized = True
    else:
        if not operator_path.exists():
            return "ignored"
        operator = json.loads(operator_path.read_text())
        authorized = (
            sender.get("id") == operator["telegram_user_id"]
            and chat.get("id") == operator["telegram_chat_id"]
        )
    if not authorized:
        acknowledge(query["id"], "권한이 없습니다.", show_alert=True)
        return "ignored"
    if record.get("action_type") == "CLEANING_ISSUE_SUBMISSION" and decision == "approve":
        from telegram_approval.cleaning_issue import session_ready
        if not session_ready(record):
            acknowledge(query["id"], "문제 사진을 한 장 이상 보낸 뒤 완료해주세요.", show_alert=True)
            return "ignored"
    if record.get("action_type") == "CLEANING_COMPLETION_SUBMISSION" and decision == "approve":
        from telegram_approval.cleaning_completion_evidence import session_ready
        if not session_ready(record):
            acknowledge(query["id"], "완료 사진을 최소 4장 보낸 뒤 제출해주세요.", show_alert=True)
            return "ignored"
    expected = hmac.new(action_secret_path.read_text().strip().encode(), f"{action_id}:{decision}".encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(expected, supplied):
        acknowledge(query["id"], "만료되었거나 이미 처리된 요청입니다.", show_alert=True)
        return "ignored"
    if decision == "approve" and not _cleaning_assignment_lock_held:
        cleaning_ids = []
        if action_type in {"CLEANING_ASSIGNMENT", "CLEANING_START"}:
            if record.get("cleaning_page_id"):
                cleaning_ids = [record["cleaning_page_id"]]
        elif action_type == "CANCEL_RESERVATION_WORKFLOW":
            cleaning_ids = sorted(set(record.get("cleaning_page_ids") or []))
        if cleaning_ids:
            from contextlib import ExitStack
            from telegram_approval.assignment_history import cleaning_assignment_lock

            # Deterministic Cleaning-key order prevents cross-Cleaning cancellation
            # callbacks from reversing lock acquisition. Any generation lock is
            # acquired only later by the Assignment-history mutation path.
            with ExitStack() as stack:
                for cleaning_id in sorted(cleaning_ids):
                    stack.enter_context(cleaning_assignment_lock(cleaning_page_id=cleaning_id))
                # Re-enter after acquiring the Cleaning-wide serialization key(s).
                # The inner call reloads durable callback state before consumption.
                return callback(
                    update, token, request_dir=request_dir,
                    action_secret_path=action_secret_path,
                    allowed_action_types=allowed_action_types,
                    preauthorized_identity=preauthorized_identity,
                    operator_path=operator_path, request_api=request_api,
                    assignment_history_store=assignment_history_store,
                    _cleaning_assignment_lock_held=True,
                )
    if record.get("status") == "SUPERSEDED":
        acknowledge(query["id"], "이 제안은 더 이상 수락할 수 없습니다.", show_alert=True)
        return "ignored"
    if record.get("consumed"):
        if (
            decision == "approve"
            and record.get("action_type") == "CLEANING_COMPLETION_SUBMISSION"
            and record.get("status") == "EXECUTED"
            and record.get("replacement_urgency") == "URGENT"
            and record.get("execution_result", {}).get("cleaning_completion")
                == "PHOTOS_SUBMITTED_REVIEW_REQUIRED"
            and record.get("performance_event_status") != PERFORMANCE_APPLIED
        ):
            acknowledge(query["id"], "완료 성과 이벤트 상태를 다시 확인합니다.")
            _converge_urgent_completed_event(record, path)
            request_api(
                token, "sendMessage", chat_id=chat["id"],
                text="✅ 청소 완료 사실은 유지되며 긴급 완료 성과 이벤트를 재조정했습니다.",
            )
            return "approved"
        receipt_backed = _is_receipt_backed_assignment_execution(
            record, decision, secret_path=action_secret_path
        )
        if not receipt_backed:
            receipt_backed = _recover_durable_assignment_execution(
                record, path, decision, secret_path=action_secret_path
            )
        if receipt_backed:
            acknowledge(query["id"], "배정 이력 상태를 다시 확인합니다.")
            history_error = _converge_assignment_history(
                record, path, store=assignment_history_store
            )
            if history_error:
                request_api(
                    token,
                    "sendMessage",
                    chat_id=chat["id"],
                    text="⚠️ 청소 수락은 반영됐지만 배정 이력 확정이 완료되지 않았습니다. 같은 실행 식별자로 복구가 필요합니다.",
                )
            else:
                if record.get("replacement_urgency") == "URGENT":
                    _converge_urgent_accepted_event(record, path)
                request_api(
                    token,
                    "sendMessage",
                    chat_id=chat["id"],
                    text="✅ 청소 수락·배정 이력 반영 완료 — 정산 생성 없음",
                )
            return "approved"
        acknowledge(query["id"], "만료되었거나 이미 처리된 요청입니다.", show_alert=True)
        return "ignored"
    expired = datetime.now(timezone.utc) > datetime.fromisoformat(record["expires_at"])
    if expired:
        acknowledge(query["id"], "만료되었거나 이미 처리된 요청입니다.", show_alert=True)
        return "ignored"
    if action_type == "CLEANING_ASSIGNMENT" and decision == "approve":
        from telegram_approval.cleaner_reassignment import (
            original_reassignment_decision_committed,
        )
        if original_reassignment_decision_committed(
            request_dir=request_dir, cleaning_page_id=record["cleaning_page_id"]
        ):
            acknowledge(query["id"], "이 제안은 더 이상 수락할 수 없습니다.", show_alert=True)
            return "ignored"
        from telegram_approval.assignment_history import (
            NotionAssignmentHistoryStore,
            other_effective_accepted_assignment_exists,
        )

        guard_store = assignment_history_store
        if guard_store is None:
            guard_store = NotionAssignmentHistoryStore()
        # Historical focused test stores predate this read seam. Production uses
        # the full canonical store; Phase-4 fakes implement it explicitly.
        if hasattr(guard_store, "query_accepted_for_cleaning") and (
            other_effective_accepted_assignment_exists(
                store=guard_store,
                cleaning_page_id=record["cleaning_page_id"],
                assignment_version=record["action_id"],
            )
        ):
            acknowledge(query["id"], "이 제안은 더 이상 수락할 수 없습니다.", show_alert=True)
            return "ignored"
    record["consumed"] = True
    record["consumed_at"] = datetime.now(timezone.utc).isoformat()
    record["status"] = ("APPROVED_TEST" if record.get("test_mode") else "APPROVED_PRODUCTION") if decision == "approve" else "REJECTED"
    record["external_writes_executed"] = 0
    # Persist single-use consumption before any external effects. A process
    # interruption must never make the same Telegram button executable twice.
    atomic_private(path, record)
    # Stop Telegram's loading spinner before Drive/Notion work begins. The
    # durable result is always delivered as a separate message below.
    acknowledge(query["id"], "요청을 접수했습니다. 처리 결과를 곧 전송합니다.")
    execution_error = None
    assignment_history_error = None
    next_action = None
    next_assignment = None
    issue_session = None
    should_execute = (
        record.get("action_type")
        and not record.get("test_mode")
        and (decision == "approve" or record.get("execute_on_reject"))
        and record.get("action_type") != "CLEANING_COMPLETION"
    )
    if should_execute:
        assignment_execution_intent = None
        successful_assignment_intent = (
            record.get("action_type") == "CLEANING_ASSIGNMENT"
            and decision == "approve"
        )
        if successful_assignment_intent:
            from telegram_approval.assignment_execution_receipt import (
                persist_assignment_execution_intent,
            )

            assignment_execution_intent = persist_assignment_execution_intent(
                path, record, decision, secret_path=action_secret_path
            )
            # Parent-side pre-effect fencing makes the assignment boundary
            # explicit even when the adapter is substituted in focused tests.
            assert_current_production_writer()
        try:
            from gmail_ingest.lifecycle_actions import execute_lifecycle_action
            if record.get("action_type") == "CANCEL_RESERVATION_WORKFLOW":
                execution = execute_lifecycle_action(
                    record, decision, history_store=assignment_history_store
                )
            else:
                execution = execute_lifecycle_action(record, decision)
        except ProductionWriterError:
            raise
        except Exception as exc:
            execution_error = type(exc).__name__
            record["status"] = "EXECUTION_RETRY_REQUIRED"
            record["execution_error_type"] = execution_error
        else:
            executed_at = datetime.now(timezone.utc).isoformat()
            successful_assignment = (
                record.get("action_type") == "CLEANING_ASSIGNMENT"
                and decision == "approve"
                and execution.get("cleaning_assignment") == "ACCEPTED"
            )
            if successful_assignment:
                from telegram_approval.assignment_execution_receipt import (
                    acceptance_operation_identity,
                    persist_successful_assignment_execution_receipt,
                )

                record["assignment_execution_receipt"] = (
                    persist_successful_assignment_execution_receipt(
                        path,
                        record,
                        execution,
                        executed_at=executed_at,
                        secret_path=action_secret_path,
                    )
                )
            # SC-13: factual evidence above is deliberately separate and
            # immutable.  No authoritative result/history state is published by
            # a writer that became stale during the external effect.
            assert_current_production_writer()
            record["execution_result"] = execution
            record["status"] = "EXECUTED"
            record["executed_at"] = executed_at
            effect_key = "external_writes_on_approval" if decision == "approve" else "external_writes_on_reject"
            record["external_writes_executed"] = record.get(effect_key, 0)
            if successful_assignment:
                record["pre_effect_operation_identity"] = (
                    assignment_execution_intent["operation_identity"]
                )
                # SC-03/06: freeze factual success + signed provenance + semantic
                # operation identity before entering independently recoverable
                # history persistence.  History failure can never downgrade this.
                record["assignment_history_operation_identity"] = (
                    acceptance_operation_identity(
                        record, secret_path=action_secret_path
                    )
                )
                record["assignment_history_status"] = "PENDING"
                atomic_private(path, record)
                assignment_history_error = _converge_assignment_history(
                    record, path, store=assignment_history_store
                )
                if assignment_history_error is None and record.get("replacement_urgency") == "URGENT":
                    _converge_urgent_accepted_event(record, path)
    atomic_private(path, record)
    if (
        not execution_error
        and record.get("action_type") == "CLEANING_COMPLETION"
        and decision == "approve"
    ):
        try:
            from telegram_approval.cleaning_completion_evidence import start_completion_session
            next_action = start_completion_session(record)
            record["next_action"] = next_action
            record["status"] = "PHOTO_SUBMISSION_REQUIRED_TEST" if record.get("test_mode") else "PHOTO_SUBMISSION_REQUIRED"
        except Exception as exc:
            record["next_action"] = {"sent": False, "error_type": type(exc).__name__}
        atomic_private(path, record)
    if (
        not execution_error
        and record.get("action_type") in ("CLEANING_ARRIVAL", "CLEANING_START")
        and decision == "reject"
        and (record.get("test_mode") or record.get("status") == "EXECUTED")
    ):
        try:
            from telegram_approval.cleaning_issue import start_issue_session
            issue_session = start_issue_session(
                record,
                expected_last_edited_time=record.get("execution_result", {}).get("next_expected_last_edited_time"),
            )
            record["issue_session"] = issue_session
        except Exception as exc:
            record["issue_session"] = {"sent": False, "error_type": type(exc).__name__}
        atomic_private(path, record)
    if record.get("action_type") == "CLEANING_ISSUE_SUBMISSION" and not execution_error:
        from telegram_approval.cleaning_issue import finish_session
        if decision == "reject" or record.get("test_mode") or record.get("status") == "EXECUTED":
            finish_session(record, submitted=decision == "approve")
    if (
        record.get("action_type") == "CLEANING_COMPLETION_SUBMISSION"
        and not execution_error
        and decision == "approve"
        and not record.get("test_mode")
        and record.get("status") == "EXECUTED"
        and record.get("replacement_urgency") == "URGENT"
    ):
        _converge_urgent_completed_event(record, path)
    if record.get("action_type") == "CLEANING_COMPLETION_SUBMISSION" and not execution_error:
        from telegram_approval.cleaning_completion_evidence import finish_session
        if decision == "reject" or record.get("test_mode") or record.get("status") == "EXECUTED":
            finish_session(record, submitted=decision == "approve")
    if (
        not execution_error
        and record.get("action_type") == "CLEANING_COMPLETION_SUBMISSION"
        and decision == "approve"
        and (record.get("test_mode") or record.get("status") == "EXECUTED")
    ):
        try:
            from telegram_approval.cleaning_completion import send_operator_review
            next_action = send_operator_review(record)
            record["next_action"] = next_action
        except ProductionWriterError:
            raise
        except Exception as exc:
            if not record.get("test_mode"):
                assert_current_production_writer()
            record["next_action"] = {"sent": False, "error_type": type(exc).__name__}
        if not record.get("test_mode"):
            assert_current_production_writer()
        atomic_private(path, record)
    if (
        not execution_error
        and record.get("action_type") == "CLEANING_EVIDENCE_REVIEW"
        and decision == "approve"
        and (record.get("test_mode") or record.get("status") == "EXECUTED")
    ):
        try:
            from telegram_approval.cleaning_completion import send_transfer_confirmation
            next_action = send_transfer_confirmation(record)
            record["next_action"] = next_action
        except ProductionWriterError:
            raise
        except Exception as exc:
            if not record.get("test_mode"):
                assert_current_production_writer()
            record["next_action"] = {"sent": False, "error_type": type(exc).__name__}
        if not record.get("test_mode"):
            assert_current_production_writer()
        atomic_private(path, record)
    if (
        not execution_error
        and record.get("status") == "EXECUTED"
        and record.get("action_type") == "CLEANING_ARRIVAL"
        and decision == "approve"
    ):
        try:
            from telegram_approval.cleaning_operations import send_next_stage
            next_action = send_next_stage(
                record,
                test_mode=False,
                expected_last_edited_time=record.get("execution_result", {}).get("next_expected_last_edited_time"),
            )
            record["next_action"] = next_action
        except ProductionWriterError:
            raise
        except Exception as exc:
            assert_current_production_writer()
            record["next_action"] = {"sent": False, "error_type": type(exc).__name__}
        assert_current_production_writer()
        atomic_private(path, record)
    if (
        not execution_error
        and record.get("test_mode")
        and record.get("action_type") in ("CLEANING_DAY_CONFIRM", "CLEANING_ARRIVAL")
        and decision == "approve"
    ):
        try:
            from telegram_approval.cleaning_operations import send_next_test_stage
            next_action = send_next_test_stage(record)
            record["next_action"] = next_action
        except Exception as exc:
            record["next_action"] = {"sent": False, "error_type": type(exc).__name__}
        atomic_private(path, record)
    if (
        not execution_error
        and record.get("status") == "EXECUTED"
        and record.get("action_type") == "CLEANING_ASSIGNMENT"
        and decision == "reject"
        and record.get("execution_result", {}).get("cleaning_assignment") == "NEXT_CANDIDATE_REQUIRED"
    ):
        try:
            from telegram_approval.cleaning_assignment import send_next_assignment
            next_assignment = send_next_assignment(
                record,
                record["execution_result"].get("next_expected_last_edited_time"),
            )
            record["next_assignment"] = next_assignment
        except Exception as exc:
            record["next_assignment"] = {"sent": False, "error_type": type(exc).__name__}
        atomic_private(path, record)
    if execution_error:
        message = "⚠️ 승인됐지만 반영 중 오류가 발생했습니다. 로컬 재시도가 필요합니다."
    elif assignment_history_error:
        message = "⚠️ 청소 수락은 반영됐지만 배정 이력 확정이 완료되지 않았습니다. 같은 실행 식별자로 복구가 필요합니다."
    elif record.get("status") == "EXECUTED" and record.get("action_type") == "CLEANING_ASSIGNMENT":
        if decision == "approve":
            message = "✅ 청소 수락·배정 이력 반영 완료 — 정산 생성 없음"
        elif next_assignment and next_assignment.get("sent"):
            message = "↪️ 청소 거절 반영 완료 — 다음 담당자에게 제안 전송"
        else:
            message = "↪️ 청소 거절 반영 완료 — 대체 담당자 필요"
    elif record.get("status") == "EXECUTED" and record.get("action_type") == "CLEANING_COMPLETION_SUBMISSION":
        message = (
            "✅ 완료 사진 Drive 저장·Cleaning 연결 — 운영자 검수 요청 전송"
            if decision == "approve" and next_action and next_action.get("sent")
            else "⏳ 완료 사진 제출 취소 — 상태 변경 없음"
        )
    elif record.get("status") == "EXECUTED" and record.get("action_type") == "CLEANING_EVIDENCE_REVIEW":
        message = (
            "✅ 완료 사진 검수 승인 — 운영자 이체 확인 요청 전송"
            if decision == "approve" and next_action and next_action.get("sent")
            else "🔁 완료 사진 보완 요청 — 이체 확인 차단"
        )
    elif record.get("status") == "EXECUTED" and record.get("action_type") == "CLEANING_PAYMENT_CONFIRMATION":
        message = (
            "✅ 이체 완료 반영 — Finance·인건비·Cleaning 정산 완료"
            if decision == "approve"
            else "⏸ 이체 보류 기록 — 정산 변경 없음"
        )
    elif record.get("status") == "EXECUTED" and record.get("action_type") == "CLEANING_DAY_CONFIRM":
        message = (
            "✅ 오늘 방문 확인 반영 완료"
            if decision == "approve"
            else "⚠️ 방문 불가 반영 — 대체 담당자 필요"
        )
    elif record.get("status") == "EXECUTED" and record.get("action_type") == "CLEANING_ARRIVAL":
        message = (
            "📍 도착 확인 반영 — 현장 문제 확인 화면 전송"
            if decision == "approve" and next_action and next_action.get("sent")
            else "⚠️ 출입 문제 반영 — 운영자 확인 필요"
        )
    elif record.get("status") == "EXECUTED" and record.get("action_type") == "CLEANING_START":
        message = (
            "🧹 청소 시작 반영 완료"
            if decision == "approve"
            else "⚠️ 현장 문제 반영 — 운영자 확인 필요"
        )
    elif record.get("status") == "EXECUTED" and record.get("action_type") == "CLEANING_ISSUE_SUBMISSION":
        message = "✅ 현장 문제·사진을 Drive에 검증 저장하고 Cleaning에 링크했습니다."
    elif record.get("status") == "EXECUTED":
        message = "✅ 예약 취소 반영 완료 — 예약·청소·캘린더·담당자 안내·Finance 검토 생성"
    elif not record.get("test_mode") and record.get("action_type") == "CLEANING_COMPLETION" and decision == "approve":
        message = (
            "📷 청소 완료 확인 — 완료 사진 제출 화면 전송"
            if next_action and next_action.get("sent")
            else "⚠️ 완료 사진 제출 화면 생성 실패 — 로컬 확인 필요"
        )
    elif record.get("test_mode") and record.get("action_type") == "CLEANING_ASSIGNMENT":
        message = (
            "✅ TEST 청소 수락 기록 완료 — Notion·Calendar·정산 변경 0건"
            if decision == "approve"
            else "❌ TEST 청소 거절 기록 완료 — 외부 변경 0건"
        )
    elif record.get("test_mode") and record.get("action_type") == "CLEANING_COMPLETION":
        message = (
            "📷 TEST 청소 완료 확인 — 완료 사진 제출 화면 전송"
            if decision == "approve" and next_action and next_action.get("sent")
            else "⏳ TEST 청소 미완료 기록 — 외부 변경 0건"
        )
    elif record.get("test_mode") and record.get("action_type") == "CLEANING_COMPLETION_SUBMISSION":
        message = (
            "✅ TEST 완료 사진 제출 — 운영자 검수 화면 전송, 외부 변경 0건"
            if decision == "approve" and next_action and next_action.get("sent")
            else "취소했습니다. — 외부 변경 0건"
        )
    elif record.get("test_mode") and record.get("action_type") == "CLEANING_EVIDENCE_REVIEW":
        message = (
            "✅ TEST 사진 검수 승인 — 이체 확인 화면 전송, 외부 변경 0건"
            if decision == "approve" and next_action and next_action.get("sent")
            else "🔁 TEST 사진 보완 요청 — 이체 확인 없음, 외부 변경 0건"
        )
    elif record.get("test_mode") and record.get("action_type") == "CLEANING_PAYMENT_CONFIRMATION":
        message = (
            "✅ TEST 이체 완료 확인 기록 — 정산 DB 변경 0건"
            if decision == "approve"
            else "⏸ TEST 이체 보류 기록 — 정산 DB 변경 0건"
        )
    elif record.get("test_mode") and record.get("action_type") == "CLEANING_DAY_CONFIRM":
        message = (
            "✅ TEST 오늘 방문 확인 — 도착 확인 화면 전송"
            if decision == "approve" and next_action and next_action.get("sent")
            else "⚠️ TEST 방문 어려움 기록 — 운영자 확인 필요, 외부 변경 0건"
        )
    elif record.get("test_mode") and record.get("action_type") == "CLEANING_ARRIVAL":
        message = (
            "📍 TEST 도착 확인 — 현장 문제 확인 화면 전송"
            if decision == "approve" and next_action and next_action.get("sent")
            else "⚠️ TEST 출입 문제 기록 — 운영자 확인 필요, 외부 변경 0건"
        )
    elif record.get("test_mode") and record.get("action_type") == "CLEANING_START":
        message = (
            "🧹 TEST 청소 시작 기록 완료 — 외부 변경 0건"
            if decision == "approve"
            else "⚠️ TEST 현장 문제 기록 — 운영자 확인 필요, 외부 변경 0건"
        )
    elif record.get("test_mode") and record.get("action_type") == "CLEANING_ISSUE_SUBMISSION":
        message = (
            "✅ TEST 문제·사진 보고 완료 — Notion 변경 0건"
            if decision == "approve"
            else "취소했습니다. — 외부 변경 0건"
        )
    elif record.get("action_type") in ("CLEANING_COMPLETION", "CLEANING_COMPLETION_SUBMISSION") and decision == "reject":
        message = "⏳ 아직 청소 중으로 기록 — Notion·정산 변경 없음"
    elif record.get("action_type") == "CLEANING_PAYMENT_CONFIRMATION" and decision == "reject":
        message = "⏸ 이체 보류 기록 — Finance·인건비·Cleaning 변경 없음"
    elif decision == "approve" and record.get("test_mode"):
        message = "✅ TEST 승인 기록 완료 — 외부 쓰기 0건"
    elif decision == "approve":
        message = "✅ 실제 쓰기 승인 기록 완료 — 대상 작업 1건"
    else:
        message = "❌ 거절 기록 완료 — 외부 쓰기 0건"
    request_api(token, "sendMessage", chat_id=chat["id"], text=message)
    return "approved" if decision == "approve" else "rejected"


def expire_cleaning_assignments(token, now=None):
    """Expire due cleaner proposals exactly once and route to the next candidate."""
    now = now or datetime.now(timezone.utc)
    operator = json.loads(OPERATOR_PATH.read_text()) if OPERATOR_PATH.exists() else None
    counts = {"expired": 0, "next_sent": 0, "replacement_required": 0, "errors": 0}
    for path in sorted(REQUEST_DIR.glob("*.json")):
        record = json.loads(path.read_text())
        if (
            record.get("action_type") != "CLEANING_ASSIGNMENT"
            or record.get("status") != "PENDING"
            or record.get("consumed")
            or now <= datetime.fromisoformat(record["expires_at"])
        ):
            continue
        record.update({
            "consumed": True,
            "consumed_at": now.isoformat(),
            "status": "EXPIRED",
            "assignment_response_reason": "EXPIRED_24H",
            "external_writes_executed": 0,
        })
        atomic_private(path, record)
        if record.get("test_mode"):
            record["status"] = "EXPIRED_TEST"
            record["expired_at"] = now.isoformat()
            api(
                token,
                "sendMessage",
                chat_id=record["candidate_chat_id"],
                text="⌛ TEST 청소 제안이 24시간 만료되었습니다. 외부 변경은 없습니다.",
            )
            counts["expired"] += 1
            atomic_private(path, record)
            continue
        try:
            from gmail_ingest.lifecycle_actions import execute_lifecycle_action
            execution = execute_lifecycle_action(record, "reject")
            record["execution_result"] = execution
            record["expired_at"] = now.isoformat()
            record["external_writes_executed"] = record.get("external_writes_on_reject", 0)
            api(
                token,
                "sendMessage",
                chat_id=record["candidate_chat_id"],
                text="⌛ 24시간 내 수락하지 않아 청소 제안이 만료되었습니다.",
            )
            if execution.get("cleaning_assignment") == "NEXT_CANDIDATE_REQUIRED":
                from telegram_approval.cleaning_assignment import send_next_assignment
                next_assignment = send_next_assignment(
                    record,
                    execution.get("next_expected_last_edited_time"),
                )
                record["next_assignment"] = next_assignment
                if next_assignment.get("sent"):
                    record["status"] = "EXPIRED_ROUTED"
                    counts["next_sent"] += 1
                else:
                    record["status"] = "EXPIRED_RETRY_REQUIRED"
                    counts["errors"] += 1
            else:
                record["status"] = "EXPIRED_REPLACEMENT_REQUIRED"
                counts["replacement_required"] += 1
                if operator:
                    api(
                        token,
                        "sendMessage",
                        chat_id=operator["telegram_chat_id"],
                        text=(
                            "⚠️ 청소 제안이 24시간 만료되었고 다음 담당자 후보가 없습니다.\n"
                            f"숙소: {record.get('property_nickname', '확인 필요')}\n"
                            f"청소 시작: {record.get('start_at', '확인 필요')}"
                        ),
                    )
            counts["expired"] += 1
        except Exception as exc:
            record["status"] = "EXPIRED_RETRY_REQUIRED"
            record["execution_error_type"] = type(exc).__name__
            counts["errors"] += 1
        atomic_private(path, record)
    return counts


def door_code_reply(update, token):
    """Consume an authorized reply to a pending regular-lock prompt."""
    message = update.get("message", {})
    reply_to = message.get("reply_to_message", {})
    if "text" not in message or not OPERATOR_PATH.exists():
        return None
    if message["text"].startswith("/"):
        return None

    operator = json.loads(OPERATOR_PATH.read_text())
    sender = message.get("from", {})
    chat = message.get("chat", {})
    authorized = (
        chat.get("type") == "private"
        and sender.get("id") == operator["telegram_user_id"]
        and chat.get("id") == operator["telegram_chat_id"]
    )
    if not authorized:
        return None

    now = datetime.now(timezone.utc)
    prompt_message_id = reply_to.get("message_id")
    candidates = []
    for path in sorted(DOOR_CODE_REQUEST_DIR.glob("*.json")):
        record = json.loads(path.read_text())
        if record.get("status") != "PENDING_OPERATOR_INPUT":
            continue
        if now > datetime.fromisoformat(record["expires_at"]):
            record["status"] = "EXPIRED"
            atomic_private(path, record)
            continue
        if prompt_message_id is not None and record.get("prompt_message_id") != prompt_message_id:
            continue
        candidates.append((path, record))

    # Telegram clients may omit reply_to_message for ForceReply.  A plain text
    # message is unambiguous when exactly one active capture request exists.
    if len(candidates) != 1:
        return None

    path, record = candidates[0]
    supplied = message["text"]
    outgoing = build_cleaner_message(
        record["base_cleaner_message"],
        supplied,
        smart_doorlock=record.get("smart_doorlock", False),
    )
    sent = api(token, "sendMessage", chat_id=record["delivery_chat_id"], text=outgoing)
    notion_write_status = "SKIPPED_TEST"
    if not record.get("test_mode"):
        reservation_page_id = record.get("reservation_page_id")
        if not reservation_page_id:
            notion_write_status = "BLOCKED_MISSING_RESERVATION_PAGE_ID"
        else:
            try:
                update_reservation_door_code(reservation_page_id, supplied)
                notion_write_status = "SYNCED"
            except NotionDoorCodeWriteError:
                notion_write_status = "FAILED_RETRY_REQUIRED"
    record.update({
        "status": "DELIVERED_TEST" if record.get("test_mode") else "DELIVERED",
        "captured_at": now.isoformat(),
        "delivered_at": now.isoformat(),
        "source_message_id": message.get("message_id"),
        "delivery_message_id": sent["message_id"],
        "airbnb_phone_last4": supplied,
        "input_preserved_exactly": True,
        "notion_write_status": notion_write_status,
    })
    atomic_private(path, record)
    if notion_write_status == "FAILED_RETRY_REQUIRED":
        api(token, "sendMessage", chat_id=chat["id"], text="Notion 출입 코드 저장에 실패했습니다. 로컬 재시도가 필요합니다.")
    elif notion_write_status == "BLOCKED_MISSING_RESERVATION_PAGE_ID":
        api(token, "sendMessage", chat_id=chat["id"], text="Reservation 연결 정보가 없어 Notion 저장을 보류했습니다.")
    return "door_code_delivered"


def handle_cleaner(
    update,
    token,
    *,
    request_dir=None,
    action_secret_path=None,
    request_api=None,
    now=None,
):
    """Cleaner-only inbound composition; OPS messages and requests are excluded."""
    request_api = request_api or api
    from telegram_approval.cleaner_application import handle_application_callback
    application_result = handle_application_callback(
        update,
        token,
        request_api=request_api,
        now=now,
    )
    if application_result:
        return application_result
    from telegram_approval.cleaner_property_access import handle_property_request_callback
    property_request_result = handle_property_request_callback(
        update, token, request_api=request_api, now=now
    )
    if property_request_result:
        return property_request_result
    from telegram_approval.cleaner_unavailable import handle_unavailable_callback
    unavailable_result = handle_unavailable_callback(
        update,
        token,
        request_api=request_api,
        request_dir=request_dir or CleanerRuntimePaths().request_dir,
        secret_path=action_secret_path or ACTION_SECRET_PATH,
        now=now,
    )
    if unavailable_result:
        return unavailable_result
    callback_result = callback(
        update,
        token,
        request_dir=request_dir or CleanerRuntimePaths().request_dir,
        action_secret_path=action_secret_path or ACTION_SECRET_PATH,
        allowed_action_types=CLEANER_CALLBACK_ACTION_TYPES,
        preauthorized_identity=False,
        request_api=request_api,
    )
    if callback_result:
        return callback_result
    pairing_result = pair(update, token, cleaner_only=True, request_api=request_api)
    if pairing_result:
        return pairing_result if isinstance(pairing_result, str) else "paired"
    from telegram_approval.cleaner_property_access import handle_properties
    from telegram_approval.cleaner_jobs import handle_jobs
    from telegram_approval.cleaner_my_schedule import handle_my_schedule

    def handle_list_command(command_update):
        properties_result = handle_properties(command_update, token, request_api=request_api)
        if properties_result:
            return properties_result
        jobs_result = handle_jobs(
            command_update,
            token,
            request_dir=request_dir or CleanerRuntimePaths().request_dir,
            request_api=request_api,
            now=now,
        )
        if jobs_result:
            return jobs_result
        return handle_my_schedule(
            command_update,
            token,
            request_api=request_api,
            now=now,
            request_dir=request_dir or CleanerRuntimePaths().request_dir,
            action_secret_path=action_secret_path or ACTION_SECRET_PATH,
        )

    # Slash commands remain the direct fallback path and retain their existing
    # authorization, data selection, privacy, and application behavior.
    list_result = handle_list_command(update)
    if list_result:
        return list_result

    from telegram_approval.cleaning_issue import capture_issue_message
    issue_result = capture_issue_message(update, token)
    if issue_result:
        return issue_result
    from telegram_approval.cleaning_completion_evidence import capture_completion_message
    completion_result = capture_completion_message(update, token)
    if completion_result:
        return completion_result

    # Reply-scoped issue/completion input gets priority over Korean aliases so
    # menu text cannot steal a valid ForceReply workflow message.
    from telegram_approval.cleaner_home import alias_command_update, handle_home_navigation
    normalized_update = alias_command_update(update)
    if normalized_update is not None:
        alias_result = handle_list_command(normalized_update)
        if alias_result:
            return alias_result
    home_result = handle_home_navigation(update, token, request_api=request_api)
    if home_result:
        return home_result
    return "ignored"


def handle(update, token):
    callback_result = callback(update, token)
    if callback_result:
        return callback_result
    message = update.get("message", {})
    chat = message.get("chat", {})
    sender = message.get("from", {})
    pairing_result = pair(update, token)
    if pairing_result:
        return pairing_result if isinstance(pairing_result, str) else "paired"
    from telegram_approval.cleaning_issue import capture_issue_message
    issue_result = capture_issue_message(update, token)
    if issue_result:
        return issue_result
    from telegram_approval.cleaning_completion_evidence import capture_completion_message
    completion_result = capture_completion_message(update, token)
    if completion_result:
        return completion_result
    door_code_result = door_code_reply(update, token)
    if door_code_result:
        return door_code_result
    if OPERATOR_PATH.exists():
        operator = json.loads(OPERATOR_PATH.read_text())
        authorized = sender.get("id") == operator["telegram_user_id"] and chat.get("id") == operator["telegram_chat_id"]
        if authorized and message.get("text") in ("/start", "/status"):
            api(token, "sendMessage", chat_id=chat["id"], text="PropertyAI 승인 봇 정상 작동 중\n모드: LOCAL PRODUCTION\nGmail 자동 수집: ON\n취소 실행: Telegram 승인 필요")
            return "status"
    return "ignored"


def main():
    raise SystemExit(
        "Legacy mixed Telegram poller is disabled; use a role-specific Stage-2 runtime binding"
    )


if __name__ == "__main__":
    main()

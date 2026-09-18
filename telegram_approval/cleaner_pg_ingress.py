"""W03 Telegram command adapter for Cleaner PostgreSQL business authority."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
from typing import Callable, ContextManager, Mapping
from uuid import UUID

from propertyai_core.application.cleaner_pg import (
    AcceptAssignmentCommand,
    CommandMetadata,
    CompleteCleaningCommand,
    ContinueReplacementCommand,
    DeclineAssignmentCommand,
    MarkUnavailableCommand,
    ReassignOriginalCommand,
    RequestReassignmentCommand,
)
from propertyai_core.config.cleaner_authority import CleanerAuthorityConfig
from propertyai_core.global_writer import assert_current_production_writer


class CleanerPostgresTelegramError(RuntimeError):
    pass


def _uuid(record: Mapping[str, object], name: str) -> UUID:
    value = record.get(name)
    if not isinstance(value, str) or not value:
        raise CleanerPostgresTelegramError(f"{name}_MISSING")
    try:
        return UUID(value)
    except ValueError as error:
        raise CleanerPostgresTelegramError(f"{name}_INVALID") from error


def _load_action(request_dir: Path, action_id: str) -> tuple[Path, dict]:
    if not action_id or "/" in action_id or ".." in action_id:
        raise CleanerPostgresTelegramError("ACTION_ID_INVALID")
    path = request_dir / f"{action_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise CleanerPostgresTelegramError("ACTION_RECORD_UNAVAILABLE") from error
    if not isinstance(value, dict) or value.get("action_id") != action_id:
        raise CleanerPostgresTelegramError("ACTION_RECORD_IDENTITY_MISMATCH")
    return path, value


def _signature(secret_path: Path, action_id: str, operation: str) -> str:
    try:
        key = secret_path.read_text(encoding="utf-8").strip().encode()
    except OSError as error:
        raise CleanerPostgresTelegramError("ACTION_SECRET_UNAVAILABLE") from error
    if not key:
        raise CleanerPostgresTelegramError("ACTION_SECRET_EMPTY")
    return hmac.new(key, f"{action_id}:{operation}".encode(), hashlib.sha256).hexdigest()[:16]


def _authorized(update: Mapping[str, object], record: Mapping[str, object]) -> bool:
    query = update.get("callback_query")
    if not isinstance(query, Mapping):
        return False
    sender = query.get("from")
    message = query.get("message")
    if not isinstance(sender, Mapping) or not isinstance(message, Mapping):
        return False
    chat = message.get("chat")
    if not isinstance(chat, Mapping):
        return False
    expected_user = record.get("candidate_user_id", record.get("telegram_user_id"))
    expected_chat = record.get("candidate_chat_id", record.get("telegram_chat_id"))
    return sender.get("id") == expected_user and chat.get("id") == expected_chat


def _metadata(
    *,
    record: Mapping[str, object],
    action_id: str,
    operation: str,
    authority: CleanerAuthorityConfig,
) -> CommandMetadata:
    if authority.authority_epoch is None:
        raise CleanerPostgresTelegramError("POSTGRES_AUTHORITY_EPOCH_MISSING")
    party_id = _uuid(record, "pg_cleaner_party_id")
    user_id = record.get("candidate_user_id", record.get("telegram_user_id"))
    if not isinstance(user_id, int) or user_id <= 0:
        raise CleanerPostgresTelegramError("TELEGRAM_USER_ID_MISSING")
    now = datetime.now(timezone.utc)
    return CommandMetadata(
        idempotency_key=f"TELEGRAM_CLEANER_COMMAND:{action_id}:{operation}",
        source_channel_code="TELEGRAM",
        source_stream_key=f"telegram-user:{user_id}",
        source_event_id=f"{action_id}:{operation}",
        authority_epoch=authority.authority_epoch,
        decided_at=now,
        actor_party_id=party_id,
    )


def _persist_local_result(path: Path, record: dict, result: str) -> None:
    assert_current_production_writer()
    record["pg_command_result"] = result
    record["pg_command_completed_at"] = datetime.now(timezone.utc).isoformat()
    record["authority_route"] = "POSTGRES"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def handle_postgres_cleaner_callback(
    update: Mapping[str, object],
    *,
    request_dir: Path,
    secret_path: Path,
    authority: CleanerAuthorityConfig,
    service,
) -> str | None:
    if not authority.uses_postgres:
        raise CleanerPostgresTelegramError("POSTGRES_AUTHORITY_REQUIRED")
    query = update.get("callback_query")
    if not isinstance(query, Mapping):
        return None
    data = query.get("data")
    if not isinstance(data, str):
        return None
    parts = data.split(":")
    if len(parts) != 4 or parts[0] not in {"a", "u"}:
        return None
    prefix, action_id, operation, supplied = parts
    path, record = _load_action(request_dir, action_id)
    if not _authorized(update, record):
        raise CleanerPostgresTelegramError("TELEGRAM_ACTION_UNAUTHORIZED")
    if not hmac.compare_digest(_signature(secret_path, action_id, operation), supplied):
        raise CleanerPostgresTelegramError("TELEGRAM_ACTION_SIGNATURE_MISMATCH")
    if record.get("consumed") is True and record.get("pg_command_result"):
        return str(record["pg_command_result"])

    action_type = record.get("action_type")
    if prefix == "u" and action_type == "CLEANER_UNAVAILABLE" and operation in {"open", "keep"}:
        if operation == "open":
            record["status"] = "CONFIRMATION_OPEN"
            result = "postgres_unavailable_confirmation_open"
        else:
            record["status"] = "CONFIRMATION_CANCELLED"
            result = "postgres_unavailable_keep_no_business_mutation"
        _persist_local_result(path, record, result)
        return result
    if prefix == "u" and action_type == "CLEANER_UNAVAILABLE" and (
        operation.startswith("reason_") or operation == "reason_skip"
    ):
        record["pg_unavailable_reason_operation"] = operation
        result = "postgres_unavailable_reason_local_only"
        _persist_local_result(path, record, result)
        return result
    if (
        prefix == "a"
        and action_type == "CLEANING_COMPLETION_SUBMISSION"
        and operation == "reject"
    ):
        result = "postgres_completion_not_submitted"
        record["consumed"] = True
        record["consumed_at"] = datetime.now(timezone.utc).isoformat()
        _persist_local_result(path, record, result)
        return result

    metadata = _metadata(
        record=record,
        action_id=action_id,
        operation=operation,
        authority=authority,
    )

    if prefix == "a" and action_type == "CLEANING_ASSIGNMENT" and operation == "approve":
        assignment_id = service.accept_assignment(
            AcceptAssignmentCommand(
                metadata=metadata,
                campaign_id=_uuid(record, "pg_campaign_id"),
                offer_candidate_id=_uuid(record, "pg_offer_candidate_id"),
                cleaner_party_id=_uuid(record, "pg_cleaner_party_id"),
            )
        )
        result = f"postgres_assignment_accepted:{assignment_id}"
    elif prefix == "a" and action_type == "CLEANING_ASSIGNMENT" and operation == "reject":
        cleaning_id = service.decline_assignment(
            DeclineAssignmentCommand(
                metadata=metadata,
                campaign_id=_uuid(record, "pg_campaign_id"),
                offer_candidate_id=_uuid(record, "pg_offer_candidate_id"),
                cleaner_party_id=_uuid(record, "pg_cleaner_party_id"),
            )
        )
        result = f"postgres_assignment_declined:{cleaning_id}"
    elif prefix == "u" and action_type == "CLEANER_UNAVAILABLE" and operation == "confirm":
        classification = record.get("pg_availability_classification")
        urgency = record.get("replacement_urgency", record.get("pg_replacement_urgency"))
        if classification not in {"EARLY_UNAVAILABLE", "SAME_DAY_UNAVAILABLE"}:
            raise CleanerPostgresTelegramError("PG_AVAILABILITY_CLASSIFICATION_MISSING")
        if urgency not in {"NORMAL", "URGENT"}:
            raise CleanerPostgresTelegramError("PG_REPLACEMENT_URGENCY_MISSING")
        unavailable_id = service.mark_unavailable(
            MarkUnavailableCommand(
                metadata=metadata,
                assignment_id=_uuid(record, "pg_assignment_id"),
                availability_classification=str(classification),
                replacement_urgency=str(urgency),
                reason_code=(str(record["reason_code"]) if record.get("reason_code") else None),
                reason_text=(str(record["reason_text"]) if record.get("reason_text") else None),
            )
        )
        result = f"postgres_unavailable_confirmed:{unavailable_id}"
    elif prefix == "u" and action_type == "CLEANER_REASSIGNMENT_REQUEST" and operation == "reassign":
        request_id = service.request_reassignment(
            RequestReassignmentCommand(
                metadata=metadata,
                unavailability_id=_uuid(record, "pg_unavailability_id"),
            )
        )
        result = f"postgres_reassignment_requested:{request_id}"
    elif (
        prefix == "a"
        and action_type == "CLEANING_COMPLETION_SUBMISSION"
        and operation == "approve"
    ):
        from telegram_approval.cleaning_completion_evidence import session_ready

        if not session_ready(record):
            raise CleanerPostgresTelegramError("COMPLETION_EVIDENCE_NOT_READY")
        cleaning_id = service.complete_cleaning(
            CompleteCleaningCommand(
                metadata=metadata,
                assignment_id=_uuid(record, "pg_assignment_id"),
            )
        )
        result = f"postgres_cleaning_completed:{cleaning_id}"
    else:
        # PG authority must never fall through to a legacy business mutation path.
        raise CleanerPostgresTelegramError("POSTGRES_CLEANER_COMMAND_UNSUPPORTED")

    record["consumed"] = True
    record["consumed_at"] = datetime.now(timezone.utc).isoformat()
    _persist_local_result(path, record, result)
    return result


PG_REASSIGNMENT_ACTION_TYPE = "CLEANER_REASSIGNMENT_DECISION"
PG_REASSIGNMENT_PENDING = "PENDING"
PG_REASSIGNMENT_EXECUTED = "EXECUTED"


def handle_postgres_ops_reassignment_callback(
    update: Mapping[str, object],
    *,
    request_dir: Path,
    secret_path: Path,
) -> str | None:
    """W02 records an authenticated human decision but performs no PG mutation."""

    query = update.get("callback_query")
    if not isinstance(query, Mapping):
        return None
    data = query.get("data")
    if not isinstance(data, str) or not data.startswith("r:"):
        return None
    parts = data.split(":")
    if len(parts) != 4:
        return None
    _prefix, action_id, operation, supplied = parts
    path, record = _load_action(request_dir, action_id)
    if (
        record.get("action_type") != PG_REASSIGNMENT_ACTION_TYPE
        or record.get("authority_route") != "POSTGRES"
    ):
        return None
    if operation not in {"reassign_original", "continue_replacement"}:
        raise CleanerPostgresTelegramError("PG_REASSIGNMENT_DECISION_UNSUPPORTED")
    from telegram_approval.cleaner_reassignment import decision_callback

    expected = decision_callback(
        action_id, operation, secret_path=secret_path
    ).rsplit(":", 1)[-1]
    if not hmac.compare_digest(expected, supplied):
        raise CleanerPostgresTelegramError("PG_REASSIGNMENT_DECISION_SIGNATURE_MISMATCH")
    message = query.get("message")
    chat = message.get("chat") if isinstance(message, Mapping) else None
    chat_id = chat.get("id") if isinstance(chat, Mapping) else None
    if not isinstance(chat_id, int) or chat_id <= 0:
        raise CleanerPostgresTelegramError("PG_REASSIGNMENT_DECISION_CHAT_ID_MISSING")
    if record.get("decision_committed") is True:
        if record.get("pg_decision_operation") != operation:
            raise CleanerPostgresTelegramError("PG_REASSIGNMENT_DECISION_CONFLICT")
        return "postgres_reassignment_decision_already_recorded"
    assert_current_production_writer()
    now = datetime.now(timezone.utc)
    record["decision_committed"] = True
    record["decision"] = (
        "REASSIGNED_ORIGINAL" if operation == "reassign_original" else "CONTINUE_REPLACEMENT"
    )
    record["pg_decision_operation"] = operation
    record["pg_command_status"] = PG_REASSIGNMENT_PENDING
    record["decision_actor_chat_id"] = chat_id
    record["decision_at"] = now.isoformat()
    _persist_local_result(path, record, "postgres_reassignment_decision_recorded")
    # _persist_local_result writes the factual local receipt but must not mark the
    # human decision consumed before W03 executes the PG command.
    record = json.loads(path.read_text(encoding="utf-8"))
    record["consumed"] = False
    record.pop("consumed_at", None)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return "postgres_reassignment_decision_recorded"


class CleanerPostgresReassignmentDecisionPump:
    """W03 executes only previously authenticated OPS decisions as PG commands."""

    def __init__(
        self,
        *,
        request_dir: Path,
        authority: CleanerAuthorityConfig,
        service,
        mutation_scope_factory: Callable[[str], ContextManager] | None = None,
    ) -> None:
        self.request_dir = request_dir
        self.authority = authority
        self.service = service
        self.mutation_scope_factory = mutation_scope_factory

    def _scope(self, action_id: str):
        if self.mutation_scope_factory is not None:
            return self.mutation_scope_factory(action_id)
        from contextlib import nullcontext
        return nullcontext()

    @staticmethod
    def _decision_time(record: Mapping[str, object]) -> datetime:
        raw = record.get("decision_at")
        if not isinstance(raw, str) or not raw:
            raise CleanerPostgresTelegramError("PG_REASSIGNMENT_DECISION_TIME_MISSING")
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None or value.utcoffset() is None:
            raise CleanerPostgresTelegramError("PG_REASSIGNMENT_DECISION_TIME_NAIVE")
        return value

    def run_once(self) -> dict[str, int]:
        if not self.authority.uses_postgres or self.authority.authority_epoch is None:
            raise CleanerPostgresTelegramError("POSTGRES_AUTHORITY_REQUIRED")
        result = {"executed": 0, "skipped": 0}
        for path in sorted(self.request_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                result["skipped"] += 1
                continue
            if not isinstance(record, dict) or (
                record.get("action_type") != PG_REASSIGNMENT_ACTION_TYPE
                or record.get("authority_route") != "POSTGRES"
                or record.get("decision_committed") is not True
                or record.get("pg_command_status") != PG_REASSIGNMENT_PENDING
            ):
                continue
            action_id = record.get("action_id")
            operation = record.get("pg_decision_operation")
            if not isinstance(action_id, str) or operation not in {
                "reassign_original", "continue_replacement"
            }:
                raise CleanerPostgresTelegramError("PG_REASSIGNMENT_DECISION_RECORD_INVALID")
            with self._scope(action_id):
                assert_current_production_writer()
                current = json.loads(path.read_text(encoding="utf-8"))
                if current.get("pg_command_status") != PG_REASSIGNMENT_PENDING:
                    continue
                chat_id = current.get("decision_actor_chat_id")
                if not isinstance(chat_id, int) or chat_id <= 0:
                    raise CleanerPostgresTelegramError("PG_REASSIGNMENT_DECISION_CHAT_ID_MISSING")
                metadata = CommandMetadata(
                    idempotency_key=f"TELEGRAM_OPS_CLEANER_COMMAND:{action_id}:{operation}",
                    source_channel_code="TELEGRAM",
                    source_stream_key=f"telegram-ops-chat:{chat_id}",
                    source_event_id=f"{action_id}:{operation}",
                    authority_epoch=self.authority.authority_epoch,
                    decided_at=self._decision_time(current),
                    actor_party_id=None,
                )
                request_id = _uuid(current, "pg_reassignment_request_id")
                if operation == "reassign_original":
                    assignment_id = self.service.reassign_original(
                        ReassignOriginalCommand(
                            metadata=metadata,
                            reassignment_request_id=request_id,
                        )
                    )
                    command_result = f"postgres_original_reassigned:{assignment_id}"
                else:
                    self.service.continue_replacement(
                        ContinueReplacementCommand(
                            metadata=metadata,
                            reassignment_request_id=request_id,
                        )
                    )
                    command_result = "postgres_replacement_continues"
                assert_current_production_writer()
                current["pg_command_status"] = PG_REASSIGNMENT_EXECUTED
                current["pg_command_result"] = command_result
                current["pg_command_completed_at"] = datetime.now(timezone.utc).isoformat()
                current["consumed"] = True
                current["consumed_at"] = current["pg_command_completed_at"]
                temporary = path.with_suffix(path.suffix + ".tmp")
                temporary.write_text(
                    json.dumps(current, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                temporary.chmod(0o600)
                temporary.replace(path)
                result["executed"] += 1
        return result


__all__ = [
    "CleanerPostgresReassignmentDecisionPump",
    "CleanerPostgresTelegramError",
    "PG_REASSIGNMENT_ACTION_TYPE",
    "handle_postgres_cleaner_callback",
    "handle_postgres_ops_reassignment_callback",
]

#!/usr/bin/env python3
"""Current-head durable Cleaner Assignment history authority.

`Cleaning` remains the current assignment projection.  This module owns only the
bounded durable Offer/Assignment history surface in canonical 19 DB.  It creates
or accepts history only from an authenticated successful execution receipt and
never infers legacy history from the current Cleaning projection.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from propertyai_core.global_writer import assert_current_production_writer
from telegram_approval.assignment_execution_receipt import (
    AssignmentExecutionReceiptError,
    acceptance_operation_identity,
    verify_assignment_execution_receipt,
)
from telegram_approval.cleaner_config import CleanerRuntimePaths
from telegram_approval.cleaner_jobs import NOTION_TOKEN_PATH, NOTION_VERSION


ASSIGNMENT_HISTORY_SOURCE = "3b43edac-140d-4699-8db1-00020d10652c"
ASSIGNMENT_HISTORY_DATABASE = "e9278ba6-ad32-440c-b1c3-75f4abadf8cb"
CANONICAL_ASSIGNMENT_REQUEST_DIR = CleanerRuntimePaths().request_dir
RECEIPT_SECRET_PATH = (
    Path(__file__).resolve().parents[1] / "secrets" / "telegram" / "action-secret"
)

ALLOWED_END_REASONS = frozenset(
    {
        "CLEANER_UNAVAILABLE",
        "OPERATOR_REPLACED",
        "RESERVATION_CANCELLED",
        "CLEANING_CANCELLED",
    }
)

NOT_FOUND = "NOT_FOUND"
AMBIGUOUS = "AMBIGUOUS"
CONFLICT = "CONFLICT"
ALREADY_ACCEPTED = "ALREADY_ACCEPTED"
ALREADY_ENDED = "ALREADY_ENDED"
RETRY_SAME_KEY = "RETRY_SAME_KEY"
REMOTE_NOTION_FAILURE = "REMOTE/NOTION_FAILURE"
SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
UNVERIFIED_BINDING_ASSIGNMENT = "UNVERIFIED_BINDING_ASSIGNMENT"
UNVERIFIED_BINDING_OFFER = UNVERIFIED_BINDING_ASSIGNMENT
INVALID_HISTORY_TARGET = "INVALID_HISTORY_TARGET"
VERSION_CONFLICT = "VERSION_CONFLICT"
CREATED = "CREATED"
UPDATED = "UPDATED"

_PAGE_REF_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_ACTION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
ASSIGNMENT_HISTORY_LOCK_DIR = (
    CleanerRuntimePaths().state_path.parent / "assignment-history-locks"
)
ASSIGNMENT_HISTORY_LOCK_TIMEOUT_SECONDS = 30.0
ASSIGNMENT_HISTORY_LOCK_POLL_SECONDS = 0.05


class AssignmentHistoryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DirectAssignmentHistorySemanticsRequired(AssignmentHistoryError):
    """Compatibility error name for pre-current-head evaluator vocabulary."""

    def __init__(self):
        super().__init__(
            UNVERIFIED_BINDING_ASSIGNMENT,
            "VERIFIED_ASSIGNMENT_EXECUTION_EVIDENCE_REQUIRED",
        )


@dataclass(frozen=True)
class AssignmentHistoryResult:
    status: str
    page_id: str
    mutated: bool


@dataclass(frozen=True)
class BindingAssignmentEvidence:
    action_id: str
    cleaning_page_id: str
    cleaner_party_page_id: str
    offered_at: datetime
    accepted_at: datetime
    executed_at: datetime
    telegram_message_id: str
    proposal_round: int
    cleaner_telegram_user_id: int
    cleaner_telegram_chat_id: int
    assignment_generation_identity: str
    assignment_version: str
    operation_identity: str
    replacement_urgency: str | None = None
    base_fee_snapshot: int | None = None
    urgent_premium_snapshot: int | None = None
    total_agreed_fee_snapshot: int | None = None
    urgent_premium_policy_version: str | None = None
    binding_offer: bool = True


# Historical name retained as a type-level compatibility alias only.  The
# evidence now proves factual assignment execution, not merely an Offer state.
BindingOfferEvidence = BindingAssignmentEvidence


class AssignmentHistoryStore(Protocol):
    def query_assignment_generation(
        self, *, cleaning_page_id: str, assignment_version: str
    ) -> list[dict]: ...

    def query_by_idempotency(self, key: str) -> list[dict]: ...

    def create_accepted_assignment(
        self,
        *,
        evidence: BindingAssignmentEvidence,
        assignment_version: str,
        idempotency_key: str,
    ) -> dict: ...

    def create_reassigned_assignment(
        self,
        *,
        evidence: BindingAssignmentEvidence,
        assignment_version: str,
        idempotency_key: str,
    ) -> dict: ...

    def accept_existing_offer(
        self,
        page_id: str,
        *,
        evidence: BindingAssignmentEvidence,
        assignment_version: str,
        idempotency_key: str,
    ) -> dict: ...

    def release_accepted_assignment(
        self,
        page_id: str,
        *,
        cleaning_page_id: str,
        cleaner_party_page_id: str,
        assignment_version: str,
        action_id: str,
        idempotency_key: str,
        ended_at: datetime,
        end_reason: str,
        end_key: str,
    ) -> dict: ...

    def query_effective_accepted(
        self, *, cleaning_page_id: str, cleaner_party_page_id: str
    ) -> list[dict]: ...

    def query_recent_unavailable(
        self, *, cleaner_party_page_id: str
    ) -> list[dict]: ...

    def query_accepted_for_cleaning(
        self, *, cleaning_page_id: str
    ) -> list[dict]: ...

    def capture_unavailable_reason(
        self,
        page_id: str,
        *,
        cleaning_page_id: str,
        cleaner_party_page_id: str,
        assignment_version: str,
        action_id: str,
        idempotency_key: str,
        end_key: str,
        reason: str,
        captured_at: datetime,
    ) -> dict: ...

    def record_reassignment_request(
        self,
        page_id: str,
        *,
        cleaning_page_id: str,
        cleaner_party_page_id: str,
        assignment_version: str,
        action_id: str,
        idempotency_key: str,
        end_key: str,
        request_key: str,
        requested_at: datetime,
    ) -> dict: ...

    def resolve_reassignment_request(
        self,
        page_id: str,
        *,
        cleaning_page_id: str,
        cleaner_party_page_id: str,
        assignment_version: str,
        action_id: str,
        idempotency_key: str,
        end_key: str,
        request_key: str,
        resolution: str,
    ) -> dict: ...


def _normalize_page_id(value: str) -> str:
    if not isinstance(value, str) or _PAGE_REF_RE.fullmatch(value or "") is None:
        raise AssignmentHistoryError(SCHEMA_MISMATCH, "invalid Notion page reference")
    compact = value.replace("-", "").lower()
    if len(compact) == 32 and all(char in "0123456789abcdef" for char in compact):
        return compact
    return value


def _require_action_id(value: str) -> str:
    if not isinstance(value, str) or _ACTION_ID_RE.fullmatch(value or "") is None:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "canonical Action ID is malformed"
        )
    return value


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AssignmentHistoryError(SCHEMA_MISMATCH, f"{label} is required")
    return value.strip()


def _require_aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AssignmentHistoryError(
            SCHEMA_MISMATCH, f"{label} must be timezone-aware"
        )
    return value


def _parse_aware(value: object, label: str, *, evidence: bool = False) -> datetime:
    code = UNVERIFIED_BINDING_ASSIGNMENT if evidence else SCHEMA_MISMATCH
    if not isinstance(value, str) or not value:
        raise AssignmentHistoryError(code, f"{label} missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AssignmentHistoryError(code, f"{label} malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AssignmentHistoryError(code, f"{label} must be timezone-aware")
    return parsed


@contextmanager
def _key_lock(
    key: str,
    *,
    lock_dir: Path | None = None,
    timeout_seconds: float = ASSIGNMENT_HISTORY_LOCK_TIMEOUT_SECONDS,
):
    """Serialize one generation locally across threads/processes, fail closed."""

    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise AssignmentHistoryError(
            SCHEMA_MISMATCH, "serialization timeout must be positive"
        )
    deadline = time.monotonic() + float(timeout_seconds)
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    if not thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise AssignmentHistoryError(
            CONFLICT, "assignment generation serialization busy"
        )

    handle = None
    try:
        directory = Path(lock_dir or ASSIGNMENT_HISTORY_LOCK_DIR)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{hashlib.sha256(key.encode()).hexdigest()}.lock"
        handle = path.open("a+")
        os.chmod(path, 0o600)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise AssignmentHistoryError(
                        CONFLICT, "assignment generation serialization busy"
                    ) from exc
                time.sleep(ASSIGNMENT_HISTORY_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if handle is not None:
            handle.close()
        thread_lock.release()


@contextmanager
def cleaning_assignment_lock(
    *,
    cleaning_page_id: str,
    lock_dir: Path | None = None,
    timeout_seconds: float = ASSIGNMENT_HISTORY_LOCK_TIMEOUT_SECONDS,
):
    """Serialize all effective Assignment decisions for one Cleaning.

    Phase 4 deliberately reuses the existing local thread/file lock primitive.
    Callers that also need a generation lock must acquire this lock first.
    """

    cleaning_page_id = _normalize_page_id(cleaning_page_id)
    with _key_lock(
        f"PRODUCTION:CLEANING:{cleaning_page_id}",
        lock_dir=lock_dir,
        timeout_seconds=timeout_seconds,
    ):
        yield


@contextmanager
def assignment_generation_lock(
    *,
    cleaning_page_id: str,
    assignment_version: str,
    lock_dir: Path | None = None,
    timeout_seconds: float = ASSIGNMENT_HISTORY_LOCK_TIMEOUT_SECONDS,
):
    cleaning_page_id = _normalize_page_id(cleaning_page_id)
    assignment_version = _require_text(assignment_version, "Assignment Version")
    with _key_lock(
        f"PRODUCTION:{cleaning_page_id}:{assignment_version}",
        lock_dir=lock_dir,
        timeout_seconds=timeout_seconds,
    ):
        yield


def _relation_property(page_id: str) -> dict:
    return {"relation": [{"id": page_id}]}


def _select_property(value: str) -> dict:
    return {"select": {"name": value}}


def _rich_text_property(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value}}]}


def _title_property(value: str) -> dict:
    return {"title": [{"text": {"content": value}}]}


def _date_property(value: datetime) -> dict:
    return {"date": {"start": value.isoformat()}}


def _checkbox_property(value: bool) -> dict:
    return {"checkbox": value}


def _number_property(value: int) -> dict:
    return {"number": value}


def _require_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AssignmentHistoryError(SCHEMA_MISMATCH, f"{label} malformed")
    return value


def _accepted_economics_properties(evidence: BindingAssignmentEvidence) -> dict:
    if evidence.base_fee_snapshot is None:
        return {}
    properties = {
        "Replacement Urgency": _select_property(evidence.replacement_urgency),
        "Base Fee Snapshot": _number_property(evidence.base_fee_snapshot),
        "Urgent Premium Snapshot": _number_property(evidence.urgent_premium_snapshot),
        "Total Agreed Fee Snapshot": _number_property(evidence.total_agreed_fee_snapshot),
    }
    if evidence.urgent_premium_policy_version is not None:
        properties["Urgent Premium Policy Version"] = _rich_text_property(
            evidence.urgent_premium_policy_version
        )
    return properties


def _validate_accepted_economics(page: dict, evidence: BindingAssignmentEvidence) -> None:
    if evidence.base_fee_snapshot is None:
        return
    if _select(page, "Replacement Urgency") != evidence.replacement_urgency:
        raise AssignmentHistoryError(CONFLICT, "Replacement Urgency snapshot conflict")
    if _number(page, "Base Fee Snapshot") != evidence.base_fee_snapshot:
        raise AssignmentHistoryError(CONFLICT, "Base Fee Snapshot conflict")
    if _number(page, "Urgent Premium Snapshot") != evidence.urgent_premium_snapshot:
        raise AssignmentHistoryError(CONFLICT, "Urgent Premium Snapshot conflict")
    if _number(page, "Total Agreed Fee Snapshot") != evidence.total_agreed_fee_snapshot:
        raise AssignmentHistoryError(CONFLICT, "Total Agreed Fee Snapshot conflict")
    actual_version = _text(page, "Urgent Premium Policy Version")
    if actual_version != evidence.urgent_premium_policy_version:
        raise AssignmentHistoryError(CONFLICT, "Urgent Premium Policy Version conflict")


def accepted_compensation_snapshot(page: dict, *, legacy_base_fee_krw: int | None = None) -> dict:
    """Read immutable accepted economics from 19, with bounded legacy-normal fallback."""

    urgency = _select(page, "Replacement Urgency")
    base = _number(page, "Base Fee Snapshot")
    premium = _number(page, "Urgent Premium Snapshot")
    total = _number(page, "Total Agreed Fee Snapshot")
    policy_version = _text(page, "Urgent Premium Policy Version")
    present = any(value is not None for value in (urgency, base, premium, total, policy_version))
    if not present:
        if legacy_base_fee_krw is None:
            return {}
        legacy = _require_nonnegative_int(legacy_base_fee_krw, "legacy base fee")
        return {
            "replacement_urgency": "NORMAL",
            "base_fee_krw": legacy,
            "urgent_premium_krw": 0,
            "total_agreed_fee_krw": legacy,
            "urgent_premium_policy_version": None,
            "legacy_normal_fallback": True,
        }
    if urgency not in {"NORMAL", "URGENT"}:
        raise AssignmentHistoryError(SCHEMA_MISMATCH, "Replacement Urgency snapshot malformed")
    values = {
        "base": _require_nonnegative_int(base, "Base Fee Snapshot"),
        "premium": _require_nonnegative_int(premium, "Urgent Premium Snapshot"),
        "total": _require_nonnegative_int(total, "Total Agreed Fee Snapshot"),
    }
    if values["total"] != values["base"] + values["premium"]:
        raise AssignmentHistoryError(SCHEMA_MISMATCH, "accepted compensation total mismatch")
    if urgency == "NORMAL":
        if values["premium"] != 0 or values["total"] != values["base"] or policy_version is not None:
            raise AssignmentHistoryError(SCHEMA_MISMATCH, "NORMAL accepted compensation malformed")
    elif not policy_version:
        raise AssignmentHistoryError(SCHEMA_MISMATCH, "URGENT accepted compensation policy missing")
    return {
        "replacement_urgency": urgency,
        "base_fee_krw": values["base"],
        "urgent_premium_krw": values["premium"],
        "total_agreed_fee_krw": values["total"],
        "urgent_premium_policy_version": policy_version,
        "legacy_normal_fallback": False,
    }


def _relation_ids(page: dict, name: str) -> list[str] | None:
    prop = page.get("properties", {}).get(name)
    if not isinstance(prop, dict) or not isinstance(prop.get("relation"), list):
        return None
    result: list[str] = []
    for value in prop["relation"]:
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            return None
        result.append(value["id"])
    return result


def _select(page: dict, name: str) -> str | None:
    prop = page.get("properties", {}).get(name, {})
    selected = prop.get("select") if isinstance(prop, dict) else None
    return selected.get("name") if isinstance(selected, dict) else None


def _date(page: dict, name: str) -> str | None:
    prop = page.get("properties", {}).get(name, {})
    value = prop.get("date") if isinstance(prop, dict) else None
    return value.get("start") if isinstance(value, dict) else None


def _text(page: dict, name: str) -> str | None:
    prop = page.get("properties", {}).get(name, {})
    if not isinstance(prop, dict):
        return None
    values = prop.get("rich_text")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        return None
    item = values[0]
    if isinstance(item.get("plain_text"), str):
        return item["plain_text"]
    text = item.get("text")
    return (
        text.get("content")
        if isinstance(text, dict) and isinstance(text.get("content"), str)
        else None
    )


def _checkbox(page: dict, name: str) -> bool | None:
    prop = page.get("properties", {}).get(name, {})
    value = prop.get("checkbox") if isinstance(prop, dict) else None
    return value if isinstance(value, bool) else None


def _number(page: dict, name: str) -> int | float | None:
    prop = page.get("properties", {}).get(name, {})
    value = prop.get("number") if isinstance(prop, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _page_id(page: dict) -> str:
    value = page.get("id") if isinstance(page, dict) else None
    if not isinstance(value, str) or not value:
        raise AssignmentHistoryError(SCHEMA_MISMATCH, "history row id missing")
    return value


def _same_page_id(left: str, right: str) -> bool:
    return _normalize_page_id(left) == _normalize_page_id(right)


def _same_datetime(left: str | None, right: datetime) -> bool:
    """Compare canonical Notion date properties at their persisted minute precision.

    Notion normalizes time-bearing date properties to minute precision on readback.
    Preserve fail-closed semantics across minute boundaries while allowing the
    receipt-authenticated source timestamp to retain its original seconds.
    """

    if left is None:
        return False
    try:
        persisted = _parse_aware(left, "history timestamp")
    except AssignmentHistoryError:
        return False
    if not isinstance(right, datetime) or right.tzinfo is None or right.utcoffset() is None:
        return False
    return persisted.replace(second=0, microsecond=0) == right.replace(second=0, microsecond=0)


def _canonical_assignment_path(action_id: str) -> Path:
    return CANONICAL_ASSIGNMENT_REQUEST_DIR / f"{_require_action_id(action_id)}.json"


def resolve_canonical_binding_assignment(action_id: str) -> BindingAssignmentEvidence:
    """Resolve only receipt-authenticated factual assignment success.

    This is the cutover provenance gate.  It deliberately does not query the
    current Cleaning projection to infer historical rows.
    """

    action_id = _require_action_id(action_id)
    try:
        record = json.loads(_canonical_assignment_path(action_id).read_text())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT,
            "canonical CLEANING_ASSIGNMENT execution evidence unavailable",
        ) from exc
    if not isinstance(record, dict) or record.get("action_id") != action_id:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "canonical assignment identity mismatch"
        )
    try:
        receipt = verify_assignment_execution_receipt(
            record, secret_path=RECEIPT_SECRET_PATH
        )
        operation_identity = acceptance_operation_identity(
            record, secret_path=RECEIPT_SECRET_PATH
        )
        cleaning_page_id = _normalize_page_id(receipt["cleaning_page_id"])
        cleaner_party_page_id = _normalize_page_id(receipt["cleaner_party_page_id"])
        offered_at = _parse_aware(
            receipt["offered_at"], "receipt offered_at", evidence=True
        )
        accepted_at = _parse_aware(
            receipt["consumed_at"], "receipt consumed_at", evidence=True
        )
        executed_at = _parse_aware(
            receipt["executed_at"], "receipt executed_at", evidence=True
        )
        assignment_version = _require_text(
            receipt["assignment_version"], "receipt Assignment Version"
        )
        generation_identity = _require_text(
            receipt["assignment_generation_identity"], "assignment generation identity"
        )
        economics = receipt.get("accepted_economics")
        if economics is not None:
            if not isinstance(economics, dict):
                raise AssignmentHistoryError(SCHEMA_MISMATCH, "accepted economics malformed")
            replacement_urgency = _require_text(
                economics.get("replacement_urgency"), "Replacement Urgency"
            )
            if replacement_urgency not in {"NORMAL", "URGENT"}:
                raise AssignmentHistoryError(SCHEMA_MISMATCH, "Replacement Urgency malformed")
            base_fee_snapshot = _require_nonnegative_int(
                economics.get("base_fee_krw"), "Base Fee Snapshot"
            )
            urgent_premium_snapshot = _require_nonnegative_int(
                economics.get("urgent_premium_krw"), "Urgent Premium Snapshot"
            )
            total_agreed_fee_snapshot = _require_nonnegative_int(
                economics.get("total_agreed_fee_krw"), "Total Agreed Fee Snapshot"
            )
            if total_agreed_fee_snapshot != base_fee_snapshot + urgent_premium_snapshot:
                raise AssignmentHistoryError(SCHEMA_MISMATCH, "accepted economics total mismatch")
            premium_version = economics.get("urgent_premium_policy_version")
            if replacement_urgency == "URGENT":
                urgent_premium_policy_version = _require_text(
                    premium_version, "Urgent Premium Policy Version"
                )
            else:
                if urgent_premium_snapshot != 0 or total_agreed_fee_snapshot != base_fee_snapshot:
                    raise AssignmentHistoryError(SCHEMA_MISMATCH, "NORMAL economics malformed")
                if premium_version not in (None, ""):
                    raise AssignmentHistoryError(SCHEMA_MISMATCH, "NORMAL premium policy version malformed")
                urgent_premium_policy_version = None
        else:
            replacement_urgency = None
            base_fee_snapshot = None
            urgent_premium_snapshot = None
            total_agreed_fee_snapshot = None
            urgent_premium_policy_version = None
    except (AssignmentExecutionReceiptError, AssignmentHistoryError, KeyError, TypeError, ValueError) as exc:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT,
            "assignment execution receipt verification failed",
        ) from exc

    return BindingAssignmentEvidence(
        action_id=receipt["action_id"],
        cleaning_page_id=cleaning_page_id,
        cleaner_party_page_id=cleaner_party_page_id,
        offered_at=offered_at,
        accepted_at=accepted_at,
        executed_at=executed_at,
        telegram_message_id=str(receipt["telegram_message_id"]),
        proposal_round=receipt["proposal_round"],
        cleaner_telegram_user_id=receipt["cleaner_telegram_user_id"],
        cleaner_telegram_chat_id=receipt["cleaner_telegram_chat_id"],
        assignment_generation_identity=generation_identity,
        assignment_version=assignment_version,
        operation_identity=operation_identity,
        replacement_urgency=replacement_urgency,
        base_fee_snapshot=base_fee_snapshot,
        urgent_premium_snapshot=urgent_premium_snapshot,
        total_agreed_fee_snapshot=total_agreed_fee_snapshot,
        urgent_premium_policy_version=urgent_premium_policy_version,
    )


def resolve_canonical_assignment_authority(action_id: str) -> BindingAssignmentEvidence:
    """Resolve either a signed Cleaner acceptance or one frozen Phase-4 reassign decision.

    The direct-reassignment branch is deliberately narrow: only the exact
    ``CLEANER_REASSIGNMENT_DECISION`` action that durably committed
    ``REASSIGNED_ORIGINAL`` can become Assignment authority.
    """

    action_id = _require_action_id(action_id)
    try:
        record = json.loads(_canonical_assignment_path(action_id).read_text())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "canonical Assignment authority unavailable"
        ) from exc
    if not isinstance(record, dict) or record.get("action_id") != action_id:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "canonical Assignment authority identity mismatch"
        )
    if record.get("action_type") == "CLEANING_ASSIGNMENT":
        return resolve_canonical_binding_assignment(action_id)
    if (
        record.get("action_type") != "CLEANER_REASSIGNMENT_DECISION"
        or record.get("decision_committed") is not True
        or record.get("decision") != "REASSIGNED_ORIGINAL"
    ):
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "direct reassignment authority is not committed"
        )
    try:
        cleaning_page_id = _normalize_page_id(record["cleaning_page_id"])
        cleaner_party_page_id = _normalize_page_id(record["cleaner_party_page_id"])
        assignment_version = _require_text(
            record["reassignment_assignment_version"], "Assignment Version"
        )
        operation_identity = _require_text(
            record["reassignment_idempotency_key"], "Idempotency Key"
        )
        decision_at = _parse_aware(record["decision_at"], "reassignment decision time", evidence=True)
        user_id = record["cleaner_telegram_user_id"]
        chat_id = record["cleaner_telegram_chat_id"]
        if (
            isinstance(user_id, bool) or not isinstance(user_id, int)
            or isinstance(chat_id, bool) or not isinstance(chat_id, int)
        ):
            raise AssignmentHistoryError(SCHEMA_MISMATCH, "reassigned Cleaner Telegram identity malformed")
        economics = record.get("reassignment_economics")
        if not isinstance(economics, dict):
            raise AssignmentHistoryError(SCHEMA_MISMATCH, "reassignment economics missing")
        if "legacy_base_fee_krw" in economics:
            if set(economics) != {"legacy_base_fee_krw"}:
                raise AssignmentHistoryError(SCHEMA_MISMATCH, "legacy reassignment economics malformed")
            _require_nonnegative_int(economics["legacy_base_fee_krw"], "legacy base fee")
            replacement_urgency = None
            base_fee_snapshot = None
            urgent_premium_snapshot = None
            total_agreed_fee_snapshot = None
            urgent_premium_policy_version = None
        else:
            replacement_urgency = _require_text(
                economics.get("replacement_urgency"), "Replacement Urgency"
            )
            if replacement_urgency not in {"NORMAL", "URGENT"}:
                raise AssignmentHistoryError(SCHEMA_MISMATCH, "Replacement Urgency malformed")
            base_fee_snapshot = _require_nonnegative_int(
                economics.get("base_fee_krw"), "Base Fee Snapshot"
            )
            urgent_premium_snapshot = _require_nonnegative_int(
                economics.get("urgent_premium_krw"), "Urgent Premium Snapshot"
            )
            total_agreed_fee_snapshot = _require_nonnegative_int(
                economics.get("total_agreed_fee_krw"), "Total Agreed Fee Snapshot"
            )
            if total_agreed_fee_snapshot != base_fee_snapshot + urgent_premium_snapshot:
                raise AssignmentHistoryError(SCHEMA_MISMATCH, "reassignment economics total mismatch")
            premium = economics.get("urgent_premium_policy_version")
            if replacement_urgency == "URGENT":
                urgent_premium_policy_version = _require_text(
                    premium, "Urgent Premium Policy Version"
                )
            else:
                if urgent_premium_snapshot != 0 or total_agreed_fee_snapshot != base_fee_snapshot:
                    raise AssignmentHistoryError(SCHEMA_MISMATCH, "NORMAL reassignment economics malformed")
                if premium not in (None, ""):
                    raise AssignmentHistoryError(SCHEMA_MISMATCH, "NORMAL reassignment premium policy malformed")
                urgent_premium_policy_version = None
    except (KeyError, TypeError, ValueError, AssignmentHistoryError) as exc:
        if isinstance(exc, AssignmentHistoryError):
            raise
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "direct reassignment authority malformed"
        ) from exc
    return BindingAssignmentEvidence(
        action_id=action_id,
        cleaning_page_id=cleaning_page_id,
        cleaner_party_page_id=cleaner_party_page_id,
        offered_at=decision_at,
        accepted_at=decision_at,
        executed_at=decision_at,
        telegram_message_id=action_id,
        proposal_round=0,
        cleaner_telegram_user_id=user_id,
        cleaner_telegram_chat_id=chat_id,
        assignment_generation_identity=(
            f"CLEANER_REASSIGNMENT:{cleaning_page_id}:{assignment_version}"
        ),
        assignment_version=assignment_version,
        operation_identity=operation_identity,
        replacement_urgency=replacement_urgency,
        base_fee_snapshot=base_fee_snapshot,
        urgent_premium_snapshot=urgent_premium_snapshot,
        total_agreed_fee_snapshot=total_agreed_fee_snapshot,
        urgent_premium_policy_version=urgent_premium_policy_version,
        binding_offer=False,
    )


def accepted_assignment_compensation_snapshot(page: dict, *, action_id: str) -> dict:
    """Read immutable accepted economics without policy recalculation.

    Phase-3 rows are self-contained. Only a legacy base-only row needs the
    original signed Offer to recover the exact historical base amount.
    """

    snapshot = accepted_compensation_snapshot(page)
    if snapshot:
        return snapshot
    action_id = _require_action_id(action_id)
    try:
        record = json.loads(_canonical_assignment_path(action_id).read_text())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "legacy accepted economics authority unavailable"
        ) from exc
    if record.get("action_type") == "CLEANING_ASSIGNMENT":
        legacy_base = _require_nonnegative_int(record.get("cleaning_fee_krw"), "legacy base fee")
    elif record.get("action_type") == "CLEANER_REASSIGNMENT_DECISION":
        economics = record.get("reassignment_economics")
        if not isinstance(economics, dict) or set(economics) != {"legacy_base_fee_krw"}:
            raise AssignmentHistoryError(SCHEMA_MISMATCH, "legacy reassignment economics malformed")
        legacy_base = _require_nonnegative_int(economics.get("legacy_base_fee_krw"), "legacy base fee")
    else:
        raise AssignmentHistoryError(SCHEMA_MISMATCH, "legacy accepted economics source malformed")
    return accepted_compensation_snapshot(page, legacy_base_fee_krw=legacy_base)


# Compatibility name for historical evaluators and old focused test vocabulary.
resolve_canonical_binding_offer = resolve_canonical_binding_assignment


def acceptance_idempotency_key(action_id: str) -> str:
    """Return the semantic-payload-bound operation key for one accepted action."""

    return resolve_canonical_binding_assignment(action_id).operation_identity


def _validate_generation_identity(
    page: dict, *, cleaning_page_id: str, assignment_version: str
) -> None:
    cleanings = _relation_ids(page, "Cleaning")
    if cleanings is None or len(cleanings) != 1:
        raise AssignmentHistoryError(
            SCHEMA_MISMATCH, "history Cleaning relation must be an exact singleton"
        )
    if not _same_page_id(cleanings[0], cleaning_page_id):
        raise AssignmentHistoryError(
            VERSION_CONFLICT,
            "history Cleaning identity conflicts with assignment generation",
        )
    if _text(page, "Assignment Version") != assignment_version:
        raise AssignmentHistoryError(VERSION_CONFLICT, "Assignment Version conflict")
    if _select(page, "데이터 환경") != "PRODUCTION":
        raise AssignmentHistoryError(CONFLICT, "history environment conflict")


def _validate_offer_identity(
    page: dict,
    *,
    evidence: BindingAssignmentEvidence,
    assignment_version: str,
    idempotency_key: str,
) -> None:
    _validate_generation_identity(
        page,
        cleaning_page_id=evidence.cleaning_page_id,
        assignment_version=assignment_version,
    )
    cleaners = _relation_ids(page, "후보 인력")
    if cleaners is None or len(cleaners) != 1:
        raise AssignmentHistoryError(
            SCHEMA_MISMATCH, "history Cleaner relation must be an exact singleton"
        )
    if not _same_page_id(cleaners[0], evidence.cleaner_party_page_id):
        raise AssignmentHistoryError(
            VERSION_CONFLICT, "same Assignment Version targets a different Cleaner"
        )
    if _text(page, "Action ID") != evidence.action_id:
        raise AssignmentHistoryError(VERSION_CONFLICT, "Action ID conflict")
    if _text(page, "Idempotency Key") != idempotency_key:
        raise AssignmentHistoryError(VERSION_CONFLICT, "Idempotency Key conflict")
    if idempotency_key != evidence.operation_identity:
        raise AssignmentHistoryError(
            VERSION_CONFLICT, "Idempotency Key does not bind semantic execution payload"
        )
    if _checkbox(page, "Binding Offer") is not True:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "history row is not a binding Offer"
        )
    if not _same_datetime(_date(page, "제안 시각"), evidence.offered_at):
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "history Offer timestamp mismatch"
        )
    if _text(page, "Telegram Message ID") != evidence.telegram_message_id:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "history Telegram message identity mismatch"
        )
    if _number(page, "제안 라운드") != evidence.proposal_round:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "history proposal round mismatch"
        )


def _validate_end_identity(
    page: dict,
    *,
    cleaning_page_id: str,
    cleaner_party_page_id: str,
    assignment_version: str,
    action_id: str,
    idempotency_key: str,
    binding_offer: bool | None = None,
) -> None:
    _validate_generation_identity(
        page,
        cleaning_page_id=cleaning_page_id,
        assignment_version=assignment_version,
    )
    cleaners = _relation_ids(page, "후보 인력")
    if cleaners is None or len(cleaners) != 1 or not _same_page_id(
        cleaners[0], cleaner_party_page_id
    ):
        raise AssignmentHistoryError(CONFLICT, "history Cleaner identity conflict")
    if _text(page, "Action ID") != action_id:
        raise AssignmentHistoryError(CONFLICT, "Action ID conflict")
    if _text(page, "Idempotency Key") != idempotency_key:
        raise AssignmentHistoryError(CONFLICT, "Idempotency Key conflict")
    actual_binding = _checkbox(page, "Binding Offer")
    if actual_binding not in {True, False}:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "accepted history binding type is missing"
        )
    if binding_offer is not None and actual_binding is not binding_offer:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "accepted history binding authority mismatch"
        )


def _validate_reassigned_identity(
    page: dict, *, evidence: BindingAssignmentEvidence, assignment_version: str,
    idempotency_key: str,
) -> str:
    if evidence.binding_offer is not False:
        raise AssignmentHistoryError(SCHEMA_MISMATCH, "reassignment evidence must be direct")
    _validate_end_identity(
        page,
        cleaning_page_id=evidence.cleaning_page_id,
        cleaner_party_page_id=evidence.cleaner_party_page_id,
        assignment_version=assignment_version,
        action_id=evidence.action_id,
        idempotency_key=idempotency_key,
        binding_offer=False,
    )
    if _select(page, "제안 상태") != "ACCEPTED":
        raise AssignmentHistoryError(CONFLICT, "reassigned Assignment is not accepted")
    hold = _select(page, "Hold 상태")
    ended = (
        _date(page, "Assignment Ended At"),
        _select(page, "Assignment End Reason"),
        _text(page, "Assignment End Key"),
    )
    if hold == "HARD_BOOKED":
        if any(ended):
            raise AssignmentHistoryError(CONFLICT, "effective reassigned Assignment has terminal metadata")
        state = ALREADY_ACCEPTED
    elif hold == "RELEASED":
        if not all(ended):
            raise AssignmentHistoryError(SCHEMA_MISMATCH, "released reassigned Assignment lacks terminal metadata")
        state = ALREADY_ENDED
    else:
        raise AssignmentHistoryError(CONFLICT, "reassigned Assignment Hold state is invalid")
    if not _same_datetime(_date(page, "응답 시각"), evidence.accepted_at):
        raise AssignmentHistoryError(CONFLICT, "reassignment decision timestamp conflict")
    _validate_accepted_economics(page, evidence)
    return state


def _accepted_result(
    page: dict, evidence: BindingAssignmentEvidence
) -> AssignmentHistoryResult | None:
    if _select(page, "제안 상태") != "ACCEPTED":
        return None
    if not _same_datetime(_date(page, "응답 시각"), evidence.accepted_at):
        raise AssignmentHistoryError(
            CONFLICT, "accepted timestamp conflicts with authenticated callback"
        )
    _validate_accepted_economics(page, evidence)
    page_id = _page_id(page)
    hold = _select(page, "Hold 상태")
    if hold == "HARD_BOOKED":
        return AssignmentHistoryResult(ALREADY_ACCEPTED, page_id, False)
    if hold == "RELEASED":
        if not (
            _date(page, "Assignment Ended At")
            and _select(page, "Assignment End Reason")
            and _text(page, "Assignment End Key")
        ):
            raise AssignmentHistoryError(
                SCHEMA_MISMATCH, "released accepted history is missing end metadata"
            )
        return AssignmentHistoryResult(ALREADY_ENDED, page_id, False)
    raise AssignmentHistoryError(
        SCHEMA_MISMATCH, "accepted history has invalid Hold state"
    )


def record_accepted_assignment(
    *, store: AssignmentHistoryStore, action_id: str, idempotency_key: str | None = None
) -> AssignmentHistoryResult:
    """Materialize exactly one accepted generation from verified provenance."""

    evidence = resolve_canonical_binding_assignment(_require_action_id(action_id))
    expected_key = evidence.operation_identity
    if idempotency_key is None:
        idempotency_key = expected_key
    if _require_text(idempotency_key, "Idempotency Key") != expected_key:
        raise AssignmentHistoryError(
            VERSION_CONFLICT, "Idempotency Key is not bound to semantic payload"
        )
    version = evidence.assignment_version

    with assignment_generation_lock(
        cleaning_page_id=evidence.cleaning_page_id, assignment_version=version
    ):
        rows = store.query_assignment_generation(
            cleaning_page_id=evidence.cleaning_page_id, assignment_version=version
        )
        if len(rows) > 1:
            raise AssignmentHistoryError(
                AMBIGUOUS, "multiple rows share one Production assignment generation"
            )
        if rows:
            page = rows[0]
            _validate_offer_identity(
                page,
                evidence=evidence,
                assignment_version=version,
                idempotency_key=idempotency_key,
            )
            existing = _accepted_result(page, evidence)
            if existing is not None:
                return existing
            if _select(page, "제안 상태") not in {"CREATED", "SENT"}:
                raise AssignmentHistoryError(
                    CONFLICT, "existing binding Offer cannot transition to ACCEPTED"
                )
            if _select(page, "Hold 상태") not in {None, "NONE", "SOFT_HOLD"}:
                raise AssignmentHistoryError(
                    CONFLICT, "existing binding Offer Hold state conflicts"
                )
            updated = store.accept_existing_offer(
                _page_id(page),
                evidence=evidence,
                assignment_version=version,
                idempotency_key=idempotency_key,
            )
            _validate_offer_identity(
                updated,
                evidence=evidence,
                assignment_version=version,
                idempotency_key=idempotency_key,
            )
            result = _accepted_result(updated, evidence)
            if result is None or result.status != ALREADY_ACCEPTED:
                raise AssignmentHistoryError(
                    SCHEMA_MISMATCH,
                    "history acceptance update did not produce ACCEPTED/HARD_BOOKED",
                )
            return AssignmentHistoryResult(UPDATED, _page_id(updated), True)

        rows_by_key = store.query_by_idempotency(idempotency_key)
        if len(rows_by_key) > 1:
            raise AssignmentHistoryError(
                AMBIGUOUS, "multiple Production rows share acceptance idempotency key"
            )
        if rows_by_key:
            raise AssignmentHistoryError(
                CONFLICT,
                "acceptance idempotency key belongs to another assignment generation",
            )

        try:
            created = store.create_accepted_assignment(
                evidence=evidence,
                assignment_version=version,
                idempotency_key=idempotency_key,
            )
        except AssignmentHistoryError as exc:
            if exc.code != REMOTE_NOTION_FAILURE:
                raise
            # Response loss may follow a successful external create.  Recovery is
            # query-only and must verify the exact same semantic generation.
            recovered = store.query_assignment_generation(
                cleaning_page_id=evidence.cleaning_page_id,
                assignment_version=version,
            )
            if len(recovered) == 1:
                _validate_offer_identity(
                    recovered[0],
                    evidence=evidence,
                    assignment_version=version,
                    idempotency_key=idempotency_key,
                )
                result = _accepted_result(recovered[0], evidence)
                if result is not None:
                    return result
            raise

        after = store.query_assignment_generation(
            cleaning_page_id=evidence.cleaning_page_id, assignment_version=version
        )
        if len(after) != 1:
            raise AssignmentHistoryError(
                AMBIGUOUS, "acceptance create did not converge to one generation"
            )
        _validate_offer_identity(
            after[0],
            evidence=evidence,
            assignment_version=version,
            idempotency_key=idempotency_key,
        )
        accepted = _accepted_result(after[0], evidence)
        if accepted is None or accepted.status != ALREADY_ACCEPTED:
            raise AssignmentHistoryError(
                SCHEMA_MISMATCH, "created history row is not ACCEPTED/HARD_BOOKED"
            )
        if not _same_page_id(_page_id(after[0]), _page_id(created)):
            raise AssignmentHistoryError(
                AMBIGUOUS, "acceptance create resolved to a different history row"
            )
        return AssignmentHistoryResult(CREATED, _page_id(created), True)


def record_reassigned_original_assignment(
    *, store: AssignmentHistoryStore, action_id: str
) -> AssignmentHistoryResult:
    """Materialize one direct original-Cleaner reassignment from frozen operator authority."""

    evidence = resolve_canonical_assignment_authority(_require_action_id(action_id))
    if evidence.binding_offer is not False:
        raise AssignmentHistoryError(
            UNVERIFIED_BINDING_ASSIGNMENT, "operator reassignment authority required"
        )
    version = evidence.assignment_version
    key = evidence.operation_identity
    with assignment_generation_lock(
        cleaning_page_id=evidence.cleaning_page_id, assignment_version=version
    ):
        rows = store.query_assignment_generation(
            cleaning_page_id=evidence.cleaning_page_id, assignment_version=version
        )
        if len(rows) > 1:
            raise AssignmentHistoryError(AMBIGUOUS, "multiple rows share reassignment generation")
        if rows:
            state = _validate_reassigned_identity(
                rows[0], evidence=evidence, assignment_version=version, idempotency_key=key
            )
            return AssignmentHistoryResult(state, _page_id(rows[0]), False)
        keyed = store.query_by_idempotency(key)
        if len(keyed) > 1:
            raise AssignmentHistoryError(AMBIGUOUS, "multiple rows share reassignment idempotency key")
        if keyed:
            raise AssignmentHistoryError(CONFLICT, "reassignment idempotency key belongs elsewhere")
        try:
            created = store.create_reassigned_assignment(
                evidence=evidence, assignment_version=version, idempotency_key=key
            )
        except AssignmentHistoryError as exc:
            if exc.code != REMOTE_NOTION_FAILURE:
                raise
            recovered = store.query_assignment_generation(
                cleaning_page_id=evidence.cleaning_page_id, assignment_version=version
            )
            if len(recovered) == 1:
                state = _validate_reassigned_identity(
                    recovered[0], evidence=evidence, assignment_version=version, idempotency_key=key
                )
                return AssignmentHistoryResult(state, _page_id(recovered[0]), False)
            raise
        after = store.query_assignment_generation(
            cleaning_page_id=evidence.cleaning_page_id, assignment_version=version
        )
        if len(after) != 1:
            raise AssignmentHistoryError(AMBIGUOUS, "reassignment create did not converge")
        _validate_reassigned_identity(
            after[0], evidence=evidence, assignment_version=version, idempotency_key=key
        )
        if not _same_page_id(_page_id(after[0]), _page_id(created)):
            raise AssignmentHistoryError(AMBIGUOUS, "reassignment create resolved to different row")
        return AssignmentHistoryResult(CREATED, _page_id(created), True)


def resolve_effective_accepted_assignment(
    *,
    store: AssignmentHistoryStore,
    cleaning_page_id: str,
    cleaner_party_page_id: str,
) -> tuple[BindingAssignmentEvidence, dict]:
    """Resolve exactly one current ACCEPTED/HARD_BOOKED fact from canonical 19."""

    cleaning_page_id = _normalize_page_id(cleaning_page_id)
    cleaner_party_page_id = _normalize_page_id(cleaner_party_page_id)
    rows = store.query_effective_accepted(
        cleaning_page_id=cleaning_page_id,
        cleaner_party_page_id=cleaner_party_page_id,
    )
    if not rows:
        raise AssignmentHistoryError(
            NOT_FOUND, "effective accepted assignment history not found"
        )
    if len(rows) != 1:
        raise AssignmentHistoryError(
            AMBIGUOUS, "effective accepted assignment history is ambiguous"
        )
    page = rows[0]
    if (
        _select(page, "제안 상태") != "ACCEPTED"
        or _select(page, "Hold 상태") != "HARD_BOOKED"
        or _select(page, "데이터 환경") != "PRODUCTION"
        or _date(page, "Assignment Ended At") is not None
        or _select(page, "Assignment End Reason") is not None
        or _text(page, "Assignment End Key") is not None
    ):
        raise AssignmentHistoryError(
            CONFLICT, "history row is not an effective accepted assignment"
        )
    action_id = _require_action_id(_text(page, "Action ID"))
    assignment_version = _require_text(
        _text(page, "Assignment Version"), "Assignment Version"
    )
    idempotency_key = _require_text(
        _text(page, "Idempotency Key"), "Idempotency Key"
    )
    evidence = resolve_canonical_assignment_authority(action_id)
    if evidence.operation_identity != idempotency_key:
        raise AssignmentHistoryError(
            VERSION_CONFLICT, "history idempotency identity is not canonical"
        )
    _validate_end_identity(
        page,
        cleaning_page_id=cleaning_page_id,
        cleaner_party_page_id=cleaner_party_page_id,
        assignment_version=assignment_version,
        action_id=action_id,
        idempotency_key=idempotency_key,
        binding_offer=evidence.binding_offer,
    )
    if evidence.assignment_version != assignment_version:
        raise AssignmentHistoryError(VERSION_CONFLICT, "Assignment Version conflict")
    return evidence, page


def recent_unavailable_assignments(
    *, store: AssignmentHistoryStore, cleaner_party_page_id: str
) -> list[dict]:
    """Return bounded, privacy-safe metadata for this Cleaner's ended facts."""

    cleaner_party_page_id = _normalize_page_id(cleaner_party_page_id)
    result = []
    for page in store.query_recent_unavailable(
        cleaner_party_page_id=cleaner_party_page_id
    ):
        if (
            _select(page, "데이터 환경") != "PRODUCTION"
            or _select(page, "제안 상태") != "ACCEPTED"
            or _select(page, "Hold 상태") != "RELEASED"
            or _select(page, "Assignment End Reason") != "CLEANER_UNAVAILABLE"
        ):
            raise AssignmentHistoryError(CONFLICT, "recent unavailable row malformed")
        cleanings = _relation_ids(page, "Cleaning")
        cleaners = _relation_ids(page, "후보 인력")
        if (
            cleanings is None
            or len(cleanings) != 1
            or cleaners is None
            or len(cleaners) != 1
            or not _same_page_id(cleaners[0], cleaner_party_page_id)
        ):
            raise AssignmentHistoryError(CONFLICT, "recent unavailable binding malformed")
        ended_at = _date(page, "Assignment Ended At")
        end_key = _text(page, "Assignment End Key")
        if not ended_at or not end_key:
            raise AssignmentHistoryError(SCHEMA_MISMATCH, "unavailable end metadata missing")
        result.append({
            "history_page_id": _page_id(page),
            "cleaning_page_id": cleanings[0],
            "cleaner_party_page_id": cleaners[0],
            "assignment_version": _require_text(_text(page, "Assignment Version"), "Assignment Version"),
            "assignment_action_id": _require_action_id(_text(page, "Action ID")),
            "acceptance_idempotency_key": _require_text(_text(page, "Idempotency Key"), "Idempotency Key"),
            "ended_at": ended_at,
            "end_key": end_key,
            "reassignment_request_status": _select(page, "Reassignment Request Status"),
            "reassignment_requested_at": _date(page, "Reassignment Requested At"),
            "reassignment_request_key": _text(page, "Reassignment Request Key"),
        })
    return result


def resolve_ended_unavailable_assignment(
    *,
    store: AssignmentHistoryStore,
    history_page_id: str,
    cleaning_page_id: str,
    cleaner_party_page_id: str,
    assignment_version: str,
    action_id: str,
    idempotency_key: str,
    end_key: str,
) -> dict:
    """Resolve one exact historical CLEANER_UNAVAILABLE Assignment from canonical 19.

    This is a compatibility read for already-issued signed reassignment actions.
    It never consults mutable Cleaning 08 and never guesses among canonical rows.
    """

    history_page_id = _normalize_page_id(history_page_id)
    cleaning_page_id = _normalize_page_id(cleaning_page_id)
    cleaner_party_page_id = _normalize_page_id(cleaner_party_page_id)
    assignment_version = _require_text(assignment_version, "Assignment Version")
    action_id = _require_action_id(action_id)
    idempotency_key = _require_text(idempotency_key, "Idempotency Key")
    end_key = _require_text(end_key, "Assignment End Key")

    rows = store.query_assignment_generation(
        cleaning_page_id=cleaning_page_id,
        assignment_version=assignment_version,
    )
    candidates = []
    for page in rows:
        _validate_generation_identity(
            page,
            cleaning_page_id=cleaning_page_id,
            assignment_version=assignment_version,
        )
        cleaners = _relation_ids(page, "후보 인력")
        if (
            cleaners is not None
            and len(cleaners) == 1
            and _same_page_id(cleaners[0], cleaner_party_page_id)
            and _text(page, "Action ID") == action_id
            and _text(page, "Idempotency Key") == idempotency_key
        ):
            candidates.append(page)

    if not candidates:
        raise AssignmentHistoryError(
            NOT_FOUND, "original unavailable Assignment not found"
        )
    if len(candidates) != 1:
        raise AssignmentHistoryError(
            AMBIGUOUS, "original unavailable Assignment is ambiguous"
        )

    page = candidates[0]
    if not _same_page_id(_page_id(page), history_page_id):
        raise AssignmentHistoryError(
            NOT_FOUND, "signed action history target does not match canonical Assignment"
        )
    _validate_end_identity(
        page,
        cleaning_page_id=cleaning_page_id,
        cleaner_party_page_id=cleaner_party_page_id,
        assignment_version=assignment_version,
        action_id=action_id,
        idempotency_key=idempotency_key,
    )
    if (
        _select(page, "제안 상태") != "ACCEPTED"
        or _select(page, "Hold 상태") != "RELEASED"
        or _select(page, "Assignment End Reason") != "CLEANER_UNAVAILABLE"
        or _text(page, "Assignment End Key") != end_key
    ):
        raise AssignmentHistoryError(
            CONFLICT, "original Assignment is not the exact ended unavailable fact"
        )
    ended_at = _date(page, "Assignment Ended At")
    if not ended_at:
        raise AssignmentHistoryError(
            SCHEMA_MISMATCH, "original unavailable Assignment end timestamp missing"
        )
    return {
        "history_page_id": _page_id(page),
        "ended_at": _parse_aware(ended_at, "Assignment Ended At").isoformat(),
        "page": page,
    }


def other_effective_accepted_assignment_exists(
    *, store: AssignmentHistoryStore, cleaning_page_id: str, assignment_version: str
) -> bool:
    """Fail-closed guard for a pending Offer after another generation became effective."""

    cleaning_page_id = _normalize_page_id(cleaning_page_id)
    assignment_version = _require_text(assignment_version, "Assignment Version")
    for page in store.query_accepted_for_cleaning(cleaning_page_id=cleaning_page_id):
        if _select(page, "데이터 환경") != "PRODUCTION" or _select(page, "제안 상태") != "ACCEPTED":
            raise AssignmentHistoryError(CONFLICT, "accepted Assignment history malformed")
        cleanings = _relation_ids(page, "Cleaning")
        if cleanings is None or len(cleanings) != 1 or not _same_page_id(cleanings[0], cleaning_page_id):
            raise AssignmentHistoryError(CONFLICT, "accepted Assignment Cleaning binding malformed")
        version = _require_text(_text(page, "Assignment Version"), "Assignment Version")
        if version != assignment_version and _select(page, "Hold 상태") == "HARD_BOOKED":
            return True
    return False


def newer_accepted_assignment_exists(
    *,
    store: AssignmentHistoryStore,
    cleaning_page_id: str,
    original_assignment_version: str,
    original_ended_at: str,
) -> bool:
    """Return whether canonical 19 proves a replacement was ever ACCEPTED later."""

    cleaning_page_id = _normalize_page_id(cleaning_page_id)
    original_assignment_version = _require_text(
        original_assignment_version, "Assignment Version"
    )
    ended_at = _parse_aware(original_ended_at, "Assignment Ended At")
    rows = store.query_accepted_for_cleaning(cleaning_page_id=cleaning_page_id)
    for page in rows:
        if (
            _select(page, "데이터 환경") != "PRODUCTION"
            or _select(page, "제안 상태") != "ACCEPTED"
        ):
            raise AssignmentHistoryError(CONFLICT, "accepted replacement history malformed")
        cleanings = _relation_ids(page, "Cleaning")
        if cleanings is None or len(cleanings) != 1 or not _same_page_id(
            cleanings[0], cleaning_page_id
        ):
            raise AssignmentHistoryError(CONFLICT, "accepted replacement Cleaning binding malformed")
        version = _require_text(_text(page, "Assignment Version"), "Assignment Version")
        accepted_at = _parse_aware(_date(page, "응답 시각"), "응답 시각")
        if version != original_assignment_version and accepted_at >= ended_at:
            return True
    return False


def end_accepted_assignment(
    *,
    store: AssignmentHistoryStore,
    cleaning_page_id: str,
    cleaner_party_page_id: str,
    assignment_version: str,
    action_id: str,
    acceptance_idempotency_key: str,
    ended_at: datetime,
    end_reason: str,
    end_key: str,
) -> AssignmentHistoryResult:
    """Release an existing accepted fact; never synthesize missing legacy history."""

    cleaning_page_id = _normalize_page_id(cleaning_page_id)
    cleaner_party_page_id = _normalize_page_id(cleaner_party_page_id)
    assignment_version = _require_text(assignment_version, "Assignment Version")
    action_id = _require_action_id(action_id)
    acceptance_idempotency_key = _require_text(
        acceptance_idempotency_key, "Idempotency Key"
    )
    ended_at = _require_aware(ended_at, "ended_at")
    end_key = _require_text(end_key, "Assignment End Key")
    if end_reason not in ALLOWED_END_REASONS:
        raise AssignmentHistoryError(
            SCHEMA_MISMATCH, "Assignment End Reason is not allowed"
        )
    with assignment_generation_lock(
        cleaning_page_id=cleaning_page_id, assignment_version=assignment_version
    ):
        rows = store.query_by_idempotency(acceptance_idempotency_key)
        if not rows:
            raise AssignmentHistoryError(
                NOT_FOUND, "accepted assignment history not found; no backfill permitted"
            )
        if len(rows) > 1:
            raise AssignmentHistoryError(
                AMBIGUOUS, "accepted assignment history is ambiguous"
            )
        page = rows[0]
        authority = resolve_canonical_assignment_authority(action_id)
        if (
            authority.cleaning_page_id != cleaning_page_id
            or authority.cleaner_party_page_id != cleaner_party_page_id
            or authority.assignment_version != assignment_version
            or authority.operation_identity != acceptance_idempotency_key
        ):
            raise AssignmentHistoryError(VERSION_CONFLICT, "Assignment end authority mismatch")
        _validate_end_identity(
            page,
            cleaning_page_id=cleaning_page_id,
            cleaner_party_page_id=cleaner_party_page_id,
            assignment_version=assignment_version,
            action_id=action_id,
            idempotency_key=acceptance_idempotency_key,
            binding_offer=authority.binding_offer,
        )
        if _select(page, "제안 상태") != "ACCEPTED":
            raise AssignmentHistoryError(CONFLICT, "history fact is not ACCEPTED")
        page_id = _page_id(page)
        hold = _select(page, "Hold 상태")
        old_key = _text(page, "Assignment End Key")
        old_reason = _select(page, "Assignment End Reason")
        old_time = _date(page, "Assignment Ended At")
        if hold == "RELEASED":
            if not old_key or not old_reason or not old_time:
                raise AssignmentHistoryError(
                    SCHEMA_MISMATCH, "released history is missing end metadata"
                )
            if old_key == end_key and old_reason == end_reason and _same_datetime(
                old_time, ended_at
            ):
                return AssignmentHistoryResult(RETRY_SAME_KEY, page_id, False)
            raise AssignmentHistoryError(
                CONFLICT, "accepted assignment already ended with different metadata"
            )
        if hold != "HARD_BOOKED":
            raise AssignmentHistoryError(
                SCHEMA_MISMATCH, "accepted history is not HARD_BOOKED before release"
            )
        if any(value is not None for value in (old_key, old_reason, old_time)):
            raise AssignmentHistoryError(
                SCHEMA_MISMATCH, "active accepted history already has end metadata"
            )
        updated = store.release_accepted_assignment(
            page_id,
            cleaning_page_id=cleaning_page_id,
            cleaner_party_page_id=cleaner_party_page_id,
            assignment_version=assignment_version,
            action_id=action_id,
            idempotency_key=acceptance_idempotency_key,
            ended_at=ended_at,
            end_reason=end_reason,
            end_key=end_key,
        )
        if not _same_page_id(_page_id(updated), page_id):
            raise AssignmentHistoryError(
                SCHEMA_MISMATCH, "history release returned a different page"
            )
        if (
            _select(updated, "제안 상태") != "ACCEPTED"
            or _select(updated, "Hold 상태") != "RELEASED"
            or _text(updated, "Assignment End Key") != end_key
            or _select(updated, "Assignment End Reason") != end_reason
            or not _same_datetime(_date(updated, "Assignment Ended At"), ended_at)
        ):
            raise AssignmentHistoryError(
                SCHEMA_MISMATCH, "history release did not preserve terminal semantics"
            )
        return AssignmentHistoryResult(UPDATED, page_id, True)


def end_effective_assignment_for_cancellation(
    *,
    store: AssignmentHistoryStore,
    cleaning_page_id: str,
    ended_at: datetime,
    end_key: str,
) -> AssignmentHistoryResult | None:
    """Release the sole effective Assignment when its Reservation is cancelled."""

    cleaning_page_id = _normalize_page_id(cleaning_page_id)
    ended_at = _require_aware(ended_at, "ended_at")
    end_key = _require_text(end_key, "Assignment End Key")
    active = []
    for page in store.query_accepted_for_cleaning(cleaning_page_id=cleaning_page_id):
        if (
            _select(page, "데이터 환경") != "PRODUCTION"
            or _select(page, "제안 상태") != "ACCEPTED"
        ):
            raise AssignmentHistoryError(CONFLICT, "accepted Assignment history malformed")
        cleanings = _relation_ids(page, "Cleaning")
        if cleanings is None or len(cleanings) != 1 or not _same_page_id(
            cleanings[0], cleaning_page_id
        ):
            raise AssignmentHistoryError(CONFLICT, "accepted Assignment Cleaning binding malformed")
        hold = _select(page, "Hold 상태")
        if hold == "HARD_BOOKED":
            active.append(page)
        elif hold != "RELEASED":
            raise AssignmentHistoryError(CONFLICT, "accepted Assignment Hold state malformed")
    if len(active) > 1:
        raise AssignmentHistoryError(AMBIGUOUS, "multiple effective Assignments block cancellation")
    if not active:
        return None

    page = active[0]
    cleaners = _relation_ids(page, "후보 인력")
    if cleaners is None or len(cleaners) != 1:
        raise AssignmentHistoryError(CONFLICT, "effective Assignment Cleaner binding malformed")
    return end_accepted_assignment(
        store=store,
        cleaning_page_id=cleaning_page_id,
        cleaner_party_page_id=cleaners[0],
        assignment_version=_require_text(_text(page, "Assignment Version"), "Assignment Version"),
        action_id=_require_action_id(_text(page, "Action ID")),
        acceptance_idempotency_key=_require_text(_text(page, "Idempotency Key"), "Idempotency Key"),
        ended_at=ended_at,
        end_reason="RESERVATION_CANCELLED",
        end_key=end_key,
    )


class NotionAssignmentHistoryStore:
    """Narrow canonical 19 DB adapter with current Global Writer fencing."""

    def __init__(
        self,
        *,
        token_path: Path = NOTION_TOKEN_PATH,
        urlopen=urllib.request.urlopen,
        authority_assert: Callable[[], object] = assert_current_production_writer,
    ):
        self._token_path = token_path
        self._urlopen = urlopen
        self._authority_assert = authority_assert

    def _headers(self) -> dict[str, str]:
        try:
            token = self._token_path.read_text().strip()
        except OSError as exc:
            raise AssignmentHistoryError(
                REMOTE_NOTION_FAILURE, "Notion token unavailable"
            ) from exc
        if not token:
            raise AssignmentHistoryError(
                REMOTE_NOTION_FAILURE, "Notion token unavailable"
            )
        return {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _request(
        self, method: str, path: str, body: dict | None = None, *, mutating: bool = False
    ) -> dict:
        allowed = (
            (
                method == "GET"
                and re.fullmatch(r"/v1/pages/[A-Za-z0-9_-]+", path) is not None
            )
            or (
                method == "POST"
                and path == f"/v1/data_sources/{ASSIGNMENT_HISTORY_SOURCE}/query"
            )
            or (method == "POST" and path == "/v1/pages" and mutating)
            or (
                method == "PATCH"
                and re.fullmatch(r"/v1/pages/[A-Za-z0-9_-]+", path) is not None
                and mutating
            )
        )
        if not allowed:
            raise AssignmentHistoryError(
                SCHEMA_MISMATCH, "assignment history endpoint rejected"
            )
        if mutating:
            self._authority_assert()
        try:
            request = urllib.request.Request(
                "https://api.notion.com" + path,
                data=(
                    json.dumps(body, ensure_ascii=False).encode()
                    if body is not None
                    else None
                ),
                method=method,
                headers=self._headers(),
            )
            with self._urlopen(request, timeout=40) as response:
                result = json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError) as exc:
            if mutating:
                # Response loss can follow a real external effect.  Reasserting
                # here fences stale owners before recovery metadata is published.
                self._authority_assert()
            raise AssignmentHistoryError(
                REMOTE_NOTION_FAILURE, type(exc).__name__
            ) from exc
        if mutating:
            # SC-13: no durable success publication from a stale writer after an
            # external Notion effect.
            self._authority_assert()
        if not isinstance(result, dict):
            raise AssignmentHistoryError(
                SCHEMA_MISMATCH, "Notion response must be an object"
            )
        return result

    @staticmethod
    def _assert_history_target(page: dict) -> None:
        parent = page.get("parent") if isinstance(page, dict) else None
        if not isinstance(parent, dict):
            raise AssignmentHistoryError(
                INVALID_HISTORY_TARGET, "history target parent missing"
            )
        if parent.get("data_source_id") != ASSIGNMENT_HISTORY_SOURCE:
            raise AssignmentHistoryError(
                INVALID_HISTORY_TARGET,
                "target does not belong to canonical 19 DB data source",
            )
        database = parent.get("database_id")
        if database is not None and database != ASSIGNMENT_HISTORY_DATABASE:
            raise AssignmentHistoryError(
                INVALID_HISTORY_TARGET,
                "target does not belong to canonical 19 DB database",
            )

    def _query_results(self, response: dict) -> list[dict]:
        rows = response.get("results")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise AssignmentHistoryError(
                SCHEMA_MISMATCH, "Notion query results malformed"
            )
        if response.get("has_more") is not False or response.get("next_cursor") is not None:
            raise AssignmentHistoryError(
                AMBIGUOUS, "history query unexpectedly paginated"
            )
        for row in rows:
            self._assert_history_target(row)
        return rows

    def _history_target(self, page_id: str) -> dict:
        page = self._request("GET", f"/v1/pages/{_normalize_page_id(page_id)}")
        self._assert_history_target(page)
        if _select(page, "데이터 환경") != "PRODUCTION":
            raise AssignmentHistoryError(
                INVALID_HISTORY_TARGET, "history update target is not PRODUCTION"
            )
        return page

    def query_assignment_generation(
        self, *, cleaning_page_id: str, assignment_version: str
    ) -> list[dict]:
        response = self._request(
            "POST",
            f"/v1/data_sources/{ASSIGNMENT_HISTORY_SOURCE}/query",
            {
                "filter": {
                    "and": [
                        {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
                        {
                            "property": "Cleaning",
                            "relation": {"contains": _normalize_page_id(cleaning_page_id)},
                        },
                        {
                            "property": "Assignment Version",
                            "rich_text": {
                                "equals": _require_text(
                                    assignment_version, "Assignment Version"
                                )
                            },
                        },
                    ]
                },
                "page_size": 100,
            },
        )
        return self._query_results(response)

    def query_by_idempotency(self, key: str) -> list[dict]:
        response = self._request(
            "POST",
            f"/v1/data_sources/{ASSIGNMENT_HISTORY_SOURCE}/query",
            {
                "filter": {
                    "and": [
                        {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
                        {
                            "property": "Idempotency Key",
                            "rich_text": {"equals": _require_text(key, "Idempotency Key")},
                        },
                    ]
                },
                "page_size": 100,
            },
        )
        return self._query_results(response)

    def query_effective_accepted(
        self, *, cleaning_page_id: str, cleaner_party_page_id: str
    ) -> list[dict]:
        response = self._request(
            "POST",
            f"/v1/data_sources/{ASSIGNMENT_HISTORY_SOURCE}/query",
            {
                "filter": {
                    "and": [
                        {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
                        {"property": "Cleaning", "relation": {"contains": _normalize_page_id(cleaning_page_id)}},
                        {"property": "후보 인력", "relation": {"contains": _normalize_page_id(cleaner_party_page_id)}},
                        {"property": "제안 상태", "select": {"equals": "ACCEPTED"}},
                        {"property": "Hold 상태", "select": {"equals": "HARD_BOOKED"}},
                    ]
                },
                "page_size": 3,
            },
        )
        return self._query_results(response)

    def query_recent_unavailable(
        self, *, cleaner_party_page_id: str
    ) -> list[dict]:
        response = self._request(
            "POST",
            f"/v1/data_sources/{ASSIGNMENT_HISTORY_SOURCE}/query",
            {
                "filter": {
                    "and": [
                        {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
                        {"property": "후보 인력", "relation": {"contains": _normalize_page_id(cleaner_party_page_id)}},
                        {"property": "제안 상태", "select": {"equals": "ACCEPTED"}},
                        {"property": "Hold 상태", "select": {"equals": "RELEASED"}},
                        {"property": "Assignment End Reason", "select": {"equals": "CLEANER_UNAVAILABLE"}},
                    ]
                },
                "sorts": [{"property": "Assignment Ended At", "direction": "descending"}],
                "page_size": 20,
            },
        )
        return self._query_results(response)

    def query_accepted_for_cleaning(
        self, *, cleaning_page_id: str
    ) -> list[dict]:
        response = self._request(
            "POST",
            f"/v1/data_sources/{ASSIGNMENT_HISTORY_SOURCE}/query",
            {
                "filter": {
                    "and": [
                        {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
                        {"property": "Cleaning", "relation": {"contains": _normalize_page_id(cleaning_page_id)}},
                        {"property": "제안 상태", "select": {"equals": "ACCEPTED"}},
                    ]
                },
                "sorts": [{"property": "응답 시각", "direction": "descending"}],
                "page_size": 20,
            },
        )
        return self._query_results(response)

    def create_accepted_assignment(
        self,
        *,
        evidence: BindingAssignmentEvidence,
        assignment_version: str,
        idempotency_key: str,
    ) -> dict:
        if idempotency_key != evidence.operation_identity:
            raise AssignmentHistoryError(
                VERSION_CONFLICT, "semantic idempotency identity mismatch"
            )
        properties = {
            "제안명": _title_property(evidence.action_id),
            "Cleaning": _relation_property(evidence.cleaning_page_id),
            "후보 인력": _relation_property(evidence.cleaner_party_page_id),
            "제안 상태": _select_property("ACCEPTED"),
            "Hold 상태": _select_property("HARD_BOOKED"),
            "Binding Offer": _checkbox_property(True),
            "응답 시각": _date_property(evidence.accepted_at),
            "제안 시각": _date_property(evidence.offered_at),
            "Action ID": _rich_text_property(evidence.action_id),
            "Idempotency Key": _rich_text_property(idempotency_key),
            "Assignment Version": _rich_text_property(assignment_version),
            "데이터 환경": _select_property("PRODUCTION"),
            "Telegram Message ID": _rich_text_property(evidence.telegram_message_id),
            "제안 라운드": _number_property(evidence.proposal_round),
        }
        properties.update(_accepted_economics_properties(evidence))
        created = self._request(
            "POST",
            "/v1/pages",
            {
                "parent": {
                    "type": "data_source_id",
                    "data_source_id": ASSIGNMENT_HISTORY_SOURCE,
                },
                "properties": properties,
            },
            mutating=True,
        )
        self._assert_history_target(created)
        return created

    def create_reassigned_assignment(
        self,
        *,
        evidence: BindingAssignmentEvidence,
        assignment_version: str,
        idempotency_key: str,
    ) -> dict:
        if evidence.binding_offer is not False or idempotency_key != evidence.operation_identity:
            raise AssignmentHistoryError(VERSION_CONFLICT, "direct reassignment identity mismatch")
        properties = {
            "제안명": _title_property(evidence.action_id),
            "Cleaning": _relation_property(evidence.cleaning_page_id),
            "후보 인력": _relation_property(evidence.cleaner_party_page_id),
            "제안 상태": _select_property("ACCEPTED"),
            "Hold 상태": _select_property("HARD_BOOKED"),
            "Binding Offer": _checkbox_property(False),
            "응답 시각": _date_property(evidence.accepted_at),
            "Action ID": _rich_text_property(evidence.action_id),
            "Idempotency Key": _rich_text_property(idempotency_key),
            "Assignment Version": _rich_text_property(assignment_version),
            "데이터 환경": _select_property("PRODUCTION"),
        }
        properties.update(_accepted_economics_properties(evidence))
        created = self._request(
            "POST", "/v1/pages",
            {
                "parent": {"type": "data_source_id", "data_source_id": ASSIGNMENT_HISTORY_SOURCE},
                "properties": properties,
            },
            mutating=True,
        )
        self._assert_history_target(created)
        return created

    def _patch_history_properties(
        self,
        page_id: str,
        *,
        properties: dict,
        allowed_fields: frozenset[str],
        validate_target,
    ) -> dict:
        normalized = _normalize_page_id(page_id)
        target = self._history_target(normalized)
        validate_target(target)
        if set(properties) != set(allowed_fields):
            raise AssignmentHistoryError(
                SCHEMA_MISMATCH, "history PATCH property whitelist mismatch"
            )
        updated = self._request(
            "PATCH",
            f"/v1/pages/{normalized}",
            {"properties": properties},
            mutating=True,
        )
        self._assert_history_target(updated)
        return updated

    def accept_existing_offer(
        self,
        page_id: str,
        *,
        evidence: BindingAssignmentEvidence,
        assignment_version: str,
        idempotency_key: str,
    ) -> dict:
        def validate_target(page: dict) -> None:
            _validate_offer_identity(
                page,
                evidence=evidence,
                assignment_version=assignment_version,
                idempotency_key=idempotency_key,
            )
            if _select(page, "제안 상태") not in {"CREATED", "SENT"}:
                raise AssignmentHistoryError(
                    CONFLICT, "history target is not an accept-pending binding Offer"
                )
            if _select(page, "Hold 상태") not in {None, "NONE", "SOFT_HOLD"}:
                raise AssignmentHistoryError(
                    CONFLICT, "history target Hold state blocks acceptance"
                )

        properties = {
            "제안 상태": _select_property("ACCEPTED"),
            "Hold 상태": _select_property("HARD_BOOKED"),
            "응답 시각": _date_property(evidence.accepted_at),
        }
        properties.update(_accepted_economics_properties(evidence))
        return self._patch_history_properties(
            page_id,
            properties=properties,
            allowed_fields=frozenset(properties),
            validate_target=validate_target,
        )

    def release_accepted_assignment(
        self,
        page_id: str,
        *,
        cleaning_page_id: str,
        cleaner_party_page_id: str,
        assignment_version: str,
        action_id: str,
        idempotency_key: str,
        ended_at: datetime,
        end_reason: str,
        end_key: str,
    ) -> dict:
        def validate_target(page: dict) -> None:
            _validate_end_identity(
                page,
                cleaning_page_id=cleaning_page_id,
                cleaner_party_page_id=cleaner_party_page_id,
                assignment_version=assignment_version,
                action_id=action_id,
                idempotency_key=idempotency_key,
            )
            if (
                _select(page, "제안 상태") != "ACCEPTED"
                or _select(page, "Hold 상태") != "HARD_BOOKED"
            ):
                raise AssignmentHistoryError(
                    CONFLICT, "history target is not active ACCEPTED/HARD_BOOKED"
                )
            if any(
                (
                    _text(page, "Assignment End Key"),
                    _select(page, "Assignment End Reason"),
                    _date(page, "Assignment Ended At"),
                )
            ):
                raise AssignmentHistoryError(
                    SCHEMA_MISMATCH, "active history target already has end metadata"
                )

        if end_reason not in ALLOWED_END_REASONS:
            raise AssignmentHistoryError(
                SCHEMA_MISMATCH, "Assignment End Reason is not allowed"
            )
        return self._patch_history_properties(
            page_id,
            properties={
                "Hold 상태": _select_property("RELEASED"),
                "Assignment Ended At": _date_property(ended_at),
                "Assignment End Reason": _select_property(end_reason),
                "Assignment End Key": _rich_text_property(end_key),
            },
            allowed_fields=frozenset(
                {
                    "Hold 상태",
                    "Assignment Ended At",
                    "Assignment End Reason",
                    "Assignment End Key",
                }
            ),
            validate_target=validate_target,
        )
    def capture_unavailable_reason(
        self,
        page_id: str,
        *,
        cleaning_page_id: str,
        cleaner_party_page_id: str,
        assignment_version: str,
        action_id: str,
        idempotency_key: str,
        end_key: str,
        reason: str,
        captured_at: datetime,
    ) -> dict:
        captured_at = _require_aware(captured_at, "captured_at")
        reason = _require_text(reason, "Unavailable Reason")

        def validate_target(page: dict) -> None:
            _validate_end_identity(
                page, cleaning_page_id=cleaning_page_id,
                cleaner_party_page_id=cleaner_party_page_id,
                assignment_version=assignment_version, action_id=action_id,
                idempotency_key=idempotency_key,
            )
            if (
                _select(page, "제안 상태") != "ACCEPTED"
                or _select(page, "Hold 상태") != "RELEASED"
                or _select(page, "Assignment End Reason") != "CLEANER_UNAVAILABLE"
                or _text(page, "Assignment End Key") != end_key
            ):
                raise AssignmentHistoryError(CONFLICT, "reason target is not the unavailable fact")
            old_reason = _select(page, "Unavailable Reason")
            old_time = _date(page, "Unavailable Reason Captured At")
            if old_reason is not None or old_time is not None:
                if old_reason == reason and _same_datetime(old_time, captured_at):
                    return
                raise AssignmentHistoryError(CONFLICT, "unavailable reason already captured differently")

        target = self._history_target(page_id)
        validate_target(target)
        if _select(target, "Unavailable Reason") == reason and _same_datetime(
            _date(target, "Unavailable Reason Captured At"), captured_at
        ):
            return target
        return self._patch_history_properties(
            page_id,
            properties={
                "Unavailable Reason": _select_property(reason),
                "Unavailable Reason Captured At": _date_property(captured_at),
            },
            allowed_fields=frozenset({"Unavailable Reason", "Unavailable Reason Captured At"}),
            validate_target=validate_target,
        )

    def record_reassignment_request(
        self,
        page_id: str,
        *,
        cleaning_page_id: str,
        cleaner_party_page_id: str,
        assignment_version: str,
        action_id: str,
        idempotency_key: str,
        end_key: str,
        request_key: str,
        requested_at: datetime,
    ) -> dict:
        requested_at = _require_aware(requested_at, "requested_at")
        request_key = _require_text(request_key, "Reassignment Request Key")

        def validate_target(page: dict) -> None:
            _validate_end_identity(
                page, cleaning_page_id=cleaning_page_id,
                cleaner_party_page_id=cleaner_party_page_id,
                assignment_version=assignment_version, action_id=action_id,
                idempotency_key=idempotency_key,
            )
            if (
                _select(page, "제안 상태") != "ACCEPTED"
                or _select(page, "Hold 상태") != "RELEASED"
                or _select(page, "Assignment End Reason") != "CLEANER_UNAVAILABLE"
                or _text(page, "Assignment End Key") != end_key
            ):
                raise AssignmentHistoryError(CONFLICT, "reassignment target is not the unavailable fact")
            old_status = _select(page, "Reassignment Request Status")
            old_key = _text(page, "Reassignment Request Key")
            old_time = _date(page, "Reassignment Requested At")
            if old_status is not None or old_key is not None or old_time is not None:
                if (old_status == "REASSIGNMENT_REQUESTED" and old_key == request_key):
                    return
                raise AssignmentHistoryError(CONFLICT, "reassignment request already recorded differently")

        target = self._history_target(page_id)
        validate_target(target)
        if (
            _select(target, "Reassignment Request Status") == "REASSIGNMENT_REQUESTED"
            and _text(target, "Reassignment Request Key") == request_key
        ):
            return target
        return self._patch_history_properties(
            page_id,
            properties={
                "Reassignment Request Status": _select_property("REASSIGNMENT_REQUESTED"),
                "Reassignment Requested At": _date_property(requested_at),
                "Reassignment Request Key": _rich_text_property(request_key),
            },
            allowed_fields=frozenset({
                "Reassignment Request Status", "Reassignment Requested At", "Reassignment Request Key"
            }),
            validate_target=validate_target,
        )

    def resolve_reassignment_request(
        self, page_id: str, *, cleaning_page_id: str, cleaner_party_page_id: str,
        assignment_version: str, action_id: str, idempotency_key: str, end_key: str,
        request_key: str, resolution: str,
    ) -> dict:
        if resolution not in {"REASSIGNED_ORIGINAL", "CONTINUE_REPLACEMENT"}:
            raise AssignmentHistoryError(SCHEMA_MISMATCH, "unsupported reassignment resolution")
        request_key = _require_text(request_key, "Reassignment Request Key")

        def validate_target(page: dict) -> None:
            _validate_end_identity(
                page, cleaning_page_id=cleaning_page_id,
                cleaner_party_page_id=cleaner_party_page_id,
                assignment_version=assignment_version, action_id=action_id,
                idempotency_key=idempotency_key,
            )
            if (
                _select(page, "제안 상태") != "ACCEPTED"
                or _select(page, "Hold 상태") != "RELEASED"
                or _select(page, "Assignment End Reason") != "CLEANER_UNAVAILABLE"
                or _text(page, "Assignment End Key") != end_key
                or _text(page, "Reassignment Request Key") != request_key
            ):
                raise AssignmentHistoryError(CONFLICT, "reassignment resolution target mismatch")
            old = _select(page, "Reassignment Request Status")
            if old not in {"REASSIGNMENT_REQUESTED", resolution}:
                raise AssignmentHistoryError(CONFLICT, "reassignment request already resolved differently")

        target = self._history_target(page_id)
        validate_target(target)
        if _select(target, "Reassignment Request Status") == resolution:
            return target
        return self._patch_history_properties(
            page_id,
            properties={"Reassignment Request Status": _select_property(resolution)},
            allowed_fields=frozenset({"Reassignment Request Status"}),
            validate_target=validate_target,
        )

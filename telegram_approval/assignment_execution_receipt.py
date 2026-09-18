#!/usr/bin/env python3
"""Authenticated evidence for a successful Cleaner assignment execution.

The current-head Assignment History cutover may only materialize history from a
receipt created after the Cleaning projection has factually accepted the
assignment.  The HMAC covers the complete semantic execution payload so a retry
can recover history without trusting mutable caller fields.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram_approval.cleaner_performance import canonical_offer_economics


RECEIPT_VERSION = 1
RECEIPT_DOMAIN_SEPARATOR = "ASSIGNMENT_EXECUTION_RECEIPT_V1"
ACCEPTANCE_OPERATION_DOMAIN = "CLEANING_ASSIGNMENT_ACCEPTED_OPERATION_V1"
RECEIPT_SIGNATURE_ALGORITHM = "HMAC-SHA256"
EXECUTION_INTENT_VERSION = 1
EXECUTION_INTENT_DOMAIN_SEPARATOR = "CLEANING_ASSIGNMENT_EXECUTION_INTENT_V1"
EXECUTION_OPERATION_DOMAIN = "CLEANING_ASSIGNMENT_EFFECT_OPERATION_V1"
PROVENANCE_DIRECTORY_NAME = ".assignment-execution-provenance"
_ACTION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


class AssignmentExecutionReceiptError(ValueError):
    """Receipt evidence is missing, malformed, or fails authentication."""


def _aware_iso(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AssignmentExecutionReceiptError(f"{label} missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AssignmentExecutionReceiptError(f"{label} malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AssignmentExecutionReceiptError(f"{label} must be timezone-aware")
    return value


def _int_identity(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssignmentExecutionReceiptError(f"{label} malformed")
    return value


def _positive_int(value: Any, label: str) -> int:
    value = _int_identity(value, label)
    if value <= 0:
        raise AssignmentExecutionReceiptError(f"{label} malformed")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AssignmentExecutionReceiptError(f"{label} missing")
    return value.strip()


def _action_id(value: Any) -> str:
    value = _required_text(value, "Action ID")
    if _ACTION_ID_RE.fullmatch(value) is None:
        raise AssignmentExecutionReceiptError("Action ID malformed")
    return value


def _read_key(secret_path: Path) -> bytes:
    try:
        value = secret_path.read_text().strip().encode()
    except OSError as exc:
        raise AssignmentExecutionReceiptError("receipt signing key unavailable") from exc
    if not value:
        raise AssignmentExecutionReceiptError("receipt signing key unavailable")
    return value


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise AssignmentExecutionReceiptError("receipt payload is not canonical JSON") from exc


def _receipt_mac(payload: dict[str, Any], key: bytes) -> str:
    message = RECEIPT_DOMAIN_SEPARATOR.encode() + b"\0" + _canonical_json_bytes(payload)
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def canonical_assignment_version(record: dict[str, Any]) -> str:
    """Return the durable generation identity allocated by the Offer action."""

    if record.get("schema_version") != 2:
        raise AssignmentExecutionReceiptError("assignment request schema unsupported")
    if record.get("action_type") != "CLEANING_ASSIGNMENT":
        raise AssignmentExecutionReceiptError(
            "Assignment Version requires CLEANING_ASSIGNMENT"
        )
    return _action_id(record.get("action_id"))


def _generation_identity(record: dict[str, Any]) -> str:
    return "CLEANING_ASSIGNMENT_EXECUTION:" + ":".join(
        (
            _required_text(record.get("cleaning_page_id"), "Cleaning"),
            canonical_assignment_version(record),
            str(_positive_int(record.get("proposal_round"), "proposal_round")),
        )
    )


def _accepted_economics_payload(record: dict[str, Any]) -> dict[str, Any] | None:
    phase3_keys = {
        "replacement_urgency",
        "urgent_premium_krw",
        "total_agreed_fee_krw",
        "urgent_premium_policy_version",
    }
    # Pre-Phase3 pending Offers may carry only the historical base fee. They
    # remain valid legacy-normal actions and must not be rewritten as if they
    # had accepted a Phase3 economic snapshot. New send_assignment records
    # always carry the explicit Phase3 keys, including NORMAL premium=0.
    if not any(key in record for key in phase3_keys):
        return None
    try:
        economics = canonical_offer_economics(record)
    except ValueError as exc:
        raise AssignmentExecutionReceiptError(str(exc)) from exc
    if economics is None:
        return None
    return {
        "replacement_urgency": economics.replacement_urgency,
        "base_fee_krw": economics.base_fee_krw,
        "urgent_premium_krw": economics.urgent_premium_krw,
        "total_agreed_fee_krw": economics.total_agreed_fee_krw,
        "urgent_premium_policy_version": economics.urgent_premium_policy_version,
    }


def _execution_intent_payload(
    record: dict[str, Any], decision: str
) -> dict[str, Any]:
    """Canonical pre-effect identity for exactly one requested assignment effect."""

    if not isinstance(record, dict) or record.get("schema_version") != 2:
        raise AssignmentExecutionReceiptError("assignment request schema unsupported")
    if record.get("action_type") != "CLEANING_ASSIGNMENT":
        raise AssignmentExecutionReceiptError(
            "execution intent is only valid for CLEANING_ASSIGNMENT"
        )
    if record.get("test_mode") is not False:
        raise AssignmentExecutionReceiptError(
            "TEST assignment cannot produce a Production execution intent"
        )
    if record.get("consumed") is not True or record.get("status") not in {
        "APPROVED_PRODUCTION",
        "EXECUTED",
    }:
        raise AssignmentExecutionReceiptError(
            "assignment callback was not durably approved before execution"
        )
    if decision != "approve":
        raise AssignmentExecutionReceiptError(
            "execution intent is only valid for assignment acceptance"
        )

    offered_at = _aware_iso(record.get("created_at"), "offered_at")
    expires_at = _aware_iso(record.get("expires_at"), "expires_at")
    consumed_at = _aware_iso(record.get("consumed_at"), "consumed_at")
    offered_dt = datetime.fromisoformat(offered_at.replace("Z", "+00:00"))
    expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    consumed_dt = datetime.fromisoformat(consumed_at.replace("Z", "+00:00"))
    if expires_dt <= offered_dt or not (offered_dt <= consumed_dt <= expires_dt):
        raise AssignmentExecutionReceiptError(
            "callback timing is outside the Offer lifetime"
        )

    expected_last_edited_time = record.get("expected_last_edited_time")
    if expected_last_edited_time is not None and not isinstance(
        expected_last_edited_time, str
    ):
        raise AssignmentExecutionReceiptError("expected_last_edited_time malformed")

    cleaner_party_page_id = _required_text(
        record.get("candidate_party_page_id"), "Cleaner Party"
    )
    return {
        "version": EXECUTION_INTENT_VERSION,
        "action_id": _action_id(record.get("action_id")),
        "action_type": "CLEANING_ASSIGNMENT",
        "cleaning_page_id": _required_text(record.get("cleaning_page_id"), "Cleaning"),
        "cleaner_party_page_id": cleaner_party_page_id,
        "cleaner_telegram_user_id": _int_identity(
            record.get("candidate_user_id"), "Cleaner Telegram user"
        ),
        "cleaner_telegram_chat_id": _int_identity(
            record.get("candidate_chat_id"), "Cleaner Telegram chat"
        ),
        "telegram_message_id": _positive_int(
            record.get("telegram_message_id"), "Telegram message"
        ),
        "offered_at": offered_at,
        "expires_at": expires_at,
        "consumed_at": consumed_at,
        "proposal_round": _positive_int(
            record.get("proposal_round"), "proposal_round"
        ),
        "assignment_generation_identity": _generation_identity(record),
        "assignment_version": canonical_assignment_version(record),
        "expected_acceptance_status": _required_text(
            record.get("expected_acceptance_status", "수락대기"),
            "expected acceptance status",
        ),
        "expected_last_edited_time": expected_last_edited_time,
        "accepted_economics": _accepted_economics_payload(record),
        "semantic_requested_effect": {
            "decision": "approve",
            "cleaning_assignment": "ACCEPTED",
            "target_cleaning_status": "담당자 배정",
            "target_acceptance_status": "수락",
            "target_cleaner_party_page_id": cleaner_party_page_id,
            "payment_effects": 0,
        },
    }


def _pre_effect_operation_identity(payload: dict[str, Any]) -> str:
    message = EXECUTION_OPERATION_DOMAIN.encode() + b"\0" + _canonical_json_bytes(
        payload
    )
    return "CLEANING_ASSIGNMENT_EFFECT:v1:" + hashlib.sha256(message).hexdigest()


def create_assignment_execution_intent(
    record: dict[str, Any],
    decision: str,
    *,
    secret_path: Path,
) -> dict[str, Any]:
    """Authenticate the semantic operation identity before the external effect."""

    payload = _execution_intent_payload(record, decision)
    message = EXECUTION_INTENT_DOMAIN_SEPARATOR.encode() + b"\0" + _canonical_json_bytes(
        payload
    )
    return {
        "operation_identity": _pre_effect_operation_identity(payload),
        "intent": copy.deepcopy(payload),
        "signature": hmac.new(_read_key(secret_path), message, hashlib.sha256).hexdigest(),
    }


def verify_assignment_execution_intent(
    record: dict[str, Any],
    intent: dict[str, Any],
    decision: str,
    *,
    secret_path: Path,
) -> str:
    """Verify intent authentication and exact semantic equality with its action."""

    if not isinstance(intent, dict):
        raise AssignmentExecutionReceiptError("assignment execution intent missing")
    expected_payload = _execution_intent_payload(record, decision)
    actual_payload = intent.get("intent")
    if actual_payload != expected_payload:
        raise AssignmentExecutionReceiptError(
            "assignment execution intent/request identity mismatch"
        )
    expected_identity = _pre_effect_operation_identity(expected_payload)
    if intent.get("operation_identity") != expected_identity:
        raise AssignmentExecutionReceiptError(
            "assignment execution operation identity mismatch"
        )
    supplied = intent.get("signature")
    if not isinstance(supplied, str) or not supplied:
        raise AssignmentExecutionReceiptError(
            "assignment execution intent signature missing"
        )
    message = EXECUTION_INTENT_DOMAIN_SEPARATOR.encode() + b"\0" + _canonical_json_bytes(
        expected_payload
    )
    expected_signature = hmac.new(
        _read_key(secret_path), message, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, supplied):
        raise AssignmentExecutionReceiptError(
            "assignment execution intent signature invalid"
        )
    return expected_identity


def _provenance_paths(request_path: Path, action_id: str) -> tuple[Path, Path]:
    directory = Path(request_path).parent / PROVENANCE_DIRECTORY_NAME
    return (
        directory / f"{_action_id(action_id)}.intent.json",
        directory / f"{_action_id(action_id)}.success.json",
    )


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AssignmentExecutionReceiptError(f"{label} unavailable") from exc
    if not isinstance(value, dict):
        raise AssignmentExecutionReceiptError(f"{label} malformed")
    return value


def _publish_immutable_json(path: Path, value: dict[str, Any]) -> None:
    """Create one durable evidence file, accepting only an exact idempotent replay."""

    encoded = _canonical_json_bytes(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.allocation"
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = _read_json_object(path, "durable assignment provenance")
            if existing != value:
                raise AssignmentExecutionReceiptError(
                    "durable assignment provenance overwrite rejected"
                )
        else:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def persist_assignment_execution_intent(
    request_path: Path,
    record: dict[str, Any],
    decision: str,
    *,
    secret_path: Path,
) -> dict[str, Any]:
    """Freeze authenticated operation identity before calling the effect adapter."""

    intent = create_assignment_execution_intent(
        record, decision, secret_path=secret_path
    )
    intent_path, _ = _provenance_paths(request_path, record.get("action_id"))
    _publish_immutable_json(intent_path, intent)
    verify_assignment_execution_intent(
        record, _read_json_object(intent_path, "assignment execution intent"), decision,
        secret_path=secret_path,
    )
    return copy.deepcopy(intent)


def _authority_payload(
    record: dict[str, Any],
    execution_result: dict[str, Any],
    *,
    executed_at: str,
) -> dict[str, Any]:
    if record.get("schema_version") != 2:
        raise AssignmentExecutionReceiptError("assignment request schema unsupported")
    if record.get("action_type") != "CLEANING_ASSIGNMENT":
        raise AssignmentExecutionReceiptError(
            "receipt is only valid for CLEANING_ASSIGNMENT"
        )
    if record.get("test_mode") is not False:
        raise AssignmentExecutionReceiptError(
            "TEST assignment cannot produce a Production receipt"
        )
    if record.get("consumed") is not True or record.get("status") != "APPROVED_PRODUCTION":
        raise AssignmentExecutionReceiptError(
            "assignment callback was not durably approved before execution"
        )
    if (
        not isinstance(execution_result, dict)
        or execution_result.get("cleaning_assignment") != "ACCEPTED"
    ):
        raise AssignmentExecutionReceiptError("assignment execution was not ACCEPTED")

    offered_at = _aware_iso(record.get("created_at"), "offered_at")
    expires_at = _aware_iso(record.get("expires_at"), "expires_at")
    consumed_at = _aware_iso(record.get("consumed_at"), "consumed_at")
    executed_at = _aware_iso(executed_at, "executed_at")

    offered_dt = datetime.fromisoformat(offered_at.replace("Z", "+00:00"))
    expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    consumed_dt = datetime.fromisoformat(consumed_at.replace("Z", "+00:00"))
    executed_dt = datetime.fromisoformat(executed_at.replace("Z", "+00:00"))
    if expires_dt <= offered_dt or not (offered_dt <= consumed_dt <= expires_dt):
        raise AssignmentExecutionReceiptError(
            "callback timing is outside the Offer lifetime"
        )
    if executed_dt < consumed_dt:
        raise AssignmentExecutionReceiptError("executed_at precedes consumed_at")

    result_copy = copy.deepcopy(execution_result)
    _canonical_json_bytes({"execution_result": result_copy})
    return {
        "version": RECEIPT_VERSION,
        "action_id": _required_text(record.get("action_id"), "Action ID"),
        "action_type": "CLEANING_ASSIGNMENT",
        "cleaning_page_id": _required_text(record.get("cleaning_page_id"), "Cleaning"),
        "cleaner_party_page_id": _required_text(
            record.get("candidate_party_page_id"), "Cleaner Party"
        ),
        "cleaner_telegram_user_id": _int_identity(
            record.get("candidate_user_id"), "Cleaner Telegram user"
        ),
        "cleaner_telegram_chat_id": _int_identity(
            record.get("candidate_chat_id"), "Cleaner Telegram chat"
        ),
        "telegram_message_id": _positive_int(
            record.get("telegram_message_id"), "Telegram message"
        ),
        "offered_at": offered_at,
        "expires_at": expires_at,
        "proposal_round": _positive_int(
            record.get("proposal_round"), "proposal_round"
        ),
        "consumed_at": consumed_at,
        "executed_at": executed_at,
        "execution_result": result_copy,
        "accepted_economics": _accepted_economics_payload(record),
        "assignment_generation_identity": _generation_identity(record),
        "assignment_version": canonical_assignment_version(record),
    }


def create_successful_assignment_execution_receipt(
    record: dict[str, Any],
    execution_result: dict[str, Any],
    *,
    executed_at: str,
    secret_path: Path,
) -> dict[str, Any]:
    """Sign only a factual ACCEPTED result returned by assignment execution."""

    payload = _authority_payload(record, execution_result, executed_at=executed_at)
    receipt = copy.deepcopy(payload)
    receipt["signature"] = _receipt_mac(payload, _read_key(secret_path))
    return receipt


def verify_successful_assignment_execution_receipt(
    record: dict[str, Any],
    receipt: dict[str, Any],
    *,
    secret_path: Path,
) -> dict[str, Any]:
    """Verify factual receipt evidence without trusting authoritative projection state."""

    if not isinstance(record, dict) or record.get("status") not in {
        "APPROVED_PRODUCTION",
        "EXECUTED",
    }:
        raise AssignmentExecutionReceiptError(
            "assignment request is not approved or executed"
        )
    if not isinstance(receipt, dict):
        raise AssignmentExecutionReceiptError("assignment execution receipt missing")
    supplied = receipt.get("signature")
    if not isinstance(supplied, str) or not supplied:
        raise AssignmentExecutionReceiptError(
            "assignment execution receipt signature missing"
        )
    actual_payload = {key: value for key, value in receipt.items() if key != "signature"}
    execution_result = actual_payload.get("execution_result")
    executed_at = actual_payload.get("executed_at")
    source_record = copy.deepcopy(record)
    source_record["status"] = "APPROVED_PRODUCTION"
    expected_payload = _authority_payload(
        source_record, execution_result, executed_at=executed_at
    )
    if actual_payload != expected_payload:
        raise AssignmentExecutionReceiptError(
            "assignment execution receipt/request identity mismatch"
        )
    expected_mac = _receipt_mac(expected_payload, _read_key(secret_path))
    if not hmac.compare_digest(expected_mac, supplied):
        raise AssignmentExecutionReceiptError(
            "assignment execution receipt signature invalid"
        )
    return copy.deepcopy(expected_payload)


def persist_successful_assignment_execution_receipt(
    request_path: Path,
    record: dict[str, Any],
    execution_result: dict[str, Any],
    *,
    executed_at: str,
    secret_path: Path,
) -> dict[str, Any]:
    """Durably publish only factual ACCEPTED evidence after the external effect."""

    intent_path, receipt_path = _provenance_paths(
        request_path, record.get("action_id")
    )
    intent = _read_json_object(intent_path, "assignment execution intent")
    operation_identity = verify_assignment_execution_intent(
        record, intent, "approve", secret_path=secret_path
    )
    receipt = create_successful_assignment_execution_receipt(
        record,
        execution_result,
        executed_at=executed_at,
        secret_path=secret_path,
    )
    evidence = {
        "pre_effect_operation_identity": operation_identity,
        "assignment_execution_receipt": receipt,
    }
    _publish_immutable_json(receipt_path, evidence)
    persisted_evidence = _read_json_object(
        receipt_path, "durable assignment execution receipt"
    )
    if persisted_evidence.get("pre_effect_operation_identity") != operation_identity:
        raise AssignmentExecutionReceiptError(
            "factual receipt operation identity mismatch"
        )
    persisted = persisted_evidence.get("assignment_execution_receipt")
    verify_successful_assignment_execution_receipt(
        record, persisted, secret_path=secret_path
    )
    return copy.deepcopy(persisted)


def load_durable_successful_assignment_execution(
    request_path: Path,
    record: dict[str, Any],
    *,
    secret_path: Path,
) -> dict[str, Any]:
    """Load process-independent factual evidence for fresh-writer reconciliation."""

    intent_path, receipt_path = _provenance_paths(
        request_path, record.get("action_id")
    )
    intent = _read_json_object(intent_path, "assignment execution intent")
    operation_identity = verify_assignment_execution_intent(
        record, intent, "approve", secret_path=secret_path
    )
    evidence = _read_json_object(
        receipt_path, "durable assignment execution receipt"
    )
    if evidence.get("pre_effect_operation_identity") != operation_identity:
        raise AssignmentExecutionReceiptError(
            "durable factual provenance operation identity mismatch"
        )
    receipt = evidence.get("assignment_execution_receipt")
    payload = verify_successful_assignment_execution_receipt(
        record, receipt, secret_path=secret_path
    )
    return {
        "pre_effect_operation_identity": operation_identity,
        "assignment_execution_receipt": copy.deepcopy(receipt),
        "execution_result": copy.deepcopy(payload["execution_result"]),
        "executed_at": payload["executed_at"],
    }


def verify_assignment_execution_receipt(
    record: dict[str, Any],
    *,
    secret_path: Path,
) -> dict[str, Any]:
    """Verify HMAC plus exact identity equality with the persisted request."""

    if not isinstance(record, dict) or record.get("status") != "EXECUTED":
        raise AssignmentExecutionReceiptError("assignment request is not EXECUTED")
    execution_result = record.get("execution_result")
    executed_at = record.get("executed_at")
    receipt = record.get("assignment_execution_receipt")
    payload = verify_successful_assignment_execution_receipt(
        record, receipt, secret_path=secret_path
    )
    if execution_result != payload.get("execution_result"):
        raise AssignmentExecutionReceiptError(
            "assignment execution result is not receipt-bound"
        )
    if executed_at != payload.get("executed_at"):
        raise AssignmentExecutionReceiptError(
            "assignment executed_at is not receipt-bound"
        )
    return payload


def acceptance_operation_identity(
    record: dict[str, Any],
    *,
    secret_path: Path,
) -> str:
    """Return a retry-stable idempotency identity bound to semantic payload.

    Unlike an action-id-only key, this identity commits to the authenticated
    Cleaning, Cleaner, Offer timing, generation, and complete execution result.
    Any semantic drift produces a different digest or fails receipt verification.
    """

    payload = verify_assignment_execution_receipt(record, secret_path=secret_path)
    message = ACCEPTANCE_OPERATION_DOMAIN.encode() + b"\0" + _canonical_json_bytes(payload)
    return "CLEANING_ASSIGNMENT_ACCEPTED:v1:" + hashlib.sha256(message).hexdigest()

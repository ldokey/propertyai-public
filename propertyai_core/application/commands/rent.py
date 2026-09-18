"""Frozen P1 Rent command envelopes and normalized idempotency identity."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
from typing import Any
from uuid import UUID

from propertyai_core.application.rent_errors import rent_error
from propertyai_core.domain.finance import money, signed_money, FinanceError

COMMAND_TYPES = {
    "createResident": "CREATE_RESIDENT", "createContract": "CREATE_CONTRACT",
    "reviseContract": "REVISE_CONTRACT",
    "createAccount": "CREATE_ACCOUNT", "reviseAccount": "REVISE_ACCOUNT",
    "issueRent": "ISSUE_RENT",
    "recordReceipt": "RECORD_RECEIPT", "allocate": "ALLOCATE",
    "confirmOccupancyStart": "CONFIRM_OCCUPANCY_START",
    "confirmOccupancyEnd": "CONFIRM_OCCUPANCY_END",
    "correctOccupancyDates": "CORRECT_OCCUPANCY_DATES",
    "moveOccupancy": "MOVE_OCCUPANCY",
    "adjustReceivable": "ADJUST_RECEIVABLE", "voidReceivable": "VOID_RECEIVABLE",
    "correctAllocations": "CORRECT_ALLOCATION", "correctMovement": "CORRECT_MOVEMENT",
    "recordRefund": "RECORD_REFUND", "correctRefund": "CORRECT_REFUND",
}
REQUIRED = {
    "createResident": {"display_name","linked_party_id","private_contact_ref","review_status","expected_ledger_revision"},
    "createContract": {"rental_unit_id","starts_on","ends_on_exclusive","lifecycle","readiness","resident_ids","contract_parties","term","billing_periods","previous_contract_id","reason","expected_ledger_revision","occupancies"},
    "reviseContract": {"expected_version","effective_on","starts_on","ends_on_exclusive","lifecycle","readiness","term","reason","expected_ledger_revision"},
    "createAccount": {"display_name","currency","owner_party_id","masked_identifier","protected_identifier_ref","expected_ledger_revision"},
    "reviseAccount": {"expected_version","display_name","masked_identifier","protected_identifier_ref","active","reason","expected_ledger_revision"},
    "issueRent": {"contract_id","period_id","expected_contract_version","expected_term_versions","expected_ledger_revision","calculation_sha256","issuance_mode","replaces_receivable_id"},
    "recordReceipt": {"account_id","occurred_on","amount","currency","payer_raw","counterparty_party_id","attribution","allocations","expected_ledger_revision"},
    "allocate": {"source_id","expected_source_version","expected_ledger_revision","allocations","attribution"},
    "confirmOccupancyStart": {"expected_version","expected_contract_version","expected_ledger_revision","evidence","actual_start"},
    "confirmOccupancyEnd": {"expected_version","expected_contract_version","expected_ledger_revision","evidence","actual_end_exclusive"},
    "correctOccupancyDates": {"expected_version","expected_contract_version","expected_ledger_revision","evidence","actual_start","actual_end_exclusive"},
    "moveOccupancy": {"expected_version","expected_contract_version","expected_ledger_revision","evidence","target_contract_id","expected_target_contract_version","effective_move_on"},
}
REQUIRED.update({
    "adjustReceivable": {"delta","reason","expected_version","expected_ledger_revision","allocation_correction","reverse_adjustment_id"},
    "voidReceivable": {"expected_version","expected_ledger_revision","allocation_correction","reason"},
    "correctAllocations": {"plan","reason","expected_ledger_revision"},
    "correctMovement": {"action","corrected_values","expected_version","expected_ledger_revision","expected_source_version","allocation_correction","reason","replacement_receipt"},
    "recordRefund": {"source_id","expected_source_version","expected_ledger_revision","out_account_id","occurred_on","amount","payee_raw","actual_transfer_confirmed","reason"},
    "correctRefund": {"outgoing_movement_id","expected_movement_version","source_versions","reverse_return_ids","replacement_returns","corrected_values","action","actual_record_correction_confirmed","reason","expected_ledger_revision"},
})
CORRECTION_OPERATIONS = frozenset({"adjustReceivable", "voidReceivable", "correctAllocations",
                                   "correctMovement", "recordRefund", "correctRefund"})
_CORRECTION_TARGETS = CORRECTION_OPERATIONS - {"correctAllocations", "recordRefund"}
_MONEY_KEYS = {"amount", "monthly_rent"}
_UUID_KEYS = {"id", "resident_id", "contract_id", "period_id", "account_id", "source_id", "receivable_id", "rental_unit_id", "property_id", "party_id", "linked_party_id", "owner_party_id", "counterparty_party_id", "target_contract_id", "previous_contract_id", "replaces_receivable_id"}

_UUID_KEYS.update({"out_account_id", "outgoing_movement_id", "reverse_adjustment_id",
                   "reverse_return_ids", "reverse_allocation_ids"})


def version(value: Any, *, positive: bool = False) -> int:
    try: result = money(value)
    except FinanceError: raise rent_error('VALIDATION_ERROR') from None
    if positive and result == 0:
        raise rent_error("VALIDATION_ERROR")
    return result


def uuid(value: Any) -> UUID:
    if not isinstance(value, str):
        raise rent_error("VALIDATION_ERROR")
    try:
        return UUID(value)
    except ValueError:
        raise rent_error("VALIDATION_ERROR") from None


def day(value: Any) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise rent_error("VALIDATION_ERROR")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise rent_error("VALIDATION_ERROR") from None
    if result.isoformat() != value:
        raise rent_error("VALIDATION_ERROR")
    return result


def _canonical(value: Any, key: str | None = None) -> Any:
    if isinstance(value, float):
        raise rent_error("VALIDATION_ERROR")
    if isinstance(value, dict):
        return {k: _canonical(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(v, key) for v in value]
    if isinstance(value, str) and key in _UUID_KEYS:
        return str(uuid(value))
    if isinstance(value, str) and key == "resident_ids":
        return str(uuid(value))
    if isinstance(value, str) and key == "delta":
        try: return str(signed_money(value))
        except FinanceError: raise rent_error("VALIDATION_ERROR") from None
    if isinstance(value, str) and key in _MONEY_KEYS:
        try: return str(money(value, positive=key == 'amount'))
        except FinanceError: raise rent_error('VALIDATION_ERROR') from None
    return value


def strict_object(value: Any, required: set[str], allowed: set[str] | None = None) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or (allowed is not None and not value.keys() <= allowed):
        raise rent_error("VALIDATION_ERROR")
    return value


@dataclass(frozen=True)
class RentCommand:
    operation_id: str
    body: dict
    idempotency_key: UUID
    target_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.operation_id not in COMMAND_TYPES or not isinstance(self.idempotency_key, UUID):
            raise rent_error("VALIDATION_ERROR")
        strict_object(self.body, REQUIRED[self.operation_id], REQUIRED[self.operation_id])
        if self.operation_id in _CORRECTION_TARGETS and not isinstance(self.target_id, UUID):
            raise rent_error("VALIDATION_ERROR")
        if self.operation_id in CORRECTION_OPERATIONS - _CORRECTION_TARGETS and self.target_id is not None:
            raise rent_error("VALIDATION_ERROR")
        if self.operation_id in {"reviseContract", "reviseAccount"} and not isinstance(self.target_id, UUID):
            raise rent_error("VALIDATION_ERROR")
        if self.operation_id.startswith(("confirmOccupancy", "correctOccupancy", "moveOccupancy")) and self.target_id is None:
            raise rent_error("VALIDATION_ERROR")

    @property
    def command_type(self) -> str:
        return COMMAND_TYPES[self.operation_id]

    @property
    def expected_ledger_revision(self) -> int:
        return version(self.body["expected_ledger_revision"])

    @property
    def normalized_hash(self) -> str:
        payload = {
            "api_schema_version": 1,
            "command_type": self.command_type,
            "route_target_id": str(self.target_id) if self.target_id else None,
            "body": _canonical(self.body),
        }
        return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

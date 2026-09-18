"""Frozen P1 Longstay/Finance Application handlers."""
from __future__ import annotations

from datetime import date
from hashlib import sha256
import json
from typing import Any, Callable
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from propertyai_core.application.commands.rent import RentCommand, day, strict_object, uuid, version
from propertyai_core.application.rent_errors import rent_error
from propertyai_core.domain.finance import (
    ConfirmedTerm, FinanceError, IssueRentEligibilityV1, calculate_rent,
    due_on, generation_on, money, money_text, POLICY_VERSION,
    signed_money, adjusted_obligation, require_finance_balances,
)
from propertyai_core.domain.longstay import ActualOccupancy, LongstayError, confirm_start, confirm_end, correct_dates
from propertyai_core.ports.rent_repository import RentRepositoryPort, RentTransactionPort

_TERM_FIELDS = {"valid_from","valid_to_exclusive","monthly_rent","amount_confirmed","due_day","cycle_confirmed","cycle_rule","policy_version"}
_TERM_CHANGE_FIELDS = {"monthly_rent","amount_confirmed","due_day","cycle_confirmed","cycle_rule","policy_version"}
_CONTRACT_LIFECYCLES = frozenset({"DRAFT","REVIEW_REQUIRED","SIGNED","ACTIVE","RENEWAL_REVIEW","ENDED","TERMINATED","CANCELLED"})
_CONTRACT_READINESS = frozenset({"NEEDS_REVIEW","READY"})
_PERIOD_FIELDS = {"cycle_start","cycle_end_exclusive","due_month","confirmation_ref"}
_OCC_INPUT_FIELDS = {"resident_id","actual_start","actual_end_exclusive","review_status"}
_ATTR_FIELDS = {"status","property_id","contract_id"}
_ALLOC_FIELDS = {"receivable_id","amount","expected_version","attribution_confirmed","override_attribution_reason"}
_EVIDENCE_FIELDS = {"confirmation_ref","reason","operator_confirmed"}


def _nonnull(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise rent_error("VALIDATION_ERROR")
    return value


def _optional_uuid(value: Any) -> UUID | None:
    return None if value is None else uuid(value)


def _optional_day(value: Any) -> date | None:
    return None if value is None else day(value)


def _row_required(row: Any) -> Any:
    if row is None:
        raise rent_error("NOT_FOUND")
    return row


def _snapshot(row: dict | Any) -> dict:
    return {
        "occupancy_id": str(row["occupancy_id"]), "contract_id": str(row["contract_id"]),
        "resident_id": str(row["resident_id"]), "property_id": str(row["property_id"]),
        "rental_unit_id": str(row["rental_unit_id"]),
        "actual_start": row["actual_start"].isoformat() if row["actual_start"] else None,
        "actual_end_exclusive": row["actual_end_exclusive"].isoformat() if row["actual_end_exclusive"] else None,
        "review_status": row["review_status"], "version": str(row["version"]),
    }


def _entity(tx: RentTransactionPort, kind: str, table: str, identity: UUID) -> dict:
    row = _row_required(tx.fetch(table, identity))
    return {"kind": kind, "id": str(identity), "version": str(row["version"]) if "version" in row else None}


def _result(tx: RentTransactionPort, outcome: str, entities: list[dict],
            receivables: list[UUID] = (), sources: list[UUID] = (), **extra: Any) -> dict:
    rb, sb = [], []
    for identity in dict.fromkeys(receivables):
        row = _row_required(tx.balance("RECEIVABLE", identity))
        rb.append({
            "receivable_id": str(identity), "effective_amount": money_text(int(row["effective_amount"])),
            "allocated": money_text(int(row["allocated"])), "balance": money_text(int(row["balance"])),
            "version": str(row["version"]),
        })
    for identity in dict.fromkeys(sources):
        row = _row_required(tx.balance("SOURCE", identity))
        sb.append({
            "source_id": str(identity), "principal": money_text(int(row["principal"])),
            "allocated": money_text(int(row["allocated"])), "returned": money_text(int(row["returned"])),
            "available": money_text(int(row["available"])), "version": str(row["version"]),
        })
    return {"outcome": outcome, "entities": entities, "receivable_balances": rb,
            "source_balances": sb, **extra}


class BusinessDateProvider:
    """Application-owned property-local date; fixtures may inject a deterministic TEST provider."""

    def __init__(self, current_property_date: Callable[[str], date]):
        self._current = current_property_date

    def today(self, timezone_name: str) -> date:
        value = self._current(timezone_name)
        if not isinstance(value, date):
            raise rent_error("INTERNAL_ERROR")
        return value


class RentService:
    def __init__(self, repository: RentRepositoryPort, business_date: BusinessDateProvider, *, scheduler_entry_enabled: bool = False):
        self.repository, self.business_date = repository, business_date
        self.scheduler_entry_enabled = scheduler_entry_enabled

    def handle(self, command: RentCommand, *, _scheduler_entry: bool = False) -> tuple[dict, bool]:
        if _scheduler_entry and not self.scheduler_entry_enabled:
            raise rent_error('NOT_AUTHORIZED')
        if command.operation_id == 'issueRent' and command.body['issuance_mode'] != ('SCHEDULER_TEST' if _scheduler_entry else 'OPERATOR_CONFIRMED'):
            raise rent_error('VALIDATION_ERROR')
        if _scheduler_entry and command.body['replaces_receivable_id'] is not None:
            raise rent_error('NOT_AUTHORIZED')
        planners = {
            "createResident": self._create_resident,
            "createContract": self._create_contract,
            "reviseContract": self._revise_contract,
            "createAccount": self._create_account,
            "reviseAccount": self._revise_account,
            "issueRent": self._issue_rent,
            "recordReceipt": self._record_receipt,
            "allocate": self._allocate,
            "adjustReceivable": self._adjust_receivable,
            "voidReceivable": self._void_receivable,
            "correctAllocations": self._correct_allocations,
            "correctMovement": self._correct_movement,
            "recordRefund": self._record_refund,
            "correctRefund": self._correct_refund,
            "confirmOccupancyStart": self._occupancy_command,
            "confirmOccupancyEnd": self._occupancy_command,
            "correctOccupancyDates": self._occupancy_command,
            "moveOccupancy": self._occupancy_command,
        }
        planner = planners[command.operation_id]
        # Replay is checked before the property clock; capture once inside the first business attempt.
        captured: date | None = None
        def plan(tx: RentTransactionPort, command_id: UUID) -> dict:
            nonlocal captured
            try:
                if command.operation_id == 'issueRent' and captured is None:
                    captured = self._issue_business_date(command, tx)
                return planner(tx, command_id, command, captured)
            except (FinanceError, LongstayError) as exc:
                raise rent_error(exc.code) from None
        return self.repository.run_command(
            command_type=command.command_type, idempotency_key=command.idempotency_key,
            normalized_hash=command.normalized_hash, expected_ledger_revision=command.expected_ledger_revision,
            plan=plan,
        )

    def issue_from_scheduler(self, contract_id: UUID, period_id: UUID, key: UUID) -> tuple[dict, bool]:
        if not self.scheduler_entry_enabled:
            raise rent_error('NOT_AUTHORIZED')
        preview=self.preview_charge({'contract_id':str(contract_id),'period_id':str(period_id)})
        if preview['status'] not in {'READY','ALREADY_ISSUED'}:
            raise rent_error('CONTRACT_NOT_READY')
        body={'contract_id':str(contract_id),'period_id':str(period_id),
              'expected_contract_version':preview['contract_version'],
              'expected_term_versions':preview['term_versions'],
              'expected_ledger_revision':preview['ledger_revision'],
              'calculation_sha256':preview['calculation_sha256'],
              'issuance_mode':'SCHEDULER_TEST','replaces_receivable_id':None}
        return self.handle(RentCommand('issueRent',body,key),_scheduler_entry=True)

    def _issue_business_date(self, command: RentCommand, tx: RentTransactionPort) -> date:
        contract_id = uuid(command.body['contract_id'])
        rows = tx.rows(
            "SELECT u.timezone_name FROM propertyai.v_rent_contracts c JOIN propertyai.v_rent_reference_units u USING(property_id,rental_unit_id) WHERE c.contract_id=%s",
            (contract_id,),
        )
        return self.business_date.today(_row_required(rows[0] if rows else None)["timezone_name"])

    def _create_resident(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        body = command.body
        identity = uuid4()
        tx.insert("rent_resident", {
            "resident_id": identity, "organization_id": tx.organization_id,
            "created_command_id": command_id, "last_command_id": command_id,
            "display_name": _nonnull(body["display_name"]), "linked_party_id": _optional_uuid(body["linked_party_id"]),
            "private_contact_ref": body["private_contact_ref"], "review_status": body["review_status"],
        })
        return _result(tx, "CREATED", [_entity(tx,"RESIDENT","rent_resident",identity)])

    def _create_account(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        body = command.body
        if body["currency"] != "KRW":
            raise rent_error("VALIDATION_ERROR")
        identity = uuid4()
        tx.insert("finance_account", {
            "account_id": identity, "organization_id": tx.organization_id,
            "created_command_id": command_id, "last_command_id": command_id,
            "display_name": _nonnull(body["display_name"]), "currency": "KRW",
            "owner_party_id": _optional_uuid(body["owner_party_id"]),
            "masked_identifier": body["masked_identifier"],
            "protected_identifier_ref": body["protected_identifier_ref"],
        })
        return _result(tx, "CREATED", [_entity(tx,"ACCOUNT","finance_account",identity)])

    def _revise_account(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        body, identity = command.body, command.target_id
        reason = _nonnull(body["reason"])
        if len(reason) > 500 or type(body["active"]) is not bool:
            raise rent_error("VALIDATION_ERROR")
        if not isinstance(body["masked_identifier"], str) or not isinstance(body["protected_identifier_ref"], (str, type(None))):
            raise rent_error("VALIDATION_ERROR")
        protected_identifier_ref = body["protected_identifier_ref"]
        if protected_identifier_ref is not None:
            protected_identifier_ref = _nonnull(protected_identifier_ref)
        tx.lock_entities([("finance_account", identity)])
        current = _row_required(tx.fetch("finance_account", identity))
        expected = version(body["expected_version"], positive=True)
        if current["version"] != expected:
            raise rent_error("VERSION_CONFLICT")
        tx.update_versioned("finance_account", identity, expected, {
            "display_name": _nonnull(body["display_name"]),
            "masked_identifier": body["masked_identifier"],
            # The protected reference is intentionally absent from read models/UI.
            # Null therefore means "preserve", preventing a label/deactivation edit
            # from silently clearing an existing protected binding. A non-null value
            # is an explicit replacement supplied through the authorized admin path.
            "protected_identifier_ref": current["protected_identifier_ref"] if protected_identifier_ref is None else protected_identifier_ref,
            "active": body["active"],
        }, command_id)
        return _result(tx, "UPDATED", [_entity(tx,"ACCOUNT","finance_account",identity)],
                       change_reason=reason)

    @staticmethod
    def _revision_term_values(value: Any) -> dict:
        term = strict_object(value, _TERM_CHANGE_FIELDS, _TERM_CHANGE_FIELDS)
        if term["policy_version"] != POLICY_VERSION:
            raise rent_error("VALIDATION_ERROR")
        if type(term["amount_confirmed"]) is not bool or type(term["cycle_confirmed"]) is not bool:
            raise rent_error("VALIDATION_ERROR")
        monthly = money(term["monthly_rent"]) if term["monthly_rent"] is not None else None
        if term["amount_confirmed"] and monthly is None:
            raise rent_error("CONTRACT_NOT_READY")
        due_day = term["due_day"]
        if due_day is not None and (type(due_day) is not int or not 1 <= due_day <= 31):
            raise rent_error("VALIDATION_ERROR")
        cycle_rule = term["cycle_rule"]
        if cycle_rule is not None:
            strict_object(cycle_rule, {"schema_version","mode","first_due_month","confirmation_ref"},
                          {"schema_version","mode","first_due_month","confirmation_ref","due_month_offset"})
            if cycle_rule["schema_version"] != 1 or cycle_rule["mode"] not in {"CALENDAR_MONTH","EXPLICIT_PERIODS"}:
                raise rent_error("VALIDATION_ERROR")
            if (cycle_rule["mode"] == "CALENDAR_MONTH") != ("due_month_offset" in cycle_rule):
                raise rent_error("VALIDATION_ERROR")
        if term["cycle_confirmed"] and (cycle_rule is None or due_day is None):
            raise rent_error("CONTRACT_NOT_READY")
        return {
            "monthly_rent": monthly, "amount_confirmed": term["amount_confirmed"],
            "due_day": due_day, "cycle_rule": cycle_rule,
            "cycle_confirmed": term["cycle_confirmed"], "policy_version": POLICY_VERSION,
        }

    @staticmethod
    def _insert_term_revision(tx: RentTransactionPort, command_id: UUID, contract_id: UUID,
                              revision_no: int, valid_from: date, valid_to: date | None,
                              values: dict) -> UUID:
        identity = uuid4()
        tx.insert("rent_term_revision", {
            "term_id": identity, "organization_id": tx.organization_id,
            "created_command_id": command_id, "last_command_id": command_id,
            "contract_id": contract_id, "revision_no": revision_no,
            "valid_from": valid_from, "valid_to_exclusive": valid_to,
            "monthly_rent": values["monthly_rent"], "amount_confirmed": values["amount_confirmed"],
            "due_day": values["due_day"],
            "cycle_rule": Jsonb(values["cycle_rule"]) if values["cycle_rule"] is not None else None,
            "cycle_confirmed": values["cycle_confirmed"], "policy_version": values["policy_version"],
        })
        return identity

    def _revise_contract(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        body, identity = command.body, command.target_id
        reason = _nonnull(body["reason"])
        if (len(reason) > 500 or body["lifecycle"] not in _CONTRACT_LIFECYCLES
                or body["readiness"] not in _CONTRACT_READINESS):
            raise rent_error("VALIDATION_ERROR")
        tx.lock_entities([("rent_contract", identity)])
        current = _row_required(tx.fetch("rent_contract", identity))
        expected = version(body["expected_version"], positive=True)
        if current["version"] != expected:
            raise rent_error("VERSION_CONFLICT")
        starts_on, ends_on = _optional_day(body["starts_on"]), _optional_day(body["ends_on_exclusive"])
        if starts_on and ends_on and ends_on <= starts_on:
            raise rent_error("VALIDATION_ERROR")
        term_input, effective_on = body["term"], _optional_day(body["effective_on"])
        if (term_input is None) != (effective_on is None):
            raise rent_error("VALIDATION_ERROR")
        next_term_id = current["current_term_id"]
        entities: list[dict] = []
        if term_input is not None:
            if next_term_id is None:
                raise rent_error("CONTRACT_NOT_READY")
            tx.lock_entities([("rent_term_revision", next_term_id)])
            old_term = _row_required(tx.fetch("rent_term_revision", next_term_id))
            if old_term["superseded_by"] is not None or effective_on < old_term["valid_from"] or (
                old_term["valid_to_exclusive"] is not None and effective_on >= old_term["valid_to_exclusive"]
            ):
                raise rent_error("VALIDATION_ERROR")
            new_values = self._revision_term_values(term_input)
            old_values = {name: old_term[name] for name in (
                "monthly_rent","amount_confirmed","due_day","cycle_rule","cycle_confirmed","policy_version"
            )}
            maximum = tx.rows(
                "SELECT coalesce(max(revision_no),0) AS revision_no FROM propertyai.rent_term_revision WHERE organization_id=%s AND contract_id=%s",
                (tx.organization_id, identity),
            )[0]["revision_no"]
            revision_no = int(maximum) + 1
            if effective_on > old_term["valid_from"]:
                prefix_id = self._insert_term_revision(
                    tx, command_id, identity, revision_no, old_term["valid_from"], effective_on, old_values,
                )
                revision_no += 1
                entities.append(_entity(tx,"TERM","rent_term_revision",prefix_id))
            replacement_id = self._insert_term_revision(
                tx, command_id, identity, revision_no, effective_on, old_term["valid_to_exclusive"], new_values,
            )
            tx.update_versioned("rent_term_revision", next_term_id, old_term["version"], {
                "superseded_by": replacement_id, "supersession_reason": reason,
            }, command_id)
            entities.extend([
                _entity(tx,"TERM","rent_term_revision",next_term_id),
                _entity(tx,"TERM","rent_term_revision",replacement_id),
            ])
            next_term_id = replacement_id
        if body["readiness"] == "READY" and (starts_on is None or ends_on is None or next_term_id is None):
            raise rent_error("CONTRACT_NOT_READY")
        if body["readiness"] == "READY":
            ready_term = _row_required(tx.fetch("rent_term_revision", next_term_id))
            if (not ready_term["amount_confirmed"] or not ready_term["cycle_confirmed"]
                    or ready_term["superseded_by"] is not None):
                raise rent_error("CONTRACT_NOT_READY")
        tx.update_versioned("rent_contract", identity, expected, {
            "starts_on": starts_on, "ends_on_exclusive": ends_on,
            "lifecycle": body["lifecycle"], "readiness": body["readiness"],
            "current_term_id": next_term_id, "change_reason": reason,
        }, command_id)
        entities.insert(0, _entity(tx,"CONTRACT","rent_contract",identity))
        return _result(tx, "UPDATED", entities, change_reason=reason,
                       term_effective_on=effective_on.isoformat() if effective_on else None)

    def _create_contract(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        body = command.body
        unit_id = uuid(body["rental_unit_id"])
        units = tx.rows("SELECT property_id,property_active,unit_active FROM propertyai.v_rent_reference_units WHERE rental_unit_id=%s", (unit_id,))
        unit = _row_required(units[0] if units else None)
        if not unit["property_active"] or not unit["unit_active"]:
            raise rent_error("CONTRACT_NOT_READY")
        property_id = unit["property_id"]
        previous = _optional_uuid(body["previous_contract_id"])
        if previous:
            _row_required(tx.fetch("rent_contract", previous))
        resident_ids = [uuid(v) for v in body["resident_ids"]]
        if len(set(resident_ids)) != len(resident_ids):
            raise rent_error("VALIDATION_ERROR")
        for identity in resident_ids:
            _row_required(tx.fetch("rent_resident", identity))
        start, end = _optional_day(body["starts_on"]), _optional_day(body["ends_on_exclusive"])
        if start and end and end <= start:
            raise rent_error("VALIDATION_ERROR")
        term_data = body["term"]
        term_id = uuid4() if term_data is not None else None
        if body["readiness"] == "READY" and (start is None or end is None or term_id is None or not body["billing_periods"]):
            raise rent_error("CONTRACT_NOT_READY")
        if term_data is not None:
            strict_object(term_data, _TERM_FIELDS, _TERM_FIELDS)
            if term_data["policy_version"] != POLICY_VERSION:
                raise rent_error("VALIDATION_ERROR")
            if term_data["amount_confirmed"] and term_data["monthly_rent"] is None:
                raise rent_error("CONTRACT_NOT_READY")
            if term_data["cycle_confirmed"] and (term_data["cycle_rule"] is None or term_data["due_day"] is None):
                raise rent_error("CONTRACT_NOT_READY")
        contract_id = uuid4()
        tx.insert("rent_contract", {
            "contract_id": contract_id, "organization_id": tx.organization_id,
            "created_command_id": command_id, "last_command_id": command_id,
            "property_id": property_id, "rental_unit_id": unit_id,
            "starts_on": start, "ends_on_exclusive": end,
            "lifecycle": body["lifecycle"], "readiness": body["readiness"],
            "current_term_id": term_id, "previous_contract_id": previous,
            "change_reason": _nonnull(body["reason"]),
        })
        entities = [_entity(tx,"CONTRACT","rent_contract",contract_id)]
        for identity in resident_ids:
            tx.insert("rent_contract_resident", {
                "contract_resident_id": uuid4(), "organization_id": tx.organization_id,
                "created_command_id": command_id, "contract_id": contract_id, "resident_id": identity,
            })
        for item in body["contract_parties"]:
            strict_object(item, {"party_id","party_role"}, {"party_id","party_role"})
            tx.insert("rent_contract_party", {
                "contract_party_id": uuid4(), "organization_id": tx.organization_id,
                "created_command_id": command_id, "contract_id": contract_id,
                "party_id": uuid(item["party_id"]), "party_role": item["party_role"],
            })
        if term_id:
            cycle_rule = term_data["cycle_rule"]
            if cycle_rule is not None:
                strict_object(cycle_rule, {"schema_version","mode","first_due_month","confirmation_ref"},
                              {"schema_version","mode","first_due_month","confirmation_ref","due_month_offset"})
                if cycle_rule["schema_version"] != 1 or cycle_rule["mode"] not in {"CALENDAR_MONTH","EXPLICIT_PERIODS"}:
                    raise rent_error("VALIDATION_ERROR")
                if (cycle_rule["mode"] == "CALENDAR_MONTH") != ("due_month_offset" in cycle_rule):
                    raise rent_error("VALIDATION_ERROR")
            due_day = term_data["due_day"]
            if due_day is not None and (type(due_day) is not int or not 1 <= due_day <= 31):
                raise rent_error("VALIDATION_ERROR")
            tx.insert("rent_term_revision", {
                "term_id": term_id, "organization_id": tx.organization_id,
                "created_command_id": command_id, "last_command_id": command_id,
                "contract_id": contract_id, "revision_no": 1,
                "valid_from": day(term_data["valid_from"]),
                "valid_to_exclusive": _optional_day(term_data["valid_to_exclusive"]),
                "monthly_rent": money(term_data["monthly_rent"]) if term_data["monthly_rent"] is not None else None,
                "amount_confirmed": term_data["amount_confirmed"], "due_day": due_day,
                "cycle_rule": Jsonb(cycle_rule) if cycle_rule is not None else None,
                "cycle_confirmed": term_data["cycle_confirmed"], "policy_version": POLICY_VERSION,
            })
            entities.append(_entity(tx,"TERM","rent_term_revision",term_id))
        for period in body["billing_periods"]:
            strict_object(period, _PERIOD_FIELDS, _PERIOD_FIELDS)
            if term_data is None or term_data["due_day"] is None:
                raise rent_error("CONTRACT_NOT_READY")
            cycle_start, cycle_end = day(period["cycle_start"]), day(period["cycle_end_exclusive"])
            due_month = day(period["due_month"] + "-01")
            if cycle_end <= cycle_start:
                raise rent_error("VALIDATION_ERROR")
            period_id = uuid4()
            tx.insert("rent_billing_period", {
                "period_id": period_id, "organization_id": tx.organization_id,
                "created_command_id": command_id, "contract_id": contract_id,
                "cycle_start": cycle_start, "cycle_end_exclusive": cycle_end,
                "due_month": due_month, "due_on": due_on(due_month, term_data["due_day"]),
                "confirmation_ref": _nonnull(period["confirmation_ref"]),
            })
            entities.append(_entity(tx,"PERIOD","rent_billing_period",period_id))
        for item in body["occupancies"]:
            strict_object(item, _OCC_INPUT_FIELDS, _OCC_INPUT_FIELDS)
            identity = uuid(item["resident_id"])
            if identity not in resident_ids:
                raise rent_error("VALIDATION_ERROR")
            occ_start, occ_end = _optional_day(item["actual_start"]), _optional_day(item["actual_end_exclusive"])
            if item["review_status"] == "VERIFIED" and occ_start is None:
                raise rent_error("VALIDATION_ERROR")
            if occ_start and occ_end and occ_end <= occ_start:
                raise rent_error("VALIDATION_ERROR")
            occ_id = uuid4()
            tx.insert("rent_occupancy", {
                "occupancy_id": occ_id, "organization_id": tx.organization_id,
                "created_command_id": command_id, "last_command_id": command_id,
                "contract_id": contract_id, "resident_id": identity,
                "property_id": property_id, "rental_unit_id": unit_id,
                "actual_start": occ_start, "actual_end_exclusive": occ_end,
                "review_status": item["review_status"],
            })
            entities.append(_entity(tx,"OCCUPANCY","rent_occupancy",occ_id))
        entities[0] = _entity(tx,"CONTRACT","rent_contract",contract_id)
        return _result(tx, "CREATED", entities)

    def _charge(self, tx: RentTransactionPort, contract_id: UUID, period_id: UUID) -> tuple[dict, dict, Any]:
        contract_rows=tx.rows('SELECT * FROM propertyai.v_rent_contracts WHERE contract_id=%s',(contract_id,))
        period_rows=tx.rows('SELECT * FROM propertyai.v_rent_periods WHERE period_id=%s',(period_id,))
        contract=_row_required(contract_rows[0] if contract_rows else None)
        period=_row_required(period_rows[0] if period_rows else None)
        if period["contract_id"] != contract_id:
            raise rent_error("NOT_FOUND")
        if contract["readiness"] != "READY" or contract["starts_on"] is None or contract["ends_on_exclusive"] is None:
            raise rent_error("CONTRACT_NOT_READY")
        term_rows = tx.rows(
            "SELECT * FROM propertyai.v_rent_terms WHERE organization_id=%s AND contract_id=%s AND superseded_by IS NULL ORDER BY valid_from,term_id",
            (tx.organization_id, contract_id),
        )
        terms = [
            ConfirmedTerm(r["term_id"], r["version"], r["valid_from"], r["valid_to_exclusive"],
                          r["monthly_rent"] or 0, r["amount_confirmed"], r["due_day"] or 0, r["cycle_confirmed"])
            for r in term_rows
        ]
        calc = calculate_rent(
            contract_id=contract_id, contract_version=contract["version"],
            contract_start=contract["starts_on"], contract_end_exclusive=contract["ends_on_exclusive"],
            period_id=period_id, period_start=period["cycle_start"], period_end_exclusive=period["cycle_end_exclusive"],
            due_date=period["due_on"], terms=terms,
        )
        return contract, period, calc

    def _issue_rent(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, issue_date: date | None) -> dict:
        body = command.body
        contract_id, period_id = uuid(body["contract_id"]), uuid(body["period_id"])
        if not self.scheduler_entry_enabled:
            tx.lock_entities([('rent_contract',contract_id)])
        contract, period, calc = self._charge(tx, contract_id, period_id)
        if issue_date is None:
            raise rent_error("INTERNAL_ERROR")
        eligibility = IssueRentEligibilityV1(issue_date, period["due_on"]).evaluate()
        if contract["version"] != version(body["expected_contract_version"], positive=True) or body["calculation_sha256"] != calc.fingerprint:
            raise rent_error("VERSION_CONFLICT")
        actual_terms = {str(t["term_id"]): str(t["version"]) for t in tx.rows(
            "SELECT term_id,version FROM propertyai.v_rent_terms WHERE organization_id=%s AND contract_id=%s AND superseded_by IS NULL",
            (tx.organization_id, contract_id),
        )}
        expected_terms = {str(uuid(item["id"])): str(version(item["expected_version"],positive=True)) for item in body["expected_term_versions"]}
        if actual_terms != expected_terms:
            raise rent_error("VERSION_CONFLICT")
        existing = tx.rows(
            "SELECT receivable_id,calculation_sha256,voided FROM propertyai.finance_receivable WHERE organization_id=%s AND period_id=%s ORDER BY created_at",
            (tx.organization_id,period_id),
        )
        predecessor = _optional_uuid(body["replaces_receivable_id"])
        if existing and not existing[-1]["voided"]:
            if predecessor is not None:
                raise rent_error("CHARGE_CORRECTION_REQUIRED")
            if existing[-1]["calculation_sha256"] != calc.fingerprint:
                raise rent_error("CHARGE_CORRECTION_REQUIRED")
            identity = existing[-1]["receivable_id"]
            return _result(tx,"EXISTING",[],[identity],issue_eligibility=eligibility)
        if existing and (predecessor is None or predecessor != existing[-1]["receivable_id"]):
            raise rent_error("CHARGE_CORRECTION_REQUIRED")
        if calc.amount == 0:
            return _result(tx,"NO_CHARGE",[],issue_eligibility=eligibility)
        identity = uuid4()
        tx.insert("finance_receivable", {
            "receivable_id": identity, "organization_id": tx.organization_id,
            "created_command_id": command_id, "last_command_id": command_id,
            "contract_id": contract_id, "property_id": contract["property_id"], "period_id": period_id,
            "origin_kind": "RENT", "currency": "KRW", "original_amount": calc.amount,
            "calculation_snapshot": Jsonb(calc.snapshot), "calculation_sha256": calc.fingerprint,
            "due_on": period["due_on"], "replacement_of": predecessor,
        })
        for index, segment in enumerate(calc.segments, 1):
            tx.insert("finance_receivable_line", {
                "line_id": uuid4(), "organization_id": tx.organization_id,
                "created_command_id": command_id, "receivable_id": identity, "term_id": segment.term_id,
                "line_no": index, "service_from": segment.service_from, "service_to_exclusive": segment.service_to_exclusive,
                "monthly_rent": segment.monthly_rent, "month_days": segment.month_days,
                "charge_days": segment.charge_days, "original_amount": segment.line_amount,
            })
        return _result(tx,"CREATED",[_entity(tx,"RECEIVABLE","finance_receivable",identity)],
                       [identity],issue_eligibility=eligibility)

    def _attribution(self, tx: RentTransactionPort, value: dict) -> tuple[str, UUID | None, UUID | None]:
        strict_object(value, _ATTR_FIELDS, _ATTR_FIELDS)
        status = value["status"]
        property_id, contract_id = _optional_uuid(value["property_id"]), _optional_uuid(value["contract_id"])
        if status == "UNMATCHED":
            if property_id is not None or contract_id is not None:
                raise rent_error("VALIDATION_ERROR")
        elif status == "PROPERTY_CONFIRMED":
            if property_id is None or contract_id is not None:
                raise rent_error("VALIDATION_ERROR")
        elif status == "CONTRACT_CONFIRMED":
            if property_id is None or contract_id is None:
                raise rent_error("VALIDATION_ERROR")
        else:
            raise rent_error("VALIDATION_ERROR")
        if property_id is not None:
            units = tx.rows("SELECT 1 FROM propertyai.v_rent_reference_units WHERE property_id=%s LIMIT 1", (property_id,))
            _row_required(units[0] if units else None)
        if contract_id is not None:
            contract = _row_required(tx.fetch("rent_contract", contract_id))
            if contract["property_id"] != property_id:
                raise rent_error("VALIDATION_ERROR")
        return status, property_id, contract_id

    def _apply_allocations(self, tx: RentTransactionPort, command_id: UUID, source_id: UUID,
                           available: int, source_attribution: tuple[str, UUID | None, UUID | None],
                           inputs: list[dict]) -> tuple[list[dict], list[UUID]]:
        if not isinstance(inputs, list):
            raise rent_error("VALIDATION_ERROR")
        identities = [uuid(item["receivable_id"]) for item in inputs]
        if len(identities) != len(set(identities)):
            raise rent_error("VALIDATION_ERROR")
        tx.lock_entities([("finance_funding_source",source_id)] + [("finance_receivable",i) for i in identities])
        planned: list[tuple[UUID, int, bool, str | None]] = []
        total = 0
        for item, identity in zip(inputs, identities):
            strict_object(item, _ALLOC_FIELDS, _ALLOC_FIELDS)
            amount = money(item["amount"], positive=True)
            row = _row_required(tx.fetch("finance_receivable", identity))
            balance = _row_required(tx.balance("RECEIVABLE", identity))
            if row["version"] != version(item["expected_version"], positive=True):
                raise rent_error("VERSION_CONFLICT")
            if row["voided"]:
                raise rent_error("RECEIVABLE_OVERALLOCATED")
            if amount > int(balance["balance"]):
                raise rent_error("RECEIVABLE_OVERALLOCATED")
            status, prop, contract = source_attribution
            override = item["override_attribution_reason"]
            aligned = (status == "CONTRACT_CONFIRMED" and contract == row["contract_id"]) or (
                status == "PROPERTY_CONFIRMED" and prop == row["property_id"]
            )
            if not aligned:
                if override is None:
                    raise rent_error("ATTRIBUTION_REQUIRED")
                _nonnull(override)
            if item["attribution_confirmed"] is not True:
                raise rent_error("ATTRIBUTION_REQUIRED")
            planned.append((identity, amount, True, override))
            total += amount
        if total > available:
            raise rent_error("ALLOCATION_EXCEEDS_AVAILABLE")
        entities: list[dict] = []
        for identity, amount, confirmed, override in planned:
            allocation_id = uuid4()
            tx.insert("finance_allocation", {
                "allocation_id": allocation_id, "organization_id": tx.organization_id,
                "created_command_id": command_id, "source_id": source_id,
                "receivable_id": identity, "amount": amount, "record_kind": "APPLY",
                "attribution_confirmed": confirmed,
                "override_attribution_reason": override,
            })
            entities.append(_entity(tx,"ALLOCATION","finance_allocation",allocation_id))
        return entities, identities

    def _record_receipt(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None, *, replaces: UUID | None = None) -> dict:
        body = command.body
        if body["currency"] != "KRW":
            raise rent_error("VALIDATION_ERROR")
        account_id = uuid(body["account_id"])
        tx.lock_entities([("finance_account",account_id)])
        account = _row_required(tx.fetch("finance_account", account_id))
        if not account["active"]:
            raise rent_error("VALIDATION_ERROR")
        amount = money(body["amount"], positive=True)
        occurred_on = day(body["occurred_on"])
        attribution = self._attribution(tx, body["attribution"])
        movement_id, revision_id, source_id = uuid4(), uuid4(), uuid4()
        tx.insert("finance_movement", {
            "movement_id": movement_id, "organization_id": tx.organization_id,
            "created_command_id": command_id, "last_command_id": command_id,
            "direction": "IN", "origin": "MANUAL", "current_revision": 1,
            "replaces_movement_id": replaces,
        })
        tx.insert("finance_movement_revision", {
            "movement_revision_id": revision_id, "organization_id": tx.organization_id,
            "created_command_id": command_id, "movement_id": movement_id,
            "revision_no": 1, "account_id": account_id, "occurred_on": occurred_on,
            "amount": amount, "currency": "KRW", "payer_raw": body["payer_raw"],
            "counterparty_party_id": _optional_uuid(body["counterparty_party_id"]),
            "record_status": "RECORDED",
        })
        tx.insert("finance_funding_source", {
            "source_id": source_id, "organization_id": tx.organization_id,
            "created_command_id": command_id, "last_command_id": command_id,
            "origin": "RECEIPT", "movement_id": movement_id,
            "attribution_status": attribution[0],
            "attributed_property_id": attribution[1], "attributed_contract_id": attribution[2],
        })
        allocations, receivables = self._apply_allocations(tx, command_id, source_id, amount, attribution, body["allocations"])
        entities = [
            _entity(tx,"MOVEMENT","finance_movement",movement_id),
            _entity(tx,"SOURCE","finance_funding_source",source_id),
            *allocations,
        ]
        return _result(tx,"CREATED",entities,receivables,[source_id])

    def _allocate(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        body = command.body
        source_id = uuid(body["source_id"])
        tx.lock_entities([("finance_funding_source",source_id)])
        source = _row_required(tx.fetch("finance_funding_source", source_id))
        if source["version"] != version(body["expected_source_version"], positive=True):
            raise rent_error("VERSION_CONFLICT")
        attribution = self._attribution(tx, body["attribution"])
        if (source["attribution_status"], source["attributed_property_id"], source["attributed_contract_id"]) != attribution:
            tx.update_versioned("finance_funding_source",source_id,source["version"],{
                "attribution_status": attribution[0], "attributed_property_id": attribution[1],
                "attributed_contract_id": attribution[2],
            },command_id)
        available = int(_row_required(tx.balance("SOURCE",source_id))["available"])
        allocations, receivables = self._apply_allocations(tx, command_id, source_id, available, attribution, body["allocations"])
        entities = [_entity(tx,"SOURCE","finance_funding_source",source_id), *allocations]
        return _result(tx,"UPDATED",entities,receivables,[source_id])


    # W1-A correction planners use the existing append-only tables and the
    # repository's single SERIALIZABLE / sealed-receipt transaction boundary.
    @staticmethod
    def _correction_reason(value: Any) -> str:
        value = _nonnull(value)
        if len(value) > 500:
            raise rent_error("VALIDATION_ERROR")
        return value

    @staticmethod
    def _nullable_label(value: Any) -> str | None:
        if value is not None and (not isinstance(value, str) or len(value) > 500):
            raise rent_error("VALIDATION_ERROR")
        return value

    @staticmethod
    def _uuid_list(value: Any, *, nonempty: bool = False) -> list[UUID]:
        if not isinstance(value, list) or (nonempty and not value):
            raise rent_error("VALIDATION_ERROR")
        identities = [uuid(item) for item in value]
        if len(identities) != len(set(identities)):
            raise rent_error("VALIDATION_ERROR")
        return identities

    def _check_versions(self, tx: RentTransactionPort, table: str, items: Any,
                        identities: set[UUID]) -> dict[UUID, int]:
        if not isinstance(items, list):
            raise rent_error("VALIDATION_ERROR")
        expected = {}
        for item in items:
            strict_object(item, {"id", "expected_version"}, {"id", "expected_version"})
            identity = uuid(item["id"])
            if identity in expected:
                raise rent_error("VALIDATION_ERROR")
            expected[identity] = version(item["expected_version"], positive=True)
        if expected.keys() != identities:
            raise rent_error("VALIDATION_ERROR")
        tx.lock_entities([(table, identity) for identity in identities])
        for identity, token in expected.items():
            if _row_required(tx.fetch(table, identity))["version"] != token:
                raise rent_error("VERSION_CONFLICT")
        return expected

    @staticmethod
    def _live_reversible_fact(tx: RentTransactionPort, table: str, identity: UUID) -> Any:
        # Immutable facts need no new locking API; source/receivable headers and
        # the organization ledger scope serialize every command using them.
        queries = {
            "finance_allocation": "SELECT 1 FROM propertyai.finance_allocation WHERE organization_id=%s AND reverse_of=%s",
            "finance_source_return": "SELECT 1 FROM propertyai.finance_source_return WHERE organization_id=%s AND reverse_of=%s",
        }
        row = _row_required(tx.fetch(table, identity))
        if row["record_kind"] != "APPLY" or tx.rows(queries[table], (tx.organization_id, identity)):
            raise rent_error("VERSION_CONFLICT")
        return row

    def _prepare_allocation_correction(self, tx: RentTransactionPort, plan: Any) -> dict:
        fields = {"reverse_allocation_ids", "replacement_allocations", "source_versions", "receivable_versions"}
        strict_object(plan, fields, fields)
        reverse_ids = self._uuid_list(plan["reverse_allocation_ids"])
        originals = [self._live_reversible_fact(tx, "finance_allocation", identity) for identity in reverse_ids]
        replacements = plan["replacement_allocations"]
        if not isinstance(replacements, list):
            raise rent_error("VALIDATION_ERROR")
        sources = {row["source_id"] for row in originals}
        receivables = {row["receivable_id"] for row in originals}
        seen = set()
        for item in replacements:
            strict_object(item, {"source_id", "allocation"}, {"source_id", "allocation"})
            allocation = strict_object(item["allocation"], _ALLOC_FIELDS, _ALLOC_FIELDS)
            source, receivable = uuid(item["source_id"]), uuid(allocation["receivable_id"])
            if (source, receivable) in seen:
                raise rent_error("VALIDATION_ERROR")
            seen.add((source, receivable))
            money(allocation["amount"], positive=True)
            if allocation["attribution_confirmed"] is not True:
                raise rent_error("ATTRIBUTION_REQUIRED")
            if allocation["override_attribution_reason"] is not None:
                self._correction_reason(allocation["override_attribution_reason"])
            sources.add(source)
            receivables.add(receivable)
        self._check_versions(tx, "finance_funding_source", plan["source_versions"], sources)
        expected = self._check_versions(tx, "finance_receivable", plan["receivable_versions"], receivables)
        for item in replacements:
            allocation = item["allocation"]
            if version(allocation["expected_version"], positive=True) != expected[uuid(allocation["receivable_id"])]:
                raise rent_error("VERSION_CONFLICT")
        return {"originals": originals, "replacements": replacements,
                "sources": sources, "receivables": receivables}

    def _reverse_allocations(self, tx: RentTransactionPort, command_id: UUID, plan: dict, reason: str) -> list[dict]:
        entities = []
        for row in plan["originals"]:
            identity = uuid4()
            tx.insert("finance_allocation", {
                "allocation_id": identity, "organization_id": tx.organization_id,
                "created_command_id": command_id, "source_id": row["source_id"],
                "receivable_id": row["receivable_id"], "amount": row["amount"],
                "record_kind": "REVERSE", "reverse_of": row["allocation_id"],
                "attribution_confirmed": True, "override_attribution_reason": row["override_attribution_reason"],
                "reason": reason,
            })
            entities.append(_entity(tx, "ALLOCATION", "finance_allocation", identity))
        return entities

    @staticmethod
    def _current_allocation_versions(tx: RentTransactionPort, inputs: list[dict]) -> list[dict]:
        # Only use AFTER all client versions were checked against the pre-command
        # snapshot. Reversal/adjustment triggers legitimately bump these versions.
        return [dict(item, expected_version=str(_row_required(
            tx.fetch("finance_receivable", uuid(item["receivable_id"])))["version"])) for item in inputs]

    def _replace_allocations(self, tx: RentTransactionPort, command_id: UUID, plan: dict) -> list[dict]:
        grouped: dict[UUID, list[dict]] = {}
        for item in plan["replacements"]:
            grouped.setdefault(uuid(item["source_id"]), []).append(item["allocation"])
        entities = []
        for source_id in sorted(grouped, key=str):
            source = _row_required(tx.fetch("finance_funding_source", source_id))
            attribution = (source["attribution_status"], source["attributed_property_id"], source["attributed_contract_id"])
            available = int(_row_required(tx.balance("SOURCE", source_id))["available"])
            applied, _ = self._apply_allocations(tx, command_id, source_id, available, attribution,
                                                self._current_allocation_versions(tx, grouped[source_id]))
            entities.extend(applied)
        return entities

    def _finish_correction(self, tx: RentTransactionPort, entities: list[dict],
                           receivables: Any = (), sources: Any = (), *, outcome: str = "UPDATED") -> dict:
        receivables, sources = sorted(set(receivables), key=str), sorted(set(sources), key=str)
        for identity in receivables:
            row = _row_required(tx.balance("RECEIVABLE", identity))
            require_finance_balances(obligation=int(row["effective_amount"]), allocated=int(row["allocated"]))
        for identity in sources:
            row = _row_required(tx.balance("SOURCE", identity))
            require_finance_balances(principal=int(row["principal"]), allocated=int(row["allocated"]),
                                     returned=int(row["returned"]))
        tables = {"RECEIVABLE": "finance_receivable", "SOURCE": "finance_funding_source", "MOVEMENT": "finance_movement"}
        entities = [_entity(tx, item["kind"], tables[item["kind"]], UUID(item["id"]))
                    if item["kind"] in tables else item for item in entities]
        return _result(tx, outcome, entities, receivables, sources)

    def _receivable_for_correction(self, tx: RentTransactionPort, command: RentCommand) -> Any:
        tx.lock_entities([("finance_receivable", command.target_id)])
        row = _row_required(tx.fetch("finance_receivable", command.target_id))
        if row["version"] != version(command.body["expected_version"], positive=True) or row["voided"]:
            raise rent_error("VERSION_CONFLICT")
        return row

    def _adjust_receivable(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        body, identity = command.body, command.target_id
        reason = self._correction_reason(body["reason"])
        self._receivable_for_correction(tx, command)
        delta = signed_money(body["delta"])
        adjusted_obligation(int(_row_required(tx.balance("RECEIVABLE", identity))["effective_amount"]), delta)
        reverse_of = _optional_uuid(body["reverse_adjustment_id"])
        if reverse_of is not None:
            original = _row_required(tx.fetch("finance_receivable_adjustment", reverse_of))
            if original["receivable_id"] != identity:
                raise rent_error("NOT_FOUND")
            if original["reverse_of"] is not None or original["delta"] != -delta:
                raise rent_error("VALIDATION_ERROR")
            if tx.rows("SELECT 1 FROM propertyai.finance_receivable_adjustment WHERE organization_id=%s AND reverse_of=%s",
                       (tx.organization_id, reverse_of)):
                raise rent_error("VERSION_CONFLICT")
        plan = self._prepare_allocation_correction(tx, body["allocation_correction"])
        entities = self._reverse_allocations(tx, command_id, plan, reason)
        adjustment = uuid4()
        tx.insert("finance_receivable_adjustment", {
            "adjustment_id": adjustment, "organization_id": tx.organization_id,
            "created_command_id": command_id, "receivable_id": identity,
            "delta": delta, "reason": reason, "reverse_of": reverse_of,
        })
        entities.extend(self._replace_allocations(tx, command_id, plan))
        entities.extend([_entity(tx, "ADJUSTMENT", "finance_receivable_adjustment", adjustment),
                         _entity(tx, "RECEIVABLE", "finance_receivable", identity)])
        return self._finish_correction(tx, entities, plan["receivables"] | {identity}, plan["sources"])

    def _void_receivable(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        reason = self._correction_reason(command.body["reason"])
        row = self._receivable_for_correction(tx, command)
        plan = self._prepare_allocation_correction(tx, command.body["allocation_correction"])
        entities = self._reverse_allocations(tx, command_id, plan, reason)
        current = _row_required(tx.fetch("finance_receivable", row["receivable_id"]))
        tx.update_versioned("finance_receivable", row["receivable_id"], current["version"],
                            {"voided": True, "void_reason": reason}, command_id)
        entities.extend(self._replace_allocations(tx, command_id, plan))
        entities.append(_entity(tx, "RECEIVABLE", "finance_receivable", row["receivable_id"]))
        return self._finish_correction(tx, entities, plan["receivables"] | {row["receivable_id"]}, plan["sources"])

    def _correct_allocations(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        reason = self._correction_reason(command.body["reason"])
        plan = self._prepare_allocation_correction(tx, command.body["plan"])
        if not plan["originals"] and not plan["replacements"]:
            raise rent_error("VALIDATION_ERROR")
        entities = self._reverse_allocations(tx, command_id, plan, reason)
        entities.extend(self._replace_allocations(tx, command_id, plan))
        return self._finish_correction(tx, entities, plan["receivables"], plan["sources"])

    @staticmethod
    def _current_movement(tx: RentTransactionPort, identity: UUID, expected: Any, direction: str) -> tuple[Any, Any]:
        tx.lock_entities([("finance_movement", identity)])
        movement = _row_required(tx.fetch("finance_movement", identity))
        if movement["direction"] != direction:
            raise rent_error("VALIDATION_ERROR")
        if movement["version"] != version(expected, positive=True):
            raise rent_error("VERSION_CONFLICT")
        rows = tx.rows("SELECT * FROM propertyai.finance_movement_revision WHERE organization_id=%s AND movement_id=%s AND revision_no=%s",
                       (tx.organization_id, identity, movement["current_revision"]))
        current = _row_required(rows[0] if rows else None)
        if current["record_status"] != "RECORDED":
            raise rent_error("VERSION_CONFLICT")
        return movement, current

    def _movement_values(self, tx: RentTransactionPort, values: Any, previous: Any = None) -> dict:
        fields = {"account_id", "occurred_on", "amount", "payer_raw", "counterparty_party_id"}
        strict_object(values, fields, fields)
        account_id = uuid(values["account_id"])
        tx.lock_entities([("finance_account", account_id)])
        account = _row_required(tx.fetch("finance_account", account_id))
        if not account["active"] and (previous is None or previous["account_id"] != account_id):
            raise rent_error("VALIDATION_ERROR")
        return {"account_id": account_id, "occurred_on": day(values["occurred_on"]),
                "amount": money(values["amount"], positive=True), "currency": "KRW",
                "payer_raw": self._nullable_label(values["payer_raw"]),
                "counterparty_party_id": _optional_uuid(values["counterparty_party_id"])}

    @staticmethod
    def _append_movement_revision(tx: RentTransactionPort, command_id: UUID, movement: Any,
                                  current: Any, corrected: dict | None, reason: str) -> None:
        values = corrected if corrected is not None else {key: current[key] for key in (
            "account_id", "occurred_on", "amount", "currency", "payer_raw", "counterparty_party_id")}
        revision = movement["current_revision"] + 1
        tx.insert("finance_movement_revision", {
            "movement_revision_id": uuid4(), "organization_id": tx.organization_id,
            "created_command_id": command_id, "movement_id": movement["movement_id"],
            "revision_no": revision, "previous_revision_no": movement["current_revision"],
            **values, "record_status": "RECORDED" if corrected is not None else "REVERSED", "reason": reason,
        })
        tx.update_versioned("finance_movement", movement["movement_id"], movement["version"],
                            {"current_revision": revision}, command_id)

    def _preflight_replacement_receipt(self, tx: RentTransactionPort, value: Any) -> set[UUID]:
        fields = {"account_id", "occurred_on", "amount", "currency", "payer_raw",
                  "counterparty_party_id", "attribution", "allocations"}
        strict_object(value, fields, fields)
        self._movement_values(tx, {k: value[k] for k in fields - {"currency", "attribution", "allocations"}})
        if value["currency"] != "KRW" or not isinstance(value["allocations"], list):
            raise rent_error("VALIDATION_ERROR")
        self._attribution(tx, value["attribution"])
        receivables = set()
        for item in value["allocations"]:
            strict_object(item, _ALLOC_FIELDS, _ALLOC_FIELDS)
            identity = uuid(item["receivable_id"])
            if identity in receivables:
                raise rent_error("VALIDATION_ERROR")
            receivables.add(identity)
            if _row_required(tx.fetch("finance_receivable", identity))["version"] != version(item["expected_version"], positive=True):
                raise rent_error("VERSION_CONFLICT")
            money(item["amount"], positive=True)
            if item["attribution_confirmed"] is not True:
                raise rent_error("ATTRIBUTION_REQUIRED")
            if item["override_attribution_reason"] is not None:
                self._correction_reason(item["override_attribution_reason"])
        tx.lock_entities([("finance_receivable", identity) for identity in receivables])
        return receivables

    def _correct_movement(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        body = command.body
        reason = self._correction_reason(body["reason"])
        action = body["action"]
        if not isinstance(action, str) or action not in {"APPEND_CORRECTED_REVISION", "REVERSE_RECORD", "REVERSE_AND_REPLACE"}:
            raise rent_error("VALIDATION_ERROR")
        if (body["corrected_values"] is not None) != (action == "APPEND_CORRECTED_REVISION") or (
            (body["replacement_receipt"] is not None) != (action == "REVERSE_AND_REPLACE")
        ):
            raise rent_error("VALIDATION_ERROR")
        movement, current = self._current_movement(tx, command.target_id, body["expected_version"], "IN")
        rows = tx.rows("SELECT * FROM propertyai.finance_funding_source WHERE organization_id=%s AND movement_id=%s",
                       (tx.organization_id, command.target_id))
        source = _row_required(rows[0] if rows else None)
        tx.lock_entities([("finance_funding_source", source["source_id"])])
        if source["version"] != version(body["expected_source_version"], positive=True):
            raise rent_error("VERSION_CONFLICT")
        if action != "APPEND_CORRECTED_REVISION" and int(_row_required(tx.balance("SOURCE", source["source_id"]))["returned"]) != 0:
            raise rent_error("ALLOCATION_EXCEEDS_AVAILABLE")
        corrected = self._movement_values(tx, body["corrected_values"], current) if body["corrected_values"] is not None else None
        plan = self._prepare_allocation_correction(tx, body["allocation_correction"])
        extra_receivables = self._preflight_replacement_receipt(tx, body["replacement_receipt"]) if action == "REVERSE_AND_REPLACE" else set()
        entities = self._reverse_allocations(tx, command_id, plan, reason)
        self._append_movement_revision(tx, command_id, movement, current, corrected, reason)
        entities.extend(self._replace_allocations(tx, command_id, plan))
        sources = plan["sources"] | {source["source_id"]}
        if action == "REVERSE_AND_REPLACE":
            replacement = dict(body["replacement_receipt"], expected_ledger_revision=body["expected_ledger_revision"])
            replacement["allocations"] = self._current_allocation_versions(tx, replacement["allocations"])
            result = self._record_receipt(tx, command_id, RentCommand("recordReceipt", replacement, command.idempotency_key),
                                          None, replaces=command.target_id)
            entities.extend(result["entities"])
            sources.update(UUID(row["source_id"]) for row in result["source_balances"])
        entities.extend([_entity(tx, "MOVEMENT", "finance_movement", command.target_id),
                         _entity(tx, "SOURCE", "finance_funding_source", source["source_id"])])
        return self._finish_correction(tx, entities, plan["receivables"] | extra_receivables, sources)

    @staticmethod
    def _insert_return(tx: RentTransactionPort, command_id: UUID, source_id: UUID,
                       movement_id: UUID, amount: int, reason: str, *, reverse_of: UUID | None = None) -> dict:
        identity = uuid4()
        tx.insert("finance_source_return", {
            "return_id": identity, "organization_id": tx.organization_id, "created_command_id": command_id,
            "source_id": source_id, "outgoing_movement_id": movement_id, "amount": amount,
            "record_kind": "REVERSE" if reverse_of else "APPLY", "reverse_of": reverse_of,
            "reason": reason, "actual_transfer_confirmed": True,
        })
        return _entity(tx, "RETURN", "finance_source_return", identity)

    def _record_refund(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        body = command.body
        reason = self._correction_reason(body["reason"])
        if body["actual_transfer_confirmed"] is not True:
            raise rent_error("VALIDATION_ERROR")
        source_id = uuid(body["source_id"])
        tx.lock_entities([("finance_funding_source", source_id)])
        source = _row_required(tx.fetch("finance_funding_source", source_id))
        if source["version"] != version(body["expected_source_version"], positive=True):
            raise rent_error("VERSION_CONFLICT")
        values = self._movement_values(tx, {"account_id": body["out_account_id"], "occurred_on": body["occurred_on"],
                                           "amount": body["amount"], "payer_raw": body["payee_raw"], "counterparty_party_id": None})
        if values["amount"] > int(_row_required(tx.balance("SOURCE", source_id))["available"]):
            raise rent_error("ALLOCATION_EXCEEDS_AVAILABLE")
        movement_id = uuid4()
        tx.insert("finance_movement", {
            "movement_id": movement_id, "organization_id": tx.organization_id,
            "created_command_id": command_id, "last_command_id": command_id,
            "direction": "OUT", "origin": "MANUAL", "current_revision": 1,
        })
        tx.insert("finance_movement_revision", {
            "movement_revision_id": uuid4(), "organization_id": tx.organization_id,
            "created_command_id": command_id, "movement_id": movement_id, "revision_no": 1,
            **values, "record_status": "RECORDED", "reason": reason,
        })
        returned = self._insert_return(tx, command_id, source_id, movement_id, values["amount"], reason)
        return self._finish_correction(tx, [returned, _entity(tx, "MOVEMENT", "finance_movement", movement_id)],
                                       sources=[source_id], outcome="CREATED")

    def _correct_refund(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        body = command.body
        reason = self._correction_reason(body["reason"])
        action = body["action"]
        if body["actual_record_correction_confirmed"] is not True or not isinstance(action, str) or action not in {"APPEND_CORRECTED_REVISION", "REVERSE_RECORD"}:
            raise rent_error("VALIDATION_ERROR")
        if (body["corrected_values"] is not None) != (action == "APPEND_CORRECTED_REVISION"):
            raise rent_error("VALIDATION_ERROR")
        reverse_ids = self._uuid_list(body["reverse_return_ids"], nonempty=True)
        if command.target_id not in reverse_ids:
            raise rent_error("VALIDATION_ERROR")
        movement_id = uuid(body["outgoing_movement_id"])
        movement, current = self._current_movement(tx, movement_id, body["expected_movement_version"], "OUT")
        originals = [self._live_reversible_fact(tx, "finance_source_return", identity) for identity in reverse_ids]
        if any(row["outgoing_movement_id"] != movement_id for row in originals):
            raise rent_error("NOT_FOUND")
        corrected = self._movement_values(tx, body["corrected_values"], current) if body["corrected_values"] is not None else None
        replacements = body["replacement_returns"]
        if not isinstance(replacements, list):
            raise rent_error("VALIDATION_ERROR")
        parsed = []
        for item in replacements:
            strict_object(item, {"source_id", "amount"}, {"source_id", "amount"})
            parsed.append((uuid(item["source_id"]), money(item["amount"], positive=True)))
        sources = {row["source_id"] for row in originals} | {source for source, _ in parsed}
        self._check_versions(tx, "finance_funding_source", body["source_versions"], sources)
        net = current["amount"] - sum(row["amount"] for row in originals) + sum(amount for _, amount in parsed)
        if net != (corrected["amount"] if corrected is not None else 0):
            raise rent_error("VALIDATION_ERROR")
        entities = [self._insert_return(tx, command_id, row["source_id"], movement_id, row["amount"], reason,
                                        reverse_of=row["return_id"]) for row in originals]
        self._append_movement_revision(tx, command_id, movement, current, corrected, reason)
        for source_id, amount in parsed:
            if amount > int(_row_required(tx.balance("SOURCE", source_id))["available"]):
                raise rent_error("ALLOCATION_EXCEEDS_AVAILABLE")
            entities.append(self._insert_return(tx, command_id, source_id, movement_id, amount, reason))
        entities.append(_entity(tx, "MOVEMENT", "finance_movement", movement_id))
        return self._finish_correction(tx, entities, sources=sources)

    def _occupancy_command(self, tx: RentTransactionPort, command_id: UUID, command: RentCommand, _: date | None) -> dict:
        body = command.body
        evidence = strict_object(body["evidence"], _EVIDENCE_FIELDS, _EVIDENCE_FIELDS)
        if evidence["operator_confirmed"] is not True:
            raise rent_error("VALIDATION_ERROR")
        _nonnull(evidence["confirmation_ref"])
        _nonnull(evidence["reason"])
        source_id = command.target_id
        source = _row_required(tx.fetch("rent_occupancy",source_id))
        contract_id = source["contract_id"]
        target_id = uuid(body["target_contract_id"]) if command.operation_id == "moveOccupancy" else None
        locks = [("rent_contract",contract_id),("rent_occupancy",source_id)]
        if target_id:
            locks.append(("rent_contract",target_id))
        tx.lock_entities(locks)
        source = _row_required(tx.fetch("rent_occupancy",source_id))
        contract = _row_required(tx.fetch("rent_contract",contract_id))
        if source["version"] != version(body["expected_version"],positive=True) or contract["version"] != version(body["expected_contract_version"],positive=True):
            raise rent_error("VERSION_CONFLICT")
        before = _snapshot(source)
        actual = ActualOccupancy(
            source_id, tx.organization_id, contract_id, source["resident_id"], source["rental_unit_id"],
            source["review_status"], source["actual_start"], source["actual_end_exclusive"],
        )
        changes = []
        if command.operation_id == "confirmOccupancyStart":
            updated = confirm_start(actual,day(body["actual_start"]))
        elif command.operation_id == "confirmOccupancyEnd":
            updated = confirm_end(actual,day(body["actual_end_exclusive"]))
        elif command.operation_id == "correctOccupancyDates":
            updated = correct_dates(actual,day(body["actual_start"]),_optional_day(body["actual_end_exclusive"]))
        elif command.operation_id == "moveOccupancy":
            if actual.actual_end_exclusive is not None:
                raise rent_error('PERIOD_CONFLICT')
            if actual.verified_interval() is None:
                raise rent_error('VERSION_CONFLICT')
            move_on = day(body["effective_move_on"])
            updated = confirm_end(actual, move_on)
            target = _row_required(tx.fetch("rent_contract",target_id))
            if target["version"] != version(body["expected_target_contract_version"],positive=True):
                raise rent_error("VERSION_CONFLICT")
            if target["previous_contract_id"] != contract_id or target["rental_unit_id"] == source["rental_unit_id"]:
                raise rent_error("VALIDATION_ERROR")
            links = tx.rows(
                "SELECT 1 FROM propertyai.rent_contract_resident WHERE organization_id=%s AND contract_id=%s AND resident_id=%s",
                (tx.organization_id,target_id,source["resident_id"]),
            )
            _row_required(links[0] if links else None)
        else:
            raise rent_error("INTERNAL_ERROR")
        tx.update_versioned("rent_occupancy",source_id,source["version"],{
            "actual_start": updated.actual_start,
            "actual_end_exclusive": updated.actual_end_exclusive,
            "review_status": updated.review_status,
        },command_id)
        after = _snapshot(_row_required(tx.fetch("rent_occupancy",source_id)))
        changes.append({"transition":command.command_type,"before":before,"after":after,"evidence":evidence})
        entities = [_entity(tx,"OCCUPANCY","rent_occupancy",source_id)]
        if target_id:
            new_id = uuid4()
            tx.insert("rent_occupancy", {
                "occupancy_id":new_id, "organization_id":tx.organization_id,
                "created_command_id":command_id,"last_command_id":command_id,
                "contract_id":target_id,"resident_id":source["resident_id"],
                "property_id":target["property_id"],"rental_unit_id":target["rental_unit_id"],
                "actual_start":move_on,"actual_end_exclusive":None,"review_status":"VERIFIED",
            })
            successor = _snapshot(_row_required(tx.fetch("rent_occupancy",new_id)))
            changes.append({"transition":command.command_type,"before":None,"after":successor,"evidence":evidence})
            entities.append(_entity(tx,"OCCUPANCY","rent_occupancy",new_id))
        return _result(tx,"UPDATED",entities,occupancy_changes=changes)

    def preview_charge(self, request: dict) -> dict:
        strict_object(request, {"contract_id","period_id"}, {"contract_id","period_id"})
        contract_id, period_id = uuid(request["contract_id"]), uuid(request["period_id"])
        def read(tx: RentTransactionPort) -> dict:
            contract_rows=tx.rows('SELECT * FROM propertyai.v_rent_contracts WHERE contract_id=%s',(contract_id,))
            period_rows=tx.rows('SELECT * FROM propertyai.v_rent_periods WHERE period_id=%s',(period_id,))
            contract=_row_required(contract_rows[0] if contract_rows else None)
            period=_row_required(period_rows[0] if period_rows else None)
            if period["contract_id"] != contract_id:
                raise rent_error("NOT_FOUND")
            term_rows = tx.rows(
                "SELECT term_id,version FROM propertyai.v_rent_terms WHERE organization_id=%s AND contract_id=%s AND superseded_by IS NULL ORDER BY term_id",
                (tx.organization_id,contract_id),
            )
            term_versions = [{"id":str(r["term_id"]),"expected_version":str(r["version"])} for r in term_rows]
            base = {
                "contract_id":str(contract_id),"period_id":str(period_id),
                "contract_version":str(contract["version"]),"term_versions":term_versions,
                "ledger_revision":str(tx.ledger_revision_before),
                "due_on":period["due_on"].isoformat(),
                "generation_on":generation_on(period["due_on"]).isoformat(),
            }
            try:
                _, _, calc = self._charge(tx,contract_id,period_id)
            except (FinanceError,):
                return {**base,"amount":None,"status":"NEEDS_REVIEW","reasons":["CONTRACT_NOT_READY"],
                        "calculation_sha256":None,"segments":[]}
            except Exception as exc:
                if getattr(exc,"code",None) == "CONTRACT_NOT_READY":
                    return {**base,"amount":None,"status":"NEEDS_REVIEW","reasons":["CONTRACT_NOT_READY"],
                            "calculation_sha256":None,"segments":[]}
                raise
            active = tx.rows(
                "SELECT 1 FROM propertyai.finance_receivable WHERE organization_id=%s AND period_id=%s AND NOT voided",
                (tx.organization_id,period_id),
            )
            status = "ALREADY_ISSUED" if active else "NO_CHARGE" if calc.amount == 0 else "READY"
            return {**base,"amount":money_text(calc.amount),"status":status,"reasons":[],
                    "calculation_sha256":calc.fingerprint,"segments":[s.wire() for s in calc.segments]}
        return self.repository.read_operator_snapshot(read)

    def _receivable_row(self, row: dict, business_date: date) -> dict:
        effective, allocated, balance = int(row["effective_amount"]), int(row["allocated"]), int(row["balance"])
        voided = row["voided"]
        state = "VOID" if voided else "PAID" if balance == 0 else "PARTIAL" if allocated > 0 else "UNPAID"
        overdue = "UNKNOWN" if row["due_on"] is None else (
            "OVERDUE" if balance > 0 and business_date > row["due_on"] else "NOT_OVERDUE"
        )
        return {
            "receivable_id":str(row["receivable_id"]),"contract_id":str(row["contract_id"]),
            "property_id":str(row["property_id"]),"period_id":str(row["period_id"]),
            "due_on":row["due_on"].isoformat() if row["due_on"] else None,
            "effective_amount":money_text(effective),"allocated":money_text(allocated),
            "balance":money_text(balance),"version":str(row["version"]),
            "payment_state":state,"overdue_state":overdue,
        }

    def get_contract(self, contract_id: UUID) -> dict:
        def read(tx: RentTransactionPort) -> dict:
            contract = _row_required(tx.fetch("rent_contract",contract_id))
            recorded = tx.rows("SELECT transaction_timestamp() AS as_of")[0]["as_of"]
            residents = tx.rows(
                "SELECT r.resident_id,r.display_name FROM propertyai.rent_contract_resident l JOIN propertyai.rent_resident r ON (r.organization_id,r.resident_id)=(l.organization_id,l.resident_id) WHERE l.organization_id=%s AND l.contract_id=%s ORDER BY r.resident_id",
                (tx.organization_id,contract_id),
            )
            terms = tx.rows(
                "SELECT * FROM propertyai.rent_term_revision WHERE organization_id=%s AND contract_id=%s ORDER BY revision_no",
                (tx.organization_id,contract_id),
            )
            histories = tx.rows(
                "SELECT * FROM propertyai.v_rent_contract_history WHERE contract_id=%s ORDER BY version",
                (contract_id,),
            )
            occupancies = tx.rows(
                "SELECT * FROM propertyai.rent_occupancy WHERE organization_id=%s AND contract_id=%s ORDER BY occupancy_id",
                (tx.organization_id,contract_id),
            )
            receivables = tx.rows(
                "SELECT * FROM propertyai.v_rent_receivables WHERE contract_id=%s ORDER BY due_on,receivable_id",
                (contract_id,),
            )
            timezone_rows = tx.rows(
                "SELECT timezone_name FROM propertyai.v_rent_reference_units WHERE property_id=%s LIMIT 1",
                (contract["property_id"],),
            )
            timezone_name = _row_required(timezone_rows[0] if timezone_rows else None)["timezone_name"]
            today = self.business_date.today(timezone_name)
            term_wire = []
            for t in terms:
                term_wire.append({
                    "term_id":str(t["term_id"]),"revision_no":t["revision_no"],"version":str(t["version"]),
                    "valid_from":t["valid_from"].isoformat(),
                    "valid_to_exclusive":t["valid_to_exclusive"].isoformat() if t["valid_to_exclusive"] else None,
                    "monthly_rent":money_text(t["monthly_rent"]) if t["monthly_rent"] is not None else None,
                    "amount_confirmed":t["amount_confirmed"],"due_day":t["due_day"],
                    "cycle_confirmed":t["cycle_confirmed"],"cycle_rule":t["cycle_rule"],
                    "policy_version":t["policy_version"],
                    "superseded_by":str(t["superseded_by"]) if t["superseded_by"] else None,
                    "supersession_reason":t["supersession_reason"],
                })
            current_term = next((
                t for t in terms
                if t["term_id"] == contract["current_term_id"] and t["superseded_by"] is None
            ), None)
            if contract["lifecycle"] in {"ENDED","TERMINATED","CANCELLED"}:
                billing_readiness = "NOT_READY"
            elif contract["readiness"] != "READY" or current_term is None or (
                not current_term["amount_confirmed"] or not current_term["cycle_confirmed"]
            ):
                billing_readiness = "UNKNOWN_OR_UNCONFIRMED"
            else:
                billing_readiness = "READY"
            return {
                "contract_id":str(contract_id),"version":str(contract["version"]),
                "as_of":recorded.isoformat(),"lifecycle":contract["lifecycle"],
                "readiness":contract["readiness"],"billing_readiness":billing_readiness,
                "current_term_id":str(contract["current_term_id"]) if contract["current_term_id"] else None,
                "starts_on":contract["starts_on"].isoformat() if contract["starts_on"] else None,
                "ends_on_exclusive":contract["ends_on_exclusive"].isoformat() if contract["ends_on_exclusive"] else None,
                "residents":[{"resident_id":str(r["resident_id"]),"display_name":r["display_name"]} for r in residents],
                "terms":term_wire,"occupancies":[_snapshot(r) for r in occupancies],
                "contract_history":[{
                    "version":str(h["version"]),"command_id":str(h["command_id"]),
                    "snapshot":h["snapshot"],"recorded_at":h["recorded_at"].isoformat(),
                } for h in histories],
                "receivables":[self._receivable_row(r,today) for r in receivables],
            }
        return self.repository.read_snapshot(read)

    def overview(self, month: str, property_id: UUID | None = None, cursor: UUID | None = None, page_size: int = 50) -> dict:
        selected = day(month + "-01")
        next_month = date(selected.year + (selected.month == 12), selected.month % 12 + 1, 1)
        if not 1 <= page_size <= 100:
            raise rent_error("VALIDATION_ERROR")
        def read(tx: RentTransactionPort) -> dict:
            params: tuple[Any,...] = (property_id,) if property_id else ()
            clause = " WHERE r.property_id=%s" if property_id else ""
            rows = tx.rows(
                "SELECT r.*,p.cycle_start,p.cycle_end_exclusive,u.timezone_name FROM propertyai.v_rent_receivables r JOIN propertyai.v_rent_periods p ON (p.organization_id,p.period_id)=(r.organization_id,r.period_id) JOIN (SELECT DISTINCT property_id,timezone_name FROM propertyai.v_rent_reference_units) u ON u.property_id=r.property_id" + clause + " ORDER BY r.receivable_id",
                params,
            )
            as_of = tx.rows("SELECT transaction_timestamp() AS as_of")[0]["as_of"]
            selected_rows = [r for r in rows if r["cycle_start"] < next_month and selected < r["cycle_end_exclusive"]]
            selected_obligation = sum(int(r["effective_amount"]) for r in selected_rows)
            selected_allocated = sum(int(r["allocated"]) for r in selected_rows)
            selected_balance = sum(int(r["balance"]) for r in selected_rows)
            all_balance = sum(int(r["balance"]) for r in rows)
            if cursor:
                selected_rows = [r for r in selected_rows if r["receivable_id"] > cursor]
            page = selected_rows[:page_size]
            next_cursor = str(page[-1]["receivable_id"]) if len(selected_rows) > page_size and page else None
            business_date = self.business_date.today(page[0]["timezone_name"] if page else "Asia/Seoul")
            return {
                "as_of":as_of.isoformat(),"business_date":business_date.isoformat(),
                "selected_month":month,
                "rows":[self._receivable_row(r,self.business_date.today(r["timezone_name"])) for r in page],
                "selected_month_obligation":str(selected_obligation),
                "selected_month_allocated":str(selected_allocated),
                "selected_month_balance":str(selected_balance),
                "all_period_balance":str(all_balance),
                "next_cursor":next_cursor,
            }
        return self.repository.read_snapshot(read)

    def funding_sources(self) -> dict:
        def read(tx: RentTransactionPort) -> dict:
            rows = tx.rows("SELECT * FROM propertyai.v_rent_sources ORDER BY source_id")
            as_of = tx.rows("SELECT transaction_timestamp() AS as_of")[0]["as_of"]
            return {
                "as_of":as_of.isoformat(),"next_cursor":None,
                "rows":[{
                    "source_id":str(r["source_id"]),"movement_id":str(r["movement_id"]),
                    "principal":money_text(int(r["principal"])),"allocated":money_text(int(r["allocated"])),
                    "returned":money_text(int(r["returned"])),"available":money_text(int(r["available"])),
                    "version":str(r["version"]),"attribution_status":r["attribution_status"],
                } for r in rows],
            }
        return self.repository.read_snapshot(read)

    def reference_data(self) -> dict:
        def read(tx: RentTransactionPort) -> dict:
            units = tx.rows("SELECT * FROM propertyai.v_rent_reference_units ORDER BY property_id,rental_unit_id")
            accounts = tx.rows("SELECT * FROM propertyai.v_rent_accounts ORDER BY account_id")
            return {
                "units":[{
                    "property_id":str(r["property_id"]),"rental_unit_id":str(r["rental_unit_id"]),
                    "property_name":r["property_name"],"unit_name":r["unit_name"],
                    "timezone_name":r["timezone_name"],
                    "active":bool(r["property_active"] and r["unit_active"]),
                } for r in units],
                "accounts":[{
                    "account_id":str(r["account_id"]),"display_name":r["display_name"],
                    "currency":r["currency"],"masked_identifier":r["masked_identifier"],
                    "active":r["active"],"version":str(r["version"]),
                } for r in accounts],
            }
        return self.repository.read_snapshot(read)

    def admin_reference_data(self) -> dict:
        """W2-A WRITE-admin snapshot with the current ledger token for a following command."""
        def read(tx: RentTransactionPort) -> dict:
            units = tx.rows("SELECT * FROM propertyai.v_rent_reference_units ORDER BY property_id,rental_unit_id")
            accounts = tx.rows("SELECT * FROM propertyai.v_rent_accounts ORDER BY account_id")
            return {
                "ledger_revision":str(tx.ledger_revision_before),
                "units":[{
                    "property_id":str(r["property_id"]),"rental_unit_id":str(r["rental_unit_id"]),
                    "property_name":r["property_name"],"unit_name":r["unit_name"],
                    "timezone_name":r["timezone_name"],"active":bool(r["property_active"] and r["unit_active"]),
                } for r in units],
                "accounts":[{
                    "account_id":str(r["account_id"]),"display_name":r["display_name"],
                    "currency":r["currency"],"masked_identifier":r["masked_identifier"],
                    "active":r["active"],"version":str(r["version"]),
                } for r in accounts],
            }
        return self.repository.read_operator_snapshot(read)

    def lookup_command(self, command_type: str, key: UUID) -> dict:
        found = self.repository.lookup(command_type,key)
        return {"status":"FOUND" if found else "NOT_FOUND","command":found}

    def get_command(self, command_id: UUID) -> dict:
        rows = self.repository.read_rows(
            "SELECT propertyai.rent_command_by_id(propertyai.rent_visible_org(),%s) AS command",
            (command_id,),
        )
        found = rows[0]["command"] if rows else None
        return {"status":"FOUND" if found else "NOT_FOUND","command":found}

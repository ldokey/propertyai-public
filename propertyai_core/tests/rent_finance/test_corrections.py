from __future__ import annotations

from datetime import date
from threading import Barrier, Thread
from uuid import UUID, uuid4

import psycopg
import pytest

from propertyai_core.adapters.postgres.rent_repository import RentPostgresRepository, RentPostgresTransaction
from propertyai_core.application.commands.rent import RentCommand
from propertyai_core.application.handlers.rent import BusinessDateProvider, RentService
from propertyai_core.application.rent_errors import RentError
from propertyai_core.domain.finance import BIGINT_MAX, FinanceError, adjusted_obligation, signed_money
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import create_account, create_resident, entity, receipt, run


def _command(service, operation, body, *, target=None, key=None):
    return service.handle(RentCommand(operation, body, key or uuid4(), target))


def _contract_with_rent(service, ids, resident_id, revision, monthly_rent="500000"):
    body = {
        "rental_unit_id": str(ids["unit_id"]),
        "starts_on": "2026-09-01",
        "ends_on_exclusive": "2026-10-01",
        "lifecycle": "ACTIVE",
        "readiness": "READY",
        "resident_ids": [resident_id],
        "contract_parties": [],
        "term": {
            "valid_from": "2026-09-01",
            "valid_to_exclusive": "2026-10-01",
            "monthly_rent": monthly_rent,
            "amount_confirmed": True,
            "due_day": 5,
            "cycle_confirmed": True,
            "cycle_rule": {
                "schema_version": 1,
                "mode": "EXPLICIT_PERIODS",
                "first_due_month": "2026-10",
                "confirmation_ref": "W1-A synthetic",
            },
            "policy_version": "RENT_APPROVED_1G_2G_3G_4G_V1",
        },
        "billing_periods": [{
            "cycle_start": "2026-09-01",
            "cycle_end_exclusive": "2026-10-01",
            "due_month": "2026-10",
            "confirmation_ref": "W1-A synthetic",
        }],
        "previous_contract_id": None,
        "reason": "W1-A synthetic",
        "expected_ledger_revision": revision,
        "occupancies": [],
    }
    result = run(service, "createContract", body)
    return {
        "contract_id": entity(result, "CONTRACT")["id"],
        "period_id": entity(result, "PERIOD")["id"],
        "term_id": entity(result, "TERM")["id"],
        "version": entity(result, "CONTRACT")["version"],
        "revision": result["ledger_revision"],
    }


def _issue(service, contract):
    preview = service.preview_charge({"contract_id": contract["contract_id"], "period_id": contract["period_id"]})
    body = {
        "contract_id": contract["contract_id"],
        "period_id": contract["period_id"],
        "expected_contract_version": preview["contract_version"],
        "expected_term_versions": preview["term_versions"],
        "expected_ledger_revision": preview["ledger_revision"],
        "calculation_sha256": preview["calculation_sha256"],
        "issuance_mode": "OPERATOR_CONFIRMED",
        "replaces_receivable_id": None,
    }
    result = run(service, "issueRent", body)
    return entity(result, "RECEIVABLE")["id"], result


def _setup_finance(service, ids, *, receipt_amount=None, allocation_amount=None):
    resident_id, revision, _ = create_resident(service)
    account_id, revision, _ = create_account(service, revision)
    contract = _contract_with_rent(service, ids, resident_id, revision)
    receivable_id, issued = _issue(service, contract)
    state = {
        "account_id": account_id,
        "contract_id": contract["contract_id"],
        "receivable_id": receivable_id,
        "receivable_version": issued["result"]["receivable_balances"][0]["version"],
        "ledger_revision": issued["ledger_revision"],
        "source_id": None,
        "source_version": None,
        "movement_id": None,
        "movement_version": None,
        "allocation_id": None,
    }
    if receipt_amount is not None:
        allocations = []
        if allocation_amount is not None:
            allocations = [{
                "receivable_id": receivable_id,
                "amount": allocation_amount,
                "expected_version": state["receivable_version"],
                "attribution_confirmed": True,
                "override_attribution_reason": None,
            }]
        source_id, payment = receipt(
            service,
            account_id,
            state["ledger_revision"],
            ids,
            contract["contract_id"],
            amount=receipt_amount,
            allocations=allocations,
        )
        state.update({
            "source_id": source_id,
            "source_version": payment["result"]["source_balances"][0]["version"],
            "movement_id": entity(payment, "MOVEMENT")["id"],
            "movement_version": entity(payment, "MOVEMENT")["version"],
            "ledger_revision": payment["ledger_revision"],
        })
        if payment["result"]["receivable_balances"]:
            state["receivable_version"] = payment["result"]["receivable_balances"][0]["version"]
        allocation_entities = [e for e in payment["result"]["entities"] if e["kind"] == "ALLOCATION"]
        if allocation_entities:
            state["allocation_id"] = allocation_entities[-1]["id"]
    return state


def _empty_plan():
    return {
        "reverse_allocation_ids": [],
        "replacement_allocations": [],
        "source_versions": [],
        "receivable_versions": [],
    }


def _replacement_plan(state, amount, *, allocation_id=None, source_version=None, receivable_version=None):
    return {
        "reverse_allocation_ids": [allocation_id or state["allocation_id"]],
        "replacement_allocations": [{
            "source_id": state["source_id"],
            "allocation": {
                "receivable_id": state["receivable_id"],
                "amount": amount,
                "expected_version": receivable_version or state["receivable_version"],
                "attribution_confirmed": True,
                "override_attribution_reason": None,
            },
        }],
        "source_versions": [{
            "id": state["source_id"],
            "expected_version": source_version or state["source_version"],
        }],
        "receivable_versions": [{
            "id": state["receivable_id"],
            "expected_version": receivable_version or state["receivable_version"],
        }],
    }


def _movement_values(state, amount, *, payer="Synthetic payer"):
    return {
        "account_id": state["account_id"],
        "occurred_on": "2026-09-28",
        "amount": amount,
        "payer_raw": payer,
        "counterparty_party_id": None,
    }


def _assert_error(code, callable_):
    with pytest.raises(RentError) as captured:
        callable_()
    assert captured.value.code == code


def test_signed_money_and_correction_arithmetic_boundaries():
    assert signed_money("1") == 1
    assert signed_money("-1") == -1
    assert signed_money(str(BIGINT_MAX)) == BIGINT_MAX
    assert signed_money(str(-BIGINT_MAX)) == -BIGINT_MAX
    for invalid in ("0", "-0", "+1", "01", "1.0", "", 1, 1.0, str(BIGINT_MAX + 1), str(-BIGINT_MAX - 1)):
        with pytest.raises(FinanceError):
            signed_money(invalid)
    assert adjusted_obligation(500000, -100000) == 400000
    with pytest.raises(FinanceError):
        adjusted_obligation(1, -2)


def test_representative_500k_to_400k_replay_conflict_and_explainability():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        state = _setup_finance(service, ids, receipt_amount="500000", allocation_amount="500000")
        key = uuid4()
        body = {
            "delta": "-100000",
            "reason": "Reduce confirmed obligation to 400000",
            "expected_version": state["receivable_version"],
            "expected_ledger_revision": state["ledger_revision"],
            "allocation_correction": _replacement_plan(state, "400000"),
            "reverse_adjustment_id": None,
        }
        committed, replayed = _command(
            service, "adjustReceivable", body, target=UUID(state["receivable_id"]), key=key
        )
        assert replayed is False
        balance = committed["result"]["receivable_balances"][0]
        source = committed["result"]["source_balances"][0]
        assert balance == {
            "receivable_id": state["receivable_id"],
            "effective_amount": "400000",
            "allocated": "400000",
            "balance": "0",
            "version": balance["version"],
        }
        assert source["principal"] == "500000"
        assert source["allocated"] == "400000"
        assert source["available"] == "100000"

        original = service.repository.read_rows(
            "SELECT original_amount,voided FROM propertyai.finance_receivable WHERE receivable_id=%s",
            (UUID(state["receivable_id"]),),
        )[0]
        assert original["original_amount"] == 500000 and original["voided"] is False
        adjustments = service.repository.read_rows(
            "SELECT delta,reverse_of FROM propertyai.finance_receivable_adjustment WHERE receivable_id=%s",
            (UUID(state["receivable_id"]),),
        )
        assert [(r["delta"], r["reverse_of"]) for r in adjustments] == [(-100000, None)]
        allocations = service.repository.read_rows(
            "SELECT record_kind,amount,reverse_of FROM propertyai.finance_allocation WHERE receivable_id=%s",
            (UUID(state["receivable_id"]),),
        )
        assert sorted((r["record_kind"], r["amount"]) for r in allocations) == [
            ("APPLY", 400000), ("APPLY", 500000), ("REVERSE", 500000)
        ]
        assert sum(r["amount"] for r in allocations if r["record_kind"] == "REVERSE") == 500000

        lookup = service.lookup_command("ADJUST_RECEIVABLE", key)
        assert lookup["status"] == "FOUND" and lookup["command"] == committed
        replay, was_replayed = _command(
            service, "adjustReceivable", body, target=UUID(state["receivable_id"]), key=key
        )
        assert was_replayed is True and replay == committed
        assert len(service.repository.read_rows(
            "SELECT adjustment_id FROM propertyai.finance_receivable_adjustment WHERE receivable_id=%s",
            (UUID(state["receivable_id"]),),
        )) == 1
        conflict = dict(body, reason="Different payload")
        _assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: _command(service, "adjustReceivable", conflict, target=UUID(state["receivable_id"]), key=key),
        )


def test_adjustment_increase_reverse_stale_and_void():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        state = _setup_finance(service, ids)
        invalid_decrease = {
            "delta": "-600000",
            "reason": "Reject negative obligation",
            "expected_version": state["receivable_version"],
            "expected_ledger_revision": state["ledger_revision"],
            "allocation_correction": _empty_plan(),
            "reverse_adjustment_id": None,
        }
        _assert_error(
            "VALIDATION_ERROR",
            lambda: _command(service, "adjustReceivable", invalid_decrease, target=UUID(state["receivable_id"])),
        )
        increase = {
            "delta": "100000",
            "reason": "Increase synthetic obligation",
            "expected_version": state["receivable_version"],
            "expected_ledger_revision": state["ledger_revision"],
            "allocation_correction": _empty_plan(),
            "reverse_adjustment_id": None,
        }
        raised, _ = _command(service, "adjustReceivable", increase, target=UUID(state["receivable_id"]))
        assert raised["result"]["receivable_balances"][0]["effective_amount"] == "600000"
        adjustment_id = entity(raised, "ADJUSTMENT")["id"]
        raised_version = raised["result"]["receivable_balances"][0]["version"]

        reverse = {
            "delta": "-100000",
            "reason": "Reverse erroneous increase",
            "expected_version": raised_version,
            "expected_ledger_revision": raised["ledger_revision"],
            "allocation_correction": _empty_plan(),
            "reverse_adjustment_id": adjustment_id,
        }
        restored, _ = _command(service, "adjustReceivable", reverse, target=UUID(state["receivable_id"]))
        restored_balance = restored["result"]["receivable_balances"][0]
        assert restored_balance["effective_amount"] == "500000"
        rows = service.repository.read_rows(
            "SELECT adjustment_id,delta,reverse_of FROM propertyai.finance_receivable_adjustment WHERE receivable_id=%s ORDER BY created_at",
            (UUID(state["receivable_id"]),),
        )
        assert len(rows) == 2 and rows[1]["reverse_of"] == UUID(adjustment_id)

        stale = dict(increase, expected_ledger_revision=restored["ledger_revision"])
        _assert_error(
            "VERSION_CONFLICT",
            lambda: _command(service, "adjustReceivable", stale, target=UUID(state["receivable_id"])),
        )
        void_body = {
            "expected_version": restored_balance["version"],
            "expected_ledger_revision": restored["ledger_revision"],
            "allocation_correction": _empty_plan(),
            "reason": "Void duplicate synthetic charge",
        }
        voided, _ = _command(service, "voidReceivable", void_body, target=UUID(state["receivable_id"]))
        assert voided["result"]["receivable_balances"][0]["effective_amount"] == "0"
        assert service.repository.read_rows(
            "SELECT voided,void_reason,original_amount FROM propertyai.finance_receivable WHERE receivable_id=%s",
            (UUID(state["receivable_id"]),),
        )[0] == {"voided": True, "void_reason": "Void duplicate synthetic charge", "original_amount": 500000}
        after_void = dict(increase,
                          expected_version=voided["result"]["receivable_balances"][0]["version"],
                          expected_ledger_revision=voided["ledger_revision"])
        _assert_error(
            "VERSION_CONFLICT",
            lambda: _command(service, "adjustReceivable", after_void, target=UUID(state["receivable_id"])),
        )


def test_allocation_correction_release_reallocation_stale_and_overallocation():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        state = _setup_finance(service, ids, receipt_amount="500000", allocation_amount="500000")
        body = {
            "plan": _replacement_plan(state, "300000"),
            "reason": "Correct allocation to 300000",
            "expected_ledger_revision": state["ledger_revision"],
        }
        corrected, _ = _command(service, "correctAllocations", body)
        assert corrected["result"]["receivable_balances"][0]["allocated"] == "300000"
        assert corrected["result"]["receivable_balances"][0]["balance"] == "200000"
        assert corrected["result"]["source_balances"][0]["available"] == "200000"
        active = service.repository.read_rows(
            "SELECT a.allocation_id FROM propertyai.finance_allocation a WHERE a.receivable_id=%s AND a.record_kind='APPLY' AND NOT EXISTS(SELECT 1 FROM propertyai.finance_allocation z WHERE z.reverse_of=a.allocation_id)",
            (UUID(state["receivable_id"]),),
        )
        assert len(active) == 1
        active_id = str(active[0]["allocation_id"])
        recv_version = corrected["result"]["receivable_balances"][0]["version"]
        source_version = corrected["result"]["source_balances"][0]["version"]

        stale_plan = _replacement_plan(
            state, "200000", allocation_id=active_id,
            source_version=state["source_version"], receivable_version=recv_version,
        )
        _assert_error(
            "VERSION_CONFLICT",
            lambda: _command(service, "correctAllocations", {
                "plan": stale_plan, "reason": "Stale source version",
                "expected_ledger_revision": corrected["ledger_revision"],
            }),
        )
        over_plan = _replacement_plan(
            state, "600000", allocation_id=active_id,
            source_version=source_version, receivable_version=recv_version,
        )
        _assert_error(
            "RECEIVABLE_OVERALLOCATED",
            lambda: _command(service, "correctAllocations", {
                "plan": over_plan, "reason": "Reject over allocation",
                "expected_ledger_revision": corrected["ledger_revision"],
            }),
        )
        overview = service.overview("2026-09")
        assert overview["selected_month_allocated"] == "300000"
        assert overview["selected_month_balance"] == "200000"


def test_movement_revision_reversal_and_reverse_replace():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        state = _setup_finance(service, ids, receipt_amount="500000")
        correction = {
            "action": "APPEND_CORRECTED_REVISION",
            "corrected_values": _movement_values(state, "600000"),
            "expected_version": state["movement_version"],
            "expected_ledger_revision": state["ledger_revision"],
            "expected_source_version": state["source_version"],
            "allocation_correction": _empty_plan(),
            "reason": "Correct recorded receipt amount",
            "replacement_receipt": None,
        }
        revised, _ = _command(service, "correctMovement", correction, target=UUID(state["movement_id"]))
        source_balance = revised["result"]["source_balances"][0]
        assert source_balance["principal"] == "600000" and source_balance["available"] == "600000"
        movement_entity = next(e for e in revised["result"]["entities"] if e["kind"] == "MOVEMENT")

        reverse = {
            "action": "REVERSE_RECORD",
            "corrected_values": None,
            "expected_version": movement_entity["version"],
            "expected_ledger_revision": revised["ledger_revision"],
            "expected_source_version": source_balance["version"],
            "allocation_correction": _empty_plan(),
            "reason": "Reverse erroneous receipt record",
            "replacement_receipt": None,
        }
        reversed_result, _ = _command(service, "correctMovement", reverse, target=UUID(state["movement_id"]))
        assert reversed_result["result"]["source_balances"][0]["principal"] == "0"

        source2, payment2 = receipt(
            service, state["account_id"], reversed_result["ledger_revision"], ids,
            state["contract_id"], amount="300000", allocations=[]
        )
        movement2 = entity(payment2, "MOVEMENT")
        source2_balance = payment2["result"]["source_balances"][0]
        replacement = {
            "account_id": state["account_id"],
            "occurred_on": "2026-09-28",
            "amount": "250000",
            "currency": "KRW",
            "payer_raw": "Replacement synthetic payer",
            "counterparty_party_id": None,
            "attribution": {
                "status": "CONTRACT_CONFIRMED",
                "property_id": str(ids["property_id"]),
                "contract_id": state["contract_id"],
            },
            "allocations": [],
        }
        replace_body = {
            "action": "REVERSE_AND_REPLACE",
            "corrected_values": None,
            "expected_version": movement2["version"],
            "expected_ledger_revision": payment2["ledger_revision"],
            "expected_source_version": source2_balance["version"],
            "allocation_correction": _empty_plan(),
            "reason": "Replace erroneous receipt record",
            "replacement_receipt": replacement,
        }
        replaced, _ = _command(service, "correctMovement", replace_body, target=UUID(movement2["id"]))
        balances = {row["source_id"]: row for row in replaced["result"]["source_balances"]}
        assert balances[source2]["principal"] == "0"
        replacement_sources = [row for sid, row in balances.items() if sid != source2]
        assert len(replacement_sources) == 1 and replacement_sources[0]["principal"] == "250000"
        linked = service.repository.read_rows(
            "SELECT movement_id,direction FROM propertyai.finance_movement WHERE replaces_movement_id=%s",
            (UUID(movement2["id"]),),
        )
        assert len(linked) == 1 and linked[0]["direction"] == "IN"


def test_refund_record_correction_reversal_replay_and_movement_protection():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        state = _setup_finance(service, ids, receipt_amount="500000")
        key = uuid4()
        refund_body = {
            "source_id": state["source_id"],
            "expected_source_version": state["source_version"],
            "expected_ledger_revision": state["ledger_revision"],
            "out_account_id": state["account_id"],
            "occurred_on": "2026-09-28",
            "amount": "100000",
            "payee_raw": "Synthetic payee",
            "actual_transfer_confirmed": True,
            "reason": "Record already-confirmed synthetic refund",
        }
        refund, replayed = _command(service, "recordRefund", refund_body, key=key)
        assert replayed is False
        returned = entity(refund, "RETURN")
        outgoing = entity(refund, "MOVEMENT")
        source_balance = refund["result"]["source_balances"][0]
        assert source_balance["returned"] == "100000" and source_balance["available"] == "400000"
        replay, was_replayed = _command(service, "recordRefund", refund_body, key=key)
        assert was_replayed is True and replay == refund
        _assert_error(
            "IDEMPOTENCY_CONFLICT",
            lambda: _command(service, "recordRefund", dict(refund_body, reason="different"), key=key),
        )

        reverse_receipt = {
            "action": "REVERSE_RECORD",
            "corrected_values": None,
            "expected_version": state["movement_version"],
            "expected_ledger_revision": refund["ledger_revision"],
            "expected_source_version": source_balance["version"],
            "allocation_correction": _empty_plan(),
            "reason": "Must not erase real refund relation",
            "replacement_receipt": None,
        }
        _assert_error(
            "ALLOCATION_EXCEEDS_AVAILABLE",
            lambda: _command(service, "correctMovement", reverse_receipt, target=UUID(state["movement_id"])),
        )

        correct_refund = {
            "outgoing_movement_id": outgoing["id"],
            "expected_movement_version": outgoing["version"],
            "source_versions": [{"id": state["source_id"], "expected_version": source_balance["version"]}],
            "reverse_return_ids": [returned["id"]],
            "replacement_returns": [{"source_id": state["source_id"], "amount": "80000"}],
            "corrected_values": _movement_values(state, "80000", payer="Synthetic payee"),
            "action": "APPEND_CORRECTED_REVISION",
            "actual_record_correction_confirmed": True,
            "reason": "Correct refund record to 80000",
            "expected_ledger_revision": refund["ledger_revision"],
        }
        _assert_error(
            "VALIDATION_ERROR",
            lambda: _command(
                service,
                "correctRefund",
                dict(correct_refund, action=[]),
                target=UUID(returned["id"]),
            ),
        )
        corrected, _ = _command(service, "correctRefund", correct_refund, target=UUID(returned["id"]))
        corrected_source = corrected["result"]["source_balances"][0]
        assert corrected_source["returned"] == "80000" and corrected_source["available"] == "420000"

        refund2_body = dict(
            refund_body,
            expected_source_version=corrected_source["version"],
            expected_ledger_revision=corrected["ledger_revision"],
            amount="50000",
            reason="Second confirmed synthetic refund",
        )
        refund2, _ = _command(service, "recordRefund", refund2_body)
        return2 = entity(refund2, "RETURN")
        movement2 = entity(refund2, "MOVEMENT")
        source_after2 = refund2["result"]["source_balances"][0]
        reverse_refund = {
            "outgoing_movement_id": movement2["id"],
            "expected_movement_version": movement2["version"],
            "source_versions": [{"id": state["source_id"], "expected_version": source_after2["version"]}],
            "reverse_return_ids": [return2["id"]],
            "replacement_returns": [],
            "corrected_values": None,
            "action": "REVERSE_RECORD",
            "actual_record_correction_confirmed": True,
            "reason": "Reverse erroneous refund record only",
            "expected_ledger_revision": refund2["ledger_revision"],
        }
        reversed_refund, _ = _command(service, "correctRefund", reverse_refund, target=UUID(return2["id"]))
        assert reversed_refund["result"]["source_balances"][0]["returned"] == "80000"
        out_rows = service.repository.read_rows(
            "SELECT direction,origin FROM propertyai.finance_movement WHERE movement_id IN (%s,%s)",
            (UUID(outgoing["id"]), UUID(movement2["id"])),
        )
        assert out_rows and all(row["direction"] == "OUT" and row["origin"] == "MANUAL" for row in out_rows)


def test_action_type_validation_is_sanitized_validation_error():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        state = _setup_finance(service, ids, receipt_amount="500000")
        bad = {
            "action": [],
            "corrected_values": None,
            "expected_version": state["movement_version"],
            "expected_ledger_revision": state["ledger_revision"],
            "expected_source_version": state["source_version"],
            "allocation_correction": _empty_plan(),
            "reason": "Invalid action type",
            "replacement_receipt": None,
        }
        _assert_error(
            "VALIDATION_ERROR",
            lambda: _command(service, "correctMovement", bad, target=UUID(state["movement_id"])),
        )


def test_adjustment_fault_rolls_back_reverse_adjustment_and_reallocation(monkeypatch):
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        state = _setup_finance(service, ids, receipt_amount="500000", allocation_amount="500000")
        original_insert = RentPostgresTransaction.insert

        def fail_after_adjustment(self, table, values):
            original_insert(self, table, values)
            if table == "finance_receivable_adjustment":
                raise RuntimeError("SYNTHETIC_W1_A_FAULT")

        monkeypatch.setattr(RentPostgresTransaction, "insert", fail_after_adjustment)
        key = uuid4()
        body = {
            "delta": "-100000",
            "reason": "Rollback synthetic fault",
            "expected_version": state["receivable_version"],
            "expected_ledger_revision": state["ledger_revision"],
            "allocation_correction": _replacement_plan(state, "400000"),
            "reverse_adjustment_id": None,
        }
        _assert_error(
            "INTERNAL_ERROR",
            lambda: _command(service, "adjustReceivable", body, target=UUID(state["receivable_id"]), key=key),
        )
        assert service.lookup_command("ADJUST_RECEIVABLE", key)["status"] == "NOT_FOUND"
        assert service.repository.read_rows(
            "SELECT adjustment_id FROM propertyai.finance_receivable_adjustment WHERE receivable_id=%s",
            (UUID(state["receivable_id"]),),
        ) == []
        allocations = service.repository.read_rows(
            "SELECT record_kind,amount FROM propertyai.finance_allocation WHERE receivable_id=%s",
            (UUID(state["receivable_id"]),),
        )
        assert [(row["record_kind"], row["amount"]) for row in allocations] == [("APPLY", 500000)]
        overview = service.overview("2026-09")
        assert overview["selected_month_obligation"] == "500000"
        assert overview["selected_month_allocated"] == "500000"
        assert overview["selected_month_balance"] == "0"


def _race(service, operation, body, target, barrier, out):
    barrier.wait()
    try:
        value, _ = _command(service, operation, body, target=target)
        out.append(("ok", value))
    except RentError as exc:
        out.append(("err", exc.code))


def test_concurrent_allocation_vs_adjustment_preserves_invariants():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        state = _setup_finance(service, ids, receipt_amount="500000")
        allocate = {
            "source_id": state["source_id"],
            "expected_source_version": state["source_version"],
            "expected_ledger_revision": state["ledger_revision"],
            "allocations": [{
                "receivable_id": state["receivable_id"],
                "amount": "300000",
                "expected_version": state["receivable_version"],
                "attribution_confirmed": True,
                "override_attribution_reason": None,
            }],
            "attribution": {
                "status": "CONTRACT_CONFIRMED",
                "property_id": str(ids["property_id"]),
                "contract_id": state["contract_id"],
            },
        }
        adjust = {
            "delta": "-100000",
            "reason": "Concurrent synthetic correction",
            "expected_version": state["receivable_version"],
            "expected_ledger_revision": state["ledger_revision"],
            "allocation_correction": _empty_plan(),
            "reverse_adjustment_id": None,
        }
        barrier = Barrier(2)
        out = []
        threads = [
            Thread(target=_race, args=(service, "allocate", allocate, None, barrier, out)),
            Thread(target=_race, args=(service, "adjustReceivable", adjust, UUID(state["receivable_id"]), barrier, out)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(kind for kind, _ in out) == ["err", "ok"]
        assert next(value for kind, value in out if kind == "err") in {"VERSION_CONFLICT", "RETRYABLE_TRANSACTION"}
        row = service.overview("2026-09")
        assert int(row["selected_month_balance"]) >= 0
        source = service.funding_sources()["rows"][0]
        assert int(source["available"]) >= 0
        assert int(source["allocated"]) + int(source["returned"]) <= int(source["principal"])


def test_concurrent_correction_vs_correction_preserves_single_winner():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        state = _setup_finance(service, ids)
        first = {
            "delta": "100000",
            "reason": "Concurrent correction A",
            "expected_version": state["receivable_version"],
            "expected_ledger_revision": state["ledger_revision"],
            "allocation_correction": _empty_plan(),
            "reverse_adjustment_id": None,
        }
        second = dict(first, delta="-100000", reason="Concurrent correction B")
        barrier = Barrier(2)
        out = []
        threads = [
            Thread(target=_race, args=(service, "adjustReceivable", first, UUID(state["receivable_id"]), barrier, out)),
            Thread(target=_race, args=(service, "adjustReceivable", second, UUID(state["receivable_id"]), barrier, out)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(kind for kind, _ in out) == ["err", "ok"]
        assert next(value for kind, value in out if kind == "err") in {"VERSION_CONFLICT", "RETRYABLE_TRANSACTION"}
        row = service.overview("2026-09")
        assert row["selected_month_obligation"] in {"400000", "600000"}
        adjustments = service.repository.read_rows(
            "SELECT delta FROM propertyai.finance_receivable_adjustment WHERE receivable_id=%s",
            (UUID(state["receivable_id"]),),
        )
        assert len(adjustments) == 1


def test_concurrent_refund_vs_movement_reversal_preserves_invariants():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        state = _setup_finance(service, ids, receipt_amount="500000")
        refund = {
            "source_id": state["source_id"],
            "expected_source_version": state["source_version"],
            "expected_ledger_revision": state["ledger_revision"],
            "out_account_id": state["account_id"],
            "occurred_on": "2026-09-28",
            "amount": "100000",
            "payee_raw": "Race payee",
            "actual_transfer_confirmed": True,
            "reason": "Race refund",
        }
        reverse = {
            "action": "REVERSE_RECORD",
            "corrected_values": None,
            "expected_version": state["movement_version"],
            "expected_ledger_revision": state["ledger_revision"],
            "expected_source_version": state["source_version"],
            "allocation_correction": _empty_plan(),
            "reason": "Race reversal",
            "replacement_receipt": None,
        }
        barrier = Barrier(2)
        out = []
        threads = [
            Thread(target=_race, args=(service, "recordRefund", refund, None, barrier, out)),
            Thread(target=_race, args=(service, "correctMovement", reverse, UUID(state["movement_id"]), barrier, out)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(kind for kind, _ in out) == ["err", "ok"]
        assert next(value for kind, value in out if kind == "err") in {"VERSION_CONFLICT", "RETRYABLE_TRANSACTION"}
        source = service.funding_sources()["rows"][0]
        assert int(source["available"]) >= 0
        assert int(source["allocated"]) + int(source["returned"]) <= int(source["principal"])


def test_cross_org_resource_is_hidden_and_server_org_binding_fails_closed():
    with operator_fixture(today=date(2026, 9, 28)) as (fixture, service, ids):
        state1 = _setup_finance(service, ids)
        suffix = uuid4().hex[:10]
        org2, property2, unit2, party2 = (uuid4() for _ in range(4))
        login2 = "rent_cross_" + suffix
        fixture.cluster.psql(sql_text=f"""
            CREATE ROLE {login2} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            GRANT propertyai_rent_runtime TO {login2};
            INSERT INTO propertyai.organization(organization_id,organization_code,display_name,organization_status,data_environment)
            VALUES('{org2}','W1A-ORG-{suffix}','Cross Org','ACTIVE','TEST');
            INSERT INTO propertyai.property(property_id,organization_id,property_code,display_name,timezone_name)
            VALUES('{property2}','{org2}','W1A-PROP-{suffix}','Cross Property','Asia/Seoul');
            INSERT INTO propertyai.rental_unit(rental_unit_id,rental_unit_code,property_id,display_name)
            VALUES('{unit2}','W1A-UNIT-{suffix}','{property2}','Cross Unit');
            INSERT INTO propertyai.party(party_id,party_code,display_name,data_environment)
            VALUES('{party2}','W1A-ACTOR-{suffix}','Cross Operator','TEST');
            INSERT INTO propertyai.organization_member(organization_member_id,organization_id,party_id,membership_role,membership_status,joined_at)
            VALUES('{uuid4()}','{org2}','{party2}','OPERATOR','ACTIVE',transaction_timestamp());
            INSERT INTO propertyai.finance_ledger_scope(organization_id,revision) VALUES('{org2}',0);
            INSERT INTO propertyai.rent_runtime_binding(login_name,organization_id,actor_party_id,principal_type,capability,data_environment,enabled)
            VALUES('{login2}','{org2}','{party2}','PARTY','WRITE','TEST',true);
        """)
        repo2 = RentPostgresRepository(lambda: psycopg.connect(fixture.cluster.login_dsn(login2)))
        service2 = RentService(repo2, BusinessDateProvider(lambda _: date(2026, 9, 28)))
        ids2 = {"organization_id": org2, "property_id": property2, "unit_id": unit2, "party_id": party2, "login": login2}
        state2 = _setup_finance(service2, ids2)

        cross_body = {
            "delta": "1",
            "reason": "Cross org must remain hidden",
            "expected_version": state2["receivable_version"],
            "expected_ledger_revision": state1["ledger_revision"],
            "allocation_correction": _empty_plan(),
            "reverse_adjustment_id": None,
        }
        _assert_error(
            "NOT_FOUND",
            lambda: _command(service, "adjustReceivable", cross_body, target=UUID(state2["receivable_id"])),
        )

        mismatched = RentPostgresRepository(
            lambda: psycopg.connect(fixture.cluster.login_dsn(ids["login"])),
            authorized_organization_id=org2,
            authorized_actor_party_id=ids["party_id"],
        )
        _assert_error("NOT_AUTHORIZED", lambda: mismatched.read_rows("SELECT 1 AS n"))

        client_authority = dict(
            cross_body,
            organization_id=str(ids["organization_id"]),
            actor_party_id=str(ids["party_id"]),
        )
        _assert_error(
            "VALIDATION_ERROR",
            lambda: RentCommand("adjustReceivable", client_authority, uuid4(), UUID(state1["receivable_id"])),
        )

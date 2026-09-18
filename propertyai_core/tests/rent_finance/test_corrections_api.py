from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from propertyai_core.application.commands.rent import RentCommand
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import (
    create_account, create_contract, create_resident, entity, issue, receipt,
)
from propertyai_core.web.rent_finance_corrections import (
    FinanceCorrectionContext,
    RentFinanceCorrectionsAPI,
)


@dataclass(frozen=True)
class Principal:
    organization_id: UUID
    actor_party_id: UUID
    subject: str
    capabilities: frozenset[str]


class FakeService:
    def __init__(self, principal: Principal):
        self.repository = SimpleNamespace(
            authorized_organization_id=principal.organization_id,
            authorized_actor_party_id=principal.actor_party_id,
        )
        self.commands = []

    def handle(self, command: RentCommand):
        self.commands.append(command)
        return {
            "schema_version": 1,
            "command_id": str(uuid4()),
            "ledger_revision": "1",
            "recorded_at": "2026-09-17T00:00:00+00:00",
            "result": {"outcome": "UPDATED", "entities": [], "receivable_balances": [], "source_balances": []},
        }, False


def _principal(*, write=True):
    return Principal(
        organization_id=uuid4(),
        actor_party_id=uuid4(),
        subject="synthetic:w1-a",
        capabilities=frozenset({"WRITE"} if write else {"READ"}),
    )


def _headers(key=None):
    return {
        "Content-Type": "application/json; charset=utf-8",
        "X-CSRF-Token": "csrf-w1-a",
        "Idempotency-Key": str(key or uuid4()),
    }


def _empty_plan():
    return {
        "reverse_allocation_ids": [],
        "replacement_allocations": [],
        "source_versions": [],
        "receivable_versions": [],
    }


def _body(operation):
    if operation == "adjustReceivable":
        return {"delta": "1", "reason": "Synthetic", "expected_version": "1", "expected_ledger_revision": "0",
                "allocation_correction": _empty_plan(), "reverse_adjustment_id": None}
    if operation == "voidReceivable":
        return {"expected_version": "1", "expected_ledger_revision": "0", "allocation_correction": _empty_plan(),
                "reason": "Synthetic"}
    if operation == "correctAllocations":
        return {"plan": _empty_plan(), "reason": "Synthetic", "expected_ledger_revision": "0"}
    if operation == "correctMovement":
        return {"action": "REVERSE_RECORD", "corrected_values": None, "expected_version": "1",
                "expected_ledger_revision": "0", "expected_source_version": "1",
                "allocation_correction": _empty_plan(), "reason": "Synthetic", "replacement_receipt": None}
    if operation == "recordRefund":
        return {"source_id": str(uuid4()), "expected_source_version": "1", "expected_ledger_revision": "0",
                "out_account_id": str(uuid4()), "occurred_on": "2026-09-17", "amount": "1", "payee_raw": None,
                "actual_transfer_confirmed": True, "reason": "Synthetic"}
    if operation == "correctRefund":
        return {"outgoing_movement_id": str(uuid4()), "expected_movement_version": "1", "source_versions": [],
                "reverse_return_ids": [str(uuid4())], "replacement_returns": [], "corrected_values": None,
                "action": "REVERSE_RECORD", "actual_record_correction_confirmed": True,
                "reason": "Synthetic", "expected_ledger_revision": "0"}
    raise AssertionError(operation)


@pytest.mark.parametrize(
    ("path", "operation", "has_target"),
    [
        (lambda target: f"/api/v1/receivables/{target}/adjustments", "adjustReceivable", True),
        (lambda target: f"/api/v1/receivables/{target}/void", "voidReceivable", True),
        (lambda target: "/api/v1/allocation-corrections", "correctAllocations", False),
        (lambda target: f"/api/v1/movements/{target}/corrections", "correctMovement", True),
        (lambda target: "/api/v1/refund-records", "recordRefund", False),
        (lambda target: f"/api/v1/refund-records/{target}/corrections", "correctRefund", True),
    ],
)
def test_all_six_correction_routes_dispatch_exact_commands(path, operation, has_target):
    principal = _principal()
    service = FakeService(principal)
    api = RentFinanceCorrectionsAPI()
    context = FinanceCorrectionContext(service=service, csrf_token="csrf-w1-a", principal=principal)
    target = uuid4()
    key = uuid4()
    response = api.handle(context, "POST", path(target), _headers(key), json.dumps(_body(operation)).encode())
    assert response is not None and response.status == 200
    assert response.headers["X-Idempotent-Replayed"] == "false"
    assert len(service.commands) == 1
    command = service.commands[0]
    assert command.operation_id == operation
    assert command.idempotency_key == key
    assert command.target_id == (target if has_target else None)


def test_api_rejects_client_authority_invalid_headers_and_untrusted_context():
    principal = _principal()
    service = FakeService(principal)
    api = RentFinanceCorrectionsAPI()
    context = FinanceCorrectionContext(service=service, csrf_token="csrf-w1-a", principal=principal)
    target = uuid4()
    path = f"/api/v1/receivables/{target}/adjustments"

    body = dict(_body("adjustReceivable"), organization_id=str(principal.organization_id))
    response = api.handle(context, "POST", path, _headers(), json.dumps(body).encode())
    assert response.status == 422 and response.body["code"] == "VALIDATION_ERROR"
    assert service.commands == []

    bad_key = _headers()
    bad_key["Idempotency-Key"] = "not-a-uuid"
    response = api.handle(context, "POST", path, bad_key, json.dumps(_body("adjustReceivable")).encode())
    assert response.status == 422 and response.body["code"] == "VALIDATION_ERROR"

    response = api.handle(
        context,
        "POST",
        "/api/v1/receivables/not-a-uuid/adjustments",
        _headers(),
        json.dumps(_body("adjustReceivable")).encode(),
    )
    assert response.status == 422 and response.body["code"] == "VALIDATION_ERROR"

    bad_csrf = _headers()
    bad_csrf["X-CSRF-Token"] = "wrong"
    response = api.handle(context, "POST", path, bad_csrf, json.dumps(_body("adjustReceivable")).encode())
    assert response.status == 403 and response.body["code"] == "NOT_AUTHORIZED"

    read_principal = _principal(write=False)
    read_service = FakeService(read_principal)
    response = api.handle(
        FinanceCorrectionContext(read_service, "csrf-w1-a", read_principal),
        "POST", path, _headers(), json.dumps(_body("adjustReceivable")).encode(),
    )
    assert response.status == 403 and response.body["code"] == "NOT_AUTHORIZED"

    mismatched = _principal()
    response = api.handle(
        FinanceCorrectionContext(service, "csrf-w1-a", mismatched),
        "POST", path, _headers(), json.dumps(_body("adjustReceivable")).encode(),
    )
    assert response.status == 403 and response.body["code"] == "NOT_AUTHORIZED"

    assert api.handle(context, "GET", "/api/v1/rent/overview", {}, b"") is None


def test_disposable_postgres_api_replay_and_action_type_validation():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        resident, revision, _ = create_resident(service)
        account, revision, _ = create_account(service, revision)
        contract = create_contract(service, ids, [resident], revision)
        receivable, issued, _, _ = issue(service, contract)
        source, payment = receipt(
            service, account, issued["ledger_revision"], ids, contract["contract_id"],
            amount="500000", allocations=[]
        )
        principal = Principal(ids["organization_id"], ids["party_id"], "synthetic:w1-a", frozenset({"WRITE"}))
        api = RentFinanceCorrectionsAPI()
        context = FinanceCorrectionContext(service, "csrf-w1-a", principal)

        adjust = {
            "delta": "1000",
            "reason": "API synthetic adjustment",
            "expected_version": issued["result"]["receivable_balances"][0]["version"],
            "expected_ledger_revision": payment["ledger_revision"],
            "allocation_correction": _empty_plan(),
            "reverse_adjustment_id": None,
        }
        key = uuid4()
        path = f"/api/v1/receivables/{receivable}/adjustments"
        first = api.handle(context, "POST", path, _headers(key), json.dumps(adjust).encode())
        assert first.status == 200 and first.headers["X-Idempotent-Replayed"] == "false"
        second = api.handle(context, "POST", path, _headers(key), json.dumps(adjust).encode())
        assert second.status == 200 and second.body == first.body
        assert second.headers["X-Idempotent-Replayed"] == "true"

        movement = entity(payment, "MOVEMENT")
        source_balance = payment["result"]["source_balances"][0]
        bad_action = {
            "action": [],
            "corrected_values": None,
            "expected_version": movement["version"],
            "expected_ledger_revision": first.body["ledger_revision"],
            "expected_source_version": source_balance["version"],
            "allocation_correction": _empty_plan(),
            "reason": "Reject non-string action",
            "replacement_receipt": None,
        }
        bad = api.handle(
            context,
            "POST",
            f"/api/v1/movements/{movement['id']}/corrections",
            _headers(),
            json.dumps(bad_action).encode(),
        )
        assert bad.status == 422 and bad.body["code"] == "VALIDATION_ERROR"

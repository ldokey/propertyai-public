from __future__ import annotations

from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
from threading import Thread
from uuid import UUID, uuid4

import psycopg
import pytest

from propertyai_core.application.commands.rent import RentCommand
from propertyai_core.application.handlers.rent import BusinessDateProvider
from propertyai_core.application.rent_errors import RentError
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import create_contract, create_resident, entity, receipt, run
from propertyai_core.web.auth_context import AuthorizedPrincipal
from propertyai_core.web.rent_integration import (
    PrincipalRentServiceResolver,
    RentDatabaseBinding,
    StaticRentDatabaseBindings,
)
from propertyai_core.web.rent_longstay_admin import LongstayAdminContext, RentLongstayAdmin


REPO_ROOT = Path(__file__).resolve().parents[3]
S02 = REPO_ROOT / "propertyai_core/web/s02_contract_residency.html"
S08 = REPO_ROOT / "propertyai_core/web/s08_master_data.html"
CSRF = "synthetic-w2-a-csrf"
POLICY = "RENT_APPROVED_1G_2G_3G_4G_V1"


def _bound_service(fixture, ids, capabilities=frozenset({"READ", "WRITE"}), *, subject=None):
    subject = subject or "synthetic:w2-a:" + uuid4().hex
    principal = AuthorizedPrincipal(ids["organization_id"], ids["party_id"], subject, capabilities)
    binding = RentDatabaseBinding(
        ids["organization_id"], ids["party_id"],
        lambda: psycopg.connect(fixture.cluster.login_dsn(ids["login"])),
    )
    resolver = PrincipalRentServiceResolver(
        StaticRentDatabaseBindings({subject: binding}),
        BusinessDateProvider(lambda _timezone: date(2026, 9, 28)),
    )
    return principal, resolver.resolve(principal)


def _ledger(service) -> str:
    return service.admin_reference_data()["ledger_revision"]


def _revision_term(monthly_rent: str, *, confirmed: bool = True) -> dict:
    return {
        "monthly_rent": monthly_rent,
        "amount_confirmed": confirmed,
        "due_day": 5 if confirmed else None,
        "cycle_confirmed": confirmed,
        "cycle_rule": {
            "schema_version": 1,
            "mode": "EXPLICIT_PERIODS",
            "first_due_month": "2026-11",
            "confirmation_ref": "W2_A_TEST",
        } if confirmed else None,
        "policy_version": POLICY,
    }


def _two_period_contract(service, ids, resident_id: str, revision: str) -> dict:
    result = run(service, "createContract", {
        "rental_unit_id": str(ids["unit_id"]),
        "starts_on": "2026-09-01",
        "ends_on_exclusive": "2026-11-01",
        "lifecycle": "ACTIVE",
        "readiness": "READY",
        "resident_ids": [resident_id],
        "contract_parties": [],
        "term": {
            "valid_from": "2026-09-01",
            "valid_to_exclusive": "2026-11-01",
            "monthly_rent": "1000000",
            "amount_confirmed": True,
            "due_day": 5,
            "cycle_confirmed": True,
            "cycle_rule": {
                "schema_version": 1,
                "mode": "EXPLICIT_PERIODS",
                "first_due_month": "2026-10",
                "confirmation_ref": "W2_A_INITIAL",
            },
            "policy_version": POLICY,
        },
        "billing_periods": [
            {
                "cycle_start": "2026-09-01",
                "cycle_end_exclusive": "2026-10-01",
                "due_month": "2026-10",
                "confirmation_ref": "W2_A_SEPTEMBER",
            },
            {
                "cycle_start": "2026-10-01",
                "cycle_end_exclusive": "2026-11-01",
                "due_month": "2026-11",
                "confirmation_ref": "W2_A_OCTOBER",
            },
        ],
        "previous_contract_id": None,
        "reason": "Synthetic two-period contract",
        "expected_ledger_revision": revision,
        "occupancies": [{
            "resident_id": resident_id,
            "actual_start": "2026-09-01",
            "actual_end_exclusive": None,
            "review_status": "VERIFIED",
        }],
    })
    periods = [item for item in result["result"]["entities"] if item["kind"] == "PERIOD"]
    return {
        "contract_id": entity(result, "CONTRACT")["id"],
        "version": entity(result, "CONTRACT")["version"],
        "period_ids": [item["id"] for item in periods],
        "term_id": entity(result, "TERM")["id"],
        "revision": result["ledger_revision"],
    }


def _issue_period(service, contract_id: str, period_id: str, key=None):
    preview = service.preview_charge({"contract_id": contract_id, "period_id": period_id})
    body = {
        "contract_id": contract_id,
        "period_id": period_id,
        "expected_contract_version": preview["contract_version"],
        "expected_term_versions": preview["term_versions"],
        "expected_ledger_revision": preview["ledger_revision"],
        "calculation_sha256": preview["calculation_sha256"],
        "issuance_mode": "OPERATOR_CONFIRMED",
        "replaces_receivable_id": None,
    }
    result = run(service, "issueRent", body, key=key)
    return entity(result, "RECEIVABLE")["id"], result, preview


def _contract_revision_body(detail: dict, ledger: str, *, effective="2026-10-01", rent="400000", confirmed=True):
    return {
        "expected_version": detail["version"],
        "effective_on": effective,
        "starts_on": detail["starts_on"],
        "ends_on_exclusive": detail["ends_on_exclusive"],
        "lifecycle": detail["lifecycle"],
        "readiness": "READY",
        "term": _revision_term(rent, confirmed=confirmed),
        "reason": "Synthetic future contract revision",
        "expected_ledger_revision": ledger,
    }


def test_contract_revision_preserves_history_receivable_and_idempotency():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        resident_id, revision, _ = create_resident(service)
        contract = _two_period_contract(service, ids, resident_id, revision)
        receivable_id, _, initial_preview = _issue_period(
            service, contract["contract_id"], contract["period_ids"][0]
        )
        assert initial_preview["amount"] == "1000000"

        before_receivable = dict(service.repository.read_rows(
            "SELECT original_amount,calculation_snapshot,calculation_sha256,due_on,version "
            "FROM propertyai.finance_receivable WHERE receivable_id=%s",
            (UUID(receivable_id),),
        )[0])
        before_lines = [dict(row) for row in service.repository.read_rows(
            "SELECT term_id,line_no,service_from,service_to_exclusive,monthly_rent,month_days,charge_days,original_amount "
            "FROM propertyai.finance_receivable_line WHERE receivable_id=%s ORDER BY line_no",
            (UUID(receivable_id),),
        )]

        detail = service.get_contract(UUID(contract["contract_id"]))
        key = uuid4()
        body = _contract_revision_body(detail, _ledger(service))
        command = RentCommand("reviseContract", body, key, UUID(contract["contract_id"]))
        committed, replayed = service.handle(command)
        assert replayed is False
        assert committed["result"]["outcome"] == "UPDATED"

        replay, replayed = service.handle(command)
        assert replayed is True and replay == committed
        conflicting = dict(body, reason="Different payload on same key")
        with pytest.raises(RentError) as error:
            service.handle(RentCommand("reviseContract", conflicting, key, UUID(contract["contract_id"])))
        assert error.value.code == "IDEMPOTENCY_CONFLICT"

        after = service.get_contract(UUID(contract["contract_id"]))
        assert after["version"] == "2"
        assert after["current_term_id"] != contract["term_id"]
        assert len(after["contract_history"]) == 2
        assert [row["version"] for row in after["contract_history"]] == ["1", "2"]
        original = next(term for term in after["terms"] if term["term_id"] == contract["term_id"])
        assert original["superseded_by"] == after["current_term_id"]
        assert original["supersession_reason"] == body["reason"]
        active = [term for term in after["terms"] if term["superseded_by"] is None]
        assert [(term["valid_from"], term["valid_to_exclusive"], term["monthly_rent"]) for term in active] == [
            ("2026-09-01", "2026-10-01", "1000000"),
            ("2026-10-01", "2026-11-01", "400000"),
        ]
        assert after["billing_readiness"] == "READY"

        future_preview = service.preview_charge({
            "contract_id": contract["contract_id"], "period_id": contract["period_ids"][1]
        })
        assert future_preview["amount"] == "400000"
        assert future_preview["status"] == "READY"

        after_receivable = dict(service.repository.read_rows(
            "SELECT original_amount,calculation_snapshot,calculation_sha256,due_on,version "
            "FROM propertyai.finance_receivable WHERE receivable_id=%s",
            (UUID(receivable_id),),
        )[0])
        after_lines = [dict(row) for row in service.repository.read_rows(
            "SELECT term_id,line_no,service_from,service_to_exclusive,monthly_rent,month_days,charge_days,original_amount "
            "FROM propertyai.finance_receivable_line WHERE receivable_id=%s ORDER BY line_no",
            (UUID(receivable_id),),
        )]
        assert after_receivable == before_receivable
        assert after_lines == before_lines
        visible_receivable = next(row for row in after["receivables"] if row["receivable_id"] == receivable_id)
        assert visible_receivable["effective_amount"] == "1000000"


def test_contract_revision_rejects_stale_tokens_overlap_and_unconfirmed_ready():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        resident_id, revision, _ = create_resident(service)
        contract = _two_period_contract(service, ids, resident_id, revision)
        initial = service.get_contract(UUID(contract["contract_id"]))
        old_ledger = _ledger(service)
        run(service, "reviseContract", _contract_revision_body(initial, old_ledger), target=UUID(contract["contract_id"]))
        current = service.get_contract(UUID(contract["contract_id"]))
        current_ledger = _ledger(service)

        stale_version = _contract_revision_body(current, current_ledger, effective="2026-10-15", rent="450000")
        stale_version["expected_version"] = initial["version"]
        with pytest.raises(RentError) as error:
            run(service, "reviseContract", stale_version, target=UUID(contract["contract_id"]))
        assert error.value.code == "VERSION_CONFLICT"

        stale_ledger = _contract_revision_body(current, old_ledger, effective="2026-10-15", rent="450000")
        with pytest.raises(RentError) as error:
            run(service, "reviseContract", stale_ledger, target=UUID(contract["contract_id"]))
        assert error.value.code == "VERSION_CONFLICT"

        invalid_effective = _contract_revision_body(current, current_ledger, effective="2026-09-30", rent="450000")
        with pytest.raises(RentError) as error:
            run(service, "reviseContract", invalid_effective, target=UUID(contract["contract_id"]))
        assert error.value.code == "VALIDATION_ERROR"

        incomplete = _contract_revision_body(current, current_ledger, effective="2026-10-15", rent="0", confirmed=False)
        with pytest.raises(RentError) as error:
            run(service, "reviseContract", incomplete, target=UUID(contract["contract_id"]))
        assert error.value.code == "CONTRACT_NOT_READY"

        invalid_lifecycle = dict(_contract_revision_body(current, current_ledger, effective="2026-10-15", rent="450000"), lifecycle="UNKNOWN")
        with pytest.raises(RentError) as error:
            run(service, "reviseContract", invalid_lifecycle, target=UUID(contract["contract_id"]))
        assert error.value.code == "VALIDATION_ERROR"
        observed = service.get_contract(UUID(contract["contract_id"]))
        assert {key: value for key, value in observed.items() if key != "as_of"} == {
            key: value for key, value in current.items() if key != "as_of"
        }


def test_account_revision_preserves_protected_binding_history_and_deactivation():
    with operator_fixture(today=date(2026, 9, 28)) as (_, service, ids):
        created = run(service, "createAccount", {
            "display_name": "Synthetic protected account",
            "currency": "KRW",
            "owner_party_id": str(ids["party_id"]),
            "masked_identifier": "****1000",
            "protected_identifier_ref": "vault://synthetic/original",
            "expected_ledger_revision": "0",
        })
        account_id = entity(created, "ACCOUNT")["id"]
        _, movement_result = receipt(service, account_id, created["ledger_revision"], ids, amount="1000")
        movement_id = entity(movement_result, "MOVEMENT")["id"]
        reference = service.admin_reference_data()
        account_view = next(row for row in reference["accounts"] if row["account_id"] == account_id)
        assert "protected_identifier_ref" not in account_view

        revised = run(service, "reviseAccount", {
            "expected_version": account_view["version"],
            "display_name": "Synthetic protected account corrected",
            "masked_identifier": "****2000",
            "protected_identifier_ref": None,
            "active": False,
            "reason": "Deactivate without clearing protected reference",
            "expected_ledger_revision": reference["ledger_revision"],
        }, target=UUID(account_id))
        assert entity(revised, "ACCOUNT")["id"] == account_id

        raw = dict(service.repository.read_rows(
            "SELECT account_id,currency,display_name,masked_identifier,protected_identifier_ref,active,version "
            "FROM propertyai.finance_account WHERE account_id=%s",
            (UUID(account_id),),
        )[0])
        assert raw["account_id"] == UUID(account_id)
        assert raw["currency"] == "KRW"
        assert raw["protected_identifier_ref"] == "vault://synthetic/original"
        assert raw["active"] is False and raw["version"] == 2
        movement_account = service.repository.read_rows(
            "SELECT account_id FROM propertyai.finance_movement_revision WHERE movement_id=%s ORDER BY revision_no",
            (UUID(movement_id),),
        )[0]["account_id"]
        assert movement_account == UUID(account_id)

        with pytest.raises(RentError) as error:
            receipt(service, account_id, revised["ledger_revision"], ids, amount="500")
        assert error.value.code == "VALIDATION_ERROR"

        stale = {
            "expected_version": "1",
            "display_name": raw["display_name"],
            "masked_identifier": raw["masked_identifier"],
            "protected_identifier_ref": None,
            "active": False,
            "reason": "Stale version",
            "expected_ledger_revision": _ledger(service),
        }
        with pytest.raises(RentError) as error:
            run(service, "reviseAccount", stale, target=UUID(account_id))
        assert error.value.code == "VERSION_CONFLICT"

        replacement = dict(stale,
            expected_version="2",
            protected_identifier_ref="vault://synthetic/replacement",
            reason="Explicit protected reference replacement",
            expected_ledger_revision=_ledger(service),
        )
        run(service, "reviseAccount", replacement, target=UUID(account_id))
        final_raw = service.repository.read_rows(
            "SELECT protected_identifier_ref,account_id FROM propertyai.finance_account WHERE account_id=%s",
            (UUID(account_id),),
        )[0]
        assert final_raw["protected_identifier_ref"] == "vault://synthetic/replacement"
        assert final_raw["account_id"] == UUID(account_id)


def test_admin_boundary_capabilities_principal_binding_csrf_and_client_authority():
    admin = RentLongstayAdmin()
    with operator_fixture(today=date(2026, 9, 28)) as (fixture, setup_service, ids):
        resident_id, revision, _ = create_resident(setup_service)
        contract = create_contract(setup_service, ids, [resident_id], revision)

        read_principal, read_service = _bound_service(fixture, ids, frozenset({"READ"}))
        read_context = LongstayAdminContext(read_service, CSRF, read_principal)
        page = admin.handle(read_context, "GET", "/app/rent/contracts", {})
        assert page is not None and page.status == 200 and b"Contract & Residency" in page.body
        head = admin.handle(read_context, "HEAD", "/app/rent/master-data", {})
        assert head is not None and head.status == 200 and head.body == b""
        detail = admin.handle(read_context, "GET", f"/api/v1/longstay/admin/contracts/{contract['contract_id']}", {})
        assert detail is not None and detail.status == 200 and detail.body["contract_id"] == contract["contract_id"]
        denied_admin_ref = admin.handle(read_context, "GET", "/api/v1/longstay/admin/reference-data", {})
        assert denied_admin_ref is not None and denied_admin_ref.status == 403

        write_principal, write_service = _bound_service(fixture, ids, frozenset({"WRITE"}))
        write_context = LongstayAdminContext(write_service, CSRF, write_principal)
        denied_page = admin.handle(write_context, "GET", "/app/rent/contracts", {})
        assert denied_page is not None and denied_page.status == 403
        reference = admin.handle(write_context, "GET", "/api/v1/longstay/admin/reference-data", {})
        assert reference is not None and reference.status == 200

        current = setup_service.get_contract(UUID(contract["contract_id"]))
        body = {
            "expected_version": current["version"],
            "effective_on": None,
            "starts_on": current["starts_on"],
            "ends_on_exclusive": current["ends_on_exclusive"],
            "lifecycle": current["lifecycle"],
            "readiness": current["readiness"],
            "term": None,
            "reason": "Admin boundary revision",
            "expected_ledger_revision": reference.body["ledger_revision"],
        }
        path = f"/api/v1/longstay/contracts/{contract['contract_id']}/revisions"
        key = uuid4()
        headers = {"Content-Type": "application/json", "X-CSRF-Token": CSRF, "Idempotency-Key": str(key)}
        response = admin.handle(write_context, "POST", path, headers, json.dumps(body).encode())
        assert response is not None and response.status == 200
        assert response.headers["X-Idempotent-Replayed"] == "false"
        replay = admin.handle(write_context, "POST", path, headers, json.dumps(body).encode())
        assert replay is not None and replay.status == 200
        assert replay.headers["X-Idempotent-Replayed"] == "true" and replay.body == response.body

        no_csrf = admin.handle(write_context, "POST", path,
            {"Content-Type": "application/json", "Idempotency-Key": str(uuid4())}, json.dumps(body).encode())
        assert no_csrf is not None and no_csrf.status == 403 and no_csrf.body["code"] == "NOT_AUTHORIZED"
        no_key = admin.handle(write_context, "POST", path,
            {"Content-Type": "application/json", "X-CSRF-Token": CSRF}, json.dumps(body).encode())
        assert no_key is not None and no_key.status == 422 and no_key.body["code"] == "VALIDATION_ERROR"

        latest = setup_service.get_contract(UUID(contract["contract_id"]))
        forged = dict(body,
            expected_version=latest["version"],
            expected_ledger_revision=_ledger(write_service),
            organization_id=str(uuid4()),
        )
        forged_response = admin.handle(write_context, "POST", path,
            {"Content-Type": "application/json", "X-CSRF-Token": CSRF, "Idempotency-Key": str(uuid4())},
            json.dumps(forged).encode())
        assert forged_response is not None and forged_response.status == 422
        assert forged_response.body["code"] == "VALIDATION_ERROR"

        no_caps_principal, no_caps_service = _bound_service(fixture, ids, frozenset())
        no_caps = admin.handle(LongstayAdminContext(no_caps_service, CSRF, no_caps_principal),
                               "GET", "/app/rent/contracts", {})
        assert no_caps is not None and no_caps.status == 403

        wrong_principal = AuthorizedPrincipal(uuid4(), ids["party_id"], write_principal.subject, frozenset({"WRITE"}))
        wrong = admin.handle(LongstayAdminContext(write_service, CSRF, wrong_principal),
                             "GET", "/api/v1/longstay/admin/reference-data", {})
        assert wrong is not None and wrong.status == 403

        unbound = admin.handle(LongstayAdminContext(setup_service, CSRF, write_principal),
                               "GET", "/api/v1/longstay/admin/reference-data", {})
        assert unbound is not None and unbound.status == 403


def test_s02_s08_static_admin_contract_is_mobile_history_complete_and_masked():
    s02 = S02.read_text(encoding="utf-8")
    s08 = S08.read_text(encoding="utf-8")
    for required in (
        "current.residents", "current.terms", "current.occupancies", "current.receivables",
        "current.contract_history", "SUPERSEDED", "current.current_term_id",
        "date-corrections", "room-moves", "Idempotency-Key", "X-CSRF-Token",
        "@media(max-width:720px)",
    ):
        assert required in s02
    for required in (
        "u.timezone_name", "a.version", "masked_identifier", "Idempotency-Key",
        "X-CSRF-Token", "blank to preserve", "@media(max-width:720px)",
    ):
        assert required in s08
    assert "/api/v1/properties" not in s08
    assert "/api/v1/rental-units" not in s08
    assert "Property/unit creation is not owned by Rent" in s08
    assert "current value is never displayed" in s08


_BROWSER_CONTRACT = {
    "contract_id": "00000000-0000-4000-8000-000000000201",
    "version": "2",
    "as_of": "2026-09-17T10:00:00+09:00",
    "lifecycle": "ACTIVE",
    "readiness": "READY",
    "billing_readiness": "READY",
    "current_term_id": "00000000-0000-4000-8000-000000000212",
    "starts_on": "2026-09-01",
    "ends_on_exclusive": "2027-09-01",
    "residents": [{
        "resident_id": "00000000-0000-4000-8000-000000000221",
        "display_name": "Synthetic Browser Resident",
    }],
    "terms": [
        {
            "term_id": "00000000-0000-4000-8000-000000000211",
            "revision_no": 1,
            "version": "2",
            "valid_from": "2026-09-01",
            "valid_to_exclusive": "2026-10-01",
            "monthly_rent": "1000000",
            "amount_confirmed": True,
            "due_day": 5,
            "cycle_confirmed": True,
            "cycle_rule": {"schema_version": 1, "mode": "EXPLICIT_PERIODS"},
            "policy_version": POLICY,
            "superseded_by": "00000000-0000-4000-8000-000000000212",
            "supersession_reason": "Synthetic browser revision",
        },
        {
            "term_id": "00000000-0000-4000-8000-000000000212",
            "revision_no": 2,
            "version": "1",
            "valid_from": "2026-10-01",
            "valid_to_exclusive": "2027-09-01",
            "monthly_rent": "400000",
            "amount_confirmed": True,
            "due_day": 5,
            "cycle_confirmed": True,
            "cycle_rule": {"schema_version": 1, "mode": "EXPLICIT_PERIODS"},
            "policy_version": POLICY,
            "superseded_by": None,
            "supersession_reason": None,
        },
    ],
    "occupancies": [{
        "occupancy_id": "00000000-0000-4000-8000-000000000231",
        "resident_id": "00000000-0000-4000-8000-000000000221",
        "contract_id": "00000000-0000-4000-8000-000000000201",
        "actual_start": "2026-09-03",
        "actual_end_exclusive": None,
        "review_status": "VERIFIED",
        "version": "1",
    }],
    "contract_history": [
        {"version": "1", "command_id": "00000000-0000-4000-8000-000000000241", "snapshot": {"readiness": "READY"}, "recorded_at": "2026-09-01T00:00:00+09:00"},
        {"version": "2", "command_id": "00000000-0000-4000-8000-000000000242", "snapshot": {"readiness": "READY"}, "recorded_at": "2026-09-17T10:00:00+09:00"},
    ],
    "receivables": [{
        "receivable_id": "00000000-0000-4000-8000-000000000251",
        "period_id": "00000000-0000-4000-8000-000000000252",
        "due_on": "2026-10-05",
        "effective_amount": "1000000",
        "allocated": "300000",
        "balance": "700000",
        "version": "1",
        "payment_state": "PARTIAL",
    }],
}

_BROWSER_REFERENCE = {
    "ledger_revision": "42",
    "units": [{
        "property_id": "00000000-0000-4000-8000-000000000261",
        "rental_unit_id": "00000000-0000-4000-8000-000000000262",
        "property_name": "Synthetic Property",
        "unit_name": "Synthetic Unit",
        "timezone_name": "Asia/Seoul",
        "active": True,
    }],
    "accounts": [{
        "account_id": "00000000-0000-4000-8000-000000000271",
        "display_name": "Synthetic Browser Account",
        "currency": "KRW",
        "masked_identifier": "****4242",
        "active": True,
        "version": "3",
    }],
}


class _BrowserSourceHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/app/rent/contracts":
            self._send(200, S02.read_bytes(), "text/html; charset=utf-8")
        elif path == "/app/rent/master-data":
            self._send(200, S08.read_bytes(), "text/html; charset=utf-8")
        elif path.startswith("/api/v1/longstay/admin/contracts/"):
            self._send_json(_BROWSER_CONTRACT)
        elif path == "/api/v1/longstay/admin/reference-data":
            self._send_json(_BROWSER_REFERENCE)
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def _send_json(self, value):
        self._send(200, json.dumps(value).encode(), "application/json")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        pass


_BROWSER_SCRIPT = r'''
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const [base, modulePath] = process.argv.slice(2);
const { chromium } = await import(pathToFileURL(modulePath).href);
const browser = await chromium.launch({headless:true});
try {
  for (const width of [390, 1280]) {
    const context = await browser.newContext({viewport:{width,height:900}});
    try {
      const page = await context.newPage();
      const errors=[]; page.on('pageerror',e=>errors.push(e.message));
      let response = await page.goto(base+'/app/rent/contracts');
      assert.equal(response.status(),200);
      await page.locator('#contractId').fill('00000000-0000-4000-8000-000000000201');
      await page.locator('#load').click();
      await page.waitForFunction(()=>document.querySelector('#state')?.dataset.state==='READY');
      const layout = await page.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth}));
      assert.equal(layout.width,width);
      assert.ok(layout.scrollWidth<=width,`S02 overflow ${layout.scrollWidth}>${width}`);
      assert.equal(await page.locator('#terms .superseded').count(),1);
      assert.equal(await page.locator('#terms .active').count(),1);
      assert.match(await page.locator('#receivables').innerText(),/1000000/);
      assert.match(await page.locator('#residents').innerText(),/Synthetic Browser Resident/);
      assert.deepEqual(errors,[]);

      errors.length=0;
      response = await page.goto(base+'/app/rent/master-data');
      assert.equal(response.status(),200);
      await page.waitForFunction(()=>document.querySelector('#state')?.dataset.state==='READY');
      const masterLayout = await page.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth}));
      assert.equal(masterLayout.width,width);
      assert.ok(masterLayout.scrollWidth<=width,`S08 overflow ${masterLayout.scrollWidth}>${width}`);
      const bodyText = await page.locator('body').innerText();
      assert.match(bodyText,/Asia\/Seoul/);
      assert.match(bodyText,/\*\*\*\*4242/);
      assert.match(await page.locator('#accountFacts').innerText(),/version 3/);
      assert.ok(!bodyText.includes('vault://synthetic/hidden'));
      assert.deepEqual(errors,[]);
    } finally { await context.close(); }
  }
} finally { await browser.close(); }
console.log(JSON.stringify({status:'PASS',viewports:[390,1280],s02:true,s08:true,protected_value_hidden:true}));
'''


def test_browser_390_1280_s02_s08_history_masking_and_no_overflow():
    module = os.environ.get("PROPERTYAI_W1B_PLAYWRIGHT_ENTRY")
    node = os.environ.get("PROPERTYAI_W1B_NODE")
    if not module or not node:
        pytest.skip("explicit existing browser runtime binding required")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BrowserSourceHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = "http://127.0.0.1:" + str(server.server_address[1])
        process = subprocess.run(
            [node, "--input-type=module", "-", base, module],
            input=_BROWSER_SCRIPT,
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
            env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")},
        )
        assert process.returncode == 0, process.stdout + "\n" + process.stderr
        assert json.loads(process.stdout.strip().splitlines()[-1]) == {
            "status": "PASS",
            "viewports": [390, 1280],
            "s02": True,
            "s08": True,
            "protected_value_hidden": True,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import http.client
import json
import os
import subprocess
from threading import Thread
from uuid import UUID, uuid4

import psycopg
import pytest

from propertyai_core.application.handlers.rent import BusinessDateProvider
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import create_account, create_resident, entity, receipt, run
from propertyai_core.tests.rent_finance.test_corrections import _empty_plan
from propertyai_core.web.auth_context import AuthorizedPrincipal, ServerPrincipalDirectory
from propertyai_core.web.auth_session import InMemorySessionStore, RequestAuthenticator, SessionService
from propertyai_core.web.empty_shell import EmptyShell
from propertyai_core.web.rent_api import RentAPI
from propertyai_core.web.rent_integration import (
    AuthoritativeEmptyShellLoader,
    PrincipalRentServiceResolver,
    RentDatabaseBinding,
    StaticRentDatabaseBindings,
)
from propertyai_core.web.rent_longstay_admin import RentLongstayAdmin
from propertyai_core.web.rent_operations import RentOperationsUI
from propertyai_core.web.rent_server import local_test_http_server

SUBJECT = "synthetic:w2-i2:verified-subject"


def _http(server, method: str, path: str, *, headers=None, body: dict | bytes | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
    try:
        payload = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        connection.request(method, path, body=payload, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        parsed = None
        if "application/json" in (response.getheader("Content-Type") or "") and raw:
            parsed = json.loads(raw)
        return response.status, dict(response.getheaders()), raw, parsed
    finally:
        connection.close()


def _headers(issued, *, key=None, csrf=True):
    result = {"Cookie": "rent_session=" + issued.token, "Content-Type": "application/json"}
    if csrf:
        result["X-CSRF-Token"] = issued.csrf_token
    if key is not None:
        result["Idempotency-Key"] = str(key)
    return result


@contextmanager
def _combined_server(fixture, ids, *, capabilities=frozenset({"READ", "WRITE"})):
    principal = AuthorizedPrincipal(ids["organization_id"], ids["party_id"], SUBJECT, capabilities)
    sessions = SessionService(
        InMemorySessionStore(), ServerPrincipalDirectory([principal]), runtime="ISOLATED_TEST"
    )
    issued = sessions.issue(SUBJECT)
    binding = RentDatabaseBinding(
        ids["organization_id"], ids["party_id"],
        lambda: psycopg.connect(fixture.cluster.login_dsn(ids["login"])),
    )
    services = PrincipalRentServiceResolver(
        StaticRentDatabaseBindings({SUBJECT: binding}),
        BusinessDateProvider(lambda _timezone_name: date(2026, 9, 28)),
    )
    auth = RequestAuthenticator(sessions)
    api = RentAPI(authenticator=auth, services=services)
    shell = EmptyShell(auth, AuthoritativeEmptyShellLoader(services))
    server = local_test_http_server(
        api,
        shell=shell,
        longstay_admin=RentLongstayAdmin(),
        operations=RentOperationsUI(api),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, issued
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _create_i2_state(service, ids):
    resident_id, revision, _ = create_resident(service, name="Synthetic I2 Resident")
    account_id, revision, _ = create_account(service, revision, owner=ids["party_id"])
    contract_result = run(service, "createContract", {
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
            "monthly_rent": "500000",
            "amount_confirmed": True,
            "due_day": 5,
            "cycle_confirmed": True,
            "cycle_rule": {
                "schema_version": 1,
                "mode": "EXPLICIT_PERIODS",
                "first_due_month": "2026-10",
                "confirmation_ref": "W2-I2 synthetic",
            },
            "policy_version": "RENT_APPROVED_1G_2G_3G_4G_V1",
        },
        "billing_periods": [{
            "cycle_start": "2026-09-01",
            "cycle_end_exclusive": "2026-10-01",
            "due_month": "2026-10",
            "confirmation_ref": "W2-I2 synthetic",
        }],
        "previous_contract_id": None,
        "reason": "W2-I2 synthetic combined oracle",
        "expected_ledger_revision": revision,
        "occupancies": [{
            "resident_id": resident_id,
            "actual_start": "2026-09-01",
            "actual_end_exclusive": None,
            "review_status": "VERIFIED",
        }],
    })
    contract_id = entity(contract_result, "CONTRACT")["id"]
    period_id = entity(contract_result, "PERIOD")["id"]
    occupancy_id = entity(contract_result, "OCCUPANCY")["id"]
    preview = service.preview_charge({"contract_id": contract_id, "period_id": period_id})
    issued = run(service, "issueRent", {
        "contract_id": contract_id,
        "period_id": period_id,
        "expected_contract_version": preview["contract_version"],
        "expected_term_versions": preview["term_versions"],
        "expected_ledger_revision": preview["ledger_revision"],
        "calculation_sha256": preview["calculation_sha256"],
        "issuance_mode": "OPERATOR_CONFIRMED",
        "replaces_receivable_id": None,
    })
    receivable = next(e for e in issued["result"]["entities"] if e["kind"] == "RECEIVABLE")
    return {
        "account_id": account_id,
        "contract_id": contract_id,
        "occupancy_id": occupancy_id,
        "receivable_id": receivable["id"],
        "receivable_version": issued["result"]["receivable_balances"][0]["version"],
        "ledger_revision": issued["ledger_revision"],
    }


def _overview(server, issued):
    status, _, _, body = _http(
        server,
        "GET",
        "/rent/data/overview?month=2026-09&page=1&page_size=20&search=&property_id=&unit_id=",
        headers={"Cookie": "rent_session=" + issued.token},
    )
    assert status == 200
    return body


def _context(server, issued):
    status, _, _, body = _http(
        server, "GET", "/rent/data/context", headers={"Cookie": "rent_session=" + issued.token}
    )
    assert status == 200
    return body


def _post(server, issued, path, body, *, key=None, csrf=True):
    key = key or uuid4()
    status, headers, _, parsed = _http(
        server, "POST", path, headers=_headers(issued, key=key, csrf=csrf), body=body,
    )
    return status, headers, parsed, key


def test_i2_shared_router_navigation_auth_capability_and_csrf_composition():
    with operator_fixture(today=date(2026, 9, 28)) as (fixture, service, ids):
        state = _create_i2_state(service, ids)
        with _combined_server(fixture, ids) as (server, issued):
            for path in ("/app", "/app/rent/contracts", "/app/rent/master-data", "/rent"):
                status, _, _, body = _http(server, "GET", path)
                assert status == 401 and body["code"] == "UNAUTHENTICATED"

            cookie = {"Cookie": "rent_session=" + issued.token}
            status, _, shell, _ = _http(server, "GET", "/app", headers=cookie)
            assert status == 200
            shell_text = shell.decode()
            assert 'href="/rent"' in shell_text
            assert 'href="/app/rent/contracts"' in shell_text
            assert 'href="/app/rent/master-data"' in shell_text
            assert 'data-state="loading"' in shell_text

            for path, marker in (
                ("/app/rent/contracts", "Contract & Residency"),
                ("/app/rent/master-data", "Master Data"),
                ("/rent", "월세 운영"),
                ("/rent/", "월세 운영"),
            ):
                status, _, raw, _ = _http(server, "GET", path, headers=cookie)
                assert status == 200 and marker in raw.decode()
                assert b"data-i2-shared-navigation" in raw

            status, _, contract_page, _ = _http(
                server, "GET", "/app/rent/contracts", headers=cookie
            )
            assert status == 200
            assert ("window.RENT_CSRF_TOKEN=" + json.dumps(issued.csrf_token)).encode() in contract_page

            status, _, _, body = _http(
                server, "GET", "/rent/data/context/", headers=cookie
            )
            assert status == 200 and body["ledger_revision"]
            status, _, _, body = _http(server, "GET", "/not-an-i2-route", headers=cookie)
            assert status == 404 and body["code"] == "NOT_FOUND"

            current = service.get_contract(UUID(state["contract_id"]))
            revision_body = {
                "expected_version": current["version"],
                "effective_on": None,
                "starts_on": current["starts_on"],
                "ends_on_exclusive": current["ends_on_exclusive"],
                "lifecycle": current["lifecycle"],
                "readiness": current["readiness"],
                "term": None,
                "reason": "I2 forged authority rejection",
                "expected_ledger_revision": _context(server, issued)["ledger_revision"],
            }
            path = f"/api/v1/longstay/contracts/{state['contract_id']}/revisions"
            status, _, body, _ = _post(server, issued, path, revision_body, csrf=False)
            assert status == 403 and body["code"] == "NOT_AUTHORIZED"
            forged = dict(revision_body, organization_id=str(uuid4()), actor_party_id=str(uuid4()))
            status, _, body, _ = _post(server, issued, path, forged)
            assert status == 422 and body["code"] == "VALIDATION_ERROR"

        with _combined_server(fixture, ids, capabilities=frozenset({"READ"})) as (server, issued):
            cookie = {"Cookie": "rent_session=" + issued.token}
            assert _http(server, "GET", "/app/rent/contracts", headers=cookie)[0] == 200
            assert _http(server, "GET", "/rent", headers=cookie)[0] == 200
            status, _, _, body = _http(
                server, "GET", "/api/v1/longstay/admin/reference-data", headers=cookie
            )
            assert status == 403 and body["code"] == "NOT_AUTHORIZED"
            status, _, body, _ = _post(server, issued, "/api/v1/rent/movements", {})
            assert status == 403 and body["code"] == "NOT_AUTHORIZED"

        with _combined_server(fixture, ids, capabilities=frozenset({"WRITE"})) as (server, issued):
            cookie = {"Cookie": "rent_session=" + issued.token}
            status, _, _, body = _http(server, "GET", "/rent", headers=cookie)
            assert status == 403 and body["code"] == "NOT_AUTHORIZED"
            status, _, _, body = _http(server, "GET", "/app/rent/contracts", headers=cookie)
            assert status == 403 and body["code"] == "NOT_AUTHORIZED"
            status, _, _, body = _http(
                server, "GET", "/api/v1/longstay/admin/reference-data", headers=cookie
            )
            assert status == 200 and body["ledger_revision"]


def test_i2_combined_disposable_pg_contract_occupancy_500_300_200_history_and_correction():
    with operator_fixture(today=date(2026, 9, 28)) as (fixture, service, ids):
        state = _create_i2_state(service, ids)
        with _combined_server(fixture, ids) as (server, issued):
            cookie = {"Cookie": "rent_session=" + issued.token}
            status, _, _, detail = _http(
                server, "GET", f"/api/v1/longstay/admin/contracts/{state['contract_id']}", headers=cookie
            )
            assert status == 200
            assert detail["contract_id"] == state["contract_id"]
            assert any(o["occupancy_id"] == state["occupancy_id"] and o["review_status"] == "VERIFIED"
                       for o in detail["occupancies"])
            history_before = list(detail["contract_history"])

            status, _, raw_ref, reference = _http(
                server, "GET", "/api/v1/longstay/admin/reference-data", headers=cookie
            )
            assert status == 200
            assert any(u["rental_unit_id"] == str(ids["unit_id"]) for u in reference["units"])
            assert any(a["account_id"] == state["account_id"] for a in reference["accounts"])
            assert b"protected_identifier_ref" not in raw_ref

            before = _overview(server, issued)
            assert before["summary"]["selected_month_obligation"] == "500000"
            assert before["summary"]["selected_month_allocated"] == "0"
            assert before["summary"]["selected_month_outstanding"] == "500000"
            row = next(r for r in before["rows"] if r["receivable_id"] == state["receivable_id"])

            context = _context(server, issued)
            receipt_body = {
                "account_id": state["account_id"],
                "occurred_on": "2026-09-28",
                "amount": "300000",
                "currency": "KRW",
                "payer_raw": "Synthetic I2 payer",
                "counterparty_party_id": None,
                "attribution": {
                    "status": "CONTRACT_CONFIRMED",
                    "property_id": row["property_id"],
                    "contract_id": row["contract_id"],
                },
                "allocations": [{
                    "receivable_id": row["receivable_id"],
                    "amount": "300000",
                    "expected_version": row["version"],
                    "attribution_confirmed": True,
                    "override_attribution_reason": None,
                }],
                "expected_ledger_revision": context["ledger_revision"],
            }
            key = uuid4()
            status, response_headers, committed, _ = _post(
                server, issued, "/api/v1/rent/movements", receipt_body, key=key
            )
            assert status == 200 and response_headers["X-Idempotent-Replayed"] == "false"
            balance = committed["result"]["receivable_balances"][0]
            assert (balance["effective_amount"], balance["allocated"], balance["balance"]) == (
                "500000", "300000", "200000"
            )
            status, replay_headers, replayed, _ = _post(
                server, issued, "/api/v1/rent/movements", receipt_body, key=key
            )
            assert status == 200 and replay_headers["X-Idempotent-Replayed"] == "true"
            assert replayed == committed

            current = _overview(server, issued)["rows"][0]
            context = _context(server, issued)
            adjust_body = {
                "delta": "10000",
                "reason": "I2 append-only correction proof",
                "expected_version": current["version"],
                "expected_ledger_revision": context["ledger_revision"],
                "allocation_correction": _empty_plan(),
                "reverse_adjustment_id": None,
            }
            status, _, adjusted, _ = _post(
                server, issued,
                f"/api/v1/receivables/{state['receivable_id']}/adjustments",
                adjust_body,
            )
            assert status == 200
            adjustment = next(e for e in adjusted["result"]["entities"] if e["kind"] == "ADJUSTMENT")

            current = _overview(server, issued)["rows"][0]
            context = _context(server, issued)
            reverse_body = {
                "delta": "-10000",
                "reason": "I2 reverse correction while preserving history",
                "expected_version": current["version"],
                "expected_ledger_revision": context["ledger_revision"],
                "allocation_correction": _empty_plan(),
                "reverse_adjustment_id": adjustment["id"],
            }
            status, _, restored, _ = _post(
                server, issued,
                f"/api/v1/receivables/{state['receivable_id']}/adjustments",
                reverse_body,
            )
            assert status == 200
            final_balance = restored["result"]["receivable_balances"][0]
            assert (final_balance["effective_amount"], final_balance["allocated"], final_balance["balance"]) == (
                "500000", "300000", "200000"
            )

            final = _overview(server, issued)
            assert final["summary"]["selected_month_outstanding"] == "200000"
            assert final["summary"]["selected_month_obligation"] == "500000"
            assert final["summary"]["selected_month_allocated"] == "300000"

            status, _, _, ledger = _http(server, "GET", "/rent/data/ledger", headers=cookie)
            assert status == 200
            kinds = [event["kind"] for event in ledger["events"]]
            assert "RECEIVABLE" in kinds and "MOVEMENT_REVISION" in kinds and "ALLOCATION" in kinds
            assert kinds.count("RECEIVABLE_ADJUSTMENT") == 2

            status, _, _, detail_after = _http(
                server, "GET", f"/api/v1/longstay/admin/contracts/{state['contract_id']}", headers=cookie
            )
            assert status == 200
            assert detail_after["contract_history"] == history_before
            assert any(o["occupancy_id"] == state["occupancy_id"] for o in detail_after["occupancies"])
            receivable = next(r for r in detail_after["receivables"] if r["receivable_id"] == state["receivable_id"])
            assert (receivable["effective_amount"], receivable["allocated"], receivable["balance"]) == (
                "500000", "300000", "200000"
            )


def test_i2_integrated_account_revision_preserves_protected_history_and_inactive_guard():
    with operator_fixture(today=date(2026, 9, 28)) as (fixture, service, ids):
        created = run(service, "createAccount", {
            "display_name": "Synthetic I2 protected account",
            "currency": "KRW",
            "owner_party_id": str(ids["party_id"]),
            "masked_identifier": "****1000",
            "protected_identifier_ref": "vault://synthetic/i2-original",
            "expected_ledger_revision": "0",
        })
        account_id = entity(created, "ACCOUNT")["id"]
        _, movement_result = receipt(service, account_id, created["ledger_revision"], ids, amount="1000")
        movement_id = entity(movement_result, "MOVEMENT")["id"]

        with _combined_server(fixture, ids) as (server, issued):
            cookie = {"Cookie": "rent_session=" + issued.token}
            status, _, raw_ref, reference = _http(
                server, "GET", "/api/v1/longstay/admin/reference-data", headers=cookie
            )
            assert status == 200
            assert b"protected_identifier_ref" not in raw_ref
            account_view = next(row for row in reference["accounts"] if row["account_id"] == account_id)

            body = {
                "expected_version": account_view["version"],
                "display_name": "Synthetic I2 protected account corrected",
                "masked_identifier": "****2000",
                "protected_identifier_ref": None,
                "active": False,
                "reason": "I2 deactivate without clearing protected reference",
                "expected_ledger_revision": reference["ledger_revision"],
            }
            path = f"/api/v1/money-accounts/{account_id}/revisions"
            key = uuid4()
            status, response_headers, revised, _ = _post(server, issued, path, body, key=key)
            assert status == 200 and response_headers["X-Idempotent-Replayed"] == "false"
            status, replay_headers, replayed, _ = _post(server, issued, path, body, key=key)
            assert status == 200 and replay_headers["X-Idempotent-Replayed"] == "true"
            assert replayed == revised

            raw = dict(service.repository.read_rows(
                "SELECT account_id,currency,display_name,masked_identifier,protected_identifier_ref,active,version "
                "FROM propertyai.finance_account WHERE account_id=%s",
                (UUID(account_id),),
            )[0])
            assert raw["currency"] == "KRW"
            assert raw["protected_identifier_ref"] == "vault://synthetic/i2-original"
            assert raw["active"] is False and raw["version"] == 2
            movement_account = service.repository.read_rows(
                "SELECT account_id FROM propertyai.finance_movement_revision "
                "WHERE movement_id=%s ORDER BY revision_no",
                (UUID(movement_id),),
            )[0]["account_id"]
            assert movement_account == UUID(account_id)

            context = _context(server, issued)
            inactive_receipt = {
                "account_id": account_id,
                "occurred_on": "2026-09-28",
                "amount": "500",
                "currency": "KRW",
                "payer_raw": "Synthetic inactive account attempt",
                "counterparty_party_id": None,
                "attribution": {
                    "status": "PROPERTY_CONFIRMED",
                    "property_id": str(ids["property_id"]),
                    "contract_id": None,
                },
                "allocations": [],
                "expected_ledger_revision": context["ledger_revision"],
            }
            status, _, denied, _ = _post(
                server, issued, "/api/v1/rent/movements", inactive_receipt
            )
            assert status == 422 and denied["code"] == "VALIDATION_ERROR"

            status, _, _, reference = _http(
                server, "GET", "/api/v1/longstay/admin/reference-data", headers=cookie
            )
            assert status == 200
            replacement = {
                "expected_version": "2",
                "display_name": raw["display_name"],
                "masked_identifier": raw["masked_identifier"],
                "protected_identifier_ref": "vault://synthetic/i2-replacement",
                "active": False,
                "reason": "I2 explicit protected reference replacement",
                "expected_ledger_revision": reference["ledger_revision"],
            }
            status, _, _, _ = _post(server, issued, path, replacement)
            assert status == 200
            final_raw = dict(service.repository.read_rows(
                "SELECT currency,protected_identifier_ref,active,version "
                "FROM propertyai.finance_account WHERE account_id=%s",
                (UUID(account_id),),
            )[0])
            assert final_raw == {
                "currency": "KRW",
                "protected_identifier_ref": "vault://synthetic/i2-replacement",
                "active": False,
                "version": 3,
            }


_BROWSER_SCRIPT = r'''
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const [base, token, contractId, modulePath] = process.argv.slice(2);
const { chromium } = await import(pathToFileURL(modulePath).href);
const browser = await chromium.launch({headless:true});
try {
  for (const width of [320,1280]) {
    const context = await browser.newContext({viewport:{width,height:900}});
    try {
      await context.addCookies([{name:'rent_session',value:token,url:base,httpOnly:true,sameSite:'Strict'}]);
      const page = await context.newPage();
      const errors=[]; page.on('pageerror',e=>errors.push(e.message));
      const checkLayout=async label=>{
        const layout=await page.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth}));
        assert.equal(layout.width,width,label+' viewport');
        assert.ok(layout.scrollWidth<=width,label+' overflow '+layout.scrollWidth+'>'+width);
      };

      let response=await page.goto(base+'/app');
      assert.equal(response.status(),200); await checkLayout('app');
      assert.equal(await page.locator('a[href="/rent"]').count()>0,true);
      assert.equal(await page.locator('a[href="/app/rent/contracts"]').count()>0,true);
      assert.equal(await page.locator('a[href="/app/rent/master-data"]').count()>0,true);

      response=await page.goto(base+'/app/rent/contracts');
      assert.equal(response.status(),200);
      assert.equal(await page.evaluate(()=>typeof window.RENT_CSRF_TOKEN==='string'&&window.RENT_CSRF_TOKEN.length===43),true);
      await page.locator('#contractId').fill(contractId); await page.locator('#load').click();
      await page.waitForFunction(()=>document.querySelector('#state')?.dataset.state==='READY');
      await checkLayout('s02');

      response=await page.goto(base+'/app/rent/master-data');
      assert.equal(response.status(),200);
      await page.waitForFunction(()=>document.querySelector('#state')?.dataset.state==='READY');
      await checkLayout('s08');

      response=await page.goto(base+'/rent');
      assert.equal(response.status(),200);
      await page.locator('#rentRows tr').first().waitFor();
      await checkLayout('rent');
      assert.match(await page.locator('#sumSelectedOutstanding').innerText(),/500,000/);
      assert.deepEqual(errors,[]);
    } finally { await context.close(); }
  }
} finally { await browser.close(); }
console.log(JSON.stringify({status:'PASS',viewports:[320,1280],shared_navigation:true}));
'''


def test_i2_browser_shared_navigation_320_1280_no_overflow_or_js_error():
    module = os.environ.get("PROPERTYAI_W1B_PLAYWRIGHT_ENTRY")
    node = os.environ.get("PROPERTYAI_W1B_NODE")
    if not module or not node:
        pytest.skip("explicit existing browser runtime binding required")
    with operator_fixture(today=date(2026, 9, 28)) as (fixture, service, ids):
        state = _create_i2_state(service, ids)
        with _combined_server(fixture, ids) as (server, issued):
            base = "http://127.0.0.1:" + str(server.server_address[1])
            process = subprocess.run(
                [node, "--input-type=module", "-", base, issued.token, state["contract_id"], module],
                input=_BROWSER_SCRIPT,
                text=True,
                capture_output=True,
                timeout=90,
                check=False,
                env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")},
            )
            assert process.returncode == 0, process.stdout + "\n" + process.stderr
            assert json.loads(process.stdout.strip().splitlines()[-1]) == {
                "status": "PASS", "viewports": [320, 1280], "shared_navigation": True,
            }

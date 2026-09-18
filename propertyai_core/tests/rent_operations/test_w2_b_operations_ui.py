from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import http.client
import json
import os
from pathlib import Path
import subprocess
from threading import Thread
from uuid import UUID, uuid4

import psycopg
import pytest

from propertyai_core.application.handlers.rent import BusinessDateProvider
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import create_resident
from propertyai_core.tests.rent_finance.test_corrections import _contract_with_rent, _empty_plan, _issue, _setup_finance
from propertyai_core.web.auth_context import AuthorizedPrincipal, ServerPrincipalDirectory
from propertyai_core.web.auth_session import InMemorySessionStore, RequestAuthenticator, SessionService
from propertyai_core.web.rent_api import RentAPI
from propertyai_core.web.rent_integration import (
    PrincipalRentServiceResolver,
    RentDatabaseBinding,
    StaticRentDatabaseBindings,
)
from propertyai_core.web.rent_operations import RentOperationsUI, local_rent_operations_http_server

SUBJECT = "synthetic:w2-b:verified-subject"
REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = REPO_ROOT / "propertyai_core/web/rent_operations.html"
SOURCE = REPO_ROOT / "propertyai_core/web/rent_operations.py"


def _http(server, method: str, path: str, *, headers=None, body: dict | bytes | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=15)
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


@contextmanager
def _integrated_server(fixture, ids, *, capabilities=frozenset({"READ", "WRITE"})):
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
    api = RentAPI(authenticator=RequestAuthenticator(sessions), services=services)
    server = local_rent_operations_http_server(RentOperationsUI(api))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, issued
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _headers(issued, *, key=None, csrf=True):
    result = {
        "Cookie": "rent_session=" + issued.token,
        "Content-Type": "application/json",
    }
    if csrf:
        result["X-CSRF-Token"] = issued.csrf_token
    if key is not None:
        result["Idempotency-Key"] = str(key)
    return result


def _context(server, issued):
    status, _, _, body = _http(
        server, "GET", "/rent/data/context", headers={"Cookie": "rent_session=" + issued.token}
    )
    assert status == 200
    return body


def _overview(server, issued, *, page=1, page_size=20, search="", property_id="", unit_id=""):
    path = (
        "/rent/data/overview?month=2026-09"
        f"&page={page}&page_size={page_size}&search={search}"
        f"&property_id={property_id}&unit_id={unit_id}"
    )
    status, _, _, body = _http(
        server, "GET", path, headers={"Cookie": "rent_session=" + issued.token}
    )
    assert status == 200
    return body


def _post(server, issued, path: str, body: dict, *, key=None):
    key = key or uuid4()
    status, headers, _, payload = _http(
        server, "POST", path, headers=_headers(issued, key=key), body=body
    )
    return status, headers, payload, key


def test_w2_b_source_owns_ui_without_finance_formula_or_shared_router_edit():
    source = SOURCE.read_text(encoding="utf-8")
    template = TEMPLATE.read_text(encoding="utf-8")
    assert "calculate_rent" not in source
    assert "prorat" not in source.lower()
    assert "v_rent_receivables" in source
    assert "RentAPI" in source
    assert "자동 배분 OFF" in template
    assert "selected_month_outstanding" in template
    for operation in (
        "adjustReceivable", "voidReceivable", "correctAllocations",
        "correctMovement", "recordRefund", "correctRefund",
    ):
        assert f'value="{operation}"' in template
    assert "송금 실행 아님" in template
    assert "RESULT_UNKNOWN" in template and "RETRY_SAME_OPERATION" in template
    assert "Idempotency-Key" in template
    assert "@media(max-width:720px)" in template


def test_auth_csrf_error_and_empty_are_distinct():
    with operator_fixture(today=date(2026, 9, 28)) as (fixture, setup_service, ids):
        state = _setup_finance(setup_service, ids)
        with _integrated_server(fixture, ids) as (server, issued):
            status, _, _, body = _http(server, "GET", "/rent")
            assert status == 401 and body["code"] == "UNAUTHENTICATED"

            status, _, raw, _ = _http(
                server, "GET", "/rent", headers={"Cookie": "rent_session=" + issued.token}
            )
            assert status == 200 and b"data-panel=\"rent\"" in raw

            ctx = _context(server, issued)
            body = {
                "account_id": state["account_id"],
                "occurred_on": "2026-09-28",
                "amount": "1000",
                "currency": "KRW",
                "payer_raw": "Synthetic payer",
                "counterparty_party_id": None,
                "attribution": {"status": "UNMATCHED", "property_id": None, "contract_id": None},
                "allocations": [],
                "expected_ledger_revision": ctx["ledger_revision"],
            }
            status, _, _, payload = _http(
                server, "POST", "/api/v1/rent/movements",
                headers=_headers(issued, key=uuid4(), csrf=False), body=body,
            )
            assert status == 403 and payload["code"] == "NOT_AUTHORIZED"

            # Unknown/forged organization query input is not an authority source.
            own = _overview(server, issued)
            status, _, _, forged = _http(
                server, "GET",
                "/rent/data/overview?month=2026-09&page=1&page_size=20&search=&property_id=&unit_id=&organization_id=" + str(uuid4()),
                headers={"Cookie": "rent_session=" + issued.token},
            )
            assert status == 200 and forged["rows"] == own["rows"]

            status, _, _, invalid = _http(
                server, "GET", "/rent/data/overview?month=2026-99&page=1&page_size=20&search=&property_id=&unit_id=",
                headers={"Cookie": "rent_session=" + issued.token},
            )
            assert status == 422 and invalid["code"] == "VALIDATION_ERROR"

        with _integrated_server(fixture, ids, capabilities=frozenset({"READ"})) as (server, issued):
            ctx = _context(server, issued)
            body["expected_ledger_revision"] = ctx["ledger_revision"]
            status, _, _, payload = _http(
                server, "POST", "/api/v1/rent/movements",
                headers=_headers(issued, key=uuid4()), body=body,
            )
            assert status == 403 and payload["code"] == "NOT_AUTHORIZED"


def test_real_http_disposable_pg_500_300_200_unallocated_corrections_and_refund_record():
    with operator_fixture(today=date(2026, 9, 28)) as (fixture, setup_service, ids):
        state = _setup_finance(setup_service, ids)
        with _integrated_server(fixture, ids) as (server, issued):
            cookie = {"Cookie": "rent_session=" + issued.token}
            status, _, raw, _ = _http(server, "GET", "/rent", headers=cookie)
            assert status == 200 and "월세 운영" in raw.decode("utf-8")

            before = _overview(server, issued)
            assert before["summary"] == {
                "selected_month_obligation": "500000",
                "selected_month_allocated": "0",
                "selected_month_outstanding": "500000",
                "all_period_outstanding": "500000",
                "summary_scope": "WHOLE_FILTERED_QUERY_NOT_PAGE",
            }
            row = before["rows"][0]
            assert row["receivable_id"] == state["receivable_id"]
            assert row["property_id"] == str(ids["property_id"])
            assert row["rental_unit_id"] == str(ids["unit_id"])

            ctx = _context(server, issued)
            receipt_body = {
                "account_id": state["account_id"],
                "occurred_on": "2026-09-28",
                "amount": "300000",
                "currency": "KRW",
                "payer_raw": "Synthetic W2-B payer",
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
                "expected_ledger_revision": ctx["ledger_revision"],
            }
            key = uuid4()
            status, headers, committed, _ = _post(
                server, issued, "/api/v1/rent/movements", receipt_body, key=key
            )
            assert status == 200 and headers["X-Idempotent-Replayed"] == "false"
            assert committed["result"]["receivable_balances"][0] == {
                "receivable_id": state["receivable_id"],
                "effective_amount": "500000",
                "allocated": "300000",
                "balance": "200000",
                "version": committed["result"]["receivable_balances"][0]["version"],
            }
            assert committed["result"]["source_balances"][0]["available"] == "0"

            status, headers, replayed, _ = _post(
                server, issued, "/api/v1/rent/movements", receipt_body, key=key
            )
            assert status == 200 and headers["X-Idempotent-Replayed"] == "true"
            assert replayed == committed

            oracle = _overview(server, issued)
            assert oracle["summary"]["selected_month_obligation"] == "500000"
            assert oracle["summary"]["selected_month_allocated"] == "300000"
            assert oracle["summary"]["selected_month_outstanding"] == "200000"
            assert oracle["rows"][0]["effective_amount"] == "500000"
            assert oracle["rows"][0]["allocated"] == "300000"
            assert oracle["rows"][0]["balance"] == "200000"

            filtered = _overview(
                server, issued, search="Synthetic%20Unit",
                property_id=str(ids["property_id"]), unit_id=str(ids["unit_id"]),
            )
            assert len(filtered["rows"]) == 1 and filtered["summary"]["selected_month_outstanding"] == "200000"
            none = _overview(server, issued, search="no-such-resident")
            assert none["rows"] == []
            assert none["summary"]["selected_month_outstanding"] == "0"
            assert none["summary"]["all_period_outstanding"] == "0"

            # Representative deliberately-unallocated/prepayment-like source.
            ctx = _context(server, issued)
            unallocated_body = {
                "account_id": state["account_id"],
                "occurred_on": "2026-09-28",
                "amount": "100000",
                "currency": "KRW",
                "payer_raw": "Synthetic unallocated payer",
                "counterparty_party_id": None,
                "attribution": {"status": "UNMATCHED", "property_id": None, "contract_id": None},
                "allocations": [],
                "expected_ledger_revision": ctx["ledger_revision"],
            }
            status, _, unallocated, _ = _post(server, issued, "/api/v1/rent/movements", unallocated_body)
            assert status == 200
            unallocated_source = unallocated["result"]["source_balances"][0]
            assert unallocated_source["principal"] == "100000"
            assert unallocated_source["allocated"] == "0"
            assert unallocated_source["available"] == "100000"
            unallocated_source_id = unallocated_source["source_id"]

            # Append a correction and its explicit reversal without replacing the original fact.
            ctx = _context(server, issued)
            current = _overview(server, issued)["rows"][0]
            adjust = {
                "delta": "10000",
                "reason": "Synthetic W2-B correction history",
                "expected_version": current["version"],
                "expected_ledger_revision": ctx["ledger_revision"],
                "allocation_correction": _empty_plan(),
                "reverse_adjustment_id": None,
            }
            status, _, adjusted, _ = _post(
                server, issued, f"/api/v1/receivables/{state['receivable_id']}/adjustments", adjust
            )
            assert status == 200
            adjustment = next(e for e in adjusted["result"]["entities"] if e["kind"] == "ADJUSTMENT")
            assert adjusted["result"]["receivable_balances"][0]["effective_amount"] == "510000"

            ctx = _context(server, issued)
            current = _overview(server, issued)["rows"][0]
            reverse = {
                "delta": "-10000",
                "reason": "Reverse synthetic W2-B correction",
                "expected_version": current["version"],
                "expected_ledger_revision": ctx["ledger_revision"],
                "allocation_correction": _empty_plan(),
                "reverse_adjustment_id": adjustment["id"],
            }
            status, _, restored, _ = _post(
                server, issued, f"/api/v1/receivables/{state['receivable_id']}/adjustments", reverse
            )
            assert status == 200
            balance = restored["result"]["receivable_balances"][0]
            assert (balance["effective_amount"], balance["allocated"], balance["balance"]) == (
                "500000", "300000", "200000"
            )

            # RECORD_REFUND records a previously confirmed external transfer; it does not initiate one.
            ctx = _context(server, issued)
            source = next(s for s in ctx["funding_sources"]["rows"] if s["source_id"] == unallocated_source_id)
            refund = {
                "source_id": source["source_id"],
                "expected_source_version": source["version"],
                "expected_ledger_revision": ctx["ledger_revision"],
                "out_account_id": state["account_id"],
                "occurred_on": "2026-09-28",
                "amount": "40000",
                "payee_raw": "Synthetic refund recipient",
                "actual_transfer_confirmed": True,
                "reason": "Record synthetic already-confirmed external refund",
            }
            status, _, refund_result, _ = _post(server, issued, "/api/v1/refund-records", refund)
            assert status == 200
            refunded_source = refund_result["result"]["source_balances"][0]
            assert refunded_source["principal"] == "100000"
            assert refunded_source["returned"] == "40000"
            assert refunded_source["available"] == "60000"

            status, _, _, ledger = _http(server, "GET", "/rent/data/ledger", headers=cookie)
            assert status == 200
            kinds = [event["kind"] for event in ledger["events"]]
            assert "RECEIVABLE" in kinds
            assert "MOVEMENT_REVISION" in kinds
            assert "ALLOCATION" in kinds
            assert kinds.count("RECEIVABLE_ADJUSTMENT") == 2
            assert "REFUND_RECORD" in kinds
            refund_event = next(event for event in ledger["events"] if event["kind"] == "REFUND_RECORD")
            assert refund_event["actual_transfer_confirmed"] is True

            # Exact oracle still holds after correction + reversal and an unrelated refund record.
            final = _overview(server, issued)
            assert final["summary"]["selected_month_obligation"] == "500000"
            assert final["summary"]["selected_month_allocated"] == "300000"
            assert final["summary"]["selected_month_outstanding"] == "200000"


def test_pagination_summary_is_whole_query_not_page_subtotal():
    with operator_fixture(today=date(2026, 9, 28)) as (fixture, setup_service, ids):
        _setup_finance(setup_service, ids)
        revision = setup_service.repository.read_operator_snapshot(lambda tx: str(tx.ledger_revision_before))
        resident_id, revision, _ = create_resident(
            setup_service, revision=revision, name="Synthetic Resident 2"
        )
        contract = _contract_with_rent(setup_service, ids, resident_id, revision)
        _issue(setup_service, contract)
        with _integrated_server(fixture, ids) as (server, issued):
            first = _overview(server, issued, page=1, page_size=1)
            second = _overview(server, issued, page=2, page_size=1)
            assert first["pagination"]["total_rows"] == 2
            assert first["pagination"]["page_rows"] == 1
            assert second["pagination"]["page_rows"] == 1
            assert first["rows"][0]["receivable_id"] != second["rows"][0]["receivable_id"]
            assert first["summary"] == second["summary"]
            assert first["summary"]["selected_month_obligation"] == "1000000"
            assert first["summary"]["selected_month_outstanding"] == "1000000"
            assert first["summary"]["summary_scope"] == "WHOLE_FILTERED_QUERY_NOT_PAGE"


_BROWSER_SCRIPT = r'''
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const [base, token, modulePath] = process.argv.slice(2);
const { chromium } = await import(pathToFileURL(modulePath).href);
const browser = await chromium.launch({headless:true});
try {
  for (const width of [320,390,768,1280]) {
    const context = await browser.newContext({viewport:{width,height:900}});
    try {
      await context.addCookies([{name:'rent_session',value:token,url:base,httpOnly:true,sameSite:'Strict'}]);
      const page = await context.newPage();
      const errors=[]; page.on('pageerror',e=>errors.push(e.message));
      const response=await page.goto(base+'/rent');
      assert.equal(response.status(),200);
      await page.locator('#rentRows tr').first().waitFor();
      const layout=await page.evaluate(()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth}));
      assert.equal(layout.width,width);
      assert.ok(layout.scrollWidth<=width,`horizontal overflow ${layout.scrollWidth}>${width}`);
      assert.equal(await page.locator('#allocationPolicy').innerText(),'자동 배분 OFF · 명시적 배분만');
      await page.getByRole('button',{name:'입금·배분'}).click();
      await page.locator('#previewPayment').waitFor();
      await page.getByRole('button',{name:'원장'}).click();
      await page.locator('#ledgerEvents').waitFor();
      await page.getByRole('button',{name:'정정·환불 기록'}).click();
      await page.locator('#correctionAction').waitFor();
      assert.deepEqual(errors,[]);
    } finally { await context.close(); }
  }

  const context=await browser.newContext({viewport:{width:390,height:900}});
  try {
    await context.addCookies([{name:'rent_session',value:token,url:base,httpOnly:true,sameSite:'Strict'}]);
    const page=await context.newPage();
    const seen=[];
    await page.route('**/api/v1/rent/movements',async route=>{
      seen.push(route.request().headers()['idempotency-key']);
      await route.abort();
    });
    await page.goto(base+'/rent');
    await page.locator('#rentRows tr').first().waitFor();
    await page.getByRole('button',{name:'입금·배분'}).click();
    await page.locator('#payAmount').fill('12345');
    await page.locator('#payPayer').fill('Synthetic browser timeout');
    await page.locator('#previewPayment').click();
    assert.equal(await page.locator('#operationState').getAttribute('data-operation-state'),'READY_FOR_CONFIRMATION');
    const preview=JSON.parse(await page.locator('#operationPreview').innerText());
    const stableKey=preview.idempotency_key;
    await page.locator('#operationConfirm').check();
    await page.locator('#submitOperation').evaluate(el=>{el.click();el.click();});
    await page.waitForFunction(()=>document.querySelector('#operationState')?.dataset.operationState==='RESULT_UNKNOWN');
    assert.equal(seen.length,1,'duplicate click created a second request');
    assert.equal(seen[0],stableKey);
    await page.locator('#checkOperation').click();
    await page.waitForFunction(()=>document.querySelector('#operationState')?.dataset.operationState==='RETRY_SAME_OPERATION');
    await page.locator('#retryOperation').click();
    await page.waitForFunction(()=>document.querySelector('#operationState')?.dataset.operationState==='RESULT_UNKNOWN');
    assert.equal(seen.length,2);
    assert.equal(seen[1],stableKey,'retry generated a new Idempotency-Key');
  } finally { await context.close(); }
} finally { await browser.close(); }
console.log(JSON.stringify({status:'PASS',viewports:[320,390,768,1280],timeout_same_key:true,duplicate_click:true}));
'''


def test_browser_320_390_768_1280_timeout_same_key_and_duplicate_click():
    module = os.environ.get("PROPERTYAI_W1B_PLAYWRIGHT_ENTRY")
    node = os.environ.get("PROPERTYAI_W1B_NODE")
    if not module or not node:
        pytest.skip("explicit existing browser runtime binding required")
    with operator_fixture(today=date(2026, 9, 28)) as (fixture, setup_service, ids):
        _setup_finance(setup_service, ids)
        with _integrated_server(fixture, ids) as (server, issued):
            base = "http://127.0.0.1:" + str(server.server_address[1])
            process = subprocess.run(
                [node, "--input-type=module", "-", base, issued.token, module],
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
                "viewports": [320, 390, 768, 1280],
                "timeout_same_key": True,
                "duplicate_click": True,
            }

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
from threading import Thread
from uuid import UUID, uuid4

import psycopg
import pytest

from propertyai_core.adapters.postgres.rent_repository import RentPostgresRepository
from propertyai_core.adapters.postgres.rent_session_store import PostgresSessionStore
from propertyai_core.application.handlers.rent import BusinessDateProvider, RentService
from propertyai_core.application.rent_errors import RentError
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.test_corrections import _empty_plan, _replacement_plan, _setup_finance
from propertyai_core.tests.rent_finance.test_corrections_api import _body
from propertyai_core.web.auth_context import AuthorizedPrincipal, ServerPrincipalDirectory
from propertyai_core.web.auth_session import (
    InMemorySessionStore,
    RequestAuthenticator,
    SessionService,
)
from propertyai_core.web.empty_shell import EmptyShell, ShellSnapshot, ShellState
from propertyai_core.web.rent_api import RentAPI
from propertyai_core.web.rent_integration import (
    AuthoritativeEmptyShellLoader,
    PrincipalRentServiceResolver,
    RentDatabaseBinding,
    StaticRentDatabaseBindings,
)
from propertyai_core.web.rent_server import local_test_http_server

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_112 = REPO_ROOT / "db/v2_2_1/migration/V20260904.112__rent_auth_sessions.sql"
SUBJECT = "synthetic:i1:verified-subject"


@dataclass
class CaptureRepository:
    authorized_organization_id: UUID
    authorized_actor_party_id: UUID


class CaptureService:
    def __init__(self, principal: AuthorizedPrincipal):
        self.repository = CaptureRepository(principal.organization_id, principal.actor_party_id)
        self.commands = []

    def handle(self, command):
        self.commands.append(command)
        return {"operation": command.operation_id, "target": str(command.target_id) if command.target_id else None}, False


class FixedResolver:
    def __init__(self, service):
        self.service = service

    def resolve(self, _principal):
        return self.service


def _principal(*, capabilities=frozenset({"READ", "WRITE"})) -> AuthorizedPrincipal:
    return AuthorizedPrincipal(uuid4(), uuid4(), SUBJECT, capabilities)


def _issued_api(principal: AuthorizedPrincipal):
    sessions = SessionService(
        InMemorySessionStore(), ServerPrincipalDirectory([principal]), runtime="ISOLATED_TEST"
    )
    issued = sessions.issue(SUBJECT)
    service = CaptureService(principal)
    api = RentAPI(authenticator=RequestAuthenticator(sessions), services=FixedResolver(service))
    headers = {
        "Cookie": "rent_session=" + issued.token,
        "X-CSRF-Token": issued.csrf_token,
        "Content-Type": "application/json",
        "Idempotency-Key": str(uuid4()),
    }
    return api, sessions, issued, service, headers


def test_shared_dispatcher_all_six_corrections_and_auth_fail_closed():
    principal = _principal()
    api, sessions, issued, service, headers = _issued_api(principal)
    target = uuid4()
    routes = [
        (f"/api/v1/receivables/{target}/adjustments", "adjustReceivable"),
        (f"/api/v1/receivables/{target}/void", "voidReceivable"),
        ("/api/v1/allocation-corrections", "correctAllocations"),
        (f"/api/v1/movements/{target}/corrections", "correctMovement"),
        ("/api/v1/refund-records", "recordRefund"),
        (f"/api/v1/refund-records/{target}/corrections", "correctRefund"),
    ]
    for path, operation in routes:
        request_headers = dict(headers, **{"Idempotency-Key": str(uuid4())})
        response = api.handle("POST", path, request_headers, json.dumps(_body(operation)).encode())
        assert response.status == 200
        assert response.body["operation"] == operation
        assert response.headers["X-Request-ID"]
    assert [command.operation_id for command in service.commands] == [op for _, op in routes]

    no_session = {k: v for k, v in headers.items() if k != "Cookie"}
    response = api.handle("POST", routes[0][0], no_session, json.dumps(_body("adjustReceivable")).encode())
    assert response.status == 401 and response.body["code"] == "UNAUTHENTICATED"

    for csrf in (None, "wrong", sessions.issue(SUBJECT).csrf_token):
        bad = dict(headers)
        if csrf is None:
            bad.pop("X-CSRF-Token")
        else:
            bad["X-CSRF-Token"] = csrf
        response = api.handle("POST", routes[0][0], bad, json.dumps(_body("adjustReceivable")).encode())
        assert response.status == 403 and response.body["code"] == "NOT_AUTHORIZED"

    read_only = AuthorizedPrincipal(principal.organization_id, principal.actor_party_id, SUBJECT, frozenset({"READ"}))
    ro_sessions = SessionService(
        InMemorySessionStore(), ServerPrincipalDirectory([read_only]), runtime="ISOLATED_TEST"
    )
    ro_issued = ro_sessions.issue(SUBJECT)
    ro_service = CaptureService(read_only)
    ro_api = RentAPI(authenticator=RequestAuthenticator(ro_sessions), services=FixedResolver(ro_service))
    ro_headers = {
        "Cookie": "rent_session=" + ro_issued.token,
        "X-CSRF-Token": ro_issued.csrf_token,
        "Content-Type": "application/json",
        "Idempotency-Key": str(uuid4()),
    }
    response = ro_api.handle("POST", routes[0][0], ro_headers, json.dumps(_body("adjustReceivable")).encode())
    assert response.status == 403 and not ro_service.commands

    mismatch_service = CaptureService(principal)
    mismatch_service.repository.authorized_organization_id = uuid4()
    mismatch_api = RentAPI(authenticator=RequestAuthenticator(sessions), services=FixedResolver(mismatch_service))
    response = mismatch_api.handle("POST", routes[0][0], headers, json.dumps(_body("adjustReceivable")).encode())
    assert response.status == 403 and not mismatch_service.commands

    forged = dict(_body("adjustReceivable"), organization_id=str(uuid4()), actor_party_id=str(uuid4()))
    before = len(service.commands)
    response = api.handle("POST", routes[0][0], dict(headers, **{"Idempotency-Key": str(uuid4())}), json.dumps(forged).encode())
    assert response.status == 422 and len(service.commands) == before

    response = api.handle("POST", "/api/v1/not-a-route", headers, b"{}")
    assert response.status == 404 and response.body["code"] == "NOT_FOUND"


def _http(server, method: str, path: str, *, headers=None, body: bytes | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    finally:
        connection.close()


class MutableClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self):
        return self.now


def test_durable_session_empty_loader_finance_http_idempotency_and_logout():
    assert MIGRATION_112.is_file()
    with operator_fixture(today=date(2026, 9, 28), durability=True) as (fixture, setup_service, ids):
        fixture.cluster.psql(file=MIGRATION_112)
        suffix = uuid4().hex[:10]
        session_login = "rent_session_" + suffix
        fixture.cluster.psql(sql_text=f"""
            CREATE ROLE {session_login} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            GRANT propertyai_app_runtime TO {session_login};
        """)
        principal = AuthorizedPrincipal(
            ids["organization_id"], ids["party_id"], SUBJECT, frozenset({"READ", "WRITE"})
        )
        directory = ServerPrincipalDirectory([principal])
        connect_session = lambda: psycopg.connect(fixture.cluster.login_dsn(session_login))
        store = PostgresSessionStore(connect_session)
        sessions = SessionService(store, directory, runtime="PRODUCTION", ttl=timedelta(minutes=30))

        persisted = sessions.issue(SUBJECT)
        digest = hashlib.sha256(persisted.token.encode("ascii")).hexdigest()
        columns = {row[0] for row in fixture.query(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='propertyai' AND table_name='rent_auth_session'"
        )}
        assert "token" not in columns and "token_digest" in columns
        stored_digest = fixture.query(
            "SELECT token_digest FROM propertyai.rent_auth_session WHERE session_id=%s", (persisted.session_id,)
        )[0][0]
        assert stored_digest == digest and persisted.token != stored_digest
        record = store.get(digest)
        assert record is not None and store.create(record) is False

        fixture.restart()
        after_restart = SessionService(PostgresSessionStore(connect_session), directory, runtime="PRODUCTION")
        assert after_restart.resolve(persisted.token).session_id == persisted.session_id
        assert after_restart.revoke(persisted.session_id) is True
        assert SessionService(PostgresSessionStore(connect_session), directory, runtime="PRODUCTION").resolve(persisted.token) is None

        clock = MutableClock(datetime(2026, 9, 17, tzinfo=timezone.utc))
        expiring = SessionService(
            PostgresSessionStore(connect_session), directory, runtime="PRODUCTION",
            ttl=timedelta(seconds=2), clock=clock,
        )
        expiring_token = expiring.issue(SUBJECT)
        clock.now = expiring_token.expires_at
        assert expiring.resolve(expiring_token.token) is None

        broken = SessionService(
            PostgresSessionStore(lambda: (_ for _ in ()).throw(OSError("SESSION_DB_DOWN"))),
            directory,
            runtime="PRODUCTION",
        )
        with pytest.raises(RentError) as error:
            broken.resolve("A" * 43)
        assert error.value.code == "INTERNAL_ERROR"

        active_sessions = SessionService(PostgresSessionStore(connect_session), directory, runtime="PRODUCTION")
        issued = active_sessions.issue(SUBJECT)
        binding = RentDatabaseBinding(
            ids["organization_id"], ids["party_id"],
            lambda: psycopg.connect(fixture.cluster.login_dsn(ids["login"])),
        )
        services = PrincipalRentServiceResolver(
            StaticRentDatabaseBindings({SUBJECT: binding}),
            BusinessDateProvider(lambda _timezone_name: date(2026, 9, 28)),
        )
        auth = RequestAuthenticator(active_sessions)
        api = RentAPI(authenticator=auth, services=services)
        shell = EmptyShell(auth, AuthoritativeEmptyShellLoader(services))
        server = local_test_http_server(api, shell=shell)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, _, payload = _http(server, "GET", "/app")
            assert status == 401 and json.loads(payload)["code"] == "UNAUTHENTICATED"

            cookie = {"Cookie": "rent_session=" + issued.token}
            status, _, payload = _http(server, "GET", "/app", headers=cookie)
            assert status == 200 and b'data-empty="true"' in payload

            state = _setup_finance(setup_service, ids, receipt_amount="500000", allocation_amount="500000")

            # Actual foreign resource: the authenticated org must not be able to correct it.
            suffix2 = uuid4().hex[:10]
            org2, property2, unit2, party2 = (uuid4() for _ in range(4))
            login2 = "rent_i1_cross_" + suffix2
            fixture.cluster.psql(sql_text=f"""
                CREATE ROLE {login2} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
                GRANT propertyai_rent_runtime TO {login2};
                INSERT INTO propertyai.organization(organization_id,organization_code,display_name,organization_status,data_environment)
                VALUES('{org2}','I1-ORG-{suffix2}','I1 Cross Org','ACTIVE','TEST');
                INSERT INTO propertyai.property(property_id,organization_id,property_code,display_name,timezone_name)
                VALUES('{property2}','{org2}','I1-PROP-{suffix2}','I1 Cross Property','Asia/Seoul');
                INSERT INTO propertyai.rental_unit(rental_unit_id,rental_unit_code,property_id,display_name)
                VALUES('{unit2}','I1-UNIT-{suffix2}','{property2}','I1 Cross Unit');
                INSERT INTO propertyai.party(party_id,party_code,display_name,data_environment)
                VALUES('{party2}','I1-ACTOR-{suffix2}','I1 Cross Operator','TEST');
                INSERT INTO propertyai.organization_member(organization_member_id,organization_id,party_id,membership_role,membership_status,joined_at)
                VALUES('{uuid4()}','{org2}','{party2}','OPERATOR','ACTIVE',transaction_timestamp());
                INSERT INTO propertyai.finance_ledger_scope(organization_id,revision) VALUES('{org2}',0);
                INSERT INTO propertyai.rent_runtime_binding(login_name,organization_id,actor_party_id,principal_type,capability,data_environment,enabled)
                VALUES('{login2}','{org2}','{party2}','PARTY','WRITE','TEST',true);
            """)
            service2 = RentService(
                RentPostgresRepository(lambda: psycopg.connect(fixture.cluster.login_dsn(login2))),
                BusinessDateProvider(lambda _timezone_name: date(2026, 9, 28)),
            )
            state2 = _setup_finance(
                service2,
                {"organization_id": org2, "property_id": property2, "unit_id": unit2, "party_id": party2, "login": login2},
            )
            cross_body = {
                "delta": "1",
                "reason": "Foreign organization target must stay hidden through I1 HTTP",
                "expected_version": state2["receivable_version"],
                "expected_ledger_revision": state["ledger_revision"],
                "allocation_correction": _empty_plan(),
                "reverse_adjustment_id": None,
            }
            cross_headers = {
                **cookie,
                "X-CSRF-Token": issued.csrf_token,
                "Content-Type": "application/json",
                "Idempotency-Key": str(uuid4()),
            }
            cross_path = f"/api/v1/receivables/{state2['receivable_id']}/adjustments"
            status, _, payload = _http(
                server, "POST", cross_path, headers=cross_headers, body=json.dumps(cross_body).encode()
            )
            assert status == 404 and json.loads(payload)["code"] == "NOT_FOUND"

            key = uuid4()
            body = {
                "delta": "-100000",
                "reason": "Reduce confirmed obligation to 400000 through I1 HTTP",
                "expected_version": state["receivable_version"],
                "expected_ledger_revision": state["ledger_revision"],
                "allocation_correction": _replacement_plan(state, "400000"),
                "reverse_adjustment_id": None,
            }
            request_headers = {
                **cookie,
                "X-CSRF-Token": issued.csrf_token,
                "Content-Type": "application/json",
                "Idempotency-Key": str(key),
            }
            path = f"/api/v1/receivables/{state['receivable_id']}/adjustments"
            raw = json.dumps(body).encode()
            status, response_headers, payload = _http(server, "POST", path, headers=request_headers, body=raw)
            committed = json.loads(payload)
            assert status == 200 and response_headers["X-Idempotent-Replayed"] == "false"
            balance = committed["result"]["receivable_balances"][0]
            source = committed["result"]["source_balances"][0]
            assert balance["effective_amount"] == "400000"
            assert balance["allocated"] == "400000" and balance["balance"] == "0"
            assert source["principal"] == "500000" and source["allocated"] == "400000" and source["available"] == "100000"

            original = setup_service.repository.read_rows(
                "SELECT original_amount,voided FROM propertyai.finance_receivable WHERE receivable_id=%s",
                (UUID(state["receivable_id"]),),
            )[0]
            assert original["original_amount"] == 500000 and original["voided"] is False
            adjustments = setup_service.repository.read_rows(
                "SELECT delta FROM propertyai.finance_receivable_adjustment WHERE receivable_id=%s",
                (UUID(state["receivable_id"]),),
            )
            assert [row["delta"] for row in adjustments] == [-100000]

            command_id = UUID(committed["command_id"])
            audit = fixture.query(
                "SELECT authority_scope_code,actor_party_id FROM propertyai.command_receipt WHERE command_id=%s",
                (command_id,),
            )[0]
            assert audit == ("LONGSTAY_RENT:" + str(ids["organization_id"]), ids["party_id"])

            status, response_headers, replay_payload = _http(server, "POST", path, headers=request_headers, body=raw)
            assert status == 200 and response_headers["X-Idempotent-Replayed"] == "true"
            assert json.loads(replay_payload) == committed
            assert len(setup_service.repository.read_rows(
                "SELECT adjustment_id FROM propertyai.finance_receivable_adjustment WHERE receivable_id=%s",
                (UUID(state["receivable_id"]),),
            )) == 1

            conflict = dict(body, reason="Different payload through I1 auth")
            status, _, payload = _http(
                server, "POST", path, headers=request_headers, body=json.dumps(conflict).encode()
            )
            assert status == 409 and json.loads(payload)["code"] == "IDEMPOTENCY_CONFLICT"

            status, _, payload = _http(
                server, "POST", "/auth/logout",
                headers={**cookie, "X-CSRF-Token": issued.csrf_token, "Content-Type": "application/json"},
                body=b"{}",
            )
            assert status == 200 and json.loads(payload)["status"] == "LOGGED_OUT"
            status, _, payload = _http(server, "POST", path, headers=request_headers, body=raw)
            assert status == 401 and json.loads(payload)["code"] == "UNAUTHENTICATED"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_authoritative_loader_failure_and_authorization_are_not_empty():
    principal = _principal(capabilities=frozenset({"READ"}))
    sessions = SessionService(
        InMemorySessionStore(), ServerPrincipalDirectory([principal]), runtime="ISOLATED_TEST"
    )
    issued = sessions.issue(SUBJECT)

    class BrokenServices:
        def resolve(self, _principal):
            raise OSError("DATABASE_UNAVAILABLE")

    shell = EmptyShell(RequestAuthenticator(sessions), AuthoritativeEmptyShellLoader(BrokenServices()))
    response = shell.handle("GET", "/app", {"Cookie": "rent_session=" + issued.token})
    assert response.status == 500 and b'data-empty="true"' not in response.body
    response = shell.handle("GET", "/app", {})
    assert response.status == 401 and b'data-empty="true"' not in response.body


_BROWSER_SCRIPT = r'''
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const [base, token, modulePath] = process.argv.slice(2);
const { chromium } = await import(pathToFileURL(modulePath).href);
const browser = await chromium.launch({headless:true});
try {
  for (const width of [390, 1280]) {
    const context = await browser.newContext({viewport:{width,height:844}});
    try {
      await context.addCookies([{name:'rent_session',value:token,url:base,httpOnly:true,sameSite:'Strict'}]);
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      const response = await page.goto(base + '/app');
      assert.equal(response.status(), 200);
      assert.equal(await page.locator('#content').getAttribute('data-state'), 'empty');
      const layout = await page.evaluate(() => ({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,mobile:matchMedia('(max-width:720px)').matches}));
      assert.equal(layout.width,width);
      assert.ok(layout.scrollWidth <= width);
      assert.equal(layout.mobile,width <= 720);
      assert.deepEqual(errors,[]);
    } finally { await context.close(); }
  }
} finally { await browser.close(); }
console.log(JSON.stringify({status:'PASS',viewports:[390,1280]}));
'''


def test_shared_server_representative_mobile_desktop_browser_smoke():
    module = os.environ.get("PROPERTYAI_W1B_PLAYWRIGHT_ENTRY")
    node = os.environ.get("PROPERTYAI_W1B_NODE")
    if not module or not node:
        pytest.skip("explicit existing browser runtime binding required")
    principal = _principal()
    api, sessions, issued, service, _ = _issued_api(principal)
    shell = EmptyShell(
        RequestAuthenticator(sessions),
        lambda context: ShellSnapshot(context.principal.organization_id, ShellState.EMPTY),
    )
    server = local_test_http_server(api, shell=shell)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = "http://127.0.0.1:" + str(server.server_address[1])
        process = subprocess.run(
            [node, "--input-type=module", "-", base, issued.token, module],
            input=_BROWSER_SCRIPT,
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
            env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")},
        )
        assert process.returncode == 0, process.stdout + "\n" + process.stderr
        assert json.loads(process.stdout.strip().splitlines()[-1]) == {"status": "PASS", "viewports": [390, 1280]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()

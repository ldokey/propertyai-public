from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from propertyai_core.application.rent_errors import RentError
from propertyai_core.web.auth_context import ServerPrincipalDirectory
from propertyai_core.web.auth_session import (
    InMemorySessionStore, SessionService, clear_session_cookie, session_cookie,
)
from propertyai_core.tests.rent_auth.init import (
    OTHER_ACTOR, OTHER_ORG, SUBJECT, Clock, make_sessions,
)


def test_issue_and_resolve_server_principal_without_storing_bearer():
    service, store, principal, clock = make_sessions()
    first, second = service.issue(SUBJECT), service.issue(SUBJECT)
    assert first.token != second.token and first.csrf_token != second.csrf_token
    assert first.session_id != second.session_id and first.token != first.session_id
    assert len(first.token) == len(first.csrf_token) == 43
    record = service.resolve(first.token)
    assert record.principal == principal
    assert record.issued_at == clock.now
    assert record.expires_at == clock.now + timedelta(minutes=10)
    assert record.token_digest == hashlib.sha256(first.token.encode()).hexdigest()
    assert first.token not in store._records
    for value in (record, first):
        assert first.token not in repr(value)
        assert first.csrf_token not in repr(value)
        assert SUBJECT not in repr(value)


@pytest.mark.parametrize("offset,valid", [(-1, True), (0, False), (1, False)])
def test_absolute_expiry_boundary_no_sliding_refresh(offset, valid):
    service, _, _, clock = make_sessions()
    issued = service.issue(SUBJECT)
    clock.now = issued.expires_at + timedelta(seconds=offset)
    assert (service.resolve(issued.token) is not None) is valid
    assert issued.expires_at == issued.issued_at + timedelta(minutes=10)


def test_future_issued_record_is_not_authenticated():
    service, _, _, clock = make_sessions()
    issued = service.issue(SUBJECT)
    clock.advance(seconds=-1)
    assert service.resolve(issued.token) is None


def test_logout_is_idempotent_and_does_not_revoke_other_sessions():
    service, _, _, _ = make_sessions()
    first, other = service.issue(SUBJECT), service.issue(SUBJECT)
    assert service.logout(first.token) is True
    assert service.resolve(first.token) is None
    assert service.logout(first.token) is False
    assert service.resolve(other.token) is not None
    assert service.logout("unknown") is False


def test_revocation_by_opaque_identity_is_monotonic_and_concurrent():
    service, _, _, _ = make_sessions()
    issued = service.issue(SUBJECT)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.revoke(issued.session_id), range(16)))
    assert results.count(True) == 1
    assert service.resolve(issued.token) is None
    assert service.revoke(issued.session_id) is False
    assert service.revoke("bad-identity") is False


@pytest.mark.parametrize("token", [None, "", "x", "A" * 42, "A" * 44, "A" * 100000,
                                      "\n" + "A" * 42, "한" * 43, "A" * 43, {}, []])
def test_invalid_or_unknown_session_fails_closed(token):
    service, _, _, _ = make_sessions()
    assert service.resolve(token) is None


@pytest.mark.parametrize("change", ["removed", "org", "actor", "capability-added", "capability-removed"])
def test_allowlist_change_invalidates_existing_session(change):
    _, store, principal, clock = make_sessions()

    class Directory:
        current = principal
        def resolve(self, _subject):
            return self.current

    directory = Directory()
    service = SessionService(store, directory, runtime="ISOLATED_TEST", clock=clock)
    issued = service.issue(SUBJECT)
    directory.current = {
        "removed": None,
        "org": replace(principal, organization_id=OTHER_ORG),
        "actor": replace(principal, actor_party_id=OTHER_ACTOR),
        "capability-added": replace(principal, capabilities={"READ", "WRITE", "ADMIN"}),
        "capability-removed": replace(principal, capabilities={"READ"}),
    }[change]
    assert service.resolve(issued.token) is None


def test_no_implicit_admin_or_production_test_session_fallback():
    service, store, principal, clock = make_sessions()
    with pytest.raises(RentError) as denied:
        service.issue("synthetic:unlisted")
    assert denied.value.status == 403
    with pytest.raises(ValueError, match="PRODUCTION_PERSISTENT_SESSION_ADAPTER_REQUIRED"):
        SessionService(store, ServerPrincipalDirectory([principal]))
    with pytest.raises(ValueError, match="INVALID_SESSION_RUNTIME"):
        SessionService(store, ServerPrincipalDirectory([principal]), runtime="TEST")
    empty = SessionService(InMemorySessionStore(), ServerPrincipalDirectory([]), runtime="ISOLATED_TEST", clock=clock)
    with pytest.raises(RentError) as denied:
        empty.issue(SUBJECT)
    assert denied.value.status == 403
    assert not store._records


@pytest.mark.parametrize("subject", [None, "", " ", " subject", "subject\n", {"subject": SUBJECT}])
def test_issue_rejects_unverified_shape_instead_of_request_body(subject):
    service, _, _, _ = make_sessions()
    with pytest.raises(RentError) as error:
        service.issue(subject)
    assert error.value.status == 401


def test_store_create_does_not_overwrite_token_or_identity():
    service, store, _, _ = make_sessions()
    issued = service.issue(SUBJECT)
    record = service.resolve(issued.token)
    assert store.create(record) is False
    assert store.create(replace(record, token_digest="0" * 64)) is False
    assert service.resolve(issued.token) == record


@pytest.mark.parametrize("operation", ["create", "get", "revoke"])
def test_storage_failures_are_system_errors_not_missing_sessions(operation, monkeypatch):
    service, store, _, _ = make_sessions()
    issued = service.issue(SUBJECT)
    def broken(*_args):
        raise RuntimeError("SENSITIVE_BACKEND_ERROR_MUST_NOT_LEAK")
    monkeypatch.setattr(store, operation, broken)
    call = {"create": lambda: service.issue(SUBJECT),
            "get": lambda: service.resolve(issued.token),
            "revoke": lambda: service.revoke(issued.session_id)}[operation]
    with pytest.raises(RentError) as error:
        call()
    assert error.value.code == "INTERNAL_ERROR" and error.value.status == 500
    assert "SENSITIVE" not in str(error.value.wire("synthetic-request"))


def test_malformed_store_record_and_ack_fail_closed(monkeypatch):
    service, store, _, _ = make_sessions()
    issued = service.issue(SUBJECT)
    monkeypatch.setattr(store, "get", lambda _: {"principal": "forged"})
    with pytest.raises(RentError, match="INTERNAL_ERROR"):
        service.resolve(issued.token)
    monkeypatch.setattr(store, "create", lambda _: "yes")
    with pytest.raises(RentError, match="INTERNAL_ERROR"):
        service.issue(SUBJECT)
    monkeypatch.setattr(store, "revoke", lambda *_: "yes")
    with pytest.raises(RentError, match="INTERNAL_ERROR"):
        service.revoke(issued.session_id)


def test_test_store_capacity_does_not_evict_or_authenticate_a_default():
    _, _, principal, clock = make_sessions()
    store = InMemorySessionStore(capacity=1)
    service = SessionService(store, ServerPrincipalDirectory([principal]), runtime="ISOLATED_TEST", clock=clock)
    issued = service.issue(SUBJECT)
    with pytest.raises(RentError, match="INTERNAL_ERROR"):
        service.issue(SUBJECT)
    assert service.resolve(issued.token) is not None


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(days=-1), timedelta(days=2), 600])
def test_invalid_ttl_is_rejected(ttl):
    _, store, principal, _ = make_sessions()
    with pytest.raises(ValueError, match="SESSION_TTL"):
        SessionService(store, ServerPrincipalDirectory([principal]), runtime="ISOLATED_TEST", ttl=ttl)


def test_naive_clock_is_not_treated_as_an_authenticated_session():
    service, _, _, clock = make_sessions()
    issued = service.issue(SUBJECT)
    clock.now = clock.now.replace(tzinfo=None)
    with pytest.raises(RentError, match="INTERNAL_ERROR"):
        service.resolve(issued.token)


def test_issue_and_clear_cookie_have_identical_hardened_scope():
    service, _, _, _ = make_sessions()
    issued = service.issue(SUBJECT)
    cookie, cleared = session_cookie(issued), clear_session_cookie()
    for value in (cookie, cleared):
        for attribute in ("Path=/", "Secure", "HttpOnly", "SameSite=Strict"):
            assert attribute in value
        assert "Domain=" not in value
    assert "Max-Age=600" in cookie
    assert "Max-Age=0" in cleared and "Expires=Thu, 01 Jan 1970" in cleared


def test_existing_p1_synthetic_session_contract_remains_compatible():
    # Existing P1 adapter only; no DB fixture, W1-A source, or shared file mutation.
    from propertyai_core.web.rent_api import LocalTestSessions, RentAPI, RentSession
    class ReadService:
        def overview(self, *_args):
            return {"rows": [], "selected_month_balance": "0"}
    registry = LocalTestSessions()
    registry.register("p1-synthetic", RentSession(ReadService(), "p1-csrf",
        datetime.now(timezone.utc) + timedelta(minutes=1), frozenset({"READ"})))
    api = RentAPI(registry)
    route = "/api/v1/rent/overview?month=2026-09"
    assert api.handle("GET", route, {}).status == 401
    assert api.handle("GET", route, {"Cookie": "rent_session=p1-synthetic"}).status == 200
    assert api.handle("POST", "/api/v1/rent/charge-previews",
                      {"Cookie": "rent_session=p1-synthetic"}, b"{}").status == 403

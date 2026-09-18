from __future__ import annotations

from contextlib import contextmanager
import http.client as http_client
from threading import Thread
from uuid import uuid4

import pytest

from propertyai_core.application.rent_errors import RentError
from propertyai_core.rent_runtime.common import OperationalError
from propertyai_core.rent_runtime.local_auth import (
    AUTH_BINDING_KIND,
    LOCAL_TEST_FIXED_SUBJECT,
    PersistentStagingLocalIssuer,
    fixed_local_subject,
)
from propertyai_core.rent_runtime.staging import ISOLATED_TEST, PERSISTENT_STAGING
import propertyai_core.rent_runtime.web as web_module
from propertyai_core.rent_runtime.web import RuntimeConfig, compose
from propertyai_core.web.auth_context import AuthorizedPrincipal, ServerPrincipalDirectory
from propertyai_core.web.auth_session import InMemorySessionStore, SessionService


class _PersistentMemoryStore(InMemorySessionStore):
    persistent = True


class _PrincipalExistenceStore(_PersistentMemoryStore):
    def __init__(self, *, organizations, parties):
        super().__init__()
        self._organizations = frozenset(organizations)
        self._parties = frozenset(parties)

    def create(self, record):
        if record.principal.organization_id not in self._organizations:
            raise RuntimeError("ORGANIZATION_FK_REJECTED")
        if record.principal.actor_party_id not in self._parties:
            raise RuntimeError("PARTY_FK_REJECTED")
        return super().create(record)


class _UnusedTarget:
    def connect(self, *_args, **_kwargs):
        raise AssertionError("LOCAL_LOGIN_UNIT_TEST_MUST_NOT_OPEN_DATABASE")


def _principal(*, capabilities=frozenset({"READ", "WRITE"})):
    actor_party_id = uuid4()
    return AuthorizedPrincipal(
        uuid4(),
        actor_party_id,
        fixed_local_subject(actor_party_id),
        frozenset(capabilities),
    )


def _issuer(principal: AuthorizedPrincipal, **overrides):
    values = {
        "environment": PERSISTENT_STAGING,
        "auth_binding_kind": AUTH_BINDING_KIND,
        "issuer_kind": LOCAL_TEST_FIXED_SUBJECT,
        "bind_host": "127.0.0.1",
        "actor_party_id": principal.actor_party_id,
        "allowlisted_subject": principal.subject,
    }
    values.update(overrides)
    return PersistentStagingLocalIssuer(**values)


@contextmanager
def _served(monkeypatch, *, principal=None, store=None, environment=PERSISTENT_STAGING):
    store = store or _PersistentMemoryStore()
    if principal is None:
        if environment == PERSISTENT_STAGING:
            principal = _principal()
        else:
            principal = AuthorizedPrincipal(
                uuid4(), uuid4(), "synthetic:isolated-local-login-disabled", frozenset({"READ"})
            )
    issuer = _issuer(principal) if environment == PERSISTENT_STAGING else None
    monkeypatch.setattr(web_module, "PostgresSessionStore", lambda _connect: store)
    monkeypatch.setattr(web_module, "readiness", lambda *_args: "APPLICATION_READY")
    config = RuntimeConfig(
        _UnusedTarget(),
        "WEB",
        "SESSION",
        principal,
        0,
        environment,
        None,
        issuer,
    )
    server = compose(config, {"migrations": []})
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], principal, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _request(port: int, path: str, *, method="GET", body=None, headers=None):
    connection = http_client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        payload = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, response_headers, payload
    finally:
        connection.close()


def test_fixed_local_subject_is_stable_server_owned_and_non_synthetic():
    principal = _principal()
    issuer = _issuer(principal)
    expected = "local-test:propertyai-rent:" + str(principal.actor_party_id).lower()
    assert issuer.verified_subject() == expected == principal.subject
    assert 1 <= len(expected) <= 512
    assert expected == expected.strip() and expected.isprintable()
    assert not expected.startswith("synthetic:")


@pytest.mark.parametrize(
    "override",
    [
        {"environment": ISOLATED_TEST},
        {"environment": "PRODUCTION"},
        {"auth_binding_kind": "OIDC"},
        {"issuer_kind": "OIDC"},
        {"bind_host": "0.0.0.0"},
    ],
)
def test_local_issuer_activation_is_persistent_staging_loopback_fixed_subject_only(override):
    principal = _principal()
    with pytest.raises(OperationalError, match="CONFIG_INVALID"):
        _issuer(principal, **override)


def test_local_issuer_rejects_subject_mismatch_and_zero_actor():
    principal = _principal()
    with pytest.raises(OperationalError, match="CONFIG_INVALID"):
        _issuer(principal, allowlisted_subject=fixed_local_subject(uuid4()))
    with pytest.raises(OperationalError, match="CONFIG_INVALID"):
        fixed_local_subject(type(principal.actor_party_id)(int=0))


def test_directory_rejects_not_allowlisted_and_synthetic_direct_injection():
    principal = _principal()
    service = SessionService(
        _PersistentMemoryStore(),
        ServerPrincipalDirectory([principal]),
    )
    other_actor = uuid4()
    for subject in (fixed_local_subject(other_actor), "synthetic:direct-session-bypass"):
        with pytest.raises(RentError) as denied:
            service.issue(subject)
        assert denied.value.code == "NOT_AUTHORIZED"
    assert service._store._records == {}


def test_local_login_issues_only_server_fixed_identity_and_hardened_cookie(monkeypatch):
    with _served(monkeypatch) as (port, principal, store):
        status, headers, body = _request(port, "/auth/login/local", method="POST")
        assert status == 303 and body == b""
        assert headers["location"] == "/app"
        cookie = headers["set-cookie"]
        for attribute in ("Path=/", "Secure", "HttpOnly", "SameSite=Strict"):
            assert attribute in cookie
        assert "Domain=" not in cookie
        assert principal.subject.encode() not in body
        assert len(store._records) == 1
        record = next(iter(store._records.values()))
        assert record.principal == principal


def test_local_login_rejects_caller_identity_body_query_bearer_and_public_signup(monkeypatch):
    attempts = [
        ("/auth/login/local", b'{"subject":"caller"}', {}),
        ("/auth/login/local?subject=caller", None, {}),
        ("/auth/login/local?organization_id=caller", None, {}),
        ("/auth/login/local", None, {"X-Subject": "caller"}),
        ("/auth/login/local", None, {"X-Organization-ID": str(uuid4())}),
        ("/auth/login/local", None, {"X-Actor-Party-ID": str(uuid4())}),
        ("/auth/login/local", None, {"X-Capabilities": "READ,WRITE"}),
        ("/auth/login/local", None, {"Authorization": "Bearer caller-controlled"}),
        ("/auth/login/local", None, {"Cookie": "rent_session=caller-controlled"}),
        ("/auth/signup", None, {}),
    ]
    with _served(monkeypatch) as (port, _principal_value, store):
        for path, body, headers in attempts:
            status, response_headers, _payload = _request(
                port, path, method="POST", body=body, headers=headers
            )
            assert status != 303
            assert "set-cookie" not in response_headers
            assert store._records == {}


def test_local_login_route_is_unavailable_outside_persistent_staging(monkeypatch):
    with _served(monkeypatch, environment=ISOLATED_TEST) as (port, _principal_value, store):
        status, headers, _body = _request(port, "/auth/login/local", method="POST")
        assert status == 404
        assert "set-cookie" not in headers
        assert store._records == {}


@pytest.mark.parametrize("missing", ["organization", "party"])
def test_missing_principal_database_identity_cannot_create_session_or_cookie(monkeypatch, missing):
    principal = _principal()
    organizations = set() if missing == "organization" else {principal.organization_id}
    parties = set() if missing == "party" else {principal.actor_party_id}
    store = _PrincipalExistenceStore(organizations=organizations, parties=parties)
    with _served(monkeypatch, principal=principal, store=store) as (port, _principal_value, _store):
        status, headers, body = _request(port, "/auth/login/local", method="POST")
        assert status == 500
        assert "set-cookie" not in headers
        assert b"ORGANIZATION_FK_REJECTED" not in body
        assert b"PARTY_FK_REJECTED" not in body
        assert store._records == {}


def test_capability_escalation_is_rejected_before_server_composition(monkeypatch):
    principal = _principal(capabilities={"READ", "WRITE", "ADMIN"})
    store = _PersistentMemoryStore()
    monkeypatch.setattr(web_module, "PostgresSessionStore", lambda _connect: store)
    config = RuntimeConfig(
        _UnusedTarget(),
        "WEB",
        "SESSION",
        principal,
        0,
        PERSISTENT_STAGING,
        None,
        _issuer(principal),
    )
    with pytest.raises(OperationalError, match="CONFIG_INVALID"):
        compose(config, {"migrations": []})
    assert store._records == {}

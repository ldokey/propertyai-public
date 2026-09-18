from dataclasses import FrozenInstanceError, replace
from uuid import UUID, uuid4

import pytest

from propertyai_core.application.rent_errors import RentError
from propertyai_core.web.auth_context import (
    AuthorizedPrincipal, RequestAuthContext, ServerPrincipalDirectory, require_authorized,
)
from propertyai_core.web.auth_session import RequestAuthenticator
from propertyai_core.tests.rent_auth.init import (
    ACTOR, ORG, OTHER_ACTOR, OTHER_ORG, SUBJECT, headers, make_sessions,
)


def test_principal_is_validated_immutable_and_least_privilege():
    supplied = {"READ"}
    principal = AuthorizedPrincipal(ORG, ACTOR, SUBJECT, supplied)
    supplied.add("WRITE")
    assert principal.capabilities == frozenset({"READ"})
    assert isinstance(principal.capabilities, frozenset)
    with pytest.raises(FrozenInstanceError):
        principal.organization_id = OTHER_ORG
    with pytest.raises(AttributeError):
        principal.capabilities.add("WRITE")
    assert AuthorizedPrincipal(ORG, ACTOR, SUBJECT).capabilities == frozenset()


@pytest.mark.parametrize("field,value", [
    ("organization_id", str(ORG)), ("organization_id", UUID(int=0)),
    ("actor_party_id", str(ACTOR)), ("actor_party_id", UUID(int=0)),
    ("subject", ""), ("subject", " subject"), ("subject", "subject\n"),
    ("capabilities", "READ"), ("capabilities", {"*"}),
    ("capabilities", {"READ", 3}), ("capabilities", {"READ": True}),
])
def test_principal_rejects_untrusted_shapes(field, value):
    values = dict(organization_id=ORG, actor_party_id=ACTOR, subject=SUBJECT,
                  capabilities={"READ"})
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        AuthorizedPrincipal(**values)


def test_server_directory_has_no_default_or_ambiguous_subject_binding():
    _, _, principal, _ = make_sessions()
    directory = ServerPrincipalDirectory([principal])
    assert directory.resolve(SUBJECT) == principal
    assert directory.resolve("synthetic:not-allowlisted") is None
    assert directory.resolve(None) is None
    with pytest.raises(ValueError, match="DUPLICATE_SUBJECT_BINDING"):
        ServerPrincipalDirectory([principal, replace(principal, organization_id=OTHER_ORG)])
    with pytest.raises(ValueError, match="INVALID_PRINCIPAL_BINDING"):
        ServerPrincipalDirectory([{"subject": SUBJECT, "organization_id": ORG}])


@pytest.mark.parametrize("principal", [None, {}, "anonymous"])
def test_unauthenticated_has_401_not_403(principal):
    with pytest.raises(RentError) as error:
        require_authorized(principal, organization_id=ORG, capabilities={"READ"})
    assert error.value.status == 401 and error.value.code == "UNAUTHENTICATED"


@pytest.mark.parametrize("supplied,required,allowed", [
    ((), {"READ"}, False), (("READ",), {"READ"}, True),
    (("READ",), {"WRITE"}, False), (("WRITE",), {"READ"}, False),
    (("WRITE",), {"WRITE"}, True), (("READ", "WRITE"), {"READ", "WRITE"}, True),
    (("READ",), {"READ", "WRITE"}, False), (("READ", "WRITE"), {"ADMIN"}, False),
    (("READ",), "READ", False), (("READ",), {"*"}, False),
])
def test_explicit_all_capabilities_no_implicit_wildcard_or_write_to_read(supplied, required, allowed):
    _, _, principal, _ = make_sessions(supplied)
    if allowed:
        assert require_authorized(principal, organization_id=ORG, capabilities=required) is principal
    else:
        with pytest.raises(RentError) as error:
            require_authorized(principal, organization_id=ORG, capabilities=required)
        assert error.value.status == 403 and error.value.code == "NOT_AUTHORIZED"


@pytest.mark.parametrize("target_org", [OTHER_ORG, str(ORG), UUID(int=0)])
def test_cross_organization_is_denied_even_with_write_capability(target_org):
    service, _, principal, _ = make_sessions()
    issued = service.issue(SUBJECT)
    with pytest.raises(RentError) as error:
        RequestAuthenticator(service).authenticate("GET", headers(issued),
            organization_id=target_org, capabilities={"READ"})
    assert error.value.status == 403
    assert principal.organization_id == ORG and principal.actor_party_id == ACTOR


def test_context_identity_comes_from_server_not_body_or_headers():
    service, _, principal, _ = make_sessions(("READ",))
    issued = service.issue(SUBJECT)
    client_request_id = str(uuid4())
    spoofed = {
        **headers(issued), "X-Organization-ID": str(OTHER_ORG),
        "X-Actor-Party-ID": str(OTHER_ACTOR), "X-Subject": "synthetic:admin",
        "X-Capabilities": "READ,WRITE,ADMIN", "X-Request-ID": client_request_id,
        "Authorization": "Bearer client-supplied-auth-claim",
    }
    auth = RequestAuthenticator(service)
    first, second = auth.authenticate("GET", spoofed), auth.authenticate("GET", spoofed)
    assert first.principal == second.principal == principal
    assert first.principal.capabilities == frozenset({"READ"})
    assert first.session_id == issued.session_id != issued.token
    assert first.request_id != second.request_id != client_request_id
    assert first.request_id != client_request_id
    assert UUID(first.request_id).version == UUID(second.request_id).version == 4
    assert first.csrf_token == issued.csrf_token
    with pytest.raises(TypeError):
        auth.authenticate("GET", spoofed, body={"organization_id": str(OTHER_ORG),
            "actor_party_id": str(OTHER_ACTOR), "capabilities": ["ADMIN"]})
    with pytest.raises(RentError) as denied:
        auth.authenticate("GET", spoofed, capabilities={"WRITE"})
    assert denied.value.status == 403
    assert issued.token not in repr(first) and issued.csrf_token not in repr(first)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
def test_cookie_unsafe_methods_require_current_session_synchronizer_csrf(method):
    service, _, _, _ = make_sessions()
    issued, other = service.issue(SUBJECT), service.issue(SUBJECT)
    auth = RequestAuthenticator(service)
    for csrf in (None, "", "incorrect", other.csrf_token, "한" * 43):
        request_headers = headers(issued)
        if csrf is not None:
            request_headers["X-CSRF-Token"] = csrf
        with pytest.raises(RentError) as error:
            auth.authenticate(method, request_headers, capabilities={"WRITE"})
        assert error.value.status == 403
    assert auth.authenticate(method, headers(issued, csrf=True), capabilities={"WRITE"}).session_id == issued.session_id


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_cookie_safe_methods_do_not_require_csrf(method):
    service, _, _, _ = make_sessions()
    issued = service.issue(SUBJECT)
    assert RequestAuthenticator(service).authenticate(method, headers(issued)).principal.organization_id == ORG


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "BREW", "", None])
def test_unsupported_method_fails_closed(method):
    service, _, _, _ = make_sessions()
    issued = service.issue(SUBJECT)
    with pytest.raises(RentError) as error:
        RequestAuthenticator(service).authenticate(method, headers(issued, csrf=True))
    assert error.value.status == 403


def test_authentication_failure_takes_precedence_over_missing_csrf():
    service, _, _, _ = make_sessions()
    with pytest.raises(RentError) as error:
        RequestAuthenticator(service).authenticate("POST", {"X-CSRF-Token": "A" * 43})
    assert error.value.status == 401


@pytest.mark.parametrize("kind", ["missing", "bearer-only", "duplicate-cookie", "duplicate-header",
    "newline", "nul", "oversize-cookie", "malformed-cookie", "case-spoof", "not-mapping"])
def test_invalid_or_ambiguous_request_auth_is_fail_closed(kind):
    service, _, _, _ = make_sessions()
    issued = service.issue(SUBJECT)
    valid = "rent_session=" + issued.token
    cases = {
        "missing": {}, "bearer-only": {"Authorization": "Bearer " + issued.token},
        "duplicate-cookie": {"Cookie": valid + "; " + valid},
        "duplicate-header": {"Cookie": valid, "cookie": valid},
        "newline": {"Cookie": valid + "\r\n"},
        "nul": {"Cookie": valid + "\x00"},
        "oversize-cookie": {"Cookie": "large=" + "A" * 16384 + "; " + valid},
        "malformed-cookie": {"Cookie": 'rent_session="unterminated'},
        "case-spoof": {"Cookie": "RENT_SESSION=" + issued.token},
        "not-mapping": [("Cookie", valid)],
    }
    with pytest.raises(RentError) as error:
        RequestAuthenticator(service).authenticate("GET", cases[kind])
    assert error.value.status == 401


def test_headers_are_case_insensitive_without_discarding_cookie_context():
    service, _, _, _ = make_sessions()
    issued = service.issue(SUBJECT)
    result = RequestAuthenticator(service).authenticate("POST", {
        "cOoKiE": "unrelated=one; rent_session=" + issued.token + "; another=two",
        "x-CsRf-ToKeN": issued.csrf_token,
    })
    assert result.principal.subject == SUBJECT


def test_expired_and_revoked_session_cannot_authorize_even_with_valid_csrf():
    service, _, _, clock = make_sessions()
    expired, revoked = service.issue(SUBJECT), service.issue(SUBJECT)
    service.revoke(revoked.session_id)
    auth = RequestAuthenticator(service)
    with pytest.raises(RentError) as error:
        auth.authenticate("POST", headers(revoked, csrf=True))
    assert error.value.status == 401
    clock.now = expired.expires_at
    with pytest.raises(RentError) as error:
        auth.authenticate("POST", headers(expired, csrf=True))
    assert error.value.status == 401


def test_auth_context_rejects_invalid_server_dto_fields():
    service, _, principal, _ = make_sessions()
    issued = service.issue(SUBJECT)
    valid = dict(principal=principal, session_id=issued.session_id,
                 request_id=str(uuid4()), csrf_token=issued.csrf_token)
    for field, value in (("principal", {}), ("session_id", issued.token),
                         ("request_id", "caller-request"), ("request_id", str(UUID(int=0))),
                         ("csrf_token", "")):
        with pytest.raises(ValueError):
            RequestAuthContext(**{**valid, field: value})

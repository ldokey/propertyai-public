"""Opaque sessions + synchronizer-token CSRF, without an IdP or DB bootstrap.

InMemorySessionStore is explicitly TEST ONLY. There is no login HTTP endpoint,
provider selection, credential lookup, persistent adapter, or implicit store.
Integration Owner must inject approved durable storage for a production runtime.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import re
import secrets
from threading import RLock
from typing import Literal, Protocol
from uuid import UUID, uuid4

from propertyai_core.application.rent_errors import RentError, rent_error
from propertyai_core.web.auth_context import (
    AuthorizedPrincipal, PrincipalDirectory, RequestAuthContext,
    _valid_subject, require_authorized,
)

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{32}\Z")
COOKIE_NAME = "rent_session"  # Existing P1 name; P1 registry/entrypoint stay unchanged.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_METHODS = _SAFE_METHODS | {"POST", "PUT", "PATCH", "DELETE"}


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AWARE_SESSION_TIME_REQUIRED")
    return value.astimezone(timezone.utc)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str = field(repr=False)
    token_digest: str = field(repr=False)
    principal: AuthorizedPrincipal = field(repr=False)
    csrf_token: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.principal, AuthorizedPrincipal):
            raise ValueError("INVALID_SESSION_PRINCIPAL")
        for value, pattern in ((self.session_id, _SESSION_ID), (self.token_digest, _DIGEST),
                               (self.csrf_token, _TOKEN)):
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise ValueError("INVALID_SESSION_RECORD")
        object.__setattr__(self, "issued_at", _utc(self.issued_at))
        object.__setattr__(self, "expires_at", _utc(self.expires_at))
        if self.expires_at <= self.issued_at:
            raise ValueError("INVALID_SESSION_LIFETIME")
        if self.revoked_at is not None:
            object.__setattr__(self, "revoked_at", _utc(self.revoked_at))


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Bearer value returned once to the trusted caller, never stored or logged."""
    token: str = field(repr=False)
    session_id: str = field(repr=False)
    csrf_token: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime


class SessionStore(Protocol):
    """Narrow future persistence seam (no migrations in W1-B).

    create must atomically reject duplicate token digests AND session identities.
    get must read authoritative committed state without a stale local cache.
    revoke is idempotent and monotonic: once it returns, subsequent get calls
    cannot resurrect that identity. Storage unavailability must raise, not mean
    'missing'. A persistent adapter must acknowledge writes only after durability.
    In-flight requests already authorized before revocation are an I1 boundary.
    """
    persistent: bool

    def create(self, record: SessionRecord) -> bool: ...
    def get(self, token_digest: str) -> SessionRecord | None: ...
    def revoke(self, session_id: str, revoked_at: datetime) -> bool: ...


class InMemorySessionStore:
    """Bounded, thread-safe, process-local synthetic TEST storage. NOT durable."""
    persistent = False

    def __init__(self, *, capacity: int = 1024):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("INVALID_TEST_SESSION_CAPACITY")
        self._capacity = capacity
        self._records: dict[str, SessionRecord] = {}
        self._identities: dict[str, str] = {}
        self._lock = RLock()

    def create(self, record: SessionRecord) -> bool:
        if not isinstance(record, SessionRecord):
            raise ValueError("INVALID_SESSION_RECORD")
        with self._lock:
            if record.token_digest in self._records or record.session_id in self._identities:
                return False
            if len(self._records) >= self._capacity:
                raise RuntimeError("TEST_SESSION_STORE_FULL")
            self._records[record.token_digest] = record
            self._identities[record.session_id] = record.token_digest
            return True

    def get(self, token_digest: str) -> SessionRecord | None:
        with self._lock:
            return self._records.get(token_digest)

    def revoke(self, session_id: str, revoked_at: datetime) -> bool:
        revoked_at = _utc(revoked_at)
        with self._lock:
            digest = self._identities.get(session_id)
            if digest is None:
                return False
            record = self._records[digest]
            if record.revoked_at is not None:
                return False
            self._records[digest] = replace(record, revoked_at=revoked_at)
            return True


class SessionService:
    def __init__(
        self, store: SessionStore, principals: PrincipalDirectory, *,
        runtime: Literal["PRODUCTION", "ISOLATED_TEST"] = "PRODUCTION",
        ttl: timedelta = timedelta(hours=8),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if runtime not in {"PRODUCTION", "ISOLATED_TEST"}:
            raise ValueError("INVALID_SESSION_RUNTIME")
        if runtime == "PRODUCTION" and getattr(store, "persistent", False) is not True:
            raise ValueError("PRODUCTION_PERSISTENT_SESSION_ADAPTER_REQUIRED")
        if not isinstance(ttl, timedelta) or not timedelta(seconds=1) <= ttl <= timedelta(days=1):
            raise ValueError("SESSION_TTL_MUST_BE_1_SECOND_TO_1_DAY")
        self._store, self._principals, self._ttl, self._clock = store, principals, ttl, clock

    def issue(self, verified_subject: str) -> IssuedSession:
        """SERVER ONLY, after upstream identity verification. Never bind body claims.

        Allowlist resolution (including org/actor/capabilities) occurs server-side.
        No existing caller-supplied session token can be adopted or upgraded.
        """
        if not _valid_subject(verified_subject):
            raise rent_error("UNAUTHENTICATED")
        try:
            principal = self._principals.resolve(verified_subject)
            if principal is None:
                raise rent_error("NOT_AUTHORIZED")
            if not isinstance(principal, AuthorizedPrincipal) or principal.subject != verified_subject:
                raise rent_error("INTERNAL_ERROR")
            now = _utc(self._clock())
            for _ in range(3):
                token = secrets.token_urlsafe(32)
                record = SessionRecord(
                    session_id=secrets.token_urlsafe(24), token_digest=_token_digest(token),
                    principal=principal, csrf_token=secrets.token_urlsafe(32),
                    issued_at=now, expires_at=now + self._ttl,
                )
                created = self._store.create(record)
                if type(created) is not bool:
                    raise rent_error("INTERNAL_ERROR")
                if created:
                    return IssuedSession(token, record.session_id, record.csrf_token, now, record.expires_at)
            raise rent_error("INTERNAL_ERROR")
        except RentError:
            raise
        except Exception:
            raise rent_error("INTERNAL_ERROR") from None

    def resolve(self, token: str | None) -> SessionRecord | None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return None
        try:
            digest = _token_digest(token)
            record = self._store.get(digest)
            if record is None:
                return None
            if not isinstance(record, SessionRecord) or not hmac.compare_digest(record.token_digest, digest):
                raise rent_error("INTERNAL_ERROR")
            now = _utc(self._clock())
            if record.revoked_at is not None or not record.issued_at <= now < record.expires_at:
                return None
            # Rebinding, removed allowlist membership, or ANY capability change
            # invalidates the old session; new grants never leak into an old token.
            current = self._principals.resolve(record.principal.subject)
            if current != record.principal:
                return None
            return record
        except RentError:
            raise
        except Exception:
            raise rent_error("INTERNAL_ERROR") from None

    def revoke(self, session_id: str) -> bool:
        """Server-side revocation by non-bearer identity; not a public admin route."""
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            return False
        try:
            result = self._store.revoke(session_id, _utc(self._clock()))
            if type(result) is not bool:
                raise rent_error("INTERNAL_ERROR")
            return result
        except RentError:
            raise
        except Exception:
            raise rent_error("INTERNAL_ERROR") from None

    def logout(self, token: str | None) -> bool:
        """Trusted server call; HTTP callers MUST first pass the cookie CSRF gate."""
        record = self.resolve(token)
        return self.revoke(record.session_id) if record else False


def session_cookie(session: IssuedSession) -> str:
    if not isinstance(session, IssuedSession) or not _TOKEN.fullmatch(session.token):
        raise ValueError("INVALID_ISSUED_SESSION")
    max_age = int((_utc(session.expires_at) - _utc(session.issued_at)).total_seconds())
    if max_age <= 0:
        raise ValueError("INVALID_SESSION_LIFETIME")
    return (f"{COOKIE_NAME}={session.token}; Path=/; Max-Age={max_age}; "
            "Secure; HttpOnly; SameSite=Strict")


def clear_session_cookie() -> str:
    return (f"{COOKIE_NAME}=; Path=/; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; "
            "Secure; HttpOnly; SameSite=Strict")


def _headers(headers: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    if not isinstance(headers, Mapping):
        raise rent_error("UNAUTHENTICATED")
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise rent_error("UNAUTHENTICATED")
        name = key.lower()
        if name in normalized or any(ch in key + value for ch in "\r\n\x00"):
            raise rent_error("UNAUTHENTICATED")
        normalized[name] = value
    return normalized


def _cookie_token(header: str) -> str | None:
    if len(header) > 16384:
        return None
    # SimpleCookie alone silently chooses the last duplicate. Reject ambiguity.
    occurrences = re.findall(r"(?:^|;)\s*" + COOKIE_NAME + r"\s*=", header)
    if len(occurrences) != 1:
        return None
    jar = SimpleCookie()
    try:
        jar.load(header)
    except (CookieError, TypeError, ValueError):
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel and _TOKEN.fullmatch(morsel.value) else None


class RequestAuthenticator:
    """Cookie-only boundary; identity/authorization headers and body are ignored."""
    def __init__(self, sessions: SessionService):
        self.sessions = sessions

    def authenticate(
        self, method: str, headers: Mapping[str, str], *,
        organization_id: UUID | None = None, capabilities: Iterable[str] = (),
    ) -> RequestAuthContext:
        request_id = str(uuid4())
        h = _headers(headers)
        record = self.sessions.resolve(_cookie_token(h.get("cookie", "")))
        if record is None:
            raise rent_error("UNAUTHENTICATED")
        if not isinstance(method, str) or method.upper() not in _METHODS:
            raise rent_error("NOT_AUTHORIZED")
        if method.upper() not in _SAFE_METHODS:
            csrf = h.get("x-csrf-token", "")
            if not _TOKEN.fullmatch(csrf) or not hmac.compare_digest(csrf, record.csrf_token):
                raise rent_error("NOT_AUTHORIZED")
        require_authorized(record.principal, organization_id=organization_id, capabilities=capabilities)
        return RequestAuthContext(record.principal, record.session_id, request_id, record.csrf_token)

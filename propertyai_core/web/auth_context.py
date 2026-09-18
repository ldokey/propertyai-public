"""Provider-neutral, server-derived Rent authorization boundary.

Only trusted server bindings may construct principals. No body/header organization,
actor, capability or request-id claim is an authorization source. W1-A may consume
AuthorizedPrincipal and RequestAuthContext without depending on session storage.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from propertyai_core.application.rent_errors import rent_error

_CAPABILITY = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,95}\Z")
_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{32}\Z")


def _capability_set(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes, Mapping)):
        raise ValueError("INVALID_CAPABILITIES")
    result = frozenset(values)
    if any(not isinstance(value, str) or not _CAPABILITY.fullmatch(value) for value in result):
        raise ValueError("INVALID_CAPABILITIES")
    return result


def _valid_subject(subject: object) -> bool:
    return (isinstance(subject, str) and 0 < len(subject) <= 512
            and subject == subject.strip() and subject.isprintable())


@dataclass(frozen=True, slots=True)
class AuthorizedPrincipal:
    organization_id: UUID
    actor_party_id: UUID
    subject: str = field(repr=False)
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if any(not isinstance(value, UUID) or value.int == 0
               for value in (self.organization_id, self.actor_party_id)):
            raise ValueError("INVALID_PRINCIPAL_BINDING")
        if not _valid_subject(self.subject):
            raise ValueError("INVALID_PRINCIPAL_SUBJECT")
        object.__setattr__(self, "capabilities", _capability_set(self.capabilities))


class PrincipalDirectory(Protocol):
    """Server-owned allowlist; input identity MUST already be provider-verified.

    None means no currently authorized binding. An unavailable directory must
    raise, never return an empty/default administrator. Subject keys must be
    collision-free across any identity providers connected by Integration Owner.
    """

    def resolve(self, verified_subject: str) -> AuthorizedPrincipal | None: ...


class ServerPrincipalDirectory:
    """Immutable snapshot of explicitly allowed server bindings; no default user."""

    def __init__(self, principals: Iterable[AuthorizedPrincipal]):
        bindings: dict[str, AuthorizedPrincipal] = {}
        for principal in principals:
            if not isinstance(principal, AuthorizedPrincipal):
                raise ValueError("INVALID_PRINCIPAL_BINDING")
            if principal.subject in bindings:
                raise ValueError("DUPLICATE_SUBJECT_BINDING")
            bindings[principal.subject] = principal
        self._bindings = MappingProxyType(bindings)

    def resolve(self, verified_subject: str) -> AuthorizedPrincipal | None:
        return self._bindings.get(verified_subject) if _valid_subject(verified_subject) else None


@dataclass(frozen=True, slots=True)
class RequestAuthContext:
    principal: AuthorizedPrincipal
    session_id: str = field(repr=False)
    request_id: str
    csrf_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.principal, AuthorizedPrincipal):
            raise ValueError("INVALID_AUTH_CONTEXT")
        if not isinstance(self.session_id, str) or not _OPAQUE_ID.fullmatch(self.session_id):
            raise ValueError("INVALID_SESSION_IDENTITY")
        try:
            request_id = UUID(self.request_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("INVALID_REQUEST_IDENTITY") from None
        if request_id.version != 4:
            raise ValueError("INVALID_REQUEST_IDENTITY")
        if not isinstance(self.csrf_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", self.csrf_token):
            raise ValueError("INVALID_CSRF_TOKEN")


def require_authorized(
    principal: AuthorizedPrincipal | None, *,
    organization_id: UUID | None = None,
    capabilities: Iterable[str] = (),
) -> AuthorizedPrincipal:
    """Check resource organization + ALL capabilities; WRITE does not imply READ.

    organization_id is the target resource's organization (or a requested scope
    to compare), not evidence of membership. Resource lookup must itself be
    scoped to principal.organization_id by the consuming application/repository.
    """
    if not isinstance(principal, AuthorizedPrincipal):
        raise rent_error("UNAUTHENTICATED")
    if organization_id is not None and (
        not isinstance(organization_id, UUID) or organization_id != principal.organization_id
    ):
        raise rent_error("NOT_AUTHORIZED")
    try:
        required = _capability_set(capabilities)
    except (TypeError, ValueError):
        raise rent_error("NOT_AUTHORIZED") from None
    if not required.issubset(principal.capabilities):
        raise rent_error("NOT_AUTHORIZED")
    return principal

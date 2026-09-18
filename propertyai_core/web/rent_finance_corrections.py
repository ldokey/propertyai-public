"""W1-A finance correction HTTP boundary.

Authentication/session resolution remains owned by the shared/W1-B integration layer.
This module consumes an already-authorized principal and never accepts organization
or actor authority from a client DTO.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from propertyai_core.application.commands.rent import RentCommand, uuid
from propertyai_core.application.handlers.rent import RentService
from propertyai_core.application.rent_errors import RentError, rent_error


class AuthorizedPrincipalLike(Protocol):
    organization_id: UUID
    actor_party_id: UUID
    subject: str
    capabilities: frozenset[str]


@dataclass(frozen=True)
class FinanceCorrectionContext:
    service: RentService
    csrf_token: str
    principal: AuthorizedPrincipalLike


@dataclass(frozen=True)
class FinanceCorrectionResponse:
    status: int
    body: dict
    headers: dict[str, str]


_STATIC_ROUTES = {
    ("POST", "/api/v1/allocation-corrections"): "correctAllocations",
    ("POST", "/api/v1/refund-records"): "recordRefund",
}
_DYNAMIC_ROUTES = (
    (re.compile(r"^/api/v1/receivables/([^/]+)/adjustments$"), "adjustReceivable"),
    (re.compile(r"^/api/v1/receivables/([^/]+)/void$"), "voidReceivable"),
    (re.compile(r"^/api/v1/movements/([^/]+)/corrections$"), "correctMovement"),
    (re.compile(r"^/api/v1/refund-records/([^/]+)/corrections$"), "correctRefund"),
)


def _authorized(context: FinanceCorrectionContext) -> None:
    principal = context.principal
    if (
        not isinstance(principal.organization_id, UUID)
        or not isinstance(principal.actor_party_id, UUID)
        or not isinstance(principal.subject, str)
        or not principal.subject.strip()
        or not isinstance(principal.capabilities, frozenset)
        or "WRITE" not in principal.capabilities
        or not isinstance(context.csrf_token, str)
        or not context.csrf_token
    ):
        raise rent_error("NOT_AUTHORIZED")

    # When I1 provides an explicitly principal-bound repository, fail closed on
    # any mismatch. Unbound P1 test repositories remain valid behind this frozen
    # interface until the Integration Owner connects W1-B.
    repository = context.service.repository
    expected_org = getattr(repository, "authorized_organization_id", None)
    expected_actor = getattr(repository, "authorized_actor_party_id", None)
    if expected_org is not None and expected_org != principal.organization_id:
        raise rent_error("NOT_AUTHORIZED")
    if expected_actor is not None and expected_actor != principal.actor_party_id:
        raise rent_error("NOT_AUTHORIZED")


def _route(method: str, path: str) -> tuple[str, UUID | None] | None:
    operation = _STATIC_ROUTES.get((method, path))
    if operation is not None:
        return operation, None
    if method != "POST":
        return None
    for pattern, operation in _DYNAMIC_ROUTES:
        match = pattern.fullmatch(path)
        if match:
            return operation, uuid(match.group(1))
    return None


class RentFinanceCorrectionsAPI:
    """Dispatch only the six frozen W1-A correction routes.

    Return ``None`` for non-W1-A routes so the shared Integration Owner can
    compose this boundary with the existing Rent router without duplicate auth.
    """

    def handle(
        self,
        context: FinanceCorrectionContext,
        method: str,
        raw_url: str,
        headers: dict[str, str],
        raw_body: bytes = b"",
    ) -> FinanceCorrectionResponse | None:
        path = urlsplit(raw_url).path.rstrip("/") or "/"
        request_id = str(uuid4())
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        try:
            route = _route(method, path)
            if route is None:
                return None
            _authorized(context)
            if normalized_headers.get("x-csrf-token") != context.csrf_token:
                raise rent_error("NOT_AUTHORIZED")
            if len(raw_body) > 2_000_000:
                raise rent_error("VALIDATION_ERROR")
            if normalized_headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                raise rent_error("VALIDATION_ERROR")
            body = json.loads(raw_body)
            if not isinstance(body, dict):
                raise rent_error("VALIDATION_ERROR")
            key = uuid(normalized_headers.get("idempotency-key"))
            operation, target_id = route
            command = RentCommand(operation, body, key, target_id)
            result, replayed = context.service.handle(command)
            return FinanceCorrectionResponse(
                200,
                result,
                {"Content-Type": "application/json", "X-Idempotent-Replayed": str(replayed).lower()},
            )
        except RentError as exc:
            return FinanceCorrectionResponse(
                exc.status,
                exc.wire(request_id),
                {"Content-Type": "application/json"},
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            exc = rent_error("VALIDATION_ERROR")
            return FinanceCorrectionResponse(
                exc.status,
                exc.wire(request_id),
                {"Content-Type": "application/json"},
            )
        except Exception:
            exc = rent_error("INTERNAL_ERROR")
            return FinanceCorrectionResponse(
                exc.status,
                exc.wire(request_id),
                {"Content-Type": "application/json"},
            )

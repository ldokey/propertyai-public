"""W2-A Longstay contract/occupancy/master-data administration boundary.

Shared authentication, session resolution, navigation, and HTTP server wiring remain
Integration Owner responsibilities. This module consumes an already-authorized
principal plus principal-bound RentService and never accepts organization/actor
identity from client input.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from propertyai_core.application.commands.rent import RentCommand, uuid
from propertyai_core.application.handlers.rent import RentService
from propertyai_core.application.rent_errors import RentError, rent_error
from propertyai_core.web.auth_context import AuthorizedPrincipal


@dataclass(frozen=True)
class LongstayAdminContext:
    service: RentService
    csrf_token: str
    principal: AuthorizedPrincipal


@dataclass(frozen=True)
class LongstayAdminResponse:
    status: int
    body: dict | bytes
    headers: dict[str, str]


_S02 = Path(__file__).with_name("s02_contract_residency.html")
_S08 = Path(__file__).with_name("s08_master_data.html")
_CONTRACT_REVISION = re.compile(r"^/api/v1/longstay/contracts/([^/]+)/revisions$")
_ACCOUNT_REVISION = re.compile(r"^/api/v1/money-accounts/([^/]+)/revisions$")
_ADMIN_CONTRACT = re.compile(r"^/api/v1/longstay/admin/contracts/([^/]+)$")
_PAGE_ROUTES = {
    "/app/rent/contracts": _S02,
    "/app/rent/master-data": _S08,
}


def _authorized(context: LongstayAdminContext, *, write: bool) -> None:
    principal = context.principal
    if not isinstance(principal, AuthorizedPrincipal):
        raise rent_error("NOT_AUTHORIZED")
    capabilities = principal.capabilities
    # W1 auth deliberately keeps READ and WRITE orthogonal: WRITE never implies READ.
    allowed = "WRITE" in capabilities if write else "READ" in capabilities
    if (
        not isinstance(principal.organization_id, UUID)
        or not isinstance(principal.actor_party_id, UUID)
        or not isinstance(principal.subject, str)
        or not principal.subject.strip()
        or not isinstance(capabilities, frozenset)
        or not allowed
        or not isinstance(context.csrf_token, str)
        or not context.csrf_token
    ):
        raise rent_error("NOT_AUTHORIZED")

    repository = context.service.repository
    expected_org = getattr(repository, "authorized_organization_id", None)
    expected_actor = getattr(repository, "authorized_actor_party_id", None)
    # W2-A only consumes a service already bound by the shared principal resolver.
    if expected_org != principal.organization_id or expected_actor != principal.actor_party_id:
        raise rent_error("NOT_AUTHORIZED")


def _json_body(raw_body: bytes, headers: dict[str, str]) -> dict:
    normalized = {key.lower(): value for key, value in headers.items()}
    if len(raw_body) > 2_000_000:
        raise rent_error("VALIDATION_ERROR")
    if normalized.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise rent_error("VALIDATION_ERROR")
    value = json.loads(raw_body)
    if not isinstance(value, dict):
        raise rent_error("VALIDATION_ERROR")
    return value


class RentLongstayAdmin:
    """W2-A route/template contract for later shared I2 composition."""

    def handle(
        self,
        context: LongstayAdminContext,
        method: str,
        raw_url: str,
        headers: dict[str, str],
        raw_body: bytes = b"",
    ) -> LongstayAdminResponse | None:
        request_id = str(uuid4())
        normalized_method = method.upper() if isinstance(method, str) else method
        path = urlsplit(raw_url).path.rstrip("/") or "/"
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        try:
            if not isinstance(normalized_method, str):
                raise rent_error("NOT_AUTHORIZED")

            page = _PAGE_ROUTES.get(path) if normalized_method in {"GET", "HEAD"} else None
            if page is not None:
                _authorized(context, write=False)
                content = page.read_bytes()
                return LongstayAdminResponse(
                    200,
                    b"" if normalized_method == "HEAD" else content,
                    {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Request-ID": request_id},
                )

            if normalized_method == "GET" and path == "/api/v1/longstay/admin/reference-data":
                _authorized(context, write=True)
                return LongstayAdminResponse(
                    200, context.service.admin_reference_data(),
                    {"Content-Type": "application/json", "X-Request-ID": request_id},
                )

            contract_read = _ADMIN_CONTRACT.fullmatch(path) if normalized_method == "GET" else None
            if contract_read:
                _authorized(context, write=False)
                return LongstayAdminResponse(
                    200, context.service.get_contract(uuid(contract_read.group(1))),
                    {"Content-Type": "application/json", "X-Request-ID": request_id},
                )

            operation = None
            target_id = None
            if normalized_method == "POST":
                contract_revision = _CONTRACT_REVISION.fullmatch(path)
                account_revision = _ACCOUNT_REVISION.fullmatch(path)
                if contract_revision:
                    operation, target_id = "reviseContract", uuid(contract_revision.group(1))
                elif account_revision:
                    operation, target_id = "reviseAccount", uuid(account_revision.group(1))
            if operation is None:
                return None

            _authorized(context, write=True)
            if normalized_headers.get("x-csrf-token") != context.csrf_token:
                raise rent_error("NOT_AUTHORIZED")
            body = _json_body(raw_body, headers)
            key = uuid(normalized_headers.get("idempotency-key"))
            result, replayed = context.service.handle(RentCommand(operation, body, key, target_id))
            return LongstayAdminResponse(
                200,
                result,
                {
                    "Content-Type": "application/json",
                    "X-Request-ID": request_id,
                    "X-Idempotent-Replayed": str(replayed).lower(),
                },
            )
        except RentError as exc:
            return LongstayAdminResponse(
                exc.status,
                exc.wire(request_id),
                {"Content-Type": "application/json", "X-Request-ID": request_id},
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            exc = rent_error("VALIDATION_ERROR")
            return LongstayAdminResponse(
                exc.status,
                exc.wire(request_id),
                {"Content-Type": "application/json", "X-Request-ID": request_id},
            )
        except Exception:
            exc = rent_error("INTERNAL_ERROR")
            return LongstayAdminResponse(
                exc.status,
                exc.wire(request_id),
                {"Content-Type": "application/json", "X-Request-ID": request_id},
            )

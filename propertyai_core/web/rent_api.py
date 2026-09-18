"""Shared Rent Web/API dispatcher with explicit legacy-test and W1 auth composition modes."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http.cookies import SimpleCookie
import json
import re
from typing import Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from propertyai_core.application.commands.rent import COMMAND_TYPES, RentCommand, uuid
from propertyai_core.application.handlers.rent import RentService
from propertyai_core.application.rent_errors import RentError, rent_error
from propertyai_core.web.auth_context import AuthorizedPrincipal, RequestAuthContext
from propertyai_core.web.auth_session import RequestAuthenticator
from propertyai_core.web.rent_finance_corrections import FinanceCorrectionContext, RentFinanceCorrectionsAPI


@dataclass(frozen=True)
class RentSession:
    service: RentService
    csrf_token: str
    expires_at: datetime
    capabilities: frozenset[str]


class RentSessionPort(Protocol):
    def resolve(self, token: str) -> RentSession | None: ...


class RentServiceResolver(Protocol):
    def resolve(self, principal: AuthorizedPrincipal) -> RentService: ...


class LocalTestSessions:
    """Synthetic disposable TEST session registry, never an auth-provider/Production bootstrap."""

    def __init__(self):
        self._sessions: dict[str, RentSession] = {}

    def register(self, token: str, session: RentSession) -> None:
        if not token or not session.csrf_token:
            raise ValueError("EMPTY_TEST_SESSION")
        self._sessions[token] = session

    def resolve(self, token: str) -> RentSession | None:
        session = self._sessions.get(token)
        return session if session and session.expires_at > datetime.now(timezone.utc) else None


@dataclass(frozen=True)
class APIResponse:
    status: int
    body: dict
    headers: dict[str, str]


_COMMAND_ROUTES = {
    ("POST", "/api/v1/longstay/residents"): "createResident",
    ("POST", "/api/v1/longstay/contracts"): "createContract",
    ("POST", "/api/v1/money-accounts"): "createAccount",
    ("POST", "/api/v1/rent/receivables"): "issueRent",
    ("POST", "/api/v1/rent/movements"): "recordReceipt",
    ("POST", "/api/v1/rent/allocations"): "allocate",
}
_OCC_ROUTES = {
    "start-confirmations": "confirmOccupancyStart",
    "end-confirmations": "confirmOccupancyEnd",
    "date-corrections": "correctOccupancyDates",
    "room-moves": "moveOccupancy",
}
_READ_ROUTES = {
    ("GET", "/api/v1/rent/overview"): "listRent",
    ("GET", "/api/v1/funding-sources"): "listFundingSources",
    ("GET", "/api/v1/reference-data"): "getReferenceData",
    ("POST", "/api/v1/rent/charge-previews"): "previewCharge",
}
_CORRECTION_STATIC = {
    "/api/v1/allocation-corrections",
    "/api/v1/refund-records",
}
_CORRECTION_DYNAMIC = (
    re.compile(r"^/api/v1/receivables/[^/]+/adjustments$"),
    re.compile(r"^/api/v1/receivables/[^/]+/void$"),
    re.compile(r"^/api/v1/movements/[^/]+/corrections$"),
    re.compile(r"^/api/v1/refund-records/[^/]+/corrections$"),
)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _cookie_token(header: str) -> str | None:
    jar = SimpleCookie()
    try:
        jar.load(header)
    except Exception:
        return None
    item = jar.get("rent_session")
    return item.value if item else None


def _is_correction_route(method: str, path: str) -> bool:
    return method == "POST" and (path in _CORRECTION_STATIC or any(p.fullmatch(path) for p in _CORRECTION_DYNAMIC))


def _required_capability(method: str, path: str) -> str | None:
    if (method, path) in _COMMAND_ROUTES or _is_correction_route(method, path):
        return "WRITE"
    occ = re.fullmatch(r"/api/v1/longstay/occupancies/[^/]+/([^/]+)", path) if method == "POST" else None
    if occ and occ.group(1) in _OCC_ROUTES:
        return "WRITE"
    if (method, path) in _READ_ROUTES:
        return "READ"
    if method == "GET" and (
        re.fullmatch(r"/api/v1/rent/contracts/[^/]+", path)
        or re.fullmatch(r"/api/v1/rent/commands/by-key/[^/]+", path)
        or re.fullmatch(r"/api/v1/rent/commands/[^/]+", path)
    ):
        return "READ"
    return None


class RentAPI:
    """One dispatcher; W1 runtime uses RequestAuthenticator, P1 synthetic tests may use LocalTestSessions."""

    def __init__(
        self,
        sessions: RentSessionPort | None = None,
        *,
        authenticator: RequestAuthenticator | None = None,
        services: RentServiceResolver | None = None,
    ):
        integrated = authenticator is not None or services is not None
        if integrated:
            if sessions is not None or authenticator is None or services is None:
                raise ValueError("EXACTLY_ONE_RENT_AUTH_MODE_REQUIRED")
        elif sessions is None:
            raise ValueError("RENT_SESSION_BOUNDARY_REQUIRED")
        self.sessions = sessions
        self.authenticator = authenticator
        self.services = services
        self._corrections = RentFinanceCorrectionsAPI()

    @property
    def integrated_auth(self) -> bool:
        return self.authenticator is not None

    def _scope(
        self,
        method: str,
        headers: Mapping[str, str],
        required: str | None,
    ) -> tuple[RentService, frozenset[str], RequestAuthContext | None]:
        if self.authenticator is not None:
            context = self.authenticator.authenticate(
                method, headers, capabilities={required} if required else (),
            )
            try:
                service = self.services.resolve(context.principal)  # type: ignore[union-attr]
            except RentError:
                raise
            except Exception:
                raise rent_error("INTERNAL_ERROR") from None
            repository = getattr(service, "repository", None)
            if (
                getattr(repository, "authorized_organization_id", None) != context.principal.organization_id
                or getattr(repository, "authorized_actor_party_id", None) != context.principal.actor_party_id
            ):
                raise rent_error("NOT_AUTHORIZED")
            return service, context.principal.capabilities, context

        normalized = {key.lower(): value for key, value in headers.items()}
        token = _cookie_token(normalized.get("cookie", ""))
        session = self.sessions.resolve(token) if token else None  # type: ignore[union-attr]
        if session is None:
            raise rent_error("UNAUTHENTICATED")
        if method.upper() not in _SAFE_METHODS and normalized.get("x-csrf-token") != session.csrf_token:
            raise rent_error("NOT_AUTHORIZED")
        return session.service, session.capabilities, None

    def handle(self, method: str, raw_url: str, headers: Mapping[str, str], raw_body: bytes = b"") -> APIResponse:
        request_id = str(uuid4())
        normalized_method = method.upper() if isinstance(method, str) else method
        h = {key.lower(): value for key, value in headers.items()} if isinstance(headers, Mapping) else {}
        try:
            if not isinstance(normalized_method, str):
                raise rent_error("NOT_AUTHORIZED")
            url = urlsplit(raw_url)
            path = url.path.rstrip("/") or "/"
            query = parse_qs(url.query, keep_blank_values=True)
            required = _required_capability(normalized_method, path)
            service, capabilities, context = self._scope(normalized_method, headers, required)
            if context is not None:
                request_id = context.request_id

            if len(raw_body) > 2_000_000:
                raise rent_error("VALIDATION_ERROR")
            if normalized_method == "POST" and h.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                raise rent_error("VALIDATION_ERROR")

            if _is_correction_route(normalized_method, path):
                if context is None:
                    raise rent_error("NOT_AUTHORIZED")
                response = self._corrections.handle(
                    FinanceCorrectionContext(service, context.csrf_token, context.principal),
                    normalized_method, raw_url, dict(headers), raw_body,
                )
                if response is None:
                    raise rent_error("NOT_FOUND")
                body = dict(response.body)
                if "request_id" in body:
                    body["request_id"] = request_id
                return APIResponse(
                    response.status,
                    body,
                    {**response.headers, "X-Request-ID": request_id},
                )

            body = json.loads(raw_body) if normalized_method == "POST" else None
            if normalized_method == "POST" and not isinstance(body, dict):
                raise rent_error("VALIDATION_ERROR")

            operation = _COMMAND_ROUTES.get((normalized_method, path))
            target_id = None
            occupancy_match = re.fullmatch(
                r"/api/v1/longstay/occupancies/([^/]+)/([^/]+)", path
            ) if normalized_method == "POST" else None
            if occupancy_match and occupancy_match.group(2) in _OCC_ROUTES:
                target_id = uuid(occupancy_match.group(1))
                operation = _OCC_ROUTES[occupancy_match.group(2)]
            if operation:
                if "WRITE" not in capabilities:
                    raise rent_error("NOT_AUTHORIZED")
                key = uuid(h.get("idempotency-key"))
                command = RentCommand(operation, body, key, target_id)
                result, replayed = service.handle(command)
                return APIResponse(
                    200,
                    result,
                    {"Content-Type": "application/json", "X-Idempotent-Replayed": str(replayed).lower(),
                     "X-Request-ID": request_id},
                )

            read_op = _READ_ROUTES.get((normalized_method, path))
            if read_op:
                if context is None:
                    if "READ" not in capabilities and "WRITE" not in capabilities:
                        raise rent_error("NOT_AUTHORIZED")
                elif "READ" not in capabilities:
                    raise rent_error("NOT_AUTHORIZED")
                if read_op == "previewCharge":
                    result = service.preview_charge(body)
                elif read_op == "listRent":
                    month = query.get("month", [""])[0]
                    prop = query.get("property_id", [None])[0]
                    cursor = query.get("cursor", [None])[0]
                    size = query.get("page_size", ["50"])[0]
                    if len(month) != 7 or not size.isdigit():
                        raise rent_error("VALIDATION_ERROR")
                    result = service.overview(month, uuid(prop) if prop else None, uuid(cursor) if cursor else None, int(size))
                elif read_op == "listFundingSources":
                    result = service.funding_sources()
                else:
                    result = service.reference_data()
                return APIResponse(200, result, {"Content-Type": "application/json", "X-Request-ID": request_id})

            contract_match = re.fullmatch(r"/api/v1/rent/contracts/([^/]+)", path) if normalized_method == "GET" else None
            if contract_match:
                if context is None and "READ" not in capabilities and "WRITE" not in capabilities:
                    raise rent_error("NOT_AUTHORIZED")
                return APIResponse(200, service.get_contract(uuid(contract_match.group(1))),
                                   {"Content-Type": "application/json", "X-Request-ID": request_id})

            by_key = re.fullmatch(r"/api/v1/rent/commands/by-key/([^/]+)", path) if normalized_method == "GET" else None
            if by_key:
                if context is None and "READ" not in capabilities and "WRITE" not in capabilities:
                    raise rent_error("NOT_AUTHORIZED")
                command_type = query.get("command_type", [""])[0]
                if command_type not in set(COMMAND_TYPES.values()):
                    raise rent_error("VALIDATION_ERROR")
                return APIResponse(200, service.lookup_command(command_type, uuid(by_key.group(1))),
                                   {"Content-Type": "application/json", "X-Request-ID": request_id})

            by_id = re.fullmatch(r"/api/v1/rent/commands/([^/]+)", path) if normalized_method == "GET" else None
            if by_id:
                if context is None and "READ" not in capabilities and "WRITE" not in capabilities:
                    raise rent_error("NOT_AUTHORIZED")
                return APIResponse(200, service.get_command(uuid(by_id.group(1))),
                                   {"Content-Type": "application/json", "X-Request-ID": request_id})
            raise rent_error("NOT_FOUND")
        except RentError as exc:
            return APIResponse(exc.status, exc.wire(request_id),
                               {"Content-Type": "application/json", "X-Request-ID": request_id})
        except (json.JSONDecodeError, TypeError, ValueError):
            exc = rent_error("VALIDATION_ERROR")
            return APIResponse(exc.status, exc.wire(request_id),
                               {"Content-Type": "application/json", "X-Request-ID": request_id})
        except Exception:
            exc = rent_error("INTERNAL_ERROR")
            return APIResponse(exc.status, exc.wire(request_id),
                               {"Content-Type": "application/json", "X-Request-ID": request_id})

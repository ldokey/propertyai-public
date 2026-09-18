"""W1/I1 shared auth-to-Rent composition without selecting an identity provider."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from propertyai_core.adapters.postgres.rent_repository import RentPostgresRepository
from propertyai_core.application.handlers.rent import BusinessDateProvider, RentService
from propertyai_core.application.rent_errors import rent_error
from propertyai_core.web.auth_context import AuthorizedPrincipal, RequestAuthContext
from propertyai_core.web.empty_shell import ShellSnapshot, ShellState


@dataclass(frozen=True, slots=True)
class RentDatabaseBinding:
    """Server-owned principal-to-database binding; never constructed from HTTP DTOs."""

    organization_id: UUID
    actor_party_id: UUID
    connect: Callable[[], psycopg.Connection] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.organization_id, UUID) or self.organization_id.int == 0:
            raise ValueError("INVALID_RENT_DB_ORGANIZATION")
        if not isinstance(self.actor_party_id, UUID) or self.actor_party_id.int == 0:
            raise ValueError("INVALID_RENT_DB_ACTOR")
        if not callable(self.connect):
            raise ValueError("INVALID_RENT_DB_CONNECT")


class RentDatabaseBindingDirectory(Protocol):
    def resolve(self, principal: AuthorizedPrincipal) -> RentDatabaseBinding | None: ...


class StaticRentDatabaseBindings:
    """Deterministic trusted binding set suitable for I1 integration and local runtime composition."""

    def __init__(self, bindings: dict[str, RentDatabaseBinding]):
        if not isinstance(bindings, dict) or not bindings:
            raise ValueError("RENT_DB_BINDINGS_REQUIRED")
        if any(not isinstance(k, str) or not k or not isinstance(v, RentDatabaseBinding)
               for k, v in bindings.items()):
            raise ValueError("INVALID_RENT_DB_BINDING")
        self._bindings = dict(bindings)

    def resolve(self, principal: AuthorizedPrincipal) -> RentDatabaseBinding | None:
        if not isinstance(principal, AuthorizedPrincipal):
            return None
        return self._bindings.get(principal.subject)


class PrincipalRentServiceResolver:
    """Fail-closed principal -> organization/actor-scoped RentService composition."""

    def __init__(self, bindings: RentDatabaseBindingDirectory, business_date: BusinessDateProvider):
        self._bindings = bindings
        self._business_date = business_date

    def resolve(self, principal: AuthorizedPrincipal) -> RentService:
        if not isinstance(principal, AuthorizedPrincipal):
            raise rent_error("UNAUTHENTICATED")
        try:
            binding = self._bindings.resolve(principal)
        except Exception:
            raise rent_error("INTERNAL_ERROR") from None
        if not isinstance(binding, RentDatabaseBinding):
            raise rent_error("NOT_AUTHORIZED")
        if (binding.organization_id != principal.organization_id
                or binding.actor_party_id != principal.actor_party_id):
            raise rent_error("NOT_AUTHORIZED")

        def guarded_connect() -> psycopg.Connection:
            conn = binding.connect()
            try:
                conn.autocommit = True
                conn.row_factory = dict_row
                row = conn.execute(
                    "SELECT session_user AS login_name, propertyai.rent_visible_org() AS organization_id"
                ).fetchone()
                if row is None or row["organization_id"] != principal.organization_id:
                    raise rent_error("NOT_AUTHORIZED")
                return conn
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                raise

        repository = RentPostgresRepository(
            guarded_connect,
            authorized_organization_id=principal.organization_id,
            authorized_actor_party_id=principal.actor_party_id,
        )
        return RentService(repository, self._business_date)


class AuthoritativeEmptyShellLoader:
    """Return EMPTY only after a successful organization-scoped zero-row read."""

    _SQL = """
        SELECT
          (SELECT count(*) FROM propertyai.rent_contract) AS contracts,
          (SELECT count(*) FROM propertyai.finance_receivable) AS receivables,
          (SELECT count(*) FROM propertyai.finance_movement) AS movements
    """

    def __init__(self, services: PrincipalRentServiceResolver):
        self._services = services

    def __call__(self, context: RequestAuthContext) -> ShellSnapshot:
        if not isinstance(context, RequestAuthContext):
            raise rent_error("INTERNAL_ERROR")
        service = self._services.resolve(context.principal)
        rows = service.repository.read_rows(self._SQL)
        if len(rows) != 1:
            raise rent_error("INTERNAL_ERROR")
        row = rows[0]
        try:
            counts = [row[name] for name in ("contracts", "receivables", "movements")]
        except Exception:
            raise rent_error("INTERNAL_ERROR") from None
        if any(type(value) is not int or value < 0 for value in counts):
            raise rent_error("INTERNAL_ERROR")
        state = ShellState.EMPTY if sum(counts) == 0 else ShellState.LOADING
        return ShellSnapshot(context.principal.organization_id, state)

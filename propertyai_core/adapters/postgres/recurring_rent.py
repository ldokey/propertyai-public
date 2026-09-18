"""Scheduler-only PostgreSQL adapter. No schema changes or direct Finance writes."""
from __future__ import annotations

from datetime import datetime
from typing import Callable
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from propertyai_core.adapters.postgres.rent_repository import RentPostgresRepository
from propertyai_core.application.handlers.rent import BusinessDateProvider, RentService
from propertyai_core.application.recurring_rent import BillingTarget, ScanCursor, ZERO_UUID, cycle_command_key
from propertyai_core.application.rent_errors import rent_error

# Every connection is newly created by a trusted, isolated fixture-owned factory.
# No caller-supplied SQL, organization override, runtime login or SET ROLE fallback.
_AUTHORITY_QUERY = """
SELECT propertyai.rent_visible_org() AS organization_id,
       current_user = session_user AS original_login,
       pg_has_role(session_user, 'propertyai_rent_scheduler', 'MEMBER') AS scheduler,
       r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls AS privileged,
       EXISTS (SELECT FROM pg_roles other
               WHERE other.rolname NOT IN (session_user, 'propertyai_rent_scheduler')
                 AND pg_has_role(session_user, other.oid, 'MEMBER')) AS other_roles,
       EXISTS (SELECT FROM unnest(ARRAY[
          'propertyai.rent_contract', 'propertyai.rent_term_revision', 'propertyai.rent_billing_period',
          'propertyai.rent_occupancy', 'propertyai.finance_account', 'propertyai.finance_movement',
          'propertyai.finance_movement_revision', 'propertyai.finance_funding_source',
          'propertyai.finance_allocation', 'propertyai.finance_receivable_adjustment',
          'propertyai.finance_source_return']) AS t(name)
          WHERE has_table_privilege(session_user, t.name, 'INSERT,UPDATE,DELETE,TRUNCATE')) AS broad_write,
       has_table_privilege(session_user, 'propertyai.finance_receivable', 'UPDATE,DELETE,TRUNCATE')
         OR has_table_privilege(session_user, 'propertyai.finance_receivable_line', 'UPDATE,DELETE,TRUNCATE')
         AS correction_write
FROM pg_roles r WHERE r.rolname = session_user
"""

_CANDIDATE_QUERY = """
SELECT c.organization_id, c.contract_id, p.period_id, c.readiness, u.timezone_name
FROM propertyai.v_rent_contracts c
LEFT JOIN propertyai.v_rent_periods p USING (organization_id, contract_id)
LEFT JOIN propertyai.v_rent_reference_units u USING (organization_id, property_id, rental_unit_id)
WHERE c.organization_id = %s
  AND (c.contract_id, coalesce(p.period_id, %s::uuid)) > (%s::uuid, %s::uuid)
ORDER BY c.contract_id, coalesce(p.period_id, %s::uuid)
LIMIT %s
"""


class PostgresRecurringBilling:
    """Consumes only confirmed period rows and the accepted ISSUE_RENT command.

    ISOLATED_TEST is intentional: the accepted Finance DB authority is TEST-only.
    Any production adoption is a separate authorized integration, not this flag.
    """

    def __init__(self, connect: Callable[[], psycopg.Connection], *, organization_id: UUID,
                 runtime: str = "ISOLATED_TEST"):
        if runtime != "ISOLATED_TEST" or not isinstance(organization_id, UUID):
            raise ValueError("ISOLATED_SCHEDULER_BINDING_REQUIRED")
        self.organization_id = organization_id
        self._connect = connect
        self._repository = RentPostgresRepository(self._checked_connection)

    def _checked_connection(self) -> psycopg.Connection:
        conn = self._connect()
        try:
            conn.autocommit = True
            conn.row_factory = dict_row
            conn.execute("BEGIN READ ONLY")
            conn.execute("SET LOCAL statement_timeout='5s'")
            authority = conn.execute(_AUTHORITY_QUERY).fetchone()
            if (not authority or authority["organization_id"] != self.organization_id
                    or not authority["original_login"] or not authority["scheduler"]
                    or authority["privileged"] or authority["other_roles"]
                    or authority["broad_write"] or authority["correction_write"]):
                raise rent_error("NOT_AUTHORIZED")
            conn.execute("ROLLBACK")
            return conn
        except Exception:
            conn.close()
            raise rent_error("NOT_AUTHORIZED") from None

    def candidates(self, after: ScanCursor | None, limit: int) -> list[BillingTarget]:
        if type(limit) is not int or not 1 <= limit <= 501:
            raise ValueError("BOUNDED_SCAN_REQUIRED")
        if after is not None and after.organization_id != self.organization_id:
            raise rent_error("NOT_AUTHORIZED")
        rows = self._repository.read_rows(_CANDIDATE_QUERY, (
            self.organization_id, ZERO_UUID, after.contract_id if after else ZERO_UUID,
            after.period_id if after else ZERO_UUID, ZERO_UUID, limit,
        ))
        return [BillingTarget(**row) for row in rows]

    def lookup(self, key: UUID) -> dict | None:
        return self._repository.lookup("ISSUE_RENT", key)

    def _service(self, instant: datetime) -> RentService:
        if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("AWARE_CLOCK_REQUIRED")
        provider = BusinessDateProvider(lambda name: instant.astimezone(ZoneInfo(name)).date())
        return RentService(self._repository, provider, scheduler_entry_enabled=True)

    def _check_target(self, target: BillingTarget) -> None:
        if (target.organization_id != self.organization_id or target.readiness != "READY"
                or not isinstance(target.contract_id, UUID) or not isinstance(target.period_id, UUID)):
            raise rent_error("NOT_AUTHORIZED")

    def preview(self, target: BillingTarget, instant: datetime) -> dict:
        self._check_target(target)
        return self._service(instant).preview_charge({
            "contract_id": str(target.contract_id), "period_id": str(target.period_id),
        })

    def issue(self, target: BillingTarget, key: UUID, instant: datetime) -> tuple[dict, bool]:
        self._check_target(target)
        if key != cycle_command_key(target):
            raise rent_error("VALIDATION_ERROR")
        return self._service(instant).issue_from_scheduler(target.contract_id, target.period_id, key)

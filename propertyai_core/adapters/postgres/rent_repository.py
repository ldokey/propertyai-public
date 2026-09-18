"""P1-only dedicated PostgreSQL Rent repository. No Production bootstrap or shared repository edit."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
import json
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from propertyai_core.application.rent_errors import rent_error, RentError

_IDENTITIES = {
    "rent_resident": "resident_id", "rent_contract": "contract_id",
    "rent_contract_party": "contract_party_id", "rent_contract_resident": "contract_resident_id",
    "rent_term_revision": "term_id", "rent_billing_period": "period_id",
    "rent_occupancy": "occupancy_id", "finance_account": "account_id",
    "finance_receivable": "receivable_id", "finance_receivable_line": "line_id",
    "finance_movement": "movement_id", "finance_movement_revision": "movement_revision_id",
    "finance_funding_source": "source_id", "finance_allocation": "allocation_id",
    "finance_receivable_adjustment": "adjustment_id", "finance_source_return": "return_id",
}
_LOCK_ORDER = {name: index for index, name in enumerate((
    "rent_contract", "rent_occupancy", "rent_term_revision", "rent_billing_period",
    "finance_account", "finance_funding_source", "finance_receivable", "finance_movement",
))}
_RECEIVABLE_VIEW = "v_rent_receivables"
_SOURCE_VIEW = "v_rent_sources"


def _db_error(exc: Exception) -> RentError:
    state = getattr(exc, "sqlstate", None)
    diagnostic = str(exc)
    if state == "42501":
        return rent_error("NOT_AUTHORIZED")
    if state == "23P01":
        return rent_error("PERIOD_CONFLICT")
    if state == "23503":
        return rent_error("NOT_FOUND")
    if state in ("40001", "40P01", "55P03"):
        return rent_error("RETRYABLE_TRANSACTION")
    if "RENT_IDEMPOTENCY_CONFLICT" in diagnostic:
        return rent_error("IDEMPOTENCY_CONFLICT")
    if "RENT_ATTRIBUTION_CONFIRMATION_REQUIRED" in diagnostic:
        return rent_error("ATTRIBUTION_REQUIRED")
    if "RENT_READY_TERMS_INCOMPLETE" in diagnostic:
        return rent_error("CONTRACT_NOT_READY")
    if any(marker in diagnostic for marker in (
        "RENT_LINE_TERM_MISMATCH","RENT_LINE_TOTAL_MISMATCH","RENT_BALANCE_INVARIANT",
        "RENT_RECEIVABLE_BINDING_MISMATCH","RENT_REPLACEMENT_REQUIRES_",
        "RENT_ADJUSTMENT_REVERSAL_MISMATCH","RENT_REVERSAL_TARGET_INVALID",
        "RENT_MOVEMENT_CURRENT_REVISION_MISMATCH","RENT_MOVEMENT_REVERSAL_MISMATCH",
        "RENT_FUNDING_SOURCE_BINDING_MISMATCH","RENT_RECEIPT_REQUIRES_FUNDING_SOURCE",
        "RENT_RETURN_REQUIRES_OUT","RENT_RETURN_OUT_TOTAL_MISMATCH",
    )):
        return rent_error("VALIDATION_ERROR")
    if state and state.startswith("23"):
        return rent_error("VALIDATION_ERROR")
    return rent_error("INTERNAL_ERROR")


class RentPostgresTransaction:
    def __init__(self, conn: psycopg.Connection, organization_id: UUID, revision: int):
        self.conn, self.organization_id, self.ledger_revision_before = conn, organization_id, revision

    def rows(self, sql_text: str, params: tuple[Any, ...] = ()) -> list[Mapping[str, Any]]:
        # All SQL strings are fixed Application/adapter source, never HTTP input.
        return list(self.conn.execute(sql_text, params).fetchall())

    def lock_entities(self, identities: list[tuple[str, UUID]]) -> None:
        for table, identity in sorted(identities, key=lambda x: (_LOCK_ORDER.get(x[0], 99), str(x[1]))):
            if table not in _LOCK_ORDER:
                raise rent_error("VALIDATION_ERROR")
            pk = _IDENTITIES[table]
            query = sql.SQL("SELECT 1 FROM propertyai.{} WHERE organization_id=%s AND {}=%s FOR UPDATE").format(
                sql.Identifier(table), sql.Identifier(pk))
            self.conn.execute(query, (self.organization_id, identity)).fetchone()

    def fetch(self, table: str, identity: UUID) -> Mapping[str, Any] | None:
        if table not in _IDENTITIES:
            raise rent_error("VALIDATION_ERROR")
        pk = _IDENTITIES[table]
        query = sql.SQL("SELECT * FROM propertyai.{} WHERE organization_id=%s AND {}=%s").format(
            sql.Identifier(table), sql.Identifier(pk))
        return self.conn.execute(query, (self.organization_id, identity)).fetchone()

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        if table not in _IDENTITIES or values.get("organization_id") != self.organization_id:
            raise rent_error("VALIDATION_ERROR")
        keys = list(values)
        query = sql.SQL("INSERT INTO propertyai.{} ({}) VALUES ({})").format(
            sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, keys)),
            sql.SQL(",").join(sql.Placeholder() for _ in keys))
        self.conn.execute(query, tuple(values[k] for k in keys))

    def update_versioned(self, table: str, identity: UUID, expected_version: int,
                         values: Mapping[str, Any], command_id: UUID) -> Mapping[str, Any]:
        if table not in _LOCK_ORDER or table not in _IDENTITIES:
            raise rent_error("VALIDATION_ERROR")
        pk = _IDENTITIES[table]
        fields = {**values, "version": expected_version + 1, "last_command_id": command_id}
        assignments = sql.SQL(",").join(
            sql.SQL("{}=%s").format(sql.Identifier(k)) for k in fields
        )
        query = sql.SQL("UPDATE propertyai.{} SET {} WHERE organization_id=%s AND {}=%s AND version=%s RETURNING *").format(
            sql.Identifier(table), assignments, sql.Identifier(pk))
        row = self.conn.execute(query, (*fields.values(), self.organization_id, identity, expected_version)).fetchone()
        if row is None:
            raise rent_error("VERSION_CONFLICT")
        return row

    def balance(self, kind: str, identity: UUID) -> Mapping[str, Any] | None:
        view, pk = (_RECEIVABLE_VIEW, "receivable_id") if kind == "RECEIVABLE" else (_SOURCE_VIEW, "source_id") if kind == "SOURCE" else (None, None)
        if view is None:
            raise rent_error("VALIDATION_ERROR")
        return self.conn.execute(
            sql.SQL("SELECT * FROM propertyai.{} WHERE {}=%s").format(sql.Identifier(view), sql.Identifier(pk)),
            (identity,)).fetchone()


class RentPostgresRepository:
    """Connection factory must be fixture-owned, TEST-only, and explicitly bound to a Rent login."""

    def __init__(self, connect: Callable[[], psycopg.Connection], *,
                 authorized_organization_id: UUID | None = None,
                 authorized_actor_party_id: UUID | None = None):
        # Optional server-owned binding consumed by W1-A's principal boundary.
        # The resolver owns principal -> login mapping; DTOs never set these.
        if (authorized_organization_id is None) != (authorized_actor_party_id is None):
            raise rent_error("NOT_AUTHORIZED")
        if authorized_organization_id is not None and (
            not isinstance(authorized_organization_id, UUID) or not isinstance(authorized_actor_party_id, UUID)
        ):
            raise rent_error("NOT_AUTHORIZED")
        self._connect = connect
        self.authorized_organization_id = authorized_organization_id
        self.authorized_actor_party_id = authorized_actor_party_id

    def _assert_organization(self, organization_id: UUID) -> None:
        if self.authorized_organization_id is not None and organization_id != self.authorized_organization_id:
            raise rent_error("NOT_AUTHORIZED")

    def run_command(
        self, *, command_type: str, idempotency_key: UUID, normalized_hash: str,
        expected_ledger_revision: int, plan: Callable[[RentPostgresTransaction, UUID], dict],
    ) -> tuple[dict, bool]:
        if not isinstance(idempotency_key, UUID) or len(normalized_hash) != 64:
            raise rent_error("VALIDATION_ERROR")
        for attempt in range(3):
            conn = self._connect()
            ambiguous_commit = False
            try:
                conn.autocommit = True
                conn.row_factory = dict_row
                conn.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
                conn.execute("SET LOCAL lock_timeout='3s'")
                conn.execute("SET LOCAL statement_timeout='10s'")
                organization_id = conn.execute("SELECT propertyai.rent_visible_org()").fetchone()["rent_visible_org"]
                self._assert_organization(organization_id)
                revision = conn.execute("SELECT propertyai.rent_lock_scope(%s)", (organization_id,)).fetchone()["rent_lock_scope"]
                replay = conn.execute(
                    "SELECT propertyai.rent_command_result(%s,%s,%s,%s) AS result",
                    (organization_id, command_type, idempotency_key, normalized_hash),
                ).fetchone()["result"]
                if replay is not None:
                    conn.execute("ROLLBACK")
                    return replay, True
                if revision != expected_ledger_revision:
                    raise rent_error("VERSION_CONFLICT", latest_versions=[{"id":str(organization_id),"expected_version":str(revision)}])
                command_id = uuid4()
                tx = RentPostgresTransaction(conn, organization_id, revision)
                result = plan(tx, command_id)
                if not isinstance(result, dict):
                    raise rent_error("INTERNAL_ERROR")
                sealed = conn.execute(
                    "SELECT propertyai.rent_complete_command(%s,%s,%s,%s,%s,%s::jsonb) AS result",
                    (organization_id, command_id, command_type, idempotency_key, normalized_hash,
                     json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
                ).fetchone()["result"]
                conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
                ambiguous_commit = True
                conn.execute("COMMIT")
                ambiguous_commit = False
                return sealed, False
            except RentError:
                if not ambiguous_commit:
                    try: conn.execute("ROLLBACK")
                    except Exception: pass
                raise
            except Exception as exc:
                if ambiguous_commit:
                    raise rent_error("COMMIT_RESULT_UNKNOWN") from None
                try: conn.execute("ROLLBACK")
                except Exception: pass
                mapped = _db_error(exc)
                if mapped.code == "RETRYABLE_TRANSACTION" and attempt < 2:
                    continue
                raise mapped from None
            finally:
                try: conn.close()
                except Exception: pass
        raise rent_error("RETRYABLE_TRANSACTION")

    def read_rows(self, sql_text: str, params: tuple[Any, ...] = ()) -> list[Mapping[str, Any]]:
        conn = self._connect()
        try:
            conn.autocommit = True
            conn.row_factory = dict_row
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            visible = conn.execute("SELECT propertyai.rent_visible_org() AS organization_id").fetchone()
            self._assert_organization(visible["organization_id"])
            rows = list(conn.execute(sql_text, params).fetchall())
            conn.execute("COMMIT")
            return rows
        except RentError:
            raise
        except Exception as exc:
            raise _db_error(exc) from None
        finally:
            try: conn.close()
            except Exception: pass

    def lookup(self, command_type: str, key: UUID) -> dict | None:
        rows = self.read_rows(
            "SELECT propertyai.rent_command_result(propertyai.rent_visible_org(),%s,%s,NULL) AS result",
            (command_type, key),
        )
        return rows[0]["result"] if rows else None

    def read_snapshot(self, reader: Callable[[RentPostgresTransaction], Any]) -> Any:
        conn = self._connect()
        try:
            conn.autocommit = True
            conn.row_factory = dict_row
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            org = conn.execute("SELECT propertyai.rent_visible_org() AS organization_id").fetchone()["organization_id"]
            self._assert_organization(org)
            value = reader(RentPostgresTransaction(conn, org, 0))
            conn.execute("COMMIT")
            return value
        except RentError:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            raise
        except Exception as exc:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            raise _db_error(exc) from None
        finally:
            try: conn.close()
            except Exception: pass

    def read_operator_snapshot(self, reader: Callable[[RentPostgresTransaction], Any]) -> Any:
        """No business write; obtains the current frozen ledger token through the approved scope helper."""
        conn = self._connect()
        try:
            conn.autocommit = True
            conn.row_factory = dict_row
            conn.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
            conn.execute("SET LOCAL lock_timeout='3s'")
            conn.execute("SET LOCAL statement_timeout='10s'")
            org = conn.execute("SELECT propertyai.rent_visible_org() AS organization_id").fetchone()["organization_id"]
            self._assert_organization(org)
            revision = conn.execute("SELECT propertyai.rent_lock_scope(%s) AS revision", (org,)).fetchone()["revision"]
            value = reader(RentPostgresTransaction(conn, org, revision))
            conn.execute("ROLLBACK")
            return value
        except RentError:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            raise
        except Exception as exc:
            try: conn.execute("ROLLBACK")
            except Exception: pass
            raise _db_error(exc) from None
        finally:
            try: conn.close()
            except Exception: pass

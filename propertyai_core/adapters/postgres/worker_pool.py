"""Sealed W07 Production pool; application-role behavior remains separate."""

from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Iterator, Mapping

from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from propertyai_core.config.cleaner_worker import (
    WORKER_DATABASE, WORKER_LOGIN, WORKER_ROLE, worker_connection_info,
)


class CleanerWorkerSessionError(RuntimeError):
    """Static diagnostics deliberately exclude driver text and conninfo."""


_ROUTINES = (
    "propertyai.claim_integration_outbox(text,integer,integer)",
    "propertyai.complete_integration_outbox(uuid,text,bigint,text)",
    "propertyai.fail_integration_outbox(uuid,text,bigint,text,integer)",
    "propertyai.mark_outbox_pending_reconciliation(uuid,text,bigint,text,text)",
    "propertyai.resolve_outbox_reconciliation(uuid,bigint,text,text,text,integer)",
)
_READ_TABLES = (
    "reservation", "cleaning_job", "cleaning_schedule_revision",
    "integration_resource_binding", "integration_outbox",
)
_BINDING_COLUMNS = (
    "external_resource_id", "external_uid", "sync_status", "last_applied_aggregate_version",
    "external_version", "last_synced_at", "updated_at",
)


def _verify_privileges(connection: Connection) -> None:
    identity = connection.execute("""
        SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls, rolconnlimit
          FROM pg_catalog.pg_roles WHERE rolname=session_user
    """).fetchone()
    if identity != dict(rolcanlogin=True, rolinherit=False, rolsuper=False, rolcreatedb=False,
                        rolcreaterole=False, rolreplication=False, rolbypassrls=False, rolconnlimit=-1):
        raise CleanerWorkerSessionError("W07_LOGIN_ATTRIBUTES_MISMATCH")
    memberships = connection.execute("""
        SELECT r.rolname, m.inherit_option, m.set_option, m.admin_option
          FROM pg_catalog.pg_auth_members m
          JOIN pg_catalog.pg_roles r ON r.oid=m.roleid
          JOIN pg_catalog.pg_roles u ON u.oid=m.member
         WHERE u.rolname=session_user ORDER BY r.rolname
    """).fetchall()
    if memberships != [dict(rolname=WORKER_ROLE, inherit_option=False, set_option=True, admin_option=False)]:
        raise CleanerWorkerSessionError("W07_MEMBERSHIP_MISMATCH")
    capability = connection.execute("""
        SELECT NOT rolcanlogin AND NOT rolinherit AND NOT rolsuper AND NOT rolcreatedb
               AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
               AND rolconnlimit=-1
               AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members WHERE member=r.oid)
               AND NOT EXISTS (
                   SELECT 1 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles u ON u.oid=m.roleid
                    WHERE u.rolname=session_user
               ) AS valid
          FROM pg_catalog.pg_roles r WHERE rolname='propertyai_async_worker'
    """).fetchone()
    if not capability or capability["valid"] is not True:
        raise CleanerWorkerSessionError("W07_CAPABILITY_BOUNDARY_MISMATCH")
    boundary = connection.execute("""
        SELECT NOT pg_catalog.pg_has_role(session_user,'propertyai_app_runtime','SET')
               AND NOT pg_catalog.pg_has_role('propertyai_cleaner_app','propertyai_async_worker','SET')
               AND pg_catalog.has_database_privilege(session_user,current_database(),'CONNECT')
               AND pg_catalog.has_schema_privilege(current_user,'propertyai','USAGE') AS valid
    """).fetchone()
    if not boundary or boundary["valid"] is not True:
        raise CleanerWorkerSessionError("W07_ROLE_SEPARATION_MISMATCH")
    direct_writes = connection.execute("""
        SELECT NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='propertyai' AND c.relkind IN ('r','p','v','m','f')
               AND (
                   pg_catalog.has_table_privilege(session_user,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
                   OR pg_catalog.has_any_column_privilege(session_user,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
                   OR (c.relname<>'integration_resource_binding' AND (
                       pg_catalog.has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
                       OR pg_catalog.has_any_column_privilege(current_user,c.oid,'INSERT,UPDATE,REFERENCES')
                   ))
               )
        ) AS valid
    """).fetchone()
    if not direct_writes or direct_writes["valid"] is not True:
        raise CleanerWorkerSessionError("W07_DIRECT_BUSINESS_WRITE_FORBIDDEN")
    login_functions = connection.execute("""
        SELECT NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
             WHERE n.nspname='propertyai'
               AND pg_catalog.has_function_privilege(session_user,p.oid,'EXECUTE')
        ) AS valid
    """).fetchone()
    if not login_functions or login_functions["valid"] is not True:
        raise CleanerWorkerSessionError("W07_LOGIN_FUNCTION_PRIVILEGE_FORBIDDEN")
    grant_options = connection.execute("""
        SELECT NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
              CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) a
             WHERE n.nspname='propertyai' AND a.grantee=current_user::regrole AND a.is_grantable
            UNION ALL
            SELECT 1 FROM pg_catalog.pg_attribute col
              JOIN pg_catalog.pg_class c ON c.oid=col.attrelid
              JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
              CROSS JOIN LATERAL pg_catalog.aclexplode(col.attacl) a
             WHERE n.nspname='propertyai' AND a.grantee=current_user::regrole AND a.is_grantable
            UNION ALL
            SELECT 1 FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
              CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) a
             WHERE n.nspname='propertyai' AND a.grantee=current_user::regrole AND a.is_grantable
        ) AS valid
    """).fetchone()
    if not grant_options or grant_options["valid"] is not True:
        raise CleanerWorkerSessionError("W07_OBJECT_GRANT_OPTION_FORBIDDEN")
    for signature in _ROUTINES:
        row = connection.execute("""
            SELECT p.prosecdef AND owner.rolname='propertyai_owner'
                   AND p.proconfig=ARRAY['search_path=pg_catalog, propertyai, pg_temp']::text[]
                   AND pg_catalog.has_function_privilege(current_user,p.oid,'EXECUTE')
                   AND NOT pg_catalog.has_function_privilege('propertyai_cleaner_app',p.oid,'EXECUTE')
                   AND NOT pg_catalog.has_function_privilege('propertyai_app_runtime',p.oid,'EXECUTE')
                   AND NOT EXISTS (
                       SELECT 1 FROM pg_catalog.aclexplode(COALESCE(p.proacl,pg_catalog.acldefault('f',p.proowner))) a
                        WHERE a.grantee=0 AND a.privilege_type='EXECUTE'
                   ) AS valid
              FROM pg_catalog.pg_proc p
              JOIN pg_catalog.pg_roles owner ON owner.oid=p.proowner
             WHERE p.oid=pg_catalog.to_regprocedure(%s)
        """, (signature,)).fetchone()
        if not row or row["valid"] is not True:
            raise CleanerWorkerSessionError("W07_FUNCTION_PRIVILEGE_MISMATCH")
    for table in _READ_TABLES:
        row = connection.execute("SELECT pg_catalog.has_table_privilege(current_user,%s,'SELECT') AS valid", ("propertyai." + table,)).fetchone()
        if not row or row["valid"] is not True:
            raise CleanerWorkerSessionError("W07_READ_PRIVILEGE_MISMATCH")
    row = connection.execute("SELECT pg_catalog.has_table_privilege(current_user,'propertyai.integration_resource_binding','INSERT') AS valid").fetchone()
    if not row or row["valid"] is not True:
        raise CleanerWorkerSessionError("W07_BINDING_INSERT_PRIVILEGE_MISMATCH")
    binding_bounds = connection.execute("""
        SELECT NOT pg_catalog.has_table_privilege(
                   current_user,'propertyai.integration_resource_binding','UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
               AND NOT pg_catalog.has_any_column_privilege(
                   current_user,'propertyai.integration_resource_binding','REFERENCES')
               AND NOT EXISTS (
                   SELECT 1 FROM pg_catalog.pg_attribute a
                    WHERE a.attrelid='propertyai.integration_resource_binding'::regclass
                      AND a.attnum>0 AND NOT a.attisdropped AND NOT (a.attname=ANY(%s))
                      AND pg_catalog.has_column_privilege(current_user,a.attrelid,a.attnum,'UPDATE')
               ) AS valid
    """, (list(_BINDING_COLUMNS),)).fetchone()
    if not binding_bounds or binding_bounds["valid"] is not True:
        raise CleanerWorkerSessionError("W07_EXCESS_BINDING_PRIVILEGE_FORBIDDEN")
    for column in _BINDING_COLUMNS:
        row = connection.execute("SELECT pg_catalog.has_column_privilege(current_user,'propertyai.integration_resource_binding',%s,'UPDATE') AS valid", (column,)).fetchone()
        if not row or row["valid"] is not True:
            raise CleanerWorkerSessionError("W07_BINDING_UPDATE_PRIVILEGE_MISMATCH")


class PostgresWorkerPool:
    def __init__(self, *, environment: Mapping[str, str] | None = None):
        env = os.environ if environment is None else environment
        # Actual process environment must also be clean when callers supply a map.
        if any(name.startswith("PG") for name in os.environ):
            raise CleanerWorkerSessionError("W07_AMBIENT_LIBPQ_CONFIGURATION_FORBIDDEN")
        self._pool = ConnectionPool(
            conninfo=worker_connection_info(env), min_size=1, max_size=2,
            timeout=10.0, open=False,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            configure=self._normalize_and_verify, reset=self._normalize_and_verify,
        )
        self._opened = False

    def _normalize_and_verify(self, connection: Connection) -> None:
        try:
            if connection.info.transaction_status != TransactionStatus.IDLE:
                connection.rollback()
            previous_autocommit = connection.autocommit
            connection.autocommit = True
            try:
                connection.execute("DISCARD ALL")
                row = connection.execute("SELECT session_user, current_database() AS database_name").fetchone()
                if row != {"session_user": WORKER_LOGIN, "database_name": WORKER_DATABASE}:
                    raise CleanerWorkerSessionError("W07_LOGIN_IDENTITY_MISMATCH")
                connection.execute("SET ROLE propertyai_async_worker")
                connection.execute("SET TIME ZONE 'UTC'")
                connection.execute("SET search_path TO pg_catalog, propertyai")
                row = connection.execute("""SELECT session_user,current_user,current_database() AS database_name,
                    current_setting('TimeZone') AS timezone,current_setting('search_path') AS search_path""").fetchone()
                if row != dict(session_user=WORKER_LOGIN, current_user=WORKER_ROLE,
                               database_name=WORKER_DATABASE, timezone="UTC", search_path="pg_catalog, propertyai"):
                    raise CleanerWorkerSessionError("W07_SESSION_IDENTITY_MISMATCH")
                _verify_privileges(connection)
            finally:
                connection.autocommit = previous_autocommit
        except CleanerWorkerSessionError:
            raise
        except Exception:
            raise CleanerWorkerSessionError("W07_CONNECTION_VALIDATION_FAILED") from None

    def open(self) -> None:
        if not self._opened:
            try:
                self._pool.open(wait=True, timeout=10.0)
                self._opened = True
            except Exception:
                self._pool.close()
                raise CleanerWorkerSessionError("W07_CONNECTION_OPEN_FAILED") from None

    def close(self) -> None:
        self._pool.close()
        self._opened = False

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        if not self._opened:
            raise CleanerWorkerSessionError("W07_POOL_NOT_OPEN")
        with self._pool.connection(timeout=10.0) as connection:
            self._normalize_and_verify(connection)
            yield connection

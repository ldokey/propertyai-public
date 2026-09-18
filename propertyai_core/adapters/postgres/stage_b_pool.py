from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Iterator, Mapping

from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from propertyai_core.adapters.postgres.errors import PostgresRoleMismatchError


PRE_CUTOVER_MODE = "PRE_CUTOVER_MIGRATION"
_SAFE_SEARCH_PATH = "pg_catalog, propertyai"
_EXPECTED_FLYWAY_STATE = (
    ("20260904.101", "V20260904.101__v221_migrator_preflight.sql", -304722285, True),
    ("20260904.102", "V20260904.102__v221_org_identity_command.sql", -929617548, True),
    ("20260904.103", "V20260904.103__v221_reservation_cleaning.sql", -926192525, True),
    ("20260904.104", "V20260904.104__v221_offer_assignment.sql", 238888746, True),
    ("20260904.105", "V20260904.105__v221_exception_audit.sql", 306727252, True),
    ("20260904.106", "V20260904.106__v221_async_projection.sql", -1394079808, True),
    ("20260904.107", "V20260904.107__v221_invariant_guards_functions.sql", 1216413105, True),
    ("20260904.108", "V20260904.108__v221_privileges_views.sql", 748018122, True),
)
_AUTHORITY_ROLES = (
    "propertyai_owner",
    "propertyai_migrator",
    "propertyai_app_runtime",
    "propertyai_async_worker",
)


@dataclass(frozen=True)
class PostgresStageBMigrationConfig:
    dsn: str
    expected_session_user: str
    expected_database: str
    data_environment: str
    mode: str = PRE_CUTOVER_MODE
    min_size: int = 1
    max_size: int = 4
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.mode != PRE_CUTOVER_MODE:
            raise ValueError("Stage B pool requires PRE_CUTOVER_MIGRATION mode")
        if self.data_environment not in {"TEST", "DEVELOPMENT", "PRODUCTION"}:
            raise ValueError("invalid Stage B data environment")
        if (
            not self.dsn.strip()
            or not self.expected_session_user.strip()
            or not self.expected_database.strip()
        ):
            raise ValueError("Stage B DSN, direct-login user, and expected database are required")
        if self.min_size < 0 or self.max_size <= 0 or self.min_size > self.max_size:
            raise ValueError("invalid Stage B PostgreSQL pool size")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        dsn_key: str = "PROPERTYAI_CLEANER_STAGE_B_POSTGRES_DSN",
        expected_user_key: str = "PROPERTYAI_CLEANER_STAGE_B_POSTGRES_USER",
        database_key: str = "PROPERTYAI_CLEANER_STAGE_B_POSTGRES_DATABASE",
        data_environment_key: str = "PROPERTYAI_DATA_ENVIRONMENT",
        mode_key: str = "PROPERTYAI_CLEANER_POSTGRES_MODE",
    ) -> "PostgresStageBMigrationConfig":
        return cls(
            dsn=environment.get(dsn_key, ""),
            expected_session_user=environment.get(expected_user_key, ""),
            expected_database=environment.get(database_key, ""),
            data_environment=environment.get(data_environment_key, ""),
            mode=environment.get(mode_key, ""),
        )


class PostgresStageBMigrationPool(AbstractContextManager["PostgresStageBMigrationPool"]):
    """Direct-login pool with no SET ROLE path and fail-closed role checks."""

    def __init__(self, config: PostgresStageBMigrationConfig) -> None:
        self.config = config
        self._pool = ConnectionPool(
            conninfo=config.dsn,
            min_size=config.min_size,
            max_size=config.max_size,
            timeout=config.timeout_seconds,
            open=False,
            # DISCARD ALL is the session-poison boundary. Disable psycopg's
            # automatic server-side prepared statements so its client cache
            # cannot retain names that DISCARD removed.
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            configure=self._configure_connection,
            reset=self._reset_connection,
        )
        self._opened = False

    def _normalize_and_verify(self, connection: Connection) -> None:
        if connection.info.transaction_status != TransactionStatus.IDLE:
            connection.rollback()
        prior_autocommit = connection.autocommit
        connection.autocommit = True
        try:
            connection.execute("DISCARD ALL")
            connection.execute("SET TIME ZONE 'UTC'")
            connection.execute("SET search_path TO pg_catalog, propertyai")
            connection.execute("SET propertyai.cleaner_postgres_mode = 'PRE_CUTOVER_MIGRATION'")
            row = connection.execute(
                """
                SELECT session_user,
                       current_user,
                       current_database() AS database_name,
                       current_setting('TimeZone') AS timezone,
                       current_setting('search_path') AS search_path,
                       current_setting('propertyai.cleaner_postgres_mode') AS mode
                """
            ).fetchone()
            expected = {
                "session_user": self.config.expected_session_user,
                "current_user": self.config.expected_session_user,
                "database_name": self.config.expected_database,
                "timezone": "UTC",
                "search_path": _SAFE_SEARCH_PATH,
                "mode": PRE_CUTOVER_MODE,
            }
            if row is None or {key: row[key] for key in expected} != expected:
                raise PostgresRoleMismatchError(
                    f"Stage B direct-login invariant mismatch expected={expected} actual={row}"
                )
            memberships = connection.execute(
                """
                SELECT target.rolname
                  FROM pg_catalog.pg_roles AS target
                 WHERE target.rolname = ANY(%s)
                   AND pg_catalog.pg_has_role(session_user, target.oid, 'SET')
                """,
                (list(_AUTHORITY_ROLES),),
            ).fetchall()
            if memberships:
                raise PostgresRoleMismatchError(
                    "Stage B migration login can SET an authoritative role: "
                    + ",".join(row["rolname"] for row in memberships)
                )
            schema_row = connection.execute(
                "SELECT to_regnamespace('propertyai') IS NOT NULL AS exists"
            ).fetchone()
            if schema_row is None or not schema_row["exists"]:
                raise PostgresRoleMismatchError("Stage B propertyai schema is absent")
            try:
                flyway_rows = connection.execute(
                    """
                    SELECT version::text AS version, script, checksum, success
                      FROM propertyai.flyway_schema_history
                     ORDER BY version
                    """
                ).fetchall()
            except Exception as error:
                raise PostgresRoleMismatchError(
                    "Stage B exact Flyway state is unreadable"
                ) from error
            actual_flyway = tuple(
                (row["version"], row["script"], int(row["checksum"]), bool(row["success"]))
                for row in flyway_rows
            )
            if actual_flyway != _EXPECTED_FLYWAY_STATE:
                raise PostgresRoleMismatchError(
                    f"Stage B exact Flyway state mismatch expected={_EXPECTED_FLYWAY_STATE} "
                    f"actual={actual_flyway}"
                )
        finally:
            connection.autocommit = prior_autocommit

    def _configure_connection(self, connection: Connection) -> None:
        self._normalize_and_verify(connection)

    def _reset_connection(self, connection: Connection) -> None:
        self._normalize_and_verify(connection)

    def open(self) -> None:
        if not self._opened:
            self._pool.open(wait=True, timeout=self.config.timeout_seconds)
            self._opened = True

    def close(self) -> None:
        if self._opened:
            self._pool.close()
            self._opened = False

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        if not self._opened:
            raise RuntimeError("Stage B PostgreSQL pool is not open")
        with self._pool.connection(timeout=self.config.timeout_seconds) as connection:
            self._normalize_and_verify(connection)
            yield connection

    def __enter__(self) -> "PostgresStageBMigrationPool":
        self.open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "PRE_CUTOVER_MODE",
    "_EXPECTED_FLYWAY_STATE",
    "PostgresStageBMigrationConfig",
    "PostgresStageBMigrationPool",
]

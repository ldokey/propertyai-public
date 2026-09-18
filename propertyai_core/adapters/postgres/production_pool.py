"""Production-only Cleaner PostgreSQL app-runtime pool.

The Stage-A pool intentionally remains TEST/DEVELOPMENT-only. This module reuses
its session invariants without weakening that boundary.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Iterator

from psycopg import Connection
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from propertyai_core.adapters.postgres.errors import PostgresRoleMismatchError


PRODUCTION_APP_ROLE = "propertyai_app_runtime"
_SAFE_TIMEZONE = "UTC"
_SAFE_SEARCH_PATH = "pg_catalog, propertyai"


@dataclass(frozen=True)
class PostgresProductionConfig:
    dsn: str
    expected_session_user: str
    expected_database: str
    data_environment: str = "PRODUCTION"
    min_size: int = 1
    max_size: int = 4
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.data_environment != "PRODUCTION":
            raise ValueError("Production PostgreSQL pool requires PRODUCTION")
        if not self.dsn.strip():
            raise ValueError("Production PostgreSQL DSN is required")
        if not self.expected_session_user.strip():
            raise ValueError("Production expected_session_user is required")
        if not self.expected_database.strip():
            raise ValueError("Production expected_database is required")
        if self.min_size < 0 or self.max_size <= 0 or self.min_size > self.max_size:
            raise ValueError("invalid Production PostgreSQL pool size")


class PostgresProductionPool(AbstractContextManager["PostgresProductionPool"]):
    def __init__(self, config: PostgresProductionConfig):
        self.config = config
        self._pool = ConnectionPool(
            conninfo=config.dsn,
            min_size=config.min_size,
            max_size=config.max_size,
            timeout=config.timeout_seconds,
            open=False,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            configure=self._configure_connection,
            reset=self._reset_connection,
        )
        self._opened = False

    def _normalize_and_verify(self, connection: Connection) -> None:
        if connection.info.transaction_status != TransactionStatus.IDLE:
            connection.rollback()
        previous_autocommit = connection.autocommit
        connection.autocommit = True
        try:
            connection.execute("DISCARD ALL")
            row = connection.execute(
                "SELECT session_user, current_database() AS database_name"
            ).fetchone()
            expected_login = {
                "session_user": self.config.expected_session_user,
                "database_name": self.config.expected_database,
            }
            if row is None or {key: row[key] for key in expected_login} != expected_login:
                raise PostgresRoleMismatchError(
                    f"Production login invariant mismatch expected={expected_login} actual={row}"
                )
            connection.execute(
                sql.SQL("SET ROLE {}").format(sql.Identifier(PRODUCTION_APP_ROLE))
            )
            connection.execute("SET TIME ZONE 'UTC'")
            connection.execute("SET search_path TO pg_catalog, propertyai")
            row = connection.execute(
                """
                SELECT session_user,
                       current_user,
                       current_database() AS database_name,
                       current_setting('TimeZone') AS timezone,
                       current_setting('search_path') AS search_path
                """
            ).fetchone()
            expected = {
                "session_user": self.config.expected_session_user,
                "current_user": PRODUCTION_APP_ROLE,
                "database_name": self.config.expected_database,
                "timezone": _SAFE_TIMEZONE,
                "search_path": _SAFE_SEARCH_PATH,
            }
            if row is None or {key: row[key] for key in expected} != expected:
                raise PostgresRoleMismatchError(
                    f"Production app-runtime invariant mismatch expected={expected} actual={row}"
                )
        finally:
            connection.autocommit = previous_autocommit

    def _configure_connection(self, connection: Connection) -> None:
        self._normalize_and_verify(connection)

    def _reset_connection(self, connection: Connection) -> None:
        self._normalize_and_verify(connection)

    def open(self) -> None:
        if self._opened:
            return
        self._pool.open(wait=True, timeout=self.config.timeout_seconds)
        self._opened = True

    def close(self) -> None:
        if not self._opened:
            return
        self._pool.close()
        self._opened = False

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        if not self._opened:
            raise RuntimeError("Production PostgreSQL pool is not open")
        with self._pool.connection(timeout=self.config.timeout_seconds) as connection:
            self._normalize_and_verify(connection)
            yield connection

    def __enter__(self) -> "PostgresProductionPool":
        self.open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = ["PRODUCTION_APP_ROLE", "PostgresProductionConfig", "PostgresProductionPool"]

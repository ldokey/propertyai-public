from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Iterator, Mapping

from psycopg import Connection as _PsycopgConnection, sql as _sql
from psycopg.pq import TransactionStatus as _TransactionStatus
from psycopg.rows import dict_row as _dict_row
from psycopg_pool import ConnectionPool as _PsycopgConnectionPool

from propertyai_core.adapters.postgres.errors import PostgresRoleMismatchError


_SAFE_TIMEZONE = "UTC"
_SAFE_SEARCH_PATH = "pg_catalog, propertyai"

__all__ = ["PostgresStageAConfig", "PostgresStageAPool"]


@dataclass(frozen=True)
class PostgresStageAConfig:
    dsn: str
    expected_session_user: str
    expected_role: str
    data_environment: str
    min_size: int = 1
    max_size: int = 4
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.data_environment not in {"TEST", "DEVELOPMENT"}:
            raise ValueError("Stage A PostgreSQL is restricted to TEST/DEVELOPMENT")
        if not self.dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        if not self.expected_session_user.strip():
            raise ValueError("expected_session_user is required")
        if not self.expected_role.strip():
            raise ValueError("expected_role is required")
        if self.min_size < 0 or self.max_size <= 0 or self.min_size > self.max_size:
            raise ValueError("invalid PostgreSQL pool size")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        dsn_key: str,
        expected_session_user: str,
        expected_role: str,
        data_environment: str,
    ) -> "PostgresStageAConfig":
        dsn = environment.get(dsn_key, "")
        return cls(
            dsn=dsn,
            expected_session_user=expected_session_user,
            expected_role=expected_role,
            data_environment=data_environment,
        )


class PostgresStageAPool(AbstractContextManager["PostgresStageAPool"]):
    """Bounded Stage A connection adapter with an explicit lifecycle.

    The underlying psycopg pool is deliberately private.  Every connection
    handed to repository code is normalized and re-verified immediately before
    use, and normalized again when returned to the physical pool.
    """

    def __init__(self, config: PostgresStageAConfig):
        self.config = config
        self._pool = _PsycopgConnectionPool(
            conninfo=config.dsn,
            min_size=config.min_size,
            max_size=config.max_size,
            timeout=config.timeout_seconds,
            open=False,
            kwargs={"row_factory": _dict_row},
            configure=self._configure_connection,
            reset=self._reset_connection,
        )
        self._opened = False

    def _normalize_and_verify(self, connection: _PsycopgConnection) -> None:
        # DISCARD ALL is the fail-closed boundary for session-scoped poison:
        # role, GUCs, prepared statements, LISTEN state, advisory locks, and
        # temporary objects are removed before the next borrower can execute.
        if connection.info.transaction_status != _TransactionStatus.IDLE:
            connection.rollback()
        previous_autocommit = connection.autocommit
        connection.autocommit = True
        try:
            connection.execute("DISCARD ALL")
            session_row = connection.execute(
                "SELECT session_user AS session_user"
            ).fetchone()
            actual_session_user = None if session_row is None else session_row["session_user"]
            if actual_session_user != self.config.expected_session_user:
                raise PostgresRoleMismatchError(
                    "expected session_user="
                    f"{self.config.expected_session_user} actual={actual_session_user}"
                )

            connection.execute(
                _sql.SQL("SET ROLE {}").format(_sql.Identifier(self.config.expected_role))
            )
            connection.execute("SET TIME ZONE 'UTC'")
            connection.execute("SET search_path TO pg_catalog, propertyai")
            row = connection.execute(
                """
                SELECT session_user AS session_user,
                       current_user AS current_user,
                       current_setting('TimeZone') AS timezone,
                       current_setting('search_path') AS search_path
                """
            ).fetchone()
            if row is None:
                raise PostgresRoleMismatchError("PostgreSQL session invariant returned no row")
            expected = {
                "session_user": self.config.expected_session_user,
                "current_user": self.config.expected_role,
                "timezone": _SAFE_TIMEZONE,
                "search_path": _SAFE_SEARCH_PATH,
            }
            actual = {key: row[key] for key in expected}
            if actual != expected:
                raise PostgresRoleMismatchError(
                    f"PostgreSQL session invariant mismatch expected={expected} actual={actual}"
                )
        finally:
            connection.autocommit = previous_autocommit

    def _configure_connection(self, connection: _PsycopgConnection) -> None:
        self._normalize_and_verify(connection)

    def _reset_connection(self, connection: _PsycopgConnection) -> None:
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
    def _connection(self) -> Iterator[_PsycopgConnection]:
        """Private adapter boundary used only by Stage A repositories/tests."""
        if not self._opened:
            raise RuntimeError("PostgreSQL pool is not open")
        with self._pool.connection(timeout=self.config.timeout_seconds) as connection:
            # configure() runs only for new physical connections; this check is
            # intentionally on every logical checkout.
            self._normalize_and_verify(connection)
            yield connection

    def __enter__(self) -> "PostgresStageAPool":
        self.open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

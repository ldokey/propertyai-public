"""Composition root for Product Cleaner PostgreSQL business application ingress."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass

from propertyai_core.adapters.postgres.production_pool import (
    PostgresProductionConfig,
    PostgresProductionPool,
)
from propertyai_core.adapters.postgres.repository import PostgresCleanerRepository
from propertyai_core.application.cleaner_pg import PostgresCleanerApplicationService
from propertyai_core.config.cleaner_authority import CleanerAuthorityConfig


class CleanerPostgresCompositionError(RuntimeError):
    pass


@dataclass
class CleanerPostgresApplicationBundle(AbstractContextManager["CleanerPostgresApplicationBundle"]):
    authority: CleanerAuthorityConfig
    pool: PostgresProductionPool | None = None
    repository: PostgresCleanerRepository | None = None
    service: PostgresCleanerApplicationService | None = None

    def open(self) -> "CleanerPostgresApplicationBundle":
        if not self.authority.uses_postgres:
            raise CleanerPostgresCompositionError("POSTGRES_AUTHORITY_REQUIRED")
        if self.pool is not None:
            return self
        if (
            self.authority.pg_dsn_path is None
            or self.authority.pg_expected_session_user is None
            or self.authority.pg_expected_database is None
        ):
            raise CleanerPostgresCompositionError("POSTGRES_AUTHORITY_CONFIG_INCOMPLETE")
        try:
            dsn = self.authority.pg_dsn_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise CleanerPostgresCompositionError("POSTGRES_DSN_READ_FAILED") from error
        if not dsn:
            raise CleanerPostgresCompositionError("POSTGRES_DSN_EMPTY")
        pool = PostgresProductionPool(
            PostgresProductionConfig(
                dsn=dsn,
                expected_session_user=self.authority.pg_expected_session_user,
                expected_database=self.authority.pg_expected_database,
            )
        )
        pool.open()
        repository = PostgresCleanerRepository(pool)
        self.pool = pool
        self.repository = repository
        self.service = PostgresCleanerApplicationService(repository)
        return self

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()
        self.pool = None
        self.repository = None
        self.service = None

    def __enter__(self) -> "CleanerPostgresApplicationBundle":
        return self.open()

    def __exit__(self, *_args: object) -> None:
        self.close()


def build_cleaner_postgres_application(
    authority: CleanerAuthorityConfig,
) -> CleanerPostgresApplicationBundle:
    """Return an unopened bundle; credentials are read only on explicit POSTGRES open."""
    if not authority.uses_postgres:
        raise CleanerPostgresCompositionError("POSTGRES_AUTHORITY_REQUIRED")
    return CleanerPostgresApplicationBundle(authority)


__all__ = [
    "CleanerPostgresApplicationBundle",
    "CleanerPostgresCompositionError",
    "build_cleaner_postgres_application",
]

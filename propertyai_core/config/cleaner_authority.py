"""Fail-closed Product authority routing for Cleaner business writes."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Mapping


LEGACY_AUTHORITY = "LEGACY"
POSTGRES_AUTHORITY = "POSTGRES"
AUTHORITY_ENV = "PROPERTYAI_CLEANER_AUTHORITY"
PG_INGRESS_ENV = "PROPERTYAI_CLEANER_PG_INGRESS_ENABLED"
PG_DSN_PATH_ENV = "PROPERTYAI_CLEANER_POSTGRES_APP_DSN_PATH"
PG_SESSION_USER_ENV = "PROPERTYAI_CLEANER_POSTGRES_APP_SESSION_USER"
PG_DATABASE_ENV = "PROPERTYAI_CLEANER_POSTGRES_DATABASE"
PG_AUTHORITY_EPOCH_ENV = "PROPERTYAI_CLEANER_AUTHORITY_EPOCH"


class CleanerAuthorityConfigError(RuntimeError):
    pass


def _parse_bool(raw: object, *, name: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise CleanerAuthorityConfigError(f"{name}_MALFORMED")
    value = raw.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise CleanerAuthorityConfigError(f"{name}_MALFORMED")


def _exact_nonblank(environment: Mapping[str, str], name: str) -> str:
    raw = environment.get(name)
    if not isinstance(raw, str) or raw != raw.strip() or not raw:
        raise CleanerAuthorityConfigError(f"{name}_MISSING_OR_MALFORMED")
    return raw


def _protected_reference(environment: Mapping[str, str], name: str) -> Path:
    raw = _exact_nonblank(environment, name)
    path = Path(raw)
    if not path.is_absolute():
        raise CleanerAuthorityConfigError(f"{name}_NOT_ABSOLUTE")
    if path.is_symlink():
        raise CleanerAuthorityConfigError(f"{name}_SYMLINK_FORBIDDEN")
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise CleanerAuthorityConfigError(f"{name}_NOT_FOUND") from error
    try:
        info = resolved.stat()
    except OSError as error:
        raise CleanerAuthorityConfigError(f"{name}_STAT_FAILED") from error
    if not stat.S_ISREG(info.st_mode):
        raise CleanerAuthorityConfigError(f"{name}_NOT_FILE")
    if info.st_uid != os.geteuid():
        raise CleanerAuthorityConfigError(f"{name}_OWNER_MISMATCH")
    if info.st_mode & 0o077:
        raise CleanerAuthorityConfigError(f"{name}_PERMISSIONS_TOO_BROAD")
    return resolved


@dataclass(frozen=True)
class CleanerAuthorityConfig:
    authority: str = LEGACY_AUTHORITY
    pg_ingress_enabled: bool = False
    pg_dsn_path: Path | None = None
    pg_expected_session_user: str | None = None
    pg_expected_database: str | None = None
    authority_epoch: int | None = None

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "CleanerAuthorityConfig":
        env = os.environ if environment is None else environment
        if AUTHORITY_ENV not in env:
            authority = LEGACY_AUTHORITY
        else:
            authority_raw = env.get(AUTHORITY_ENV)
            if (
                not isinstance(authority_raw, str)
                or not authority_raw
                or authority_raw != authority_raw.strip()
            ):
                raise CleanerAuthorityConfigError("CLEANER_AUTHORITY_MALFORMED")
            authority = authority_raw
        ingress = _parse_bool(env.get(PG_INGRESS_ENV), name="PG_INGRESS", default=False)

        if authority == LEGACY_AUTHORITY:
            if ingress:
                raise CleanerAuthorityConfigError("LEGACY_WITH_PG_INGRESS_FORBIDDEN")
            return cls()
        if authority != POSTGRES_AUTHORITY:
            raise CleanerAuthorityConfigError("UNKNOWN_CLEANER_AUTHORITY")
        if not ingress:
            raise CleanerAuthorityConfigError("POSTGRES_WITH_PG_INGRESS_DISABLED")

        dsn_path = _protected_reference(env, PG_DSN_PATH_ENV)
        session_user = _exact_nonblank(env, PG_SESSION_USER_ENV)
        database = _exact_nonblank(env, PG_DATABASE_ENV)
        epoch_raw = _exact_nonblank(env, PG_AUTHORITY_EPOCH_ENV)
        try:
            epoch = int(epoch_raw)
        except ValueError as error:
            raise CleanerAuthorityConfigError("CLEANER_AUTHORITY_EPOCH_MALFORMED") from error
        if epoch < 0 or str(epoch) != epoch_raw:
            raise CleanerAuthorityConfigError("CLEANER_AUTHORITY_EPOCH_MALFORMED")
        return cls(
            authority=POSTGRES_AUTHORITY,
            pg_ingress_enabled=True,
            pg_dsn_path=dsn_path,
            pg_expected_session_user=session_user,
            pg_expected_database=database,
            authority_epoch=epoch,
        )

    @property
    def uses_postgres(self) -> bool:
        return self.authority == POSTGRES_AUTHORITY and self.pg_ingress_enabled

    @property
    def uses_legacy(self) -> bool:
        return self.authority == LEGACY_AUTHORITY and not self.pg_ingress_enabled


__all__ = [
    "AUTHORITY_ENV",
    "CleanerAuthorityConfig",
    "CleanerAuthorityConfigError",
    "LEGACY_AUTHORITY",
    "PG_AUTHORITY_EPOCH_ENV",
    "PG_DATABASE_ENV",
    "PG_DSN_PATH_ENV",
    "PG_INGRESS_ENV",
    "PG_SESSION_USER_ENV",
    "POSTGRES_AUTHORITY",
]

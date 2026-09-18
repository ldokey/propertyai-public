"""Persistent-staging-only target and release configuration primitives.

This module adds no Production environment and performs no database lifecycle,
credential discovery, provider selection, or scheduler activation. Secret values
remain in externally managed protected reference files.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

import psycopg
from psycopg.conninfo import conninfo_to_dict

from .common import OperationalError, checked_file, require

ISOLATED_TEST = "ISOLATED_TEST"
PERSISTENT_STAGING = "PERSISTENT_STAGING"
SUPPORTED_ENVIRONMENTS = (ISOLATED_TEST, PERSISTENT_STAGING)
RUNTIME_CONTRACT_VERSION = "RENT_RUNTIME_V2"
EXTERNAL_ACTIVATION = "REQUIRES_LATER_EXPLICIT_APPROVAL"
_TARGET_ID = re.compile(r"[a-z0-9][a-z0-9._-]{2,127}\Z")


def protected_reference(value: object) -> Path:
    """Validate an explicit owner-private regular-file reference without reading it."""
    require(isinstance(value, str) and value == value.strip() and bool(value), "CONFIG_INVALID")
    path = Path(value)
    require(path.is_absolute() and not path.is_symlink(), "CONFIG_INVALID")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as error:
        raise OperationalError("CONFIG_INVALID") from error
    require(resolved == path and stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) & 0o077 == 0, "CONFIG_INVALID")
    return resolved


def validate_target_id(value: object) -> str:
    require(isinstance(value, str) and _TARGET_ID.fullmatch(value) is not None, "CONFIG_INVALID")
    return value


def validate_persistent_common(raw: Mapping[str, object], *, runtime_role: str) -> str:
    require(raw.get("environment") == PERSISTENT_STAGING
            and raw.get("runtime_role") == runtime_role
            and raw.get("scheduler") == "OFF"
            and raw.get("real_business_data_expected") is False
            and raw.get("external_activation") == EXTERNAL_ACTIVATION,
            "CONFIG_INVALID")
    return validate_target_id(raw.get("target_id"))


def _connection_info(path: Path) -> tuple[str, dict[str, str]]:
    data = checked_file(path)
    require(0 < len(data) <= 16384, "CONFIG_INVALID")
    try:
        dsn = data.decode("utf-8")
    except UnicodeError as error:
        raise OperationalError("CONFIG_INVALID") from error
    require(dsn == dsn.strip() and "\x00" not in dsn, "CONFIG_INVALID")
    try:
        info = conninfo_to_dict(dsn)
    except Exception as error:
        raise OperationalError("CONFIG_INVALID") from error
    # No libpq environment/default database, service lookup, or neighboring password file.
    require(bool(info.get("host")) and bool(info.get("dbname")) and bool(info.get("user"))
            and not info.get("service") and not info.get("servicefile") and not info.get("passfile"),
            "CONFIG_INVALID")
    return dsn, info


@dataclass(frozen=True, repr=False)
class PersistentPostgresTarget:
    """Explicit persistent target binding; it cannot create, reset, drop, or discover a DB."""
    target_id: str
    connection_refs: Mapping[str, Path]

    def guard(self) -> None:
        validate_target_id(self.target_id)
        require(isinstance(self.connection_refs, Mapping) and bool(self.connection_refs), "CONFIG_INVALID")
        for role, path in self.connection_refs.items():
            require(isinstance(role, str) and re.fullmatch(r"[A-Z][A-Z_]{1,31}", role) is not None,
                    "CONFIG_INVALID")
            protected_reference(str(path))

    def _role_connection_info(self, role: str) -> tuple[str, dict[str, str]]:
        self.guard()
        require(role in self.connection_refs, "CONFIG_INVALID")
        ref = protected_reference(str(self.connection_refs[role]))
        return _connection_info(ref)

    def expected_database(self, role: str) -> str:
        _, info = self._role_connection_info(role)
        database = info.get("dbname")
        require(isinstance(database, str) and bool(database), "CONFIG_INVALID")
        return database

    def connect(self, role: str, *, autocommit: bool = True):
        dsn, _ = self._role_connection_info(role)
        return psycopg.connect(
            conninfo=dsn,
            connect_timeout=3,
            passfile="/dev/null",
            options="-c statement_timeout=5000 -c idle_in_transaction_session_timeout=10000",
            autocommit=autocommit,
        )


def persistent_target(target_id: object, references: Mapping[str, object], *, required_roles: set[str]) -> PersistentPostgresTarget:
    require(isinstance(references, Mapping) and set(references) == required_roles, "CONFIG_INVALID")
    refs = {role: protected_reference(value) for role, value in references.items()}
    target = PersistentPostgresTarget(validate_target_id(target_id), refs)
    target.guard()
    return target

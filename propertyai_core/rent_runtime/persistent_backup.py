"""Persistent-staging profile adapter for the accepted Rent backup primitive.

This module adds no database lifecycle, role/grant, credential provisioning, target
discovery, overwrite, prune, or Production authority. A caller supplies one
canonical-hashed non-secret profile plus one protected Flyway connection reference.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import parse_qs, urlparse

import psycopg

from .common import (OperationalError, canonical, checked_file, clean_env, digest,
                     require)
from .recovery import backup
from .staging import (PERSISTENT_STAGING, protected_reference,
                      validate_persistent_common)

MIGRATION_LOGIN = "propertyai_flyway"
MIGRATION_EFFECTIVE_ROLE = "propertyai_owner"


def _flyway_connection(path: Path) -> dict[str, object]:
    path = protected_reference(str(path))
    data = checked_file(path)
    require(0 < len(data) <= 16384, "CONFIG_INVALID")
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise OperationalError("CONFIG_INVALID") from error
    values: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw or raw.lstrip().startswith("#"):
            continue
        require("=" in raw, "CONFIG_INVALID")
        key, value = raw.split("=", 1)
        key, value = key.strip(), value.strip()
        require(key and key not in values, "CONFIG_INVALID")
        values[key] = value
    require(set(values) == {"flyway.url", "flyway.user", "flyway.password"}, "CONFIG_INVALID")
    prefix = "jdbc:postgresql://"
    url = values["flyway.url"]
    require(url.startswith(prefix), "CONFIG_INVALID")
    parsed = urlparse("postgresql://" + url[len(prefix):])
    require(
        parsed.hostname is not None and parsed.username is None and parsed.password is None
        and not parsed.params and not parsed.fragment and parsed.path not in {"", "/"},
        "CONFIG_INVALID",
    )
    query = parse_qs(parsed.query, strict_parsing=True) if parsed.query else {}
    require(set(query) <= {"sslmode"} and all(len(v) == 1 for v in query.values()), "CONFIG_INVALID")
    sslmode = query.get("sslmode", ["prefer"])[0]
    require(sslmode in {"disable", "prefer", "require"}, "CONFIG_INVALID")
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/"),
        "user": values["flyway.user"],
        "password": values["flyway.password"],
        "sslmode": sslmode,
    }


@dataclass(frozen=True, repr=False)
class PersistentBackupProfile:
    target_id: str
    host: str
    port: int
    database: str
    server_version_num: int
    connection_ref: Path
    pg_dump: Path
    pg_dump_sha256: str
    backup_destination: Path
    password: str
    sslmode: str

    def _guard_connection(self) -> None:
        require(
            isinstance(self.host, str) and bool(self.host)
            and type(self.port) is int and 1024 <= self.port <= 65535
            and isinstance(self.database, str)
            and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", self.database) is not None
            and type(self.server_version_num) is int and self.server_version_num >= 100000,
            "CONFIG_INVALID",
        )
        protected_reference(str(self.connection_ref))
        require(bool(self.password) and "\x00" not in self.password, "CONFIG_INVALID")
        require(self.sslmode in {"disable", "prefer", "require"}, "CONFIG_INVALID")

    def guard(self) -> None:
        self._guard_connection()
        checked_file(self.pg_dump, self.pg_dump_sha256)
        destination = self.backup_destination
        require(
            destination.is_absolute() and destination == destination.resolve()
            and not destination.exists() and not destination.is_symlink(),
            "CONFIG_INVALID",
        )
        parent = destination.parent
        info = parent.stat()
        require(
            parent.is_absolute() and not parent.is_symlink() and parent.resolve(strict=True) == parent
            and stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700,
            "CONFIG_INVALID",
        )

    def connect(self, login: str, *, autocommit: bool = True):
        self._guard_connection()
        require(login == MIGRATION_LOGIN, "CONFIG_INVALID")
        return psycopg.connect(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=MIGRATION_LOGIN,
            password=self.password,
            passfile="/dev/null",
            sslmode=self.sslmode,
            connect_timeout=3,
            options=(
                f"-c role={MIGRATION_EFFECTIVE_ROLE} "
                "-c statement_timeout=5000 -c idle_in_transaction_session_timeout=10000"
            ),
            autocommit=autocommit,
        )

    def pg_args(self, login: str) -> list[str]:
        self._guard_connection()
        require(login == MIGRATION_LOGIN, "CONFIG_INVALID")
        return [
            "-h", self.host, "-p", str(self.port), "-U", MIGRATION_LOGIN,
            "-d", self.database, "--no-password", f"--role={MIGRATION_EFFECTIVE_ROLE}",
        ]

    def backup_env(self) -> dict[str, str]:
        env = clean_env()
        env.update(PGPASSWORD=self.password, PGSSLMODE=self.sslmode)
        return env

    def identity(self, conn) -> dict:
        row = conn.execute(
            "SELECT current_database(),"
            "(SELECT oid::text FROM pg_database WHERE datname=current_database()),"
            "session_user::text,current_user::text"
        ).fetchone()
        value = {
            "source_class": PERSISTENT_STAGING,
            "target_id": self.target_id,
            "host": self.host,
            "port": self.port,
            "database": row[0],
            "database_oid": row[1],
            "server_version": conn.info.server_version,
            "session_user": row[2],
            "current_user": row[3],
        }
        require(
            value["database"] == self.database
            and value["server_version"] == self.server_version_num
            and value["session_user"] == MIGRATION_LOGIN
            and value["current_user"] == MIGRATION_EFFECTIVE_ROLE,
            "CONFIG_INVALID",
        )
        return value


def load_persistent_backup_profile(path: Path, sha256: str) -> PersistentBackupProfile:
    path = Path(path)
    require(
        path.is_absolute() and not path.is_symlink()
        and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0,
        "CONFIG_INVALID",
    )
    raw = json.loads(checked_file(path, sha256))
    keys = {
        "environment", "target_id", "database_connection_ref", "runtime_role",
        "host", "port", "database", "server_version_num", "pg_dump_path",
        "pg_dump_sha256", "backup_destination", "scheduler",
        "real_business_data_expected", "external_activation",
    }
    require(isinstance(raw, dict) and set(raw) == keys, "CONFIG_INVALID")
    target_id = validate_persistent_common(raw, runtime_role="BACKUP")
    require(
        isinstance(raw["host"], str)
        and type(raw["port"]) is int
        and isinstance(raw["database"], str)
        and type(raw["server_version_num"]) is int
        and isinstance(raw["pg_dump_sha256"], str)
        and re.fullmatch(r"[0-9a-f]{64}", raw["pg_dump_sha256"]) is not None,
        "CONFIG_INVALID",
    )
    connection_ref = protected_reference(raw["database_connection_ref"])
    info = _flyway_connection(connection_ref)
    require(
        info["host"] == raw["host"]
        and info["port"] == raw["port"]
        and info["database"] == raw["database"]
        and info["user"] == MIGRATION_LOGIN
        and isinstance(info["password"], str) and bool(info["password"]),
        "CONFIG_INVALID",
    )
    pg_dump = Path(raw["pg_dump_path"])
    require(pg_dump.is_absolute(), "CONFIG_INVALID")
    pg_dump = pg_dump.resolve(strict=True)
    require(pg_dump.name == "pg_dump", "CONFIG_INVALID")
    checked_file(pg_dump, raw["pg_dump_sha256"])
    destination = Path(raw["backup_destination"])
    profile = PersistentBackupProfile(
        target_id=target_id,
        host=raw["host"],
        port=raw["port"],
        database=raw["database"],
        server_version_num=raw["server_version_num"],
        connection_ref=connection_ref,
        pg_dump=pg_dump,
        pg_dump_sha256=raw["pg_dump_sha256"],
        backup_destination=destination,
        password=info["password"],
        sslmode=info["sslmode"],
    )
    profile.guard()
    return profile


def run_persistent_backup(
    *,
    manifest: dict,
    config_path: Path,
    config_sha256: str,
) -> str:
    role = manifest.get("roles", {}).get("BACKUP")
    require(
        isinstance(role, dict)
        and role.get("entrypoint") == ["python", "-m", "propertyai_core.rent_runtime", "backup"]
        and role.get("db_login") == MIGRATION_LOGIN
        and role.get("effective_role") == MIGRATION_EFFECTIVE_ROLE
        and role.get("scheduler_default") == "OFF"
        and role.get("overwrite") is False
        and role.get("prune") is False,
        "PACKAGE_INVALID",
    )
    profile = load_persistent_backup_profile(config_path, config_sha256)
    receipt = backup(
        profile,
        MIGRATION_LOGIN,
        profile.pg_dump,
        profile.backup_destination,
    )
    receipt_bytes = checked_file(profile.backup_destination / "backup-receipt.json")
    require(receipt_bytes == canonical(receipt), "BACKUP_INVALID")
    return digest(receipt_bytes)

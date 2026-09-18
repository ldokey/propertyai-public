"""Closed-world local target validation and sanitized operational evidence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from uuid import UUID, uuid4

import psycopg


class OperationalError(RuntimeError):
    def __init__(self, code: str, state: str = "FAILED_PRE_EFFECT"):
        self.code, self.state = code, state
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise OperationalError(code)


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked_file(path: Path, expected: str | None = None) -> bytes:
    path = Path(path)
    st = path.lstat()
    require(stat.S_ISREG(st.st_mode) and not path.is_symlink(), "FILE_IDENTITY_INVALID")
    require(path.absolute() == path.resolve(), "FILE_IDENTITY_INVALID")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        data = stream.read()
        after = os.fstat(stream.fileno())
    require((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "FILE_CHANGED")
    current = path.lstat()
    require((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "FILE_CHANGED")
    require(expected is None or digest(data) == expected, "HASH_MISMATCH")
    return data


def write_new(path: Path, data: bytes) -> None:
    """Private, exclusive output; never follows or overwrites an existing name."""
    path = Path(path)
    require(path.parent.absolute() == path.parent.resolve(), "OUTPUT_PARENT_ALIAS")
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def clean_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR") if k in os.environ}
    env.setdefault("PATH", "/opt/homebrew/bin:/usr/bin:/bin")
    env.update(PYTHONDONTWRITEBYTECODE="1", PGCONNECT_TIMEOUT="3", PGPASSFILE="/dev/null")
    return env


RESULTS = frozenset({
    "NOT_STARTED", "FAILED_PRE_EFFECT", "FAILED_UNKNOWN_EFFECT", "COMPLETED", "RECOVERED",
    "PROCESS_ALIVE", "APPLICATION_READY", "DATABASE_UNAVAILABLE", "MIGRATION_NOT_READY", "CONFIG_INVALID",
    "HTTP_OK", "HTTP_CLIENT_ERROR", "HTTP_SERVER_ERROR", "STARTED", "STOPPED", "BACKUP_FRESH",
    "BACKUP_MISSING", "BACKUP_STALE", "BACKUP_INVALID", "WORKER_UNBOUND_I3", "UNKNOWN",
})
ERRORS = frozenset({
    "NONE", "DATABASE_UNAVAILABLE", "MIGRATION_NOT_READY", "CONFIG_INVALID", "BACKUP_FAILED",
    "RESTORE_FAILED", "BACKUP_INVALID", "PACKAGE_INVALID", "WORKER_UNBOUND_I3", "INTERNAL_ERROR", "UNKNOWN",
})


def observe(role: str, result: str, *, error_class: str = "NONE", correlation_id: str | None = None,
            stream=None, **untrusted) -> dict:
    """No free-form message, URL, header, principal, exception text or arbitrary fields."""
    try:
        parsed = UUID(correlation_id) if correlation_id else uuid4()
        require(parsed.version == 4, "CORRELATION_INVALID")
    except (ValueError, TypeError, OperationalError):
        parsed = uuid4()
    value = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "process_role": role if role in {"WEB", "WORKER", "MIGRATOR", "BACKUP", "RESTORE"} else "OPERATION",
        "correlation_id": str(parsed),
        "result_class": result if result in RESULTS else "UNKNOWN",
        "error_class": error_class if error_class in ERRORS else "UNKNOWN",
    }
    print(canonical(value).decode(), file=stream or sys.stdout, flush=True)
    return value


@dataclass(frozen=True, repr=False)
class LocalTarget:
    """Only an existing, owner-marked, socket-only disposable test cluster is accepted.

    This consumes the accepted fixture marker format without shipping test loaders.
    It cannot point at a shared/Production cluster, arbitrary DSN or TCP host.
    """
    root: Path
    marker_sha256: str
    port: int

    def guard(self) -> None:
        root = Path(self.root)
        require(root.is_absolute() and root == root.resolve() and root.parent == Path("/tmp").resolve()
                and root.name.startswith("pa-stage-a-"), "CONFIG_INVALID")
        st = root.lstat()
        require(stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700,
                "CONFIG_INVALID")
        require(type(self.port) is int and 1024 <= self.port <= 65535, "CONFIG_INVALID")
        marker = json.loads(checked_file(root / ".propertyai-stage-a-owned.json", self.marker_sha256))
        require(marker.get("root") == str(root) and marker.get("uid") == os.getuid()
                and marker.get("inode") == st.st_ino and isinstance(marker.get("nonce"), str)
                and len(marker["nonce"]) == 48, "CONFIG_INVALID")
        require(all(not (root / name).is_symlink() for name in ("data", "sock")), "CONFIG_INVALID")
        require(not (root / ".w3b-no-password").exists() and not (root / ".w3b-no-password").is_symlink(), "CONFIG_INVALID")
        pidfile = root / "data/postmaster.pid"
        if pidfile.exists():
            lines = checked_file(pidfile).decode().splitlines()
            require(len(lines) >= 6 and lines[1] == str(root / "data") and lines[3] == str(self.port)
                    and lines[4] == str(root / "sock") and lines[5] == "", "CONFIG_INVALID")

    def connect(self, login: str, *, autocommit: bool = True):
        self.guard()
        require(isinstance(login, str) and re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", login) is not None,
                "CONFIG_INVALID")
        return psycopg.connect(host=str(self.root / "sock"), port=self.port, dbname="postgres", user=login,
                               password="", passfile=str(self.root / ".w3b-no-password"), connect_timeout=3, sslmode="disable",
                               options="-c statement_timeout=5000 -c idle_in_transaction_session_timeout=10000",
                               autocommit=autocommit)

    def pg_args(self, login: str) -> list[str]:
        self.guard()
        require(re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", login) is not None, "CONFIG_INVALID")
        return ["-h", str(self.root / "sock"), "-p", str(self.port), "-U", login, "-d", "postgres", "--no-password"]

    def identity(self, conn) -> dict:
        row = conn.execute("SELECT system_identifier::text, current_database(), "
                           "(SELECT oid::text FROM pg_database WHERE datname=current_database()) "
                           "FROM pg_control_system()").fetchone()
        return {"system_identifier": row[0], "database": row[1], "database_oid": row[2],
                "owned_marker_sha256": self.marker_sha256, "server_version": conn.info.server_version}

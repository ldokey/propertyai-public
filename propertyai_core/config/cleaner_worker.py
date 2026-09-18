"""The single protected Production W07 connection contract (no secret loading)."""

from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Mapping

from psycopg.conninfo import make_conninfo


WORKER_LOGIN = "propertyai_cleaner_worker"
WORKER_ROLE = "propertyai_async_worker"
WORKER_DATABASE = "propertyai_cleaner_prod"
WORKER_CREDENTIAL_REF = "cleaner-prod/worker.pgpass"
WORKER_CREDENTIAL_ENV = "PROPERTYAI_CLEANER_POSTGRES_WORKER_CREDENTIAL_REF"
_WORKER_UID = 501
_WORKER_PASSFILE = Path("/Users/kate/PropertyAI/openclaw-workspace/secrets/postgres/cleaner-prod/worker.pgpass")


class CleanerWorkerConfigurationError(RuntimeError):
    pass


def worker_connection_info(environment: Mapping[str, str]) -> str:
    """Resolve one sealed reference; libpq alone reads password bytes."""
    if environment.get(WORKER_CREDENTIAL_ENV) != WORKER_CREDENTIAL_REF:
        raise CleanerWorkerConfigurationError("W07_CREDENTIAL_REFERENCE_INVALID")
    if os.geteuid() != _WORKER_UID:
        raise CleanerWorkerConfigurationError("W07_RUNTIME_UID_INVALID")
    for name, value in environment.items():
        if name.startswith("PROPERTYAI_CLEANER_POSTGRES_") and name != WORKER_CREDENTIAL_ENV:
            if name != "PROPERTYAI_CLEANER_POSTGRES_DATABASE" or value != WORKER_DATABASE:
                raise CleanerWorkerConfigurationError("W07_CONNECTION_OVERRIDE_FORBIDDEN")
    if any(name.startswith("PG") for name in environment):
        raise CleanerWorkerConfigurationError("W07_AMBIENT_LIBPQ_CONFIGURATION_FORBIDDEN")
    if any(name in environment for name in (
        "PROPERTYAI_CLEANER_POSTGRES_APP_DSN_PATH",
        "PROPERTYAI_CLEANER_POSTGRES_APP_SESSION_USER",
    )):
        raise CleanerWorkerConfigurationError("W07_APP_CREDENTIAL_CONFIGURATION_FORBIDDEN")
    path = _WORKER_PASSFILE
    try:
        info = path.lstat()
        parent = path.parent.lstat()
        if path.resolve(strict=True) != path or path.parent.resolve(strict=True) != path.parent:
            raise CleanerWorkerConfigurationError("W07_CREDENTIAL_SYMLINK_FORBIDDEN")
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != _WORKER_UID
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size == 0):
            raise CleanerWorkerConfigurationError("W07_CREDENTIAL_METADATA_INVALID")
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != _WORKER_UID
                or stat.S_IMODE(parent.st_mode) != 0o700):
            raise CleanerWorkerConfigurationError("W07_CREDENTIAL_DIRECTORY_INVALID")
    except OSError:
        raise CleanerWorkerConfigurationError("W07_CREDENTIAL_UNAVAILABLE") from None
    return make_conninfo(
        host="127.0.0.1", port="5432", dbname=WORKER_DATABASE, user=WORKER_LOGIN,
        passfile=str(path), connect_timeout="10", application_name="propertyai-w07",
        sslmode="disable", require_auth="scram-sha-256",
    )

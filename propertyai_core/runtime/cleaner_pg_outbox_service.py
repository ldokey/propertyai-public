"""W07 Cleaner PostgreSQL outbox runtime composition.

Ordinary startup uses current POST_CUTOVER_PG topology, database privileges and
live source/config admission. One-time cutover finalization is an explicit,
separate entrypoint and is never replayed by this service. Tests exercise main
only with test-owned transport, control state and deterministic fake providers.
"""

from __future__ import annotations

import os
import time
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from adcp_global_writer_client.errors import GlobalWriterClientError

from propertyai_core.adapters.postgres.worker_pool import PostgresWorkerPool
from propertyai_core.config.cleaner_worker import (
    WORKER_CREDENTIAL_ENV, WORKER_CREDENTIAL_REF, WORKER_LOGIN, WORKER_ROLE, WORKER_DATABASE,
)
from propertyai_core.adapters.postgres.repository import PostgresOutboxWorkerRepository
from propertyai_core.global_writer import ProductionWriterError, publish_startup_runtime_identity
from propertyai_core.runtime.cleaner_projection_adapters import (
    CalendarCurrentStateProjectionAdapter,
    DestinationProjectionRouter,
    NotionCurrentStateProjectionAdapter,
    TelegramSealedEffectAdapter,
)
from propertyai_core.runtime.cleaner_projection_clients import (
    GoogleCleaningCalendarClient,
    NotionCurrentStateClient,
    TelegramSealedDeliveryClient,
)
from propertyai_core.runtime.cleaner_topology import (
    CleanerRuntimeTopology,
    topology_contract_from_env,
)
from propertyai_core.runtime.postgres_outbox_worker import CleanerPostgresOutboxWorker, OutboxRunResult


DATABASE_ENV = "PROPERTYAI_CLEANER_POSTGRES_DATABASE"
AUTHORITY_ENV = "PROPERTYAI_CLEANER_AUTHORITY"
WORKER_ID = "com.propertyai.cleaner-pg-outbox"
RECONCILIATION_OPERATION_ENV = "PROPERTYAI_CLEANER_POST_PONR_RECONCILIATION_OPERATION_ID"


class CleanerPostgresOutboxRuntimeError(RuntimeError):
    pass


def _required_environment(environment: Mapping[str, str], name: str) -> str:
    value = str(environment.get(name, "")).strip()
    if not value:
        raise CleanerPostgresOutboxRuntimeError(f"{name}_REQUIRED")
    return value


def build_worker(
    *,
    environment: Mapping[str, str] | None = None,
    notion_client=None,
    calendar_client=None,
    telegram_client=None,
) -> tuple[PostgresWorkerPool, CleanerPostgresOutboxWorker]:
    """Compose W07 without starting a loop or making any destination API call."""

    env = os.environ if environment is None else environment
    contract = topology_contract_from_env(dict(env))
    if contract.topology is not CleanerRuntimeTopology.POST_CUTOVER_PG:
        raise CleanerPostgresOutboxRuntimeError("POST_CUTOVER_PG_TOPOLOGY_REQUIRED")
    if _required_environment(env, AUTHORITY_ENV) != "POSTGRES":
        raise CleanerPostgresOutboxRuntimeError("POSTGRES_AUTHORITY_REQUIRED")

    pool = PostgresWorkerPool(environment=env)
    try:
        pool.open()
        repository = PostgresOutboxWorkerRepository(pool)
        router = DestinationProjectionRouter(
            notion=NotionCurrentStateProjectionAdapter(
                repository,
                notion_client or NotionCurrentStateClient(environment=env),
            ),
            calendar=CalendarCurrentStateProjectionAdapter(
                repository,
                calendar_client or GoogleCleaningCalendarClient(environment=env),
            ),
            telegram=TelegramSealedEffectAdapter(
                telegram_client or TelegramSealedDeliveryClient(environment=env),
            ),
        )
        worker = CleanerPostgresOutboxWorker(repository, router, worker_id=WORKER_ID)
        return pool, worker
    except BaseException:
        pool.close()
        raise


def run_worker_loop(
    worker: CleanerPostgresOutboxWorker,
    *,
    idle_sleep_seconds: float = 5.0,
    active_sleep_seconds: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
    observe: Callable[[OutboxRunResult], None] | None = None,
) -> None:
    if idle_sleep_seconds < 0 or active_sleep_seconds < 0:
        raise ValueError("worker sleep intervals must be non-negative")
    if max_cycles is not None and max_cycles < 0:
        raise ValueError("max_cycles must be non-negative")
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            result = worker.run_once()
        except ProductionWriterError as error:
            cause = error.__cause__
            if (str(error) != "GLOBAL_WRITER_ACQUIRE_OR_REVALIDATE_FAILED"
                    or not isinstance(cause, GlobalWriterClientError)
                    or cause.code != "GLOBAL_PRODUCTION_WRITER_HELD"):
                raise
            # An existing control lease is normal during staged startup. No claim
            # occurred; only this precise pre-acquisition denial may safely wait.
            result = OutboxRunResult(status="WAITING_CONTROL")
        if observe is not None:
            observe(result)
        sleep(idle_sleep_seconds if result.status in {"IDLE", "WAITING_CONTROL"} else active_sleep_seconds)


def _health_observer(identity, environment: Mapping[str, str]):
    runtime_path = Path(environment["PROPERTYAI_GLOBAL_WRITER_RUNTIME_IDENTITY_PATH"])
    if not runtime_path.is_absolute() or runtime_path.name != "W07.runtime.json":
        raise CleanerPostgresOutboxRuntimeError("W07_RUNTIME_IDENTITY_PATH_INVALID")
    path = runtime_path.with_name("W07.worker-health.json")
    idle_count = 0

    def observe(result: OutboxRunResult) -> None:
        nonlocal idle_count
        if result.status == "IDLE":
            idle_count += 1
        payload = {
            "schema_version": 1, "pid": identity.pid,
            "process_incarnation_id": identity.process_incarnation_id,
            "product_build_commit": identity.product_build_commit,
            "product_build_identity": identity.product_build_identity,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "session_user": WORKER_LOGIN, "current_user": WORKER_ROLE,
            "database_name": WORKER_DATABASE, "credential_ref": WORKER_CREDENTIAL_REF,
            "privilege_contract": "W07_FROZEN_V221", "database_health": "PASS",
            "status": result.status, "idle_cycles": idle_count,
            "writer_lease": "NOT_ACQUIRED" if result.status in {"READY", "WAITING_CONTROL"} else "RELEASED",
        }
        temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return observe


def main() -> None:
    pool, worker = build_worker()
    try:
        # Publish only after the mandatory database identity/ACL proof succeeds.
        identity = publish_startup_runtime_identity("W07")
        observe = _health_observer(identity, os.environ)
        observe(OutboxRunResult(status="READY"))
        # NORMAL_STEADY_STATE_STARTUP: admission is current DB identity/ACL,
        # topology/authority, and live source/config identity. A leftover historical
        # operation environment value is NOT authority to replay cutover work.
        # Explicit one-time finalization remains in cleaner_projection_reconciliation
        # and is never invoked by this worker entrypoint.
        run_worker_loop(worker, observe=observe)
    finally:
        pool.close()


def read_worker_database_health(*, environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """No lease, queue claim, business write, or destination call; exact DB proof."""
    pool = PostgresWorkerPool(environment=environment)
    try:
        pool.open()
        with pool._connection() as connection:
            row = connection.execute("SELECT session_user,current_user,current_database() AS database_name").fetchone()
        return {
            "status": "PASS", "session_user": row["session_user"],
            "current_user": row["current_user"], "database_name": row["database_name"],
            "credential_ref": WORKER_CREDENTIAL_REF, "privilege_contract": "W07_FROZEN_V221",
        }
    finally:
        pool.close()


if __name__ == "__main__":
    main()


__all__ = [
    "AUTHORITY_ENV",
    "DATABASE_ENV",
    "WORKER_CREDENTIAL_ENV",
    "WORKER_ID",
    "RECONCILIATION_OPERATION_ENV",
    "CleanerPostgresOutboxRuntimeError",
    "build_worker",
    "read_worker_database_health",
    "run_worker_loop",
]

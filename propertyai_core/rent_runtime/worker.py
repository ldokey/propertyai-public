"""I3 single-run runtime binding for the exact accepted W3-A recurring billing worker.

This module owns process/config translation only. Billing eligibility, Finance effects,
idempotency, recovery, and scan semantics remain in the frozen W3-A implementation.
No scheduler, daemon, background thread, Production target discovery, or secret lookup is
provided here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

from propertyai_core.application.recurring_rent import ScanCursor, WorkerConfig
from propertyai_core.rent_recurring_billing import run_recurring_billing_once

from .common import LocalTarget, canonical, checked_file, require
from .staging import (ISOLATED_TEST, PERSISTENT_STAGING, PersistentPostgresTarget,
                      persistent_target, validate_persistent_common)

WORKER_ENTRYPOINT = "propertyai_core.rent_recurring_billing.run_recurring_billing_once"
WORKER_ENTRYPOINT_VERSION = 1
WORKER_TIMEOUT_SECONDS = 60

EXIT_SUCCESS = 0
EXIT_CONTINUATION = 3
EXIT_CONTRACT_FAILURES = 4
EXIT_UNKNOWN_EFFECT = 75
EXIT_CONFIG = 78
EXIT_RUNTIME = 70


@dataclass(frozen=True, repr=False)
class WorkerRuntimeConfig:
    target: LocalTarget | PersistentPostgresTarget
    worker_login: str
    organization_id: UUID
    max_items: int
    max_attempts: int
    environment: str = ISOLATED_TEST
    config_dir: Path | None = None

    @property
    def worker_config(self) -> WorkerConfig:
        # enabled=True means this explicit invocation may execute. It does not enable
        # an automatic scheduler; the runtime configuration separately requires OFF.
        return WorkerConfig(enabled=True, max_items=self.max_items, max_attempts=self.max_attempts)


def load_worker_config(path: Path, sha256: str) -> WorkerRuntimeConfig:
    path = Path(path)
    require(path.is_absolute() and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "CONFIG_INVALID")
    raw = json.loads(checked_file(path, sha256))
    require(isinstance(raw, dict), "CONFIG_INVALID")
    if raw.get("environment") == ISOLATED_TEST:
        keys = {"environment", "target_root", "target_marker_sha256", "pg_port", "worker_login",
                "organization_id", "scheduler", "max_items", "max_attempts"}
        require(set(raw) == keys and raw["scheduler"] == "OFF", "CONFIG_INVALID")
        target = LocalTarget(Path(raw["target_root"]), raw["target_marker_sha256"], raw["pg_port"])
        target.guard()
        require(path.parent.resolve() == target.root and path.name.startswith("w3b-worker-config-")
                and path.suffix == ".json", "CONFIG_INVALID")
        require(isinstance(raw["worker_login"], str)
                and re.fullmatch(r"rent_scheduler_test_[a-z0-9_]{1,40}", raw["worker_login"]) is not None,
                "CONFIG_INVALID")
        organization_id = UUID(raw["organization_id"])
        worker = WorkerConfig(enabled=True, max_items=raw["max_items"], max_attempts=raw["max_attempts"])
        return WorkerRuntimeConfig(target, raw["worker_login"], organization_id, worker.max_items, worker.max_attempts)

    keys = {"environment", "target_id", "database_connection_ref", "runtime_role", "auth_binding",
            "organization_id", "scheduler", "max_items", "max_attempts",
            "real_business_data_expected", "external_activation"}
    require(set(raw) == keys and raw.get("environment") == PERSISTENT_STAGING, "CONFIG_INVALID")
    target_id = validate_persistent_common(raw, runtime_role="WORKER")
    require(raw["auth_binding"] == {"kind": "DATABASE_ROLE_BINDING", "role": "propertyai_rent_scheduler"},
            "CONFIG_INVALID")
    target = persistent_target(target_id, {"WORKER": raw["database_connection_ref"]}, required_roles={"WORKER"})
    organization_id = UUID(raw["organization_id"])
    worker = WorkerConfig(enabled=True, max_items=raw["max_items"], max_attempts=raw["max_attempts"])
    return WorkerRuntimeConfig(target, "WORKER", organization_id, worker.max_items, worker.max_attempts,
                               PERSISTENT_STAGING, path.parent.resolve())


def load_after(path: Path | None, sha256: str | None, config: WorkerRuntimeConfig) -> ScanCursor | None:
    if path is None and sha256 is None:
        return None
    require(path is not None and sha256 is not None, "CONFIG_INVALID")
    path = Path(path)
    if config.environment == ISOLATED_TEST:
        require(isinstance(config.target, LocalTarget) and path.is_absolute() and path.parent.resolve() == config.target.root
                and path.name.startswith("w3b-worker-cursor-") and path.suffix == ".json"
                and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "CONFIG_INVALID")
    else:
        require(config.environment == PERSISTENT_STAGING and config.config_dir is not None
                and path.is_absolute() and path.parent.resolve() == config.config_dir
                and path.name.startswith("rent-worker-cursor-") and path.suffix == ".json"
                and stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "CONFIG_INVALID")
    raw = json.loads(checked_file(path, sha256))
    require(set(raw) == {"organization_id", "contract_id", "period_id"}, "CONFIG_INVALID")
    cursor = ScanCursor(**{key: UUID(raw[key]) for key in ("organization_id", "contract_id", "period_id")})
    require(cursor.organization_id == config.organization_id, "CONFIG_INVALID")
    return cursor


def require_worker_manifest(manifest: dict) -> None:
    worker = manifest.get("roles", {}).get("WORKER", {})
    require(worker.get("entrypoint") == ["python", "-m", "propertyai_core.rent_runtime", "worker"]
            and worker.get("callable") == WORKER_ENTRYPOINT
            and worker.get("callable_version") == WORKER_ENTRYPOINT_VERSION
            and worker.get("binding") == "I3_EXACT_W3_A"
            and worker.get("scheduler_default") == "OFF"
            and worker.get("automatic_scheduler_activated") is False
            and worker.get("timeout_seconds") == WORKER_TIMEOUT_SECONDS,
            "PACKAGE_INVALID")


def _envelope(*, run_id: UUID, result_class: str, worker_result: dict | None,
              error_class: str = "NONE") -> dict:
    continuation = bool(worker_result is not None and not worker_result.get("scan_complete", False))
    return {
        "schema_version": 1,
        "process_role": "WORKER",
        "worker_entrypoint": WORKER_ENTRYPOINT,
        "worker_entrypoint_version": WORKER_ENTRYPOINT_VERSION,
        "run_id": str(run_id),
        "request_id": str(run_id),
        "result_class": result_class,
        "error_class": error_class,
        "scheduler_default": "OFF",
        "automatic_scheduler_activated": False,
        "continuation_required": continuation,
        "next_cursor": worker_result.get("next_cursor") if worker_result is not None else None,
        "worker_result": worker_result,
    }


def config_error(run_id: UUID | None = None) -> tuple[int, dict]:
    correlation = run_id if isinstance(run_id, UUID) else uuid4()
    return EXIT_CONFIG, _envelope(run_id=correlation, result_class="CONFIG_ERROR",
                                  worker_result=None, error_class="CONFIG_ERROR")


def runtime_error(run_id: UUID | None = None) -> tuple[int, dict]:
    correlation = run_id if isinstance(run_id, UUID) else uuid4()
    return EXIT_RUNTIME, _envelope(run_id=correlation, result_class="RUNTIME_ERROR",
                                   worker_result=None, error_class="RUNTIME_ERROR")


def classify_result(result: dict) -> tuple[int, str]:
    worker_class = result.get("result_class")
    if worker_class == "PROCESS_FAILED":
        return EXIT_RUNTIME, "RUNTIME_ERROR"
    if worker_class == "PARTIAL_UNKNOWN_EFFECT":
        return EXIT_UNKNOWN_EFFECT, "UNKNOWN_EFFECT"
    if result.get("scan_complete") is False:
        return EXIT_CONTINUATION, "PARTIAL_SCAN_REQUIRES_CONTINUATION"
    if worker_class == "RUN_COMPLETED_WITH_CONTRACT_FAILURES":
        return EXIT_CONTRACT_FAILURES, "COMPLETED_WITH_CONTRACT_FAILURES"
    if worker_class == "ZERO_TARGET_NOOP":
        return EXIT_SUCCESS, "ZERO_TARGET_NOOP"
    if worker_class == "DRY_RUN_SUCCESS":
        return EXIT_SUCCESS, "DRY_RUN_SUCCESS"
    if worker_class == "RUN_COMPLETED":
        return EXIT_SUCCESS, "COMPLETED"
    return EXIT_RUNTIME, "RUNTIME_ERROR"


def run_worker_process(*, manifest: dict, config_path: Path, config_sha256: str,
                       dry_run: bool = False, run_id: UUID | None = None,
                       after_path: Path | None = None, after_sha256: str | None = None) -> tuple[int, dict]:
    correlation = run_id if isinstance(run_id, UUID) else uuid4()
    try:
        require(type(dry_run) is bool and (run_id is None or isinstance(run_id, UUID)), "CONFIG_INVALID")
        require_worker_manifest(manifest)
        config = load_worker_config(config_path, config_sha256)
        after = load_after(after_path, after_sha256, config)
    except Exception:
        return config_error(correlation)
    try:
        result = run_recurring_billing_once(
            connect=lambda: config.target.connect(config.worker_login),
            organization_id=config.organization_id,
            clock=lambda: datetime.now(timezone.utc),
            config=config.worker_config,
            dry_run=dry_run,
            run_id=correlation,
            after=after,
            # W3-A business bytes remain frozen. PERSISTENT_STAGING is an empty, non-Production
            # operational target and deliberately consumes the existing non-Production authority mode.
            runtime=ISOLATED_TEST,
        )
        exit_code, process_class = classify_result(result)
        return exit_code, _envelope(run_id=correlation, result_class=process_class,
                                    worker_result=result,
                                    error_class="UNKNOWN_EFFECT" if process_class == "UNKNOWN_EFFECT"
                                    else "RUNTIME_ERROR" if process_class == "RUNTIME_ERROR" else "NONE")
    except Exception:
        return runtime_error(correlation)


def emit(exit_code: int, payload: dict) -> int:
    print(canonical(payload).decode(), flush=True)
    return exit_code

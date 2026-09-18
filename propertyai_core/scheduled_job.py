"""Small reusable execution wrapper for launchd-triggered Python jobs."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable


class MutationAuthorityMode(StrEnum):
    READ_ONLY = "READ_ONLY"
    CALLER_SCOPED_MUTATION = "CALLER_SCOPED_MUTATION"


class JobTerminalStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED_CONCURRENT = "SKIPPED_CONCURRENT"


class JobAlreadyRunningError(RuntimeError):
    """Raised when another process already owns a scheduled-job lock."""


class SingleRunGuard:
    """Non-blocking process guard using the codebase's proven flock pattern."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = Path(lock_path)
        self._handle = None

    def __enter__(self) -> "SingleRunGuard":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._handle = self.lock_path.open("a+")
        os.chmod(self.lock_path, 0o600)
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._handle.close()
            self._handle = None
            raise JobAlreadyRunningError(
                f"scheduled job already owns {self.lock_path}"
            ) from error
        return self

    def __exit__(self, *_args: object) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


@dataclass(frozen=True)
class ScheduledJobContext:
    job_name: str
    run_id: str
    started_at: str
    mutation_authority_mode: MutationAuthorityMode


@dataclass(frozen=True)
class JobOutcome:
    processed_count: int = 0
    changed_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScheduledJobResult:
    job_name: str
    run_id: str
    started_at: str
    finished_at: str
    terminal_status: JobTerminalStatus
    processed_count: int
    changed_count: int
    skipped_count: int
    error_count: int
    mutation_authority_mode: MutationAuthorityMode
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_name": self.job_name,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "terminal_status": self.terminal_status.value,
            "processed_count": self.processed_count,
            "changed_count": self.changed_count,
            "skipped_count": self.skipped_count,
            "error_count": self.error_count,
            "mutation_authority_mode": self.mutation_authority_mode.value,
            **self.details,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))


class ScheduledJobRunner:
    """Own process-level execution metadata; callers retain business logic/idempotency."""

    def __init__(
        self,
        *,
        job_name: str,
        lock_path: Path,
        mutation_authority_mode: MutationAuthorityMode,
    ) -> None:
        if not job_name.strip():
            raise ValueError("job_name must be non-empty")
        self.job_name = job_name
        self.lock_path = Path(lock_path)
        self.mutation_authority_mode = MutationAuthorityMode(mutation_authority_mode)

    def run(
        self,
        work: Callable[[ScheduledJobContext], JobOutcome],
        *,
        run_id: str | None = None,
    ) -> ScheduledJobResult:
        started_at = datetime.now(timezone.utc).isoformat()
        effective_run_id = run_id or uuid.uuid4().hex
        context = ScheduledJobContext(
            job_name=self.job_name,
            run_id=effective_run_id,
            started_at=started_at,
            mutation_authority_mode=self.mutation_authority_mode,
        )
        try:
            with SingleRunGuard(self.lock_path):
                outcome = work(context)
                if not isinstance(outcome, JobOutcome):
                    raise TypeError("scheduled job work must return JobOutcome")
                terminal_status = (
                    JobTerminalStatus.FAILED
                    if outcome.error_count > 0
                    else JobTerminalStatus.SUCCESS
                )
        except JobAlreadyRunningError:
            outcome = JobOutcome(skipped_count=1)
            terminal_status = JobTerminalStatus.SKIPPED_CONCURRENT
        except Exception as error:
            outcome = JobOutcome(error_count=1, details={"error_type": type(error).__name__})
            terminal_status = JobTerminalStatus.FAILED

        return ScheduledJobResult(
            job_name=self.job_name,
            run_id=effective_run_id,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            terminal_status=terminal_status,
            processed_count=outcome.processed_count,
            changed_count=outcome.changed_count,
            skipped_count=outcome.skipped_count,
            error_count=outcome.error_count,
            mutation_authority_mode=self.mutation_authority_mode,
            details=outcome.details,
        )

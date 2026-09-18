from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from propertyai_core.scheduled_job import (
    JobOutcome,
    JobTerminalStatus,
    MutationAuthorityMode,
    ScheduledJobRunner,
    SingleRunGuard,
)


def test_structured_result_is_one_line_json(tmp_path: Path):
    runner = ScheduledJobRunner(
        job_name="demo",
        lock_path=tmp_path / "demo.lock",
        mutation_authority_mode=MutationAuthorityMode.READ_ONLY,
    )
    result = runner.run(lambda _ctx: JobOutcome(processed_count=3, changed_count=0))
    parsed = json.loads(result.to_json_line())
    assert "\n" not in result.to_json_line()
    assert parsed["job_name"] == "demo"
    assert parsed["processed_count"] == 3
    assert parsed["mutation_authority_mode"] == "READ_ONLY"
    assert parsed["terminal_status"] == "SUCCESS"


def test_single_run_guard_rejects_second_process(tmp_path: Path):
    lock_path = tmp_path / "guard.lock"
    code = """
import sys
from pathlib import Path
from propertyai_core.scheduled_job import JobAlreadyRunningError, SingleRunGuard
try:
    with SingleRunGuard(Path(sys.argv[1])):
        raise SystemExit(9)
except JobAlreadyRunningError:
    raise SystemExit(0)
"""
    with SingleRunGuard(lock_path):
        completed = subprocess.run(
            [sys.executable, "-c", code, str(lock_path)],
            cwd=Path(__file__).resolve().parents[2],
            check=False,
        )
    assert completed.returncode == 0


def test_runner_returns_concurrent_terminal_result(tmp_path: Path):
    lock_path = tmp_path / "runner.lock"
    runner = ScheduledJobRunner(
        job_name="demo",
        lock_path=lock_path,
        mutation_authority_mode=MutationAuthorityMode.READ_ONLY,
    )
    with SingleRunGuard(lock_path):
        result = runner.run(lambda _ctx: JobOutcome(processed_count=99))
    assert result.terminal_status == JobTerminalStatus.SKIPPED_CONCURRENT
    assert result.processed_count == 0
    assert result.skipped_count == 1

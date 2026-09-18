"""Owned direct-PostgreSQL fixture lifecycle, not a second database stack.

Only the exact root allocated here may be stopped or removed. Unknown ownership
is preserved for inspection. All phase failures survive successful cleanup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from typing import Any

MARKER = ".propertyai-stage-a-owned.json"


class FixtureLifecycleError(RuntimeError):
    def __init__(self, classification: str, phase: str, reason: str, report: dict | None = None):
        self.classification, self.phase, self.reason = classification, phase, reason
        self.report = report or {}
        super().__init__(f"FIXTURE_{classification}:{phase}:{reason}")


def safe_fixture_env() -> dict[str, str]:
    keys = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "JAVA_HOME")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.setdefault("PATH", "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin can retain an empty process-group entry after termination.
        # EPERM itself is never absence: independently inspect the process table.
        observed = subprocess.run(["ps", "-axo", "pgid="], check=True,
                                  capture_output=True, text=True, timeout=5,
                                  env=safe_fixture_env())
        return str(pgid) in observed.stdout.split()


def terminate_owned_group(child: subprocess.Popen, *, grace: float = 1.0) -> dict:
    """The caller must supply a child it started with start_new_session=True."""
    pgid = child.pid
    if pgid <= 1 or pgid == os.getpgrp():
        raise FixtureLifecycleError("INCOMPLETE", "STOP", "PROCESS_GROUP_OWNERSHIP_UNKNOWN")
    signals = []
    errors = []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _group_exists(pgid):
            break
        try:
            os.killpg(pgid, sig)
            signals.append(sig.name)
        except ProcessLookupError:
            break
        except PermissionError:
            errors.append("OWNED_GROUP_SIGNAL_PERMISSION_DENIED")
            break
        deadline = time.monotonic() + grace
        while _group_exists(pgid) and time.monotonic() < deadline:
            child.poll()
            time.sleep(0.01)
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    return {"owned_pgid": pgid, "termination_attempted": True, "signals": signals,
            "parent_reaped": child.poll() is not None, "signal_errors": errors, "residual_checked": True,
            "process_group_absent": not _group_exists(pgid)}


def run_owned_command(command, *, timeout: float = 30, check: bool = False,
                      allow_daemon: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """Bound init/Flyway/psql children; pg_ctl's owned server is handled separately."""
    kwargs.pop("capture_output", None)
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    kwargs.setdefault("text", True)
    kwargs.setdefault("env", safe_fixture_env())
    child = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        proof = terminate_owned_group(child)
        # Pipes can be held by a failed/escaped descendant; do not block again.
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                stream.close()
        status = "INFRA_ERROR" if proof["process_group_absent"] and proof["parent_reaped"] else "INCOMPLETE"
        raise FixtureLifecycleError(status, "TIMEOUT", "COMMAND_TIMEOUT", proof) from error
    if not allow_daemon and _group_exists(child.pid):
        proof = terminate_owned_group(child)
        status = "INFRA_ERROR" if proof["process_group_absent"] else "INCOMPLETE"
        raise FixtureLifecycleError(status, "RUN", "CHILD_PROCESS_LEAK", proof)
    result = subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


def classify_outcome(test_result: str, cleanup: dict | None) -> str:
    if not cleanup or cleanup.get("ownership") != "KNOWN" or not cleanup.get("residual_checked"):
        return "INCOMPLETE"
    if cleanup.get("classification") == "INCOMPLETE":
        return "INCOMPLETE"
    if cleanup.get("failures") or not cleanup.get("cleanup_ok"):
        return "INFRA_ERROR"
    if test_result == "PASS":
        return "PASS"
    if test_result in {"FAIL", "TEST_FAIL", "TEST_FAILURE"}:
        return "TEST_FAILURE"
    if test_result in {"TIMEOUT", "INFRA_ERROR"}:
        return "INFRA_ERROR"
    return "INCOMPLETE"


def valid_runner_parent(parent: Path) -> bool:
    raw, token = os.environ.get("PROPERTYAI_LT02_OWNED_ROOT"), os.environ.get("PROPERTYAI_LT02_OWNERSHIP_TOKEN")
    if not raw or not token or Path(raw).is_symlink() or Path(raw).resolve() != parent:
        return False
    marker = parent / ".propertyai-lt02-owned"
    return not marker.is_symlink() and marker.is_file() and marker.read_text() == token + "\n"


@dataclass
class FixtureLifecycle:
    root: Path
    nonce: str
    inode: int
    phase: str = "INIT"
    failures: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    cleanup_report: dict | None = None

    @classmethod
    def create(cls) -> "FixtureLifecycle":
        root = Path(tempfile.mkdtemp(prefix="pa-stage-a-", dir="/tmp")).resolve()
        os.chmod(root, 0o700)
        owner = cls(root, secrets.token_hex(24), root.stat().st_ino)
        marker = {"root": str(root), "nonce": owner.nonce, "inode": owner.inode,
                  "uid": os.getuid(), "creator_pid": os.getpid()}
        with (root / MARKER).open("x", encoding="utf8") as stream:
            json.dump(marker, stream, sort_keys=True)
        os.chmod(root / MARKER, 0o600)
        owner.record("INIT", "ALLOCATED", ownership="KNOWN")
        return owner

    def record(self, phase: str, result: str, **fields: Any) -> None:
        self.phase = phase
        observation = {"observed_at": datetime.now(timezone.utc).isoformat(), "root": str(self.root),
                       "owner_nonce": self.nonce, "phase": phase, "result": result, **fields}
        self.observations.append(observation)
        directory = os.environ.get("PROPERTYAI_FIXTURE_EVIDENCE_DIR")
        if directory:
            path = Path(directory) / (self.root.name + ".lifecycle.jsonl")
            with path.open("a", encoding="utf8") as stream:
                stream.write(json.dumps(observation, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def guard(self) -> None:
        root = self.root
        if root.is_symlink() or root.resolve() != root or not root.name.startswith("pa-stage-a-"):
            raise FixtureLifecycleError("INCOMPLETE", "CLEANUP", "OWNERSHIP_PATH_UNKNOWN")
        if root.parent != Path("/tmp").resolve() and not valid_runner_parent(root.parent):
            raise FixtureLifecycleError("INCOMPLETE", "CLEANUP", "OWNERSHIP_PARENT_UNKNOWN")
        st = root.lstat()
        marker = root / MARKER
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_ino != self.inode or marker.is_symlink():
            raise FixtureLifecycleError("INCOMPLETE", "CLEANUP", "OWNERSHIP_IDENTITY_UNKNOWN")
        value = json.loads(marker.read_text())
        if any(value.get(k) != v for k, v in {"root": str(root), "nonce": self.nonce, "inode": self.inode, "uid": os.getuid()}.items()):
            raise FixtureLifecycleError("INCOMPLETE", "CLEANUP", "OWNERSHIP_TOKEN_UNKNOWN")
        for child in (root / "data", root / "sock"):
            if child.is_symlink():
                raise FixtureLifecycleError("INCOMPLETE", "CLEANUP", "OWNERSHIP_CHILD_ESCAPE")

    def fail(self, phase: str, reason: str) -> None:
        self.failures.append({"phase": phase, "reason": reason})
        self.record(phase, "INFRA_ERROR", reason=reason)

    def _postmasters(self) -> list[int]:
        result = subprocess.run(["ps", "-axo", "pid=,command="], check=True, capture_output=True,
                                text=True, timeout=5, env=safe_fixture_env())
        owned = []
        for line in result.stdout.splitlines():
            try:
                pid, raw = line.strip().split(None, 1)
                argv = shlex.split(raw)
            except ValueError:
                continue
            if not argv or Path(argv[0]).name != "postgres" or "-D" not in argv:
                continue
            index = argv.index("-D") + 1
            if index < len(argv) and Path(argv[index]).resolve() == self.root / "data":
                owned.append(int(pid))
        return owned

    def finish(self, pg_ctl: str, *, remove_root: bool = True) -> dict:
        """Stop only a validated postmaster; independently prove residual absence."""
        report = {"root": str(self.root), "ownership": "UNKNOWN", "termination_attempted": False,
                  "stop_observed": False, "residual_checked": False, "cleanup_ok": False,
                  "root_absent": False, "classification": "INCOMPLETE", "failures": self.failures}
        try:
            self.guard()
            report["ownership"] = "KNOWN"
            before = self._postmasters()
            pidfile = self.root / "data/postmaster.pid"
            socket_dir = self.root / "sock"
            if pidfile.exists():
                lines = pidfile.read_text().splitlines()
                if len(lines) < 2 or not lines[0].isdigit() or Path(lines[1]).resolve() != self.root / "data":
                    raise FixtureLifecycleError("INCOMPLETE", "STOP", "POSTMASTER_OWNERSHIP_UNKNOWN")
                pid = int(lines[0])
                if before != [pid]:
                    if before:
                        raise FixtureLifecycleError("INCOMPLETE", "STOP", "POSTMASTER_PID_MISMATCH")
                    # A stale PID could now name a foreign process. Never signal it.
                    probe = subprocess.run(["ps", "-p", str(pid), "-o", "pid="], capture_output=True, text=True, timeout=5)
                    if probe.returncode == 0:
                        raise FixtureLifecycleError("INCOMPLETE", "STOP", "STALE_PID_FOREIGN_PROCESS")
                    self.fail("STOP", "STALE_PID_FILE")
                else:
                    report["termination_attempted"] = True
                    self.record("STOP", "ATTEMPTED", owned_pid=pid)
                    try:
                        result = run_owned_command([pg_ctl, "-D", str(self.root / "data"), "-m", "fast", "-w", "-t", "15", "stop"], timeout=20)
                        report["stop_returncode"] = result.returncode
                        report["stop_observed"] = result.returncode == 0
                        if result.returncode:
                            self.fail("STOP", "STOP_NONZERO")
                    except (OSError, FixtureLifecycleError) as error:
                        self.fail("STOP", type(error).__name__)
            elif before:
                raise FixtureLifecycleError("INCOMPLETE", "STOP", "POSTMASTER_WITHOUT_PID_OWNERSHIP")
            else:
                report["stop_observed"] = True
                self.record("STOP", "NO_POSTMASTER", reason="positive process-table absence")
            after = self._postmasters()
            residuals = [str(p) for p in [pidfile, *socket_dir.glob(".s.PGSQL.*")] if p.exists() or p.is_socket()]
            report.update(residual_checked=True, postmaster_pids=after, runtime_residuals=residuals)
            if after:
                raise FixtureLifecycleError("INCOMPLETE", "RESIDUAL_CHECK", "POSTMASTER_STILL_PRESENT")
            if residuals:
                self.fail("RESIDUAL_CHECK", "STALE_PID_OR_SOCKET")
            if remove_root:
                self.guard()
                self.record("CLEANUP", "ATTEMPTED", owned_artifacts=["data", "sock", "test-DCS", "test-case-files"])
                try:
                    shutil.rmtree(self.root)
                except OSError as error:
                    self.fail("CLEANUP", type(error).__name__)
                report["root_absent"] = not self.root.exists() and not self.root.is_symlink()
                if not report["root_absent"]:
                    self.fail("RESIDUAL_CHECK", "RESIDUAL_TEST_ROOT")
            report["cleanup_ok"] = not after and (report["root_absent"] if remove_root else not residuals)
            report["classification"] = "INFRA_ERROR" if self.failures or not report["cleanup_ok"] else "PASS"
        except (OSError, ValueError, FixtureLifecycleError, subprocess.SubprocessError) as error:
            report["error"] = getattr(error, "reason", type(error).__name__)
            report["classification"] = "INCOMPLETE"
        self.cleanup_report = report
        self.record("RESIDUAL_CHECK", report["classification"], cleanup=report, terminal=remove_root)
        return report

from __future__ import annotations

import getpass
import os
import re
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from propertyai_core.tests.fixture_lifecycle import (
    FixtureLifecycle, FixtureLifecycleError, run_owned_command, safe_fixture_env,
)

_run_command = run_owned_command


REPO_ROOT = Path(__file__).resolve().parents[2]
V221_ROOT = REPO_ROOT / "db" / "v2_2_1"
EXPECTED_MIGRATIONS = [
    "V20260904.101__v221_migrator_preflight.sql",
    "V20260904.102__v221_org_identity_command.sql",
    "V20260904.103__v221_reservation_cleaning.sql",
    "V20260904.104__v221_offer_assignment.sql",
    "V20260904.105__v221_exception_audit.sql",
    "V20260904.106__v221_async_projection.sql",
    "V20260904.107__v221_invariant_guards_functions.sql",
    "V20260904.108__v221_privileges_views.sql",
]
APP_LOGIN = "propertyai_stage_a_app_login"
WORKER_LOGIN = "propertyai_stage_a_worker_login"
EXPECTED_FLYWAY_VERSION = "13.5.0"
_FLYWAY_VERSION_BANNER = re.compile(
    r"^\s*Flyway(?:\s+[A-Za-z][A-Za-z0-9._-]*)*\s+"
    r"(?P<version>\d+\.\d+\.\d+)(?:\s+by\s+Redgate)?\s*$",
    re.IGNORECASE,
)

class _LoopbackUnixBridge:
    """Test-only in-process TCP loopback bridge to one exact Unix socket."""

    def __init__(self, upstream: Path) -> None:
        self.upstream = Path(upstream)
        self.bind_address: tuple[str, int] | None = None
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._relay_threads: set[threading.Thread] = set()
        self._active_sockets: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.closed = True

    def start(self) -> "_LoopbackUnixBridge":
        if self._listener is not None:
            raise RuntimeError("Flyway loopback bridge already started")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(32)
        listener.settimeout(0.2)
        self._listener = listener
        host, port = listener.getsockname()
        self.bind_address = (str(host), int(port))
        self.closed = False
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="pa-stage-a-flyway-bridge",
            daemon=True,
        )
        self._accept_thread.start()
        return self

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                raise
            relay_thread = threading.Thread(
                target=self._relay,
                args=(client,),
                name="pa-stage-a-flyway-relay",
                daemon=True,
            )
            with self._lock:
                self._relay_threads.add(relay_thread)
            relay_thread.start()

    def _relay(self, client: socket.socket) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        thread = threading.current_thread()
        try:
            upstream.connect(str(self.upstream))
            with self._lock:
                self._active_sockets.update((client, upstream))
            peers = (client, upstream)
            while not self._stop.is_set():
                try:
                    readable, _, _ = select.select(peers, [], [], 0.2)
                except (OSError, ValueError):
                    return
                for source in readable:
                    try:
                        payload = source.recv(65536)
                    except (BlockingIOError, OSError):
                        continue
                    if not payload:
                        return
                    target = upstream if source is client else client
                    try:
                        target.sendall(payload)
                    except OSError:
                        return
        finally:
            with self._lock:
                self._active_sockets.discard(client)
                self._active_sockets.discard(upstream)
                self._relay_threads.discard(thread)
            for peer in (client, upstream):
                try:
                    peer.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                peer.close()

    def close(self) -> None:
        if self.closed:
            return
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            listener.close()
        with self._lock:
            active = tuple(self._active_sockets)
            relay_threads = tuple(self._relay_threads)
        for peer in active:
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)
            self._accept_thread = None
        for relay_thread in relay_threads:
            relay_thread.join(timeout=2)
        self.closed = True

    def __enter__(self) -> "_LoopbackUnixBridge":
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


def _postgres_bin(name: str) -> str:
    direct = shutil.which(name)
    if direct:
        return direct
    homebrew = Path("/opt/homebrew/opt/postgresql@18/bin") / name
    if homebrew.exists():
        return str(homebrew)
    pytest.skip(f"PostgreSQL test binary unavailable: {name}")


def _flyway_bin() -> str:
    configured = os.environ.get("PROPERTYAI_FLYWAY_BIN")
    if configured and Path(configured).is_file():
        return configured
    direct = shutil.which("flyway")
    if direct:
        return direct
    # The accepted V2.2.1 verification downloaded Flyway 13.5.0 into a
    # disposable /private/tmp directory. Reuse that exact local tool when it is
    # still present; do not add a product dependency merely for Stage A tests.
    candidates = sorted(
        Path("/private/tmp").glob("propertyai-flyway-13.5.0.*/flyway-13.5.0/flyway")
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    pytest.skip(
        "Flyway 13.5.0 unavailable: Stage A harness will not synthesize "
        "flyway_schema_history or pretend manual SQL execution is Flyway"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class DisposablePostgres:
    root: Path
    data_dir: Path
    socket_dir: Path
    port: int
    superuser: str
    flyway_version: str = ""
    flyway_output: str = ""
    actual_flyway_executed: bool = False
    flyway_bridge_bind_address: tuple[str, int] | None = None
    flyway_bridge_upstream: str = ""
    flyway_bridge_closed: bool = False
    flyway_config_path: str = ""
    flyway_locations_env: str | None = None
    lifecycle: FixtureLifecycle | None = None

    @property
    def base_dsn(self) -> str:
        return f"host={self.socket_dir} port={self.port} dbname=postgres user={self.superuser}"

    def login_dsn(self, login_role: str) -> str:
        return f"host={self.socket_dir} port={self.port} dbname=postgres user={login_role}"

    def psql(
        self,
        *,
        user: str | None = None,
        sql_text: str | None = None,
        file: Path | None = None,
        role: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            _postgres_bin("psql"),
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            str(self.socket_dir),
            "-p",
            str(self.port),
            "-U",
            user or self.superuser,
            "-d",
            "postgres",
        ]
        if file is not None:
            command.extend(["-f", str(file)])
        elif sql_text is not None:
            command.extend(["-c", sql_text])
        else:
            raise ValueError("sql_text or file is required")
        if self.lifecycle is not None:
            self.lifecycle.guard()
        env = safe_fixture_env()
        if role:
            env["PGOPTIONS"] = f"-c role={role}"
        return _run_command(command, check=True, text=True, capture_output=True, env=env, timeout=30)

    def _finish(self, *, remove_root: bool) -> dict:
        if self.lifecycle is None:
            raise FixtureLifecycleError("INCOMPLETE", "STOP", "OWNERSHIP_NOT_REGISTERED")
        report = self.lifecycle.finish(_postgres_bin("pg_ctl"), remove_root=remove_root)
        if report["classification"] != "PASS":
            raise FixtureLifecycleError(report["classification"], "CLEANUP", "CLEANUP_NOT_PASS", report)
        return report

    def stop(self) -> dict:
        """Positive stop/residual proof; callers owning the root should use cleanup."""
        return self._finish(remove_root=False)

    def cleanup(self) -> dict:
        return self._finish(remove_root=True)


def _validated_flyway_version(output: str) -> str:
    versions = {
        match.group("version")
        for line in output.splitlines()
        if (match := _FLYWAY_VERSION_BANNER.match(line)) is not None
    }
    if len(versions) != 1:
        raise ValueError(
            "no unique recognizable Flyway version banner in version-command output"
        )
    actual = versions.pop()
    if actual != EXPECTED_FLYWAY_VERSION:
        raise ValueError(
            f"unexpected Flyway version for frozen V2.2.1 path: "
            f"expected {EXPECTED_FLYWAY_VERSION}, got {actual}"
        )
    return actual


def _run_actual_flyway(cluster: DisposablePostgres) -> None:
    flyway = _flyway_bin()
    flyway_env = safe_fixture_env()
    flyway_env.pop("FLYWAY_LOCATIONS", None)
    version = _run_command(
        [flyway, "-v"],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
        env=flyway_env,
    )
    version_output = version.stdout + version.stderr
    if version.returncode != 0:
        pytest.fail(
            "Flyway version probe failed for frozen V2.2.1 path\n"
            f"stdout:\n{version.stdout}\nstderr:\n{version.stderr}"
        )
    try:
        actual_version = _validated_flyway_version(version_output)
    except ValueError as exc:
        pytest.fail(f"{exc}\nversion output:\n{version_output}")

    # PostgreSQL remains Unix-socket-only (listen_addresses=''). pgJDBC
    # requires TCP, so Flyway gets a test-only loopback listener implemented in
    # this same Python process. There is no child bridge process that can outlive
    # the Stage A harness if it is terminated abruptly.
    upstream = cluster.socket_dir / f".s.PGSQL.{cluster.port}"
    bridge = _LoopbackUnixBridge(upstream)
    try:
        with bridge:
            assert bridge.bind_address is not None
            cluster.flyway_bridge_bind_address = bridge.bind_address
            cluster.flyway_bridge_upstream = str(bridge.upstream)
            flyway_config = V221_ROOT / "flyway" / "flyway.conf"
            cluster.flyway_config_path = str(flyway_config)
            cluster.flyway_locations_env = flyway_env.get("FLYWAY_LOCATIONS")
            result = _run_command(
                [
                    flyway,
                    f"-configFiles={flyway_config}",
                    f"-url=jdbc:postgresql://127.0.0.1:{bridge.bind_address[1]}/postgres?sslmode=disable",
                    "-user=propertyai_flyway",
                    "-password=",
                    "migrate",
                ],
                cwd=REPO_ROOT,
                check=False,
                text=True,
                capture_output=True,
                timeout=30,
                env=flyway_env,
            )
            if result.returncode != 0:
                pytest.fail(
                    "actual frozen Flyway V2.2.1 execution failed\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
            cluster.flyway_version = actual_version
            cluster.flyway_output = result.stdout + result.stderr
            cluster.actual_flyway_executed = True
    finally:
        cluster.flyway_bridge_closed = bridge.closed


def _create_runtime_login_identities(cluster: DisposablePostgres) -> None:
    cluster.psql(
        sql_text=f"""
            CREATE ROLE {APP_LOGIN}
                LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            CREATE ROLE {WORKER_LOGIN}
                LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            GRANT propertyai_app_runtime TO {APP_LOGIN}
                WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;
            GRANT propertyai_async_worker TO {WORKER_LOGIN}
                WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;
        """
    )


def start_disposable_postgres(*, fsync: bool = False) -> DisposablePostgres:
    # Resolve capabilities before allocation. A required missing fixture is infra,
    # never an implicit skip with a misleading green runner result.
    try:
        initdb, pg_ctl = _postgres_bin("initdb"), _postgres_bin("pg_ctl")
        _flyway_bin()
    except pytest.skip.Exception as error:
        raise FixtureLifecycleError("INFRA_ERROR", "INIT", "CAPABILITY_UNAVAILABLE") from error
    lifecycle = FixtureLifecycle.create()
    root = lifecycle.root
    cluster = DisposablePostgres(root, root / "data", root / "sock", _free_port(),
                                 getpass.getuser(), lifecycle=lifecycle)
    try:
        lifecycle.guard()
        cluster.socket_dir.mkdir(mode=0o700)
        lifecycle.record("INIT", "ATTEMPTED")
        _run_command([initdb, "-D", str(cluster.data_dir), "--auth=trust", "--no-locale",
                      "--encoding=UTF8", "-U", cluster.superuser], check=True, timeout=20)
        lifecycle.record("INIT", "PASS")
        lifecycle.record("START", "ATTEMPTED")
        options = (f"-p {cluster.port} -k {cluster.socket_dir} -c listen_addresses='' "
                   f"-c fsync={'on' if fsync else 'off'} -c synchronous_commit=on -c full_page_writes=on")
        _run_command([pg_ctl, "-D", str(cluster.data_dir), "-l", str(root / "postgres.log"),
                      "-o", options, "-w", "-t", "15", "start"],
                     check=True, timeout=20, allow_daemon=True)
        lifecycle.record("START", "PASS")
        lifecycle.record("READY", "ATTEMPTED")
        assert "1" in cluster.psql(sql_text="SELECT 1").stdout
        lifecycle.record("READY", "PASS")
        lifecycle.record("RUN", "ATTEMPTED")
        actual = sorted(path.name for path in (V221_ROOT / "migration").glob("*.sql"))
        if actual != EXPECTED_MIGRATIONS:
            raise AssertionError(f"frozen migration set drift: {actual}")
        cluster.psql(file=V221_ROOT / "bootstrap" / "001__privileged_roles_schema.sql")
        _run_actual_flyway(cluster)
        _create_runtime_login_identities(cluster)
        lifecycle.record("RUN", "PASS")
        return cluster
    except BaseException as error:
        phase = lifecycle.phase
        lifecycle.fail(phase, getattr(error, "reason", type(error).__name__))
        report = lifecycle.finish(pg_ctl)
        classification = "INCOMPLETE" if report["classification"] == "INCOMPLETE" else "INFRA_ERROR"
        raise FixtureLifecycleError(classification, phase, "SETUP_FAILED", report) from error

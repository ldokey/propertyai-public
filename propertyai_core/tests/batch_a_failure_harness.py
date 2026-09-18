"""Batch A only: owned PostgreSQL + real thin/DCS fences + killable W07 + ledger.

No live service, ambient DSN, production control store, or real provider is used.
The provider ledger records EVERY invocation (no deduplicating unique constraint),
so a duplicate send cannot be hidden by the fake. Checkpoints live only in these
repository subclasses/test adapters; production code has no pause/kill switches.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from adcp_global_writer_client import GlobalWriterLeaseClient
from adcp_global_writer_client.runtime_identity import (
    AuthorizedProductionIdentity, LiveOSProcessIdentityVerifier, ProductBuildIdentity,
    _capture_runtime_identity_with_verifier, write_authorized_identity,
)
from propertyai_core import global_writer
from propertyai_core.adapters.postgres.pool import PostgresStageAConfig, PostgresStageAPool
from propertyai_core.adapters.postgres.repository import PostgresCleanerRepository, PostgresOutboxWorkerRepository
from propertyai_core.ports.cleaner_repository import OutboxMessage, OutboxReconciliationState
from propertyai_core.runtime.outbox_reconciliation import (
    ProviderEvidence, ProviderEvidenceClass, effect_binding_sha256, row_binding_sha256,
)
from propertyai_core.runtime.postgres_outbox_worker import CleanerPostgresOutboxWorker, DeliveryReceipt
from propertyai_core.tests.postgres_stage_a_cluster import (
    APP_LOGIN, WORKER_LOGIN, REPO_ROOT, start_disposable_postgres,
)

from propertyai_core.tests.fixture_lifecycle import (
    valid_runner_parent, FixtureLifecycleError,
)

UTC = timezone.utc
EVIDENCE: list[dict[str, Any]] = []


def emit(kind: str, **values: Any) -> None:
    EVIDENCE.append({"kind": kind, **values})
    output = os.environ.get("BATCH_A_EVIDENCE_DIR")
    if output:
        path = Path(output) / "batch-a-observations.jsonl"
        with path.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(EVIDENCE[-1], sort_keys=True, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def source_digest() -> str:
    files = sorted(REPO_ROOT.joinpath("propertyai_core").rglob("*.py"))
    files += sorted(REPO_ROOT.joinpath("db/v2_2_1").rglob("*.sql"))
    files += [REPO_ROOT / "telegram_approval" / name for name in
              ("send_cleaning_operations.py", "send_due_completions.py")]
    manifest = [(str(p.relative_to(REPO_ROOT)), hashlib.sha256(p.read_bytes()).hexdigest()) for p in files]
    return hashlib.sha256(json.dumps(manifest, separators=(",", ":")).encode()).hexdigest()


def safe_env(home: Path) -> dict[str, str]:
    # Deliberately do not inherit credentials, provider tokens, PG* or writer state.
    return {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(home), "TMPDIR": str(home),
            "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PROPERTYAI_ADCP_GLOBAL_WRITER_CLIENT_WHEEL")])),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "LANG": "en_US.UTF-8",
            **{key: os.environ[key] for key in ("PROPERTYAI_LT02_OWNED_ROOT", "PROPERTYAI_LT02_OWNERSHIP_TOKEN", "PROPERTYAI_ADCP_GLOBAL_WRITER_CLIENT_WHEEL") if key in os.environ}}


def wait_until(predicate: Callable[[], Any], *, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("BATCH_A_DETERMINISTIC_COORDINATION_TIMEOUT")


def validate_config(config: dict[str, Any]) -> Path:
    root = Path(config["root"])
    if root.is_symlink() or root.resolve() != root or (root.parent != Path("/private/tmp") and not valid_runner_parent(root.parent)):
        raise RuntimeError("BATCH_A_OWNED_ROOT_REQUIRED")
    if not root.name.startswith("pa-stage-a-") or (root / "BATCH_A_OWNER").read_text() != config["nonce"]:
        raise RuntimeError("BATCH_A_OWNERSHIP_MISMATCH")
    case = Path(config["case"])
    if case.is_symlink() or case.resolve().parent != root or not case.name.startswith("batch-a-"):
        raise RuntimeError("BATCH_A_CASE_OWNERSHIP_MISMATCH")
    if Path(config["socket"]).resolve() != root / "sock" or not 1024 < config["port"] < 65536:
        raise RuntimeError("BATCH_A_TEST_SOCKET_REQUIRED")
    if config["source_root"] != str(REPO_ROOT) or config["source_digest"] != source_digest():
        raise RuntimeError("BATCH_A_SOURCE_BINDING_MISMATCH")
    return root


def dsn(config: dict[str, Any], login: str) -> str:
    validate_config(config)
    if login not in {APP_LOGIN, WORKER_LOGIN, config["superuser"], "propertyai_cleaner_worker"}:
        raise RuntimeError("BATCH_A_UNOWNED_LOGIN")
    return psycopg.conninfo.make_conninfo(host=config["socket"], port=config["port"],
                                        dbname="postgres", user=login, connect_timeout=3)


def stage_pool(config: dict[str, Any], *, worker: bool = False) -> PostgresStageAPool:
    login, role = (WORKER_LOGIN, "propertyai_async_worker") if worker else (APP_LOGIN, "propertyai_app_runtime")
    value = PostgresStageAPool(PostgresStageAConfig(dsn(config, login), login, role, "TEST", max_size=8))
    value.open()
    return value


def clock(config: dict[str, Any]) -> datetime:
    return datetime.fromisoformat((Path(config["case"]) / "clock.txt").read_text())


def install_coordinator(config: dict[str, Any], *, damage: str | None = None, writer_code: str = "W07"):
    validate_config(config)
    wheel = os.environ.get("PROPERTYAI_ADCP_GLOBAL_WRITER_CLIENT_WHEEL")
    if wheel:
        import adcp_global_writer_client as thin
        if not str(thin.__file__).startswith(wheel + os.sep):
            raise RuntimeError("BATCH_B_CHILD_THIN_CLIENT_ORIGIN_MISMATCH")
        emit("THIN_CLIENT_SOURCE_BINDING", origin=thin.__file__, wheel=wheel)

    case = Path(config["case"])
    identity_dir = case / f"identity-{os.getpid()}"
    identity_dir.mkdir(exist_ok=True)
    artifact = "sha256:" + config["source_digest"]
    commit = config["base_commit"]
    product = ProductBuildIdentity("PropertyAI", commit,
        f"product:PropertyAI@g{commit[:12]}|source={commit}|artifact={artifact}", artifact)
    config_identity = "sha256:" + hashlib.sha256(json.dumps(
        {k: config[k] for k in ("root", "nonce", "socket", "port", "source_digest")}, sort_keys=True).encode()).hexdigest()
    identity = _capture_runtime_identity_with_verifier(
        service_code=global_writer._WRITER_SERVICES[writer_code], product_build_identity=product,
        verifier=LiveOSProcessIdentityVerifier(), config_artifact_identity=config_identity)
    authorized = AuthorizedProductionIdentity(
        identity.service_code, identity.product_build_commit, identity.product_build_identity,
        identity.global_writer_client_build, identity.source_root_or_artifact_identity,
        config_artifact_identity=identity.config_artifact_identity)
    if damage == "config":
        authorized = replace(authorized, config_artifact_identity="sha256:" + "0" * 64)
    elif damage == "source":
        altered = "sha256:" + "0" * 64
        authorized = replace(authorized, source_root_or_artifact_identity=altered,
                             product_build_identity=f"product:PropertyAI@g{commit[:12]}|source={commit}|artifact={altered}")
    elif damage == "pid":
        identity = replace(identity, pid=os.getpid() + 100000)
    runtime_path, authorized_path = identity_dir / f"{writer_code}.runtime.json", identity_dir / f"{writer_code}.authorized.json"
    write_authorized_identity(authorized_path, authorized)
    cfg = global_writer.WriterConfig(
        dcs_path=case / "control.sqlite3", runtime_identity_path=runtime_path,
        authorized_identity_path=authorized_path, ttl_seconds=60, heartbeat_seconds=15,
        config_artifact_identity=config_identity)
    coordinator = global_writer.GlobalProductionWriterCoordinator(
        writer_code, config=cfg,
        client_factory=lambda path: GlobalWriterLeaseClient(path, _clock=lambda: clock(config)),
        capture_identity=lambda **kwargs: identity, start_heartbeat=False)
    global_writer.reset_coordinators_for_tests()
    global_writer._COORDINATORS[writer_code] = coordinator
    return coordinator


class Ledger:
    def __init__(self, config: dict[str, Any], *, quiescent: Callable[[], bool] | None = None):
        validate_config(config)
        self.config = config
        self.quiescent = quiescent or (lambda: False)
        self.path = Path(config["case"]) / "fake-provider.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS effects(sequence INTEGER PRIMARY KEY, outbox_id TEXT NOT NULL, binding TEXT NOT NULL, receipt TEXT NOT NULL, attempt INTEGER NOT NULL, fence INTEGER NOT NULL, pid INTEGER NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def record(self, row) -> str:
        receipt = "fake-provider:" + uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO effects(outbox_id,binding,receipt,attempt,fence,pid) VALUES(?,?,?,?,?,?)",
                       (str(row.outbox_id), effect_binding_sha256(row), receipt, row.attempt_count, row.lease_fence, os.getpid()))
        return receipt

    def effects(self, outbox_id: UUID) -> list[tuple]:
        with self.connect() as db:
            return db.execute("SELECT binding,receipt,attempt,fence,pid FROM effects WHERE outbox_id=? ORDER BY sequence", (str(outbox_id),)).fetchall()

    def inspect(self, row) -> ProviderEvidence:
        effects = self.effects(row.outbox_id)
        absence_proven = not effects and self.quiescent() is True
        classification = ProviderEvidenceClass.CONFIRMED_NO_EFFECT if absence_proven else ProviderEvidenceClass.AMBIGUOUS
        effect_id = None
        if len(effects) == 1 and effects[0][0] == effect_binding_sha256(row):
            classification, effect_id = ProviderEvidenceClass.CONFIRMED_EFFECT, effects[0][1]
        elif effects:
            classification = ProviderEvidenceClass.CONFLICTING_EVIDENCE
        # Harness resolution occurs only after a completed or reaped test child.
        # The local ledger has no asynchronous network effects; this is a positive,
        # complete absence proof, unlike a missing real provider object.
        return ProviderEvidence(classification, row_binding_sha256(row), datetime.now(UTC),
            "test-owned-ledger:" + self.path.name, effect_id,
            complete_no_effect_proof=absence_proven, in_flight_effects_excluded=absence_proven)


class BatchAHarness:
    def __init__(self, cluster):
        self.cluster = cluster
        root = cluster.root.resolve()
        marker = root / "BATCH_A_OWNER"
        if not marker.exists():
            marker.write_text(uuid4().hex)
        self.case = root / ("batch-a-" + uuid4().hex)
        self.case.mkdir(mode=0o700)
        self.config = {"root": str(root), "nonce": marker.read_text(), "case": str(self.case),
            "socket": str(cluster.socket_dir.resolve()), "port": cluster.port, "superuser": cluster.superuser,
            "source_root": str(REPO_ROOT), "source_digest": source_digest(),
            "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()}
        self.config_path = self.case / "config.json"
        self.config_path.write_text(json.dumps(self.config))
        (self.case / "clock.txt").write_text(datetime.now(UTC).isoformat())
        self.children: list[subprocess.Popen] = []
        self.streams = []
        self.outbox_ids: list[UUID] = []
        self.app_pool = self.worker_pool = None
        self.prior_coordinators = dict(global_writer._COORDINATORS)
        controller = Path(os.environ["BATCH_A_CONTROLLER_ROOT"])
        interpreter = os.environ["BATCH_A_CONTROLLER_PYTHON"]
        environment = safe_env(self.case)
        environment["PYTHONPATH"] = str(controller / "src")
        script = "from adcp.store import migrations; import sqlite3,sys; c=sqlite3.connect(sys.argv[1],isolation_level=None); c.execute('PRAGMA foreign_keys=ON'); migrations.migrate(c,backup_root=None,target_version=10); assert c.execute('SELECT max(version) FROM schema_migration').fetchone()[0]==10; c.close()"
        bootstrap = subprocess.run([interpreter, "-c", script, str(self.case / "control.sqlite3")],
                       env=environment, cwd=controller, capture_output=True, text=True, timeout=20)
        (self.case / "dcs-bootstrap.log").write_text(bootstrap.stdout + bootstrap.stderr)
        if bootstrap.returncode:
            raise RuntimeError("BATCH_A_DCS_BOOTSTRAP_FAILED:" + bootstrap.stderr)
        try:
            self.app_pool = stage_pool(self.config)
            self.worker_pool = stage_pool(self.config, worker=True)
            self.repository = PostgresCleanerRepository(self.app_pool)
            self.worker_repository = PostgresOutboxWorkerRepository(self.worker_pool)
            self.ledger = Ledger(self.config, quiescent=lambda: all(child.poll() is not None for child in self.children))
            self.coordinator = install_coordinator(self.config)
        except BaseException:
            self.close()
            raise

    def message(self, **overrides) -> OutboxMessage:
        identity = uuid4()
        value = OutboxMessage(identity, "RESERVATION_INGESTED", "RESERVATION", uuid4(),
                              "NOTION_PROJECTION", datetime(2026, 1, 1, tzinfo=UTC),
                              "BATCH-A:" + identity.hex, {"synthetic": True}, 5)
        value = replace(value, **overrides)
        self.outbox_ids.append(value.outbox_id)
        return value

    def enqueue(self, **overrides) -> OutboxMessage:
        value = self.message(**overrides)
        with self.repository.transaction() as tx:
            tx.enqueue_outbox(value)
        return value

    def row(self, outbox_id):
        return self.worker_repository.read_outbox_reconciliation(outbox_id)

    def full_row(self, outbox_id):
        with psycopg.connect(dsn(self.config, self.config["superuser"]), row_factory=dict_row) as connection:
            return connection.execute("SELECT * FROM propertyai.integration_outbox WHERE outbox_id=%s", (outbox_id,)).fetchone()

    def expire_global_lease(self):
        (self.case / "clock.txt").write_text((clock(self.config) + timedelta(seconds=61)).isoformat())
        emit("GLOBAL_TEST_CLOCK_ADVANCE", seconds=61, dcs_path=str(self.case / "control.sqlite3"))

    def spawn(self, *, checkpoint: str = "none", mode: str = "worker", outcome: str = "success",
              history: str | None = None, damage: str | None = None):
        run_id = uuid4().hex
        stderr = (self.case / f"{run_id}.stderr").open("w")
        self.streams.append(stderr)
        environment = safe_env(self.case)
        argv = [sys.executable, "-m", "propertyai_core.tests.batch_a_failure_harness", str(self.config_path),
                run_id, checkpoint, mode, outcome, history or "ABSENT", damage or "NONE"]
        child = subprocess.Popen(argv, cwd=REPO_ROOT, env=environment, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=stderr, text=True)
        self.children.append(child)
        child.batch_run_id = run_id
        child.batch_stderr = self.case / f"{run_id}.stderr"
        if checkpoint != "none":
            marker = self.case / f"{run_id}.checkpoint.json"
            def ready():
                if child.poll() is not None:
                    raise AssertionError(f"child exited before checkpoint: {child.returncode}: {child.batch_stderr.read_text()}")
                return json.loads(marker.read_text()) if marker.exists() else None
            point = wait_until(ready, timeout=20)
            assert point["checkpoint"] == checkpoint and point["pid"] == child.pid
            emit("WORKER_CHECKPOINT", **point)
        return child

    def finish(self, child, *, expected: int = 0):
        output, _ = child.communicate(timeout=20)
        assert child.returncode == expected, (child.returncode, child.batch_stderr.read_text())
        result = json.loads(output.strip().splitlines()[-1]) if output.strip() else {}
        emit("CHILD_EXIT", pid=child.pid, returncode=child.returncode, result=result)
        return result

    def kill(self, child):
        assert child in self.children and child.poll() is None
        child.kill()
        child.communicate(timeout=10)
        assert child.returncode == -signal.SIGKILL
        emit("OWNED_CHILD_SIGKILL", pid=child.pid, returncode=child.returncode)
        self.expire_global_lease()

    def restart_postgres(self):
        validate_config(self.config)
        self.app_pool.close()
        self.worker_pool.close()
        pidfile = self.cluster.data_dir / "postmaster.pid"
        before_pid = int(pidfile.read_text().splitlines()[0])
        options = f"-p {self.cluster.port} -k {self.cluster.socket_dir} -c listen_addresses='' -c fsync=on -c synchronous_commit=on -c full_page_writes=on"
        for args in (("stop", "-m", "immediate"), ("start", "-l", str(self.case / "restart.log"), "-o", options)):
            subprocess.run(["/opt/homebrew/bin/pg_ctl", "-D", str(self.cluster.data_dir), "-w", "-t", "15", *args],
                           env=safe_env(self.case), capture_output=True, text=True, check=True, timeout=20)
        after_pid = int(pidfile.read_text().splitlines()[0])
        assert before_pid != after_pid
        self.app_pool = stage_pool(self.config)
        self.worker_pool = stage_pool(self.config, worker=True)
        self.repository = PostgresCleanerRepository(self.app_pool)
        self.worker_repository = PostgresOutboxWorkerRepository(self.worker_pool)
        with psycopg.connect(dsn(self.config, self.config["superuser"])) as connection:
            settings = [connection.execute("SHOW " + setting).fetchone()[0] for setting in ("fsync", "synchronous_commit", "full_page_writes")]
        assert settings == ["on", "on", "on"]
        emit("POSTGRES_IMMEDIATE_RESTART", before_pid=before_pid, after_pid=after_pid,
             settings=settings, root=self.config["root"])

    def close(self):
        failures = []
        for child in self.children:
            # Stay in the outer LT-02 owned process group: a runner timeout
            # must also terminate worker descendants, not orphan private sessions.
            attempted = child.poll() is None
            if attempted:
                child.kill()
            try:
                child.communicate(timeout=10)
            except Exception as error:
                failures.append(type(error).__name__)
            emit("OWNED_CHILD_CLEANUP", child_pid=child.pid,
                 termination_attempted=attempted, parent_reaped=child.poll() is not None,
                 residual_checked=True, residual_present=child.poll() is None)
        for stream in self.streams:
            stream.close()
        for pool in (self.app_pool, self.worker_pool):
            if pool is not None:
                pool.close()
        global_writer.reset_coordinators_for_tests()
        global_writer._COORDINATORS.update(self.prior_coordinators)
        # Exact test-created identities only; no table-wide cleanup or foreign roots.
        if self.outbox_ids:
            with psycopg.connect(dsn(self.config, self.config["superuser"]), autocommit=True) as connection:
                for outbox_id in set(self.outbox_ids):
                    connection.execute("DELETE FROM propertyai.integration_outbox WHERE outbox_id=%s", (outbox_id,))
        with GlobalWriterLeaseClient(self.case / "control.sqlite3") as client:
            writer_state = client.get()
        control = sqlite3.connect(self.case / "control.sqlite3")
        try:
            integrity = control.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = control.execute("PRAGMA foreign_key_check").fetchall()
            event_count = control.execute("SELECT count(*) FROM global_production_writer_event").fetchone()[0]
        finally:
            control.close()
        assert integrity == "ok" and not foreign_keys
        emit("CASE_CLEANUP", case=str(self.case), child_pids=[p.pid for p in self.children],
             all_children_reaped=all(p.poll() is not None for p in self.children), failures=failures,
             dcs_writer=writer_state, dcs_event_count=event_count, dcs_integrity=integrity, dcs_fk_violations=0)
        assert not failures


class MemoryOutboxWorkerPort:
    """FAST contract double only; integration proof always uses the real PG port."""
    def __init__(self, claim, *, intent_allowed=True, resolution_allowed=True):
        self.claim = claim
        self.intent_allowed = intent_allowed
        self.resolution_allowed = resolution_allowed
        self.state = None
        self.calls = []
        self.failed = []

    def claim_outbox(self, worker_id, *, limit, lease_seconds):
        assert worker_id == self.claim.lease_owner and limit == 1
        self.calls.append(("CLAIM", self.claim.lease_fence))
        return [self.claim]

    def begin_outbox_delivery(self, claim, *, worker_id, error_code):
        self.calls.append(("INTENT", claim.lease_fence))
        if not self.intent_allowed:
            return None
        self.state = OutboxReconciliationState(
            outbox_id=claim.outbox_id, domain_event_id=None, event_type=claim.event_type,
            aggregate_type=claim.aggregate_type, aggregate_id=claim.aggregate_id,
            destination_type=claim.destination_type, destination_ref=claim.destination_ref,
            outbox_status="PENDING_RECONCILIATION", idempotency_key="test-memory-port",
            payload=claim.payload, attempt_count=claim.attempt_count, max_attempts=claim.max_attempts,
            lease_fence=claim.lease_fence, external_effect_id=claim.external_effect_id, last_error_code=error_code)
        return self.state

    def resolve_outbox_reconciliation_exact(self, expected, **kwargs):
        self.calls.append(("RESOLVE", expected.lease_fence))
        assert expected == self.state
        if not self.resolution_allowed:
            return None
        self.state = replace(expected, outbox_status=kwargs["resolution"],
                             external_effect_id=kwargs["external_effect_id"], last_error_code=kwargs["error_code"])
        return self.state

    def read_outbox_reconciliation(self, outbox_id):
        assert outbox_id == self.claim.outbox_id
        return self.state

    def fail_outbox(self, *args, **kwargs):
        self.failed.append((args, kwargs))
        return True


@pytest.fixture(scope="module")
def batch_a_cluster():
    try:
        cluster = start_disposable_postgres(fsync=True)
    except pytest.skip.Exception as error:
        pytest.fail(f"REQUIRED_BATCH_A_CAPABILITY_NOT_RUN:{error}")
    try:
        with psycopg.connect(cluster.base_dsn) as connection:
            connection.execute("CREATE ROLE propertyai_cleaner_worker LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
            connection.execute("GRANT propertyai_async_worker TO propertyai_cleaner_worker WITH INHERIT FALSE, SET TRUE, ADMIN FALSE")
            # Strict W07 ACL admission also proves app/worker separation against
            # this named app principal; omitting it is not equivalent to no grants.
            connection.execute("CREATE ROLE propertyai_cleaner_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
            connection.execute("GRANT propertyai_app_runtime TO propertyai_cleaner_app WITH INHERIT FALSE, SET TRUE, ADMIN FALSE")
            assert connection.execute("SHOW fsync").fetchone()[0] == "on"
            assert connection.execute("SHOW synchronous_commit").fetchone()[0] == "on"
            assert connection.execute("SHOW full_page_writes").fetchone()[0] == "on"
            assert connection.execute("SHOW listen_addresses").fetchone()[0] == ""
        emit("DISPOSABLE_CLUSTER_STARTED", root=str(cluster.root.resolve()), port=cluster.port,
             fsync=True, flyway_version=cluster.flyway_version, source_digest=source_digest())
        yield cluster
    finally:
        root = cluster.root.resolve()
        try:
            logdir = os.environ.get("BATCH_A_EVIDENCE_DIR")
            if logdir:
                retained = Path(logdir) / root.name
                shutil.copytree(root, retained, ignore=shutil.ignore_patterns("data", "sock", "*.sqlite3*", "dcs-backups"))
        finally:
            proof = cluster.cleanup()
        emit("CLUSTER_CLEANUP", root=str(root), stopped=proof["stop_observed"],
             socket_absent=not proof.get("runtime_residuals"), root_absent=proof["root_absent"],
             lifecycle=proof)



@pytest.fixture
def batch_a(batch_a_cluster):
    harness = BatchAHarness(batch_a_cluster)
    try:
        yield harness
    finally:
        harness.close()


def _child(config_path: str, run_id: str, checkpoint: str, mode: str, outcome: str, history: str, damage: str):
    config = json.loads(Path(config_path).read_text())
    validate_config(config)
    case = Path(config["case"])
    def pause(point: str):
        if checkpoint != point:
            return
        path = case / f"{run_id}.checkpoint.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump({"checkpoint": point, "pid": os.getpid()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        if sys.stdin.readline() != "continue\n":
            raise RuntimeError("BATCH_A_CHECKPOINT_NOT_RELEASED")

    # Providers are forbidden to escape the durable local ledger, even if a test
    # accidentally constructs a real client. psycopg's Unix socket is unaffected.
    import socket
    original_connect = socket.socket.connect
    def local_connect(self, address):
        if self.family != socket.AF_UNIX:
            raise AssertionError("BATCH_A_EXTERNAL_NETWORK_FORBIDDEN")
        return original_connect(self, address)
    socket.socket.connect = local_connect
    coordinator = install_coordinator(config, damage=None if damage == "NONE" else damage)
    pool = stage_pool(config, worker=True)
    ledger = Ledger(config)
    class CheckpointRepository(PostgresOutboxWorkerRepository):
        def claim_outbox(self, *args, **kwargs):
            value = super().claim_outbox(*args, **kwargs)
            if value:
                pause("after_claim")
            return value
        def begin_outbox_delivery(self, *args, **kwargs):
            value = super().begin_outbox_delivery(*args, **kwargs)
            pause("before_provider")
            return value
    repository = CheckpointRepository(pool)
    class Adapter:
        def deliver(self, claim):
            row = repository.read_outbox_reconciliation(claim.outbox_id)
            assert row.outbox_status == "PENDING_RECONCILIATION"
            if outcome == "unavailable":
                raise RuntimeError("synthetic-provider-unavailable")
            receipt = ledger.record(row)
            pause("after_provider")
            if outcome == "ambiguous":
                pause("ambiguous_response")
                raise RuntimeError("synthetic-acknowledgement-lost")
            return DeliveryReceipt(receipt)
    worker = CleanerPostgresOutboxWorker(repository, Adapter(), worker_id="batch-a-worker", lease_seconds=1)
    try:
        if mode == "startup":
            from propertyai_core.runtime import cleaner_pg_outbox_service as service
            # Actual build_worker/Production pool/ACL admission/main/loop. Only
            # connection transport and provider clients are test-owned injections.
            from propertyai_core.adapters.postgres import worker_pool as strict_pool
            pool.close()
            strict_pool.worker_connection_info = lambda env: dsn(config, "propertyai_cleaner_worker")
            strict_pool.WORKER_DATABASE = service.WORKER_DATABASE = "postgres"
            production_build = service.build_worker
            class UnusedProvider:
                def __getattr__(self, name):
                    raise AssertionError("BATCH_A_NONLEDGER_PROVIDER_FORBIDDEN:" + name)
            def build():
                nonlocal pool, repository
                pool, built = production_build(environment={
                    "PROPERTYAI_CLEANER_AUTHORITY": "POSTGRES",
                    "PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY": "POST_CUTOVER_PG"},
                    notion_client=UnusedProvider(), calendar_client=UnusedProvider(), telegram_client=UnusedProvider())
                repository = CheckpointRepository(pool)
                built.repository = repository
                built.adapter = Adapter()
                built.lease_seconds = 1
                return pool, built
            service.build_worker = build
            normal_loop = service.run_worker_loop
            results = []
            def loop(value, **kwargs):
                health = kwargs["observe"]
                def observe(result):
                    health(result)
                    results.append(asdict(result))
                return normal_loop(value, max_cycles=1, sleep=lambda _: None, observe=observe)
            service.run_worker_loop = loop
            os.environ["PROPERTYAI_GLOBAL_WRITER_RUNTIME_IDENTITY_PATH"] = str(coordinator.config.runtime_identity_path)
            if history != "ABSENT":
                os.environ[service.RECONCILIATION_OPERATION_ENV] = history
            # A historical module import itself is a regression: main must not read
            # fixed business IDs or provider cutover objects on normal startup.
            assert "propertyai_core.runtime.cleaner_projection_reconciliation" not in sys.modules
            service.main()
            assert "propertyai_core.runtime.cleaner_projection_reconciliation" not in sys.modules
            print(json.dumps({"mode": mode, "pid": os.getpid(), "results": results}))
        else:
            print(json.dumps({"pid": os.getpid(), "result": asdict(worker.run_once())}))
    finally:
        pool.close()


if __name__ == "__main__":
    _child(*sys.argv[1:])

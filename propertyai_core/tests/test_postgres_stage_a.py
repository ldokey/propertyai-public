from __future__ import annotations

import inspect
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import propertyai_core.adapters.postgres.pool as postgres_pool_module
import propertyai_core.tests.postgres_stage_a_cluster as stage_a_cluster_module
from psycopg import errors

from propertyai_core.adapters.postgres.errors import (
    AuthorityEpochError,
    PostgresConfigurationError,
    PostgresConflictError,
    PostgresConstraintError,
    PostgresRepositoryError,
    PostgresRetryableError,
    ReservationRevisionError,
    map_postgres_error,
)
from propertyai_core.adapters.postgres.pool import PostgresStageAConfig, PostgresStageAPool
from propertyai_core.adapters.postgres.repository import (
    PostgresCleanerRepository,
    PostgresOutboxWorkerRepository,
)
from propertyai_core.ports.cleaner_repository import (
    CanonicalReservationSnapshot,
    CommandReceiptInput,
    OutboxMessage,
)
from propertyai_core.tests.postgres_stage_a_cluster import (
    APP_LOGIN,
    EXPECTED_FLYWAY_VERSION,
    EXPECTED_MIGRATIONS,
    WORKER_LOGIN,
    start_disposable_postgres,
)

UTC = timezone.utc


@pytest.fixture(scope="module")
def pg_cluster():
    cluster = start_disposable_postgres()
    try:
        yield cluster
    finally:
        cluster.cleanup()


@pytest.fixture(scope="module")
def pools(pg_cluster):
    app_config = PostgresStageAConfig(
        dsn=pg_cluster.login_dsn(APP_LOGIN),
        expected_session_user=APP_LOGIN,
        expected_role="propertyai_app_runtime",
        data_environment="TEST",
        min_size=1,
        max_size=8,
    )
    worker_config = PostgresStageAConfig(
        dsn=pg_cluster.login_dsn(WORKER_LOGIN),
        expected_session_user=WORKER_LOGIN,
        expected_role="propertyai_async_worker",
        data_environment="TEST",
        min_size=1,
        max_size=4,
    )
    with PostgresStageAPool(app_config) as app_pool, PostgresStageAPool(worker_config) as worker_pool:
        yield app_pool, worker_pool


@pytest.fixture
def repository(pools):
    return PostgresCleanerRepository(pools[0])


def seed_property(repository: PostgresCleanerRepository):
    organization_id = uuid4()
    property_id = uuid4()
    rental_unit_id = uuid4()
    with repository.transaction() as tx:
        tx._connection.execute(
            """
            INSERT INTO propertyai.organization(
                organization_id, organization_code, display_name, organization_status, data_environment
            ) VALUES (%s, %s, 'Stage A Test Org', 'ACTIVE', 'TEST')
            """,
            (organization_id, f"ORG-{organization_id.hex}"),
        )
        tx._connection.execute(
            """
            INSERT INTO propertyai.property(
                property_id, organization_id, property_code, display_name, timezone_name
            ) VALUES (%s, %s, %s, 'Stage A Property', 'Asia/Seoul')
            """,
            (property_id, organization_id, f"PROP-{property_id.hex}"),
        )
        tx._connection.execute(
            """
            INSERT INTO propertyai.rental_unit(
                rental_unit_id, rental_unit_code, property_id, display_name
            ) VALUES (%s, %s, %s, 'Whole Unit')
            """,
            (rental_unit_id, f"UNIT-{rental_unit_id.hex}", property_id),
        )
    return property_id, rental_unit_id


def snapshot_for(repository: PostgresCleanerRepository, *, checkout_offset_hours: int = 0):
    property_id, rental_unit_id = seed_property(repository)
    reservation_id = uuid4()
    check_in = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
    check_out = datetime(2026, 9, 12, 2, 0, tzinfo=UTC) + timedelta(hours=checkout_offset_hours)
    return CanonicalReservationSnapshot(
        reservation_id=reservation_id,
        reservation_code=f"RSV-{reservation_id.hex}",
        property_id=property_id,
        rental_unit_id=rental_unit_id,
        source_channel="GLOBAL_RESERVATION_MASTER",
        external_reservation_id=f"MASTER-{reservation_id.hex}",
        reservation_status="CONFIRMED",
        check_in_at=check_in,
        check_out_at=check_out,
    )


def read_reservation(repository: PostgresCleanerRepository, reservation_id):
    with repository.transaction() as tx:
        return tx._connection.execute(
            """
            SELECT reservation_status, check_in_at, check_out_at, source_version
              FROM propertyai.reservation WHERE reservation_id=%s
            """,
            (reservation_id,),
        ).fetchone()


def test_a01_frozen_v221_migrations_applied_in_disposable_postgres(pg_cluster):
    assert pg_cluster.actual_flyway_executed is True
    assert pg_cluster.flyway_version == EXPECTED_FLYWAY_VERSION
    assert "Successfully applied 8 migrations" in pg_cluster.flyway_output
    result = pg_cluster.psql(
        sql_text="""
        SELECT count(*) AS core_tables
          FROM information_schema.tables
         WHERE table_schema='propertyai' AND table_type='BASE TABLE'
           AND table_name <> 'flyway_schema_history';
        SELECT count(*) AS history_rows FROM propertyai.flyway_schema_history WHERE success;
        SELECT pg_get_userbyid(c.relowner) AS history_owner
          FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace
         WHERE n.nspname='propertyai' AND c.relname='flyway_schema_history';
        SHOW listen_addresses;
        """
    )
    assert "24" in result.stdout
    assert str(len(EXPECTED_MIGRATIONS)) in result.stdout
    assert "propertyai_owner" in result.stdout
    with psycopg.connect(pg_cluster.base_dsn) as connection:
        assert connection.execute("SHOW listen_addresses").fetchone()[0] == ""


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("Flyway Community Edition 13.5.0 by Redgate\n", "13.5.0"),
        (
            "WARNING: No locations configured and default location 'sql' not found.\n"
            "Flyway Community Edition 13.5.0 by Redgate\n",
            "13.5.0",
        ),
        ("Flyway OSS Edition 13.5.0\n", "13.5.0"),
    ],
    ids=["FP-01-modern-banner", "FP-02-warning-before-banner", "FP-03-legacy-edition"],
)
def test_fp01_fp03_flyway_version_probe_accepts_exact_frozen_version(output, expected):
    assert stage_a_cluster_module._validated_flyway_version(output) == expected


@pytest.mark.parametrize(
    "output",
    [
        "Flyway Community Edition 13.4.9 by Redgate\n",
        "Flyway Community Edition 14.0.0 by Redgate\n",
        "WARNING: No locations configured and default location 'sql' not found.\n",
    ],
    ids=["FP-04-older-version", "FP-05-new-major", "FP-06-no-version"],
)
def test_fp04_fp06_flyway_version_probe_rejects_incompatible_or_missing(output):
    with pytest.raises(ValueError):
        stage_a_cluster_module._validated_flyway_version(output)


def test_fp07_actual_flyway_uses_checked_in_config_with_locations_unset(pg_cluster):
    assert pg_cluster.actual_flyway_executed is True
    assert pg_cluster.flyway_version == EXPECTED_FLYWAY_VERSION
    assert pg_cluster.flyway_locations_env is None
    assert pg_cluster.flyway_config_path == str(
        stage_a_cluster_module.V221_ROOT / "flyway" / "flyway.conf"
    )


def test_fp08_actual_flyway_history_has_exact_eight_successful_migrations(pg_cluster):
    with psycopg.connect(pg_cluster.base_dsn) as connection:
        rows = connection.execute(
            "SELECT script, success FROM propertyai.flyway_schema_history ORDER BY installed_rank"
        ).fetchall()
    assert rows == [(migration, True) for migration in EXPECTED_MIGRATIONS]
    assert len(rows) == 8


def _assert_loopback_listener_closed(address: tuple[str, int], *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection(address, timeout=0.1):
                pass
        except OSError:
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"loopback bridge listener still accepts connections: {address}")
        time.sleep(0.02)


def test_b01_actual_flyway_bridge_starts_succeeds_and_closes(pg_cluster):
    assert pg_cluster.actual_flyway_executed is True
    assert pg_cluster.flyway_bridge_closed is True
    assert pg_cluster.flyway_bridge_bind_address is not None
    assert pg_cluster.flyway_bridge_bind_address[0] == "127.0.0.1"
    assert pg_cluster.flyway_bridge_upstream == str(
        pg_cluster.socket_dir / f".s.PGSQL.{pg_cluster.port}"
    )
    _assert_loopback_listener_closed(pg_cluster.flyway_bridge_bind_address)


def test_b02_flyway_exception_closes_in_process_bridge(monkeypatch, tmp_path):
    cluster = stage_a_cluster_module.DisposablePostgres(
        root=tmp_path,
        data_dir=tmp_path / "data",
        socket_dir=tmp_path / "sock",
        port=65432,
        superuser="stage_a_test",
    )
    cluster.socket_dir.mkdir()

    monkeypatch.setattr(stage_a_cluster_module, "_flyway_bin", lambda: "/fake/flyway")

    def fake_run(command, **kwargs):
        if command == ["/fake/flyway", "-v"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="Flyway OSS Edition 13.5.0\n",
                stderr="",
            )
        if command and command[-1] == "migrate":
            raise RuntimeError("injected Flyway execution failure")
        raise AssertionError(f"unexpected subprocess command: {command}")

    monkeypatch.setattr(stage_a_cluster_module, "_run_command", fake_run)
    with pytest.raises(RuntimeError, match="injected Flyway execution failure"):
        stage_a_cluster_module._run_actual_flyway(cluster)

    assert cluster.flyway_bridge_closed is True
    assert cluster.flyway_bridge_bind_address is not None
    assert cluster.flyway_bridge_bind_address[0] == "127.0.0.1"
    assert cluster.flyway_bridge_upstream == str(cluster.socket_dir / ".s.PGSQL.65432")
    _assert_loopback_listener_closed(cluster.flyway_bridge_bind_address)


def test_b03_abrupt_stage_a_process_termination_leaves_no_bridge_survivor():
    probe_root = Path(tempfile.mkdtemp(prefix="pa-flyprobe-", dir="/tmp"))
    ready_file = probe_root / "ready.json"
    marker = f"pa-flyprobe-{uuid4().hex}"
    probe_script = r"""
import json
import os
import time
from pathlib import Path
from propertyai_core.tests.postgres_stage_a_cluster import _LoopbackUnixBridge

root = Path(os.environ["PA_FLYPROBE_ROOT"])
bridge = _LoopbackUnixBridge(root / ".s.PGSQL.probe")
bridge.start()
assert bridge.bind_address is not None
(root / "ready.json").write_text(json.dumps({
    "pid": os.getpid(),
    "host": bridge.bind_address[0],
    "port": bridge.bind_address[1],
    "marker": os.environ["PA_FLYPROBE_MARKER"],
}))
while True:
    time.sleep(1)
"""
    env = os.environ.copy()
    env["PA_FLYPROBE_ROOT"] = str(probe_root)
    env["PA_FLYPROBE_MARKER"] = marker
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", probe_script, marker],
        cwd=stage_a_cluster_module.REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    address: tuple[str, int] | None = None
    try:
        deadline = time.monotonic() + 5
        while not ready_file.exists():
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                pytest.fail(f"bridge lifetime probe exited before ready: {stderr}")
            if time.monotonic() >= deadline:
                pytest.fail("bridge lifetime probe did not become ready")
            time.sleep(0.02)
        ready = json.loads(ready_file.read_text())
        assert ready["pid"] == proc.pid
        assert ready["host"] == "127.0.0.1"
        assert ready["marker"] == marker
        address = (ready["host"], int(ready["port"]))
        with socket.create_connection(address, timeout=0.5):
            pass

        process_table = subprocess.run(
            ["ps", "-axo", "ppid=,pid=,command="],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
        child_rows = [
            line for line in process_table
            if line.split(maxsplit=2) and line.split(maxsplit=2)[0] == str(proc.pid)
        ]
        assert child_rows == []

        proc.terminate()
        proc.wait(timeout=5)
        assert proc.poll() is not None
        _assert_loopback_listener_closed(address)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stderr:
            proc.stderr.close()
        shutil.rmtree(probe_root, ignore_errors=True)
    assert not probe_root.exists()


def test_b07_disposable_stage_a_cleanup_removes_root_and_bridge_listener():
    cluster = start_disposable_postgres()
    root = cluster.root
    address = cluster.flyway_bridge_bind_address
    assert address is not None
    try:
        assert cluster.flyway_bridge_closed is True
        _assert_loopback_listener_closed(address)
    finally:
        cluster.cleanup()
    assert not root.exists()
    _assert_loopback_listener_closed(address)


def test_b08_actual_flyway_history_is_exact_and_not_synthetic(pg_cluster):
    with psycopg.connect(pg_cluster.base_dsn) as connection:
        rows = connection.execute(
            "SELECT script, success FROM propertyai.flyway_schema_history ORDER BY installed_rank"
        ).fetchall()
    assert rows == [(migration, True) for migration in EXPECTED_MIGRATIONS]
    assert len(rows) == 8
    assert all("synthetic" not in script.lower() for script, _ in rows)


def test_a02_stage_a_config_rejects_production_and_raw_pool_is_not_public(pg_cluster):
    with pytest.raises(ValueError, match="TEST/DEVELOPMENT"):
        PostgresStageAConfig(
            dsn=pg_cluster.login_dsn(APP_LOGIN),
            expected_session_user=APP_LOGIN,
            expected_role="propertyai_app_runtime",
            data_environment="PRODUCTION",
        )
    pool = PostgresStageAPool(
        PostgresStageAConfig(
            dsn=pg_cluster.login_dsn(APP_LOGIN),
            expected_session_user=APP_LOGIN,
            expected_role="propertyai_app_runtime",
            data_environment="TEST",
            min_size=0,
            max_size=1,
        )
    )
    public_names = {name for name in dir(pool) if not name.startswith("_")}
    assert "pool" not in public_names
    assert "connection" not in public_names
    assert "getconn" not in public_names
    assert not hasattr(pool, "pool")
    with pytest.raises(RuntimeError, match="not open"):
        with pool._connection():
            pass
    pool.open()
    with pool._connection() as connection:
        row = connection.execute(
            "SELECT session_user AS session_user, current_user AS current_user"
        ).fetchone()
        assert row == {"session_user": APP_LOGIN, "current_user": "propertyai_app_runtime"}
    pool.close()


def test_a03_runtime_login_role_graph_is_non_superuser_and_cross_role_denied(pg_cluster, pools):
    app_pool, worker_pool = pools
    with app_pool._connection() as connection:
        row = connection.execute(
            "SELECT session_user AS session_user, current_user AS current_user"
        ).fetchone()
        assert row == {"session_user": APP_LOGIN, "current_user": "propertyai_app_runtime"}
    with worker_pool._connection() as connection:
        row = connection.execute(
            "SELECT session_user AS session_user, current_user AS current_user"
        ).fetchone()
        assert row == {"session_user": WORKER_LOGIN, "current_user": "propertyai_async_worker"}

    for login, forbidden_role in (
        (APP_LOGIN, "propertyai_async_worker"),
        (WORKER_LOGIN, "propertyai_app_runtime"),
    ):
        with psycopg.connect(pg_cluster.login_dsn(login), autocommit=True) as connection:
            role = connection.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit "
                "FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            assert role == (False, False, False, False, False, False)
            with pytest.raises(errors.InsufficientPrivilege):
                connection.execute(f"SET ROLE {forbidden_role}")

    with psycopg.connect(pg_cluster.base_dsn) as connection:
        memberships = connection.execute(
            """
            SELECT member.rolname AS member_name, granted.rolname AS granted_role
              FROM pg_auth_members membership
              JOIN pg_roles member ON member.oid=membership.member
              JOIN pg_roles granted ON granted.oid=membership.roleid
             WHERE member.rolname IN (%s, %s)
             ORDER BY member.rolname, granted.rolname
            """,
            (APP_LOGIN, WORKER_LOGIN),
        ).fetchall()
    assert memberships == [
        (APP_LOGIN, "propertyai_app_runtime"),
        (WORKER_LOGIN, "propertyai_async_worker"),
    ]


def test_p01_clean_app_checkout_has_exact_identity(pools):
    app_pool, _ = pools
    with app_pool._connection() as connection:
        row = connection.execute(
            """
            SELECT session_user AS session_user,
                   current_user AS current_user,
                   current_setting('TimeZone') AS timezone,
                   current_setting('search_path') AS search_path
            """
        ).fetchone()
        assert row == {
            "session_user": APP_LOGIN,
            "current_user": "propertyai_app_runtime",
            "timezone": "UTC",
            "search_path": "pg_catalog, propertyai",
        }


def test_p02_role_poison_is_not_inherited_by_next_checkout(pools):
    app_pool, _ = pools
    with app_pool._connection() as connection:
        connection.execute("RESET ROLE")
        assert connection.execute("SELECT current_user AS u").fetchone()["u"] == APP_LOGIN
    with app_pool._connection() as connection:
        assert connection.execute("SELECT current_user AS u").fetchone()["u"] == "propertyai_app_runtime"


def test_p03_app_borrower_cannot_emerge_as_async_worker(pg_cluster, pools):
    app_pool, _ = pools
    with app_pool._connection() as connection:
        assert connection.execute("SELECT current_user AS u").fetchone()["u"] == "propertyai_app_runtime"
        with pytest.raises(errors.InsufficientPrivilege):
            connection.execute("SET ROLE propertyai_async_worker")
    with psycopg.connect(pg_cluster.login_dsn(APP_LOGIN), autocommit=True) as connection:
        with pytest.raises(errors.InsufficientPrivilege):
            connection.execute("SET ROLE propertyai_async_worker")


def test_p04_async_worker_checkout_has_exact_identity(pools):
    _, worker_pool = pools
    with worker_pool._connection() as connection:
        row = connection.execute(
            "SELECT session_user AS session_user, current_user AS current_user"
        ).fetchone()
        assert row == {"session_user": WORKER_LOGIN, "current_user": "propertyai_async_worker"}


def test_p05_public_stage_a_pool_surface_has_no_raw_psycopg_escape(pools):
    app_pool, _ = pools
    public = {name for name in dir(app_pool) if not name.startswith("_")}
    assert public == {"close", "config", "open"}
    assert not hasattr(app_pool, "pool")
    assert not hasattr(app_pool, "connection")
    assert not hasattr(app_pool, "getconn")
    assert "ConnectionPool" not in postgres_pool_module.__all__
    assert not hasattr(postgres_pool_module, "ConnectionPool")


def test_p06_session_poison_beyond_role_is_removed_before_reuse(pools):
    app_pool, _ = pools
    with app_pool._connection() as connection:
        connection.execute("SET TIME ZONE 'Asia/Seoul'")
        connection.execute("SET search_path TO public")
        connection.execute("SET application_name TO 'poisoned-stage-a-session'")
        row = connection.execute(
            "SELECT current_setting('TimeZone') AS timezone, "
            "current_setting('search_path') AS search_path, "
            "current_setting('application_name') AS application_name"
        ).fetchone()
        assert row["timezone"] == "Asia/Seoul"
        assert row["search_path"] == "public"
        assert row["application_name"] == "poisoned-stage-a-session"
    with app_pool._connection() as connection:
        row = connection.execute(
            "SELECT current_setting('TimeZone') AS timezone, "
            "current_setting('search_path') AS search_path, "
            "current_setting('application_name') AS application_name"
        ).fetchone()
        assert row["timezone"] == "UTC"
        assert row["search_path"] == "pg_catalog, propertyai"
        assert row["application_name"] == ""


def test_p07_transaction_exception_returns_safe_connection(repository, pools):
    app_pool, _ = pools
    with pytest.raises(RuntimeError, match="poison then rollback"):
        with repository.transaction() as tx:
            tx._connection.execute("RESET ROLE")
            tx._connection.execute("SET TIME ZONE 'Asia/Seoul'")
            raise RuntimeError("poison then rollback")
    with app_pool._connection() as connection:
        row = connection.execute(
            "SELECT session_user AS session_user, current_user AS current_user, "
            "current_setting('TimeZone') AS timezone"
        ).fetchone()
        assert row == {
            "session_user": APP_LOGIN,
            "current_user": "propertyai_app_runtime",
            "timezone": "UTC",
        }


def test_a04_explicit_transaction_rolls_back(repository):
    property_id, _ = seed_property(repository)
    marker = uuid4()
    with pytest.raises(RuntimeError, match="force rollback"):
        with repository.transaction() as tx:
            tx._connection.execute(
                "INSERT INTO propertyai.party(party_id,party_code,display_name,data_environment) VALUES (%s,%s,'Rollback','TEST')",
                (marker, f"PTY-{marker.hex}"),
            )
            raise RuntimeError("force rollback")
    with repository.transaction() as tx:
        assert tx._connection.execute(
            "SELECT count(*) AS n FROM propertyai.party WHERE party_id=%s", (marker,)
        ).fetchone()["n"] == 0


# A-08 / T-RSV-01
def test_t_rsv_01_new_row_version_1(repository):
    snapshot = snapshot_for(repository)
    result = repository.ingest_canonical_reservation(snapshot)
    assert (result.created, result.changed, result.source_version) == (True, True, 1)


# A-08 / T-RSV-02
def test_t_rsv_02_identical_retry_noop_version_unchanged(repository):
    snapshot = snapshot_for(repository)
    first = repository.ingest_canonical_reservation(snapshot)
    second = repository.ingest_canonical_reservation(snapshot)
    assert first.source_version == second.source_version == 1
    assert second.changed is False and second.created is False


# A-08 / T-RSV-03
def test_t_rsv_03_canonical_payload_change_1_to_2(repository):
    snapshot = snapshot_for(repository)
    assert repository.ingest_canonical_reservation(snapshot).source_version == 1
    changed = replace(snapshot, check_out_at=snapshot.check_out_at + timedelta(hours=1))
    result = repository.ingest_canonical_reservation(changed)
    assert result.source_version == 2 and result.changed is True


# A-08 / T-RSV-04
def test_t_rsv_04_second_canonical_payload_change_2_to_3(repository):
    snapshot = snapshot_for(repository)
    repository.ingest_canonical_reservation(snapshot)
    v2 = replace(snapshot, check_out_at=snapshot.check_out_at + timedelta(hours=1))
    v3 = replace(v2, reservation_status="CANCELLED")
    assert repository.ingest_canonical_reservation(v2).source_version == 2
    assert repository.ingest_canonical_reservation(v3).source_version == 3


# A-08 / T-RSV-05
def test_t_rsv_05_version_only_mutation_rejected(repository):
    snapshot = snapshot_for(repository)
    repository.ingest_canonical_reservation(snapshot)
    with pytest.raises(ReservationRevisionError):
        with repository.transaction() as tx:
            try:
                tx._connection.execute(
                    "UPDATE propertyai.reservation SET source_version=2 WHERE reservation_id=%s",
                    (snapshot.reservation_id,),
                )
            except psycopg.Error as error:
                raise map_postgres_error(error) from error


# A-08 / T-RSV-06
def test_t_rsv_06_direct_skip_1_to_3_rejected(repository):
    snapshot = snapshot_for(repository)
    repository.ingest_canonical_reservation(snapshot)
    with pytest.raises(ReservationRevisionError):
        with repository.transaction() as tx:
            try:
                tx._connection.execute(
                    """
                    UPDATE propertyai.reservation
                       SET check_out_at=%s, source_version=3
                     WHERE reservation_id=%s
                    """,
                    (snapshot.check_out_at + timedelta(hours=1), snapshot.reservation_id),
                )
            except psycopg.Error as error:
                raise map_postgres_error(error) from error


# A-08 / T-RSV-07
def test_t_rsv_07_concurrent_identical_ingest_safe(repository):
    snapshot = snapshot_for(repository)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: repository.ingest_canonical_reservation(snapshot), range(2)))
    assert sorted(result.source_version for result in results) == [1, 1]
    row = read_reservation(repository, snapshot.reservation_id)
    assert row["source_version"] == 1 and row["check_out_at"] == snapshot.check_out_at


# A-08 / T-RSV-08
def test_t_rsv_08_concurrent_changed_ingest_serialized_safely(repository):
    snapshot = snapshot_for(repository)
    repository.ingest_canonical_reservation(snapshot)
    change_a = replace(snapshot, check_out_at=snapshot.check_out_at + timedelta(hours=1))
    change_b = replace(snapshot, check_out_at=snapshot.check_out_at + timedelta(hours=2))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(repository.ingest_canonical_reservation, (change_a, change_b)))
    assert sorted(result.source_version for result in results) == [2, 3]
    row = read_reservation(repository, snapshot.reservation_id)
    assert row["source_version"] == 3
    assert row["check_out_at"] in {change_a.check_out_at, change_b.check_out_at}


# A-08 / T-RSV-09
def test_t_rsv_09_rolled_back_ingest_changes_neither_payload_nor_version(repository):
    snapshot = snapshot_for(repository)
    repository.ingest_canonical_reservation(snapshot)
    changed = replace(snapshot, check_out_at=snapshot.check_out_at + timedelta(hours=4))
    with pytest.raises(RuntimeError, match="rollback after ingest"):
        with repository.transaction() as tx:
            result = tx.ingest_canonical_reservation(changed)
            assert result.source_version == 2
            raise RuntimeError("rollback after ingest")
    row = read_reservation(repository, snapshot.reservation_id)
    assert row["source_version"] == 1 and row["check_out_at"] == snapshot.check_out_at


# A-08 / T-RSV-10
def test_t_rsv_10_api_does_not_expose_caller_selected_source_version(repository):
    assert "source_version" not in inspect.signature(
        PostgresCleanerRepository.ingest_canonical_reservation
    ).parameters
    assert "source_version" not in {field.name for field in fields(CanonicalReservationSnapshot)}
    with repository.transaction() as tx:
        assert not hasattr(tx, "connection")


def test_a09_authority_epoch_verification(repository):
    with repository.transaction() as tx:
        assert tx.verify_authority_epoch("CLEANER_SCHEDULING", 1) == 1
    with pytest.raises(AuthorityEpochError):
        with repository.transaction() as tx:
            tx.verify_authority_epoch("CLEANER_SCHEDULING", 2)


def make_receipt(*, key: str, payload: dict | None = None):
    return CommandReceiptInput(
        command_id=uuid4(),
        authority_scope_code="CLEANER_SCHEDULING",
        command_type="StageATestCommand",
        idempotency_key=key,
        request_payload=payload or {"action": "test"},
        source_channel_code="SYSTEM_TEST",
        authority_epoch=1,
        decided_at=datetime.now(UTC),
        principal_type="SYSTEM",
    )


def test_a10_command_receipt_idempotency_and_conflict(repository):
    key = f"cmd:{uuid4()}"
    receipt = make_receipt(key=key)
    with repository.transaction() as tx:
        first = tx.register_command_receipt(receipt)
    retry = replace(receipt, command_id=uuid4(), decided_at=datetime.now(UTC))
    with repository.transaction() as tx:
        second = tx.register_command_receipt(retry)
    assert first.reused is False and second.reused is True
    assert first.command_id == second.command_id
    with pytest.raises(PostgresConflictError):
        with repository.transaction() as tx:
            tx.register_command_receipt(replace(retry, request_payload={"action": "different"}))


def test_a10b_concurrent_identical_command_receipt_is_safe(repository):
    key = f"cmd:{uuid4()}"
    base = make_receipt(key=key)

    def submit(index: int):
        retry = replace(base, command_id=uuid4(), decided_at=datetime.now(UTC)) if index else base
        with repository.transaction() as tx:
            return tx.register_command_receipt(retry)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, range(2)))
    assert {result.command_id for result in results} == {results[0].command_id}
    assert sorted(result.reused for result in results) == [False, True]


def test_a11_command_receipt_and_outbox_commit_atomically(repository):
    receipt = make_receipt(key=f"cmd:{uuid4()}")
    outbox = OutboxMessage(
        outbox_id=uuid4(),
        event_type="STAGE_A_TEST",
        aggregate_type="COMMAND",
        aggregate_id=receipt.command_id,
        destination_type="TEST_SINK",
        available_at=datetime.now(UTC),
        idempotency_key=f"outbox:{uuid4()}",
        payload={"external_effect": False},
        max_attempts=3,
    )
    with repository.transaction() as tx:
        command_result = tx.register_command_receipt(receipt)
        outbox_result = tx.enqueue_outbox(outbox)
    assert command_result.reused is False and outbox_result.reused is False
    with repository.transaction() as tx:
        counts = tx._connection.execute(
            """
            SELECT
              (SELECT count(*) FROM propertyai.command_receipt WHERE command_id=%s) AS receipt_count,
              (SELECT count(*) FROM propertyai.integration_outbox WHERE outbox_id=%s) AS outbox_count
            """,
            (receipt.command_id, outbox.outbox_id),
        ).fetchone()
    assert counts == {"receipt_count": 1, "outbox_count": 1}


def test_a12_command_and_outbox_rollback_atomically(repository):
    receipt = make_receipt(key=f"cmd:{uuid4()}")
    outbox = OutboxMessage(
        outbox_id=uuid4(), event_type="ROLLBACK_TEST", aggregate_type="COMMAND",
        aggregate_id=receipt.command_id, destination_type="TEST_SINK",
        available_at=datetime.now(UTC), idempotency_key=f"outbox:{uuid4()}",
        payload={}, max_attempts=2,
    )
    with pytest.raises(RuntimeError, match="abort unit of work"):
        with repository.transaction() as tx:
            tx.register_command_receipt(receipt)
            tx.enqueue_outbox(outbox)
            raise RuntimeError("abort unit of work")
    with repository.transaction() as tx:
        counts = tx._connection.execute(
            """
            SELECT
              (SELECT count(*) FROM propertyai.command_receipt WHERE command_id=%s) AS receipt_count,
              (SELECT count(*) FROM propertyai.integration_outbox WHERE outbox_id=%s) AS outbox_count
            """,
            (receipt.command_id, outbox.outbox_id),
        ).fetchone()
    assert counts == {"receipt_count": 0, "outbox_count": 0}


def test_a12b_outbox_idempotent_retry_uses_immutable_event_semantics(repository):
    message = OutboxMessage(
        outbox_id=uuid4(), event_type="IDEMPOTENCY_TEST", aggregate_type="COMMAND",
        aggregate_id=uuid4(), destination_type="TEST_SINK", available_at=datetime.now(UTC),
        idempotency_key=f"outbox:{uuid4()}", payload={"kind": "same"}, max_attempts=3,
    )
    with repository.transaction() as tx:
        first = tx.enqueue_outbox(message)
    retry = replace(message, outbox_id=uuid4())
    with repository.transaction() as tx:
        second = tx.enqueue_outbox(retry)
    assert first.reused is False and second.reused is True
    assert first.outbox_id == second.outbox_id
    with pytest.raises(PostgresConflictError):
        with repository.transaction() as tx:
            tx.enqueue_outbox(replace(retry, payload={"kind": "different"}))


def test_a12c_outbox_retry_after_worker_failure_ignores_mutated_delivery_schedule(repository, pools):
    worker = PostgresOutboxWorkerRepository(pools[1])
    message = OutboxMessage(
        outbox_id=uuid4(), event_type="RETRY_STATE_TEST", aggregate_type="COMMAND",
        aggregate_id=uuid4(), destination_type="TEST_SINK",
        available_at=datetime.now(UTC) - timedelta(seconds=1),
        idempotency_key=f"outbox:{uuid4()}", payload={"kind": "retry"}, max_attempts=3,
    )
    with repository.transaction() as tx:
        first = tx.enqueue_outbox(message)
    claim = next(
        item for item in worker.claim_outbox("stage-a-retry-worker", limit=100, lease_seconds=30)
        if item.outbox_id == message.outbox_id
    )
    assert worker.fail_outbox(
        claim.outbox_id,
        worker_id=claim.lease_owner,
        lease_fence=claim.lease_fence,
        error_code="TEST_RETRY",
        retry_delay_seconds=60,
    ) is True
    with repository.transaction() as tx:
        retry = tx.enqueue_outbox(replace(message, outbox_id=uuid4()))
    assert retry.reused is True and retry.outbox_id == first.outbox_id


def test_a12d_concurrent_identical_outbox_enqueue_is_safe(repository):
    message = OutboxMessage(
        outbox_id=uuid4(), event_type="OUTBOX_CONCURRENT_TEST", aggregate_type="COMMAND",
        aggregate_id=uuid4(), destination_type="TEST_SINK", available_at=datetime.now(UTC),
        idempotency_key=f"outbox:{uuid4()}", payload={"kind": "concurrent"}, max_attempts=3,
    )

    def enqueue(index: int):
        candidate = replace(message, outbox_id=uuid4()) if index else message
        with repository.transaction() as tx:
            return tx.enqueue_outbox(candidate)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(enqueue, range(2)))
    assert {result.outbox_id for result in results} == {results[0].outbox_id}
    assert sorted(result.reused for result in results) == [False, True]


def test_a13_outbox_claim_and_fence_plumbing(repository, pools):
    worker = PostgresOutboxWorkerRepository(pools[1])
    message = OutboxMessage(
        outbox_id=uuid4(), event_type="CLAIM_TEST", aggregate_type="RESERVATION",
        aggregate_id=uuid4(), destination_type="TEST_SINK", available_at=datetime.now(UTC),
        idempotency_key=f"outbox:{uuid4()}", payload={"no_external_call": True}, max_attempts=3,
    )
    with repository.transaction() as tx:
        tx.enqueue_outbox(message)
    claims = worker.claim_outbox("stage-a-worker", limit=10, lease_seconds=30)
    claim = next(item for item in claims if item.outbox_id == message.outbox_id)
    assert claim.attempt_count == 1 and claim.lease_fence == 1
    assert worker.complete_outbox(
        claim.outbox_id,
        worker_id=claim.lease_owner,
        lease_fence=claim.lease_fence + 1,
        external_effect_id="should-not-apply",
    ) is False
    assert worker.complete_outbox(
        claim.outbox_id,
        worker_id=claim.lease_owner,
        lease_fence=claim.lease_fence,
        external_effect_id="test-only-effect-id",
    ) is True


def test_a14_database_error_mapping_is_positive_and_fail_closed():
    for error in (errors.SerializationFailure("x"), errors.DeadlockDetected("x")):
        mapped = map_postgres_error(error)
        assert isinstance(mapped, PostgresRetryableError)
        assert mapped.retryable is True

    for error in (
        errors.InvalidPassword("x"),
        errors.InvalidAuthorizationSpecification("x"),
        errors.InsufficientPrivilege("x"),
    ):
        mapped = map_postgres_error(error)
        assert isinstance(mapped, PostgresConfigurationError)
        assert mapped.retryable is False

    generic_operational = map_postgres_error(psycopg.OperationalError("unknown operational failure"))
    assert type(generic_operational) is PostgresRepositoryError
    assert generic_operational.retryable is False

    assert isinstance(map_postgres_error(errors.UniqueViolation("x")), PostgresConflictError)
    for error in (
        errors.CheckViolation("x"),
        errors.ForeignKeyViolation("x"),
        errors.NotNullViolation("x"),
        errors.ExclusionViolation("x"),
    ):
        mapped = map_postgres_error(error)
        assert isinstance(mapped, PostgresConstraintError)
        assert mapped.retryable is False

    assert isinstance(
        map_postgres_error(errors.RaiseException("RESERVATION_SOURCE_VERSION_MUST_INCREMENT")),
        ReservationRevisionError,
    )


def test_a15_frozen_reservation_trigger_remains_present(pg_cluster):
    result = pg_cluster.psql(
        sql_text="""
        SELECT pg_get_triggerdef(oid)
          FROM pg_trigger
         WHERE tgname='trg_reservation_guard_update' AND NOT tgisinternal;
        """
    )
    assert "tg_guard_reservation_update" in result.stdout


def test_a16_no_arbitrary_reservation_crud_surface():
    public = {
        name for name, _ in inspect.getmembers(PostgresCleanerRepository, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert public == {"ingest_canonical_reservation", "transaction"}


def test_a17_stage_a_adapter_has_no_external_effect_dependency():
    source = inspect.getsource(inspect.getmodule(PostgresCleanerRepository))
    forbidden = ("telegram", "gmail", "calendar", "notion", "global_writer", "requests.")
    assert not any(token in source.lower() for token in forbidden)


def test_a18_v221_remains_outside_production_migration_path():
    root = Path(__file__).resolve().parents[2]
    v221 = root / "db" / "v2_2_1" / "migration"
    production = root / "db" / "migration"
    assert v221.resolve() != production.resolve()
    assert [path.name for path in sorted(v221.glob("*.sql"))] == EXPECTED_MIGRATIONS

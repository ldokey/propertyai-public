from contextlib import contextmanager
from types import SimpleNamespace
import json
import os
import shutil
import time
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from adcp_global_writer_client.errors import GlobalWriterClientError
from propertyai_core.config import cleaner_worker as config
from propertyai_core.adapters.postgres import worker_pool as pools
from propertyai_core.adapters.postgres.repository import PostgresOutboxWorkerRepository
from propertyai_core.runtime import cleaner_pg_outbox_service as service
from propertyai_core.runtime import postgres_outbox_worker as worker
from propertyai_core.global_writer import ProductionWriterError
from propertyai_core.tests.postgres_stage_a_cluster import start_disposable_postgres


ENV = {config.WORKER_CREDENTIAL_ENV: config.WORKER_CREDENTIAL_REF}


@pytest.fixture
def passfile(tmp_path, monkeypatch):
    directory = tmp_path.resolve() / "protected"
    directory.mkdir(mode=0o700)
    path = directory / "worker.pgpass"
    path.write_text("127.0.0.1:5432:propertyai_cleaner_prod:propertyai_cleaner_worker:synthetic\n")
    path.chmod(0o600)
    monkeypatch.setattr(config, "_WORKER_PASSFILE", path)
    monkeypatch.setattr(config, "_WORKER_UID", os.geteuid())
    return path


def test_sealed_reference_never_reads_secret(passfile, monkeypatch):
    monkeypatch.setattr(type(passfile), "read_text", lambda *a, **k: pytest.fail("secret read"))
    value = config.worker_connection_info(ENV)
    assert "password=" not in value and "synthetic" not in value
    assert "user=propertyai_cleaner_worker" in value
    assert "dbname=propertyai_cleaner_prod" in value
    assert "require_auth=scram-sha-256" in value


@pytest.mark.parametrize("extra", [
    {config.WORKER_CREDENTIAL_ENV: "cleaner-prod/app.pgpass"},
    {"PGPASSWORD": "synthetic"}, {"PGSERVICE": "other"},
    {"PROPERTYAI_CLEANER_POSTGRES_APP_DSN_PATH": "/wrong"},
    {"PROPERTYAI_CLEANER_POSTGRES_WORKER_SESSION_USER": "other"},
    {"PROPERTYAI_CLEANER_POSTGRES_DATABASE": "other"},
])
def test_override_fails_closed(passfile, extra):
    with pytest.raises(config.CleanerWorkerConfigurationError):
        config.worker_connection_info({**ENV, **extra})


@pytest.mark.parametrize("damage", ["missing", "mode", "directory", "symlink", "uid", "empty"])
def test_protected_metadata_fails_closed(passfile, monkeypatch, damage):
    if damage == "missing": passfile.unlink()
    elif damage == "mode": passfile.chmod(0o644)
    elif damage == "directory": passfile.parent.chmod(0o755)
    elif damage == "symlink":
        other = passfile.with_name("other")
        passfile.rename(other)
        passfile.symlink_to(other)
    elif damage == "uid": monkeypatch.setattr(config, "_WORKER_UID", os.geteuid() + 1)
    elif damage == "empty": passfile.write_text("")
    with pytest.raises(config.CleanerWorkerConfigurationError):
        config.worker_connection_info(ENV)


@pytest.fixture(scope="module")
def cluster():
    value = start_disposable_postgres()
    try:
        value.psql(sql_text="""
            CREATE ROLE propertyai_cleaner_worker LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            CREATE ROLE propertyai_cleaner_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            GRANT propertyai_async_worker TO propertyai_cleaner_worker WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;
            GRANT propertyai_app_runtime TO propertyai_cleaner_app WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;
        """)
        yield value
    finally:
        value.cleanup()


@pytest.fixture
def pool(cluster, monkeypatch):
    # Only disposable transport/database change; exact Production role identities,
    # frozen migrations and Production normalization/privilege code are exercised.
    monkeypatch.setattr(pools, "worker_connection_info", lambda env: cluster.login_dsn(config.WORKER_LOGIN))
    monkeypatch.setattr(pools, "WORKER_DATABASE", "postgres")
    value = pools.PostgresWorkerPool(environment=ENV)
    value.open()
    try:
        yield value
    finally:
        value.close()


def test_full_frozen_privilege_surface_and_empty_work(pool, monkeypatch):
    entered = []
    @contextmanager
    def scope(*args, **kwargs):
        entered.append("acquire")
        try: yield
        finally: entered.append("release")
    monkeypatch.setattr(worker, "mutation_scope", scope)
    monkeypatch.setattr(worker, "assert_current_production_writer", lambda: None)
    repository = PostgresOutboxWorkerRepository(pool)
    adapter = SimpleNamespace(deliver=lambda claim: pytest.fail("external effect"))
    instance = worker.CleanerPostgresOutboxWorker(repository, adapter, worker_id=service.WORKER_ID)
    results = []
    service.run_worker_loop(instance, sleep=lambda delay: None, max_cycles=3, observe=results.append)
    assert [r.status for r in results] == ["IDLE"] * 3
    assert entered == ["acquire", "release"] * 3
    absent = uuid4()
    assert repository.complete_outbox(absent, worker_id=service.WORKER_ID, lease_fence=1) is False
    assert repository.fail_outbox(absent, worker_id=service.WORKER_ID, lease_fence=1, error_code="test", retry_delay_seconds=1) is False
    assert repository.mark_outbox_pending_reconciliation(absent, worker_id=service.WORKER_ID, lease_fence=1, error_code="test") is False
    with pool._connection() as connection:
        assert connection.execute("SELECT session_user,current_user").fetchone() == {
            "session_user": config.WORKER_LOGIN, "current_user": config.WORKER_ROLE}
        for table in ("command_receipt", "reservation", "domain_event", "integration_outbox"):
            assert connection.execute("SELECT count(*) AS n FROM propertyai." + table).fetchone()["n"] == 0


@pytest.mark.parametrize("login,role", [("propertyai_cleaner_worker", "propertyai_app_runtime"), ("propertyai_cleaner_app", "propertyai_async_worker")])
def test_cross_role_is_denied(cluster, login, role):
    with psycopg.connect(cluster.login_dsn(login), autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SET ROLE " + role)


def test_binding_write_surface_uses_only_worker_capability(pool, cluster):
    repository = PostgresOutboxWorkerRepository(pool)
    aggregate = uuid4()
    values = dict(aggregate_type="RESERVATION", aggregate_id=aggregate,
                  destination_type="NOTION", resource_code="RESERVATION")
    try:
        assert repository.resource_binding(**values) is None
        row = repository.bind_resource(**values, external_resource_id="synthetic1", aggregate_version=1)
        assert row["external_resource_id"] == "synthetic1"
        row = repository.rebind_resource(**values, expected_external_resource_id="synthetic1",
                                         new_external_resource_id="synthetic2", aggregate_version=2)
        assert row["external_resource_id"] == "synthetic2"
    finally:
        cluster.psql(sql_text=f"DELETE FROM propertyai.integration_resource_binding WHERE aggregate_id='{aggregate}';")


def test_pool_open_failure_hides_driver_detail(monkeypatch):
    class FailedPool:
        def __init__(self, **kwargs): self.closed = False
        def open(self, **kwargs): raise RuntimeError("synthetic-auth-secret")
        def close(self): self.closed = True
    monkeypatch.setattr(pools, "ConnectionPool", FailedPool)
    monkeypatch.setattr(pools, "worker_connection_info", lambda env: "nonsecret")
    value = pools.PostgresWorkerPool(environment=ENV)
    with pytest.raises(pools.CleanerWorkerSessionError) as result:
        value.open()
    assert str(result.value) == "W07_CONNECTION_OPEN_FAILED"
    assert result.value.__suppress_context__ and value._pool.closed


def test_real_scram_passfile_wrong_password_and_missing_file_fail_closed(cluster, tmp_path):
    hba = cluster.data_dir / "pg_hba.conf"
    old = hba.read_text()
    with psycopg.connect(cluster.base_dsn, autocommit=True) as admin:
        admin.execute("ALTER ROLE propertyai_cleaner_worker PASSWORD 'synthetic-disposable-only'")
        hba.write_text("local all propertyai_cleaner_worker scram-sha-256\n" + old)
        admin.execute("SELECT pg_reload_conf()")
        time.sleep(0.15)
        password_file = tmp_path / "synthetic.pgpass"
        password_file.write_text(f"*:{cluster.port}:postgres:propertyai_cleaner_worker:synthetic-disposable-only\n")
        password_file.chmod(0o600)
        dsn = cluster.login_dsn(config.WORKER_LOGIN)
        try:
            with psycopg.connect(dsn, passfile=str(password_file), require_auth="scram-sha-256") as connection:
                assert connection.execute("SELECT session_user").fetchone()[0] == config.WORKER_LOGIN
            password_file.write_text(f"*:{cluster.port}:postgres:propertyai_cleaner_worker:wrong-synthetic\n")
            with pytest.raises(psycopg.OperationalError):
                psycopg.connect(dsn, passfile=str(password_file), require_auth="scram-sha-256")
            password_file.unlink()
            with pytest.raises(psycopg.OperationalError):
                psycopg.connect(dsn, passfile=str(password_file), require_auth="scram-sha-256")
        finally:
            hba.write_text(old)
            admin.execute("SELECT pg_reload_conf()")
            time.sleep(0.15)


def test_startup_attestation_follows_database_health(monkeypatch):
    events = []
    def build():
        events.append("database-verified")
        return SimpleNamespace(close=lambda: events.append("closed")), object()
    monkeypatch.setattr(service, "build_worker", build)
    monkeypatch.setattr(service, "publish_startup_runtime_identity", lambda code: events.append("identity") or object())
    monkeypatch.setattr(service, "_health_observer", lambda *a: lambda result: events.append(result.status))
    monkeypatch.setattr(service, "run_worker_loop", lambda *a, **k: events.append("loop"))
    service.main()
    assert events == ["database-verified", "identity", "READY", "loop", "closed"]


def test_wrong_login_and_current_role_rejected(pool, cluster):
    with psycopg.connect(cluster.login_dsn("propertyai_cleaner_app"), row_factory=dict_row) as connection:
        with pytest.raises(pools.CleanerWorkerSessionError, match="LOGIN_IDENTITY"):
            pool._normalize_and_verify(connection)
    class Fake:
        info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.IDLE)
        autocommit = False
        def execute(self, sql):
            if sql.startswith("SELECT session_user, current_database"):
                row = {"session_user": config.WORKER_LOGIN, "database_name": "postgres"}
            else:
                row = {"session_user": config.WORKER_LOGIN, "current_user": "propertyai_app_runtime",
                       "database_name": "postgres", "timezone": "UTC", "search_path": "pg_catalog, propertyai"}
            return SimpleNamespace(fetchone=lambda: row)
    with pytest.raises(pools.CleanerWorkerSessionError, match="SESSION_IDENTITY"):
        pool._normalize_and_verify(Fake())


@pytest.mark.parametrize("signature", pools._ROUTINES)
def test_each_missing_function_acl_fails_before_work(pool, cluster, signature):
    cluster.psql(sql_text=f"REVOKE EXECUTE ON FUNCTION {signature} FROM propertyai_async_worker;")
    try:
        with psycopg.connect(cluster.login_dsn(config.WORKER_LOGIN), row_factory=dict_row) as connection:
            with pytest.raises(pools.CleanerWorkerSessionError, match="FUNCTION_PRIVILEGE"):
                pool._normalize_and_verify(connection)
    finally:
        cluster.psql(sql_text=f"GRANT EXECUTE ON FUNCTION {signature} TO propertyai_async_worker;")


def test_driver_error_is_sanitized(pool):
    class Fake:
        info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.IDLE)
        autocommit = False
        def execute(self, sql): raise RuntimeError("synthetic-password-must-not-escape")
    with pytest.raises(pools.CleanerWorkerSessionError) as result:
        pool._normalize_and_verify(Fake())
    assert str(result.value) == "W07_CONNECTION_VALIDATION_FAILED"
    assert result.value.__suppress_context__


@pytest.mark.parametrize("alter,restore", [
    ("ALTER ROLE propertyai_async_worker LOGIN", "ALTER ROLE propertyai_async_worker NOLOGIN"),
    ("GRANT propertyai_readonly TO propertyai_async_worker", "REVOKE propertyai_readonly FROM propertyai_async_worker"),
    ("GRANT propertyai_cleaner_worker TO propertyai_stage_a_worker_login", "REVOKE propertyai_cleaner_worker FROM propertyai_stage_a_worker_login"),
])
def test_capability_and_login_membership_drift_fail_closed(pool, cluster, alter, restore):
    cluster.psql(sql_text=alter + ";")
    try:
        with psycopg.connect(cluster.login_dsn(config.WORKER_LOGIN), row_factory=dict_row) as connection:
            with pytest.raises(pools.CleanerWorkerSessionError, match="CAPABILITY_BOUNDARY"):
                pool._normalize_and_verify(connection)
    finally:
        cluster.psql(sql_text=restore + ";")


def test_public_function_grant_fails_closed(pool, cluster):
    signature = pools._ROUTINES[0]
    cluster.psql(sql_text=f"GRANT EXECUTE ON FUNCTION {signature} TO PUBLIC;")
    try:
        with psycopg.connect(cluster.login_dsn(config.WORKER_LOGIN), row_factory=dict_row) as connection:
            with pytest.raises(pools.CleanerWorkerSessionError, match="FUNCTION_PRIVILEGE"):
                pool._normalize_and_verify(connection)
    finally:
        cluster.psql(sql_text=f"REVOKE EXECUTE ON FUNCTION {signature} FROM PUBLIC;")


def test_missing_binding_column_privilege_fails_closed(pool, cluster):
    cluster.psql(sql_text="REVOKE UPDATE(external_uid) ON propertyai.integration_resource_binding FROM propertyai_async_worker;")
    try:
        with psycopg.connect(cluster.login_dsn(config.WORKER_LOGIN), row_factory=dict_row) as connection:
            with pytest.raises(pools.CleanerWorkerSessionError, match="BINDING_UPDATE_PRIVILEGE"):
                pool._normalize_and_verify(connection)
    finally:
        cluster.psql(sql_text="GRANT UPDATE(external_uid) ON propertyai.integration_resource_binding TO propertyai_async_worker;")


def test_unexpected_business_privilege_fails_closed(pool, cluster):
    cluster.psql(sql_text="GRANT INSERT ON propertyai.reservation TO propertyai_async_worker;")
    try:
        with psycopg.connect(cluster.login_dsn(config.WORKER_LOGIN), row_factory=dict_row) as connection:
            with pytest.raises(pools.CleanerWorkerSessionError, match="DIRECT_BUSINESS_WRITE"):
                pool._normalize_and_verify(connection)
    finally:
        cluster.psql(sql_text="REVOKE INSERT ON propertyai.reservation FROM propertyai_async_worker;")


@pytest.mark.parametrize("signature", pools._ROUTINES)
def test_login_direct_function_privilege_fails_closed(pool, cluster, signature):
    cluster.psql(sql_text=f"GRANT EXECUTE ON FUNCTION {signature} TO propertyai_cleaner_worker;")
    try:
        with psycopg.connect(cluster.login_dsn(config.WORKER_LOGIN), row_factory=dict_row) as connection:
            with pytest.raises(pools.CleanerWorkerSessionError, match="LOGIN_FUNCTION_PRIVILEGE"):
                pool._normalize_and_verify(connection)
    finally:
        cluster.psql(sql_text=f"REVOKE EXECUTE ON FUNCTION {signature} FROM propertyai_cleaner_worker;")


@pytest.mark.parametrize("privilege", ["UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN"])
def test_binding_excess_table_privilege_fails_closed(pool, cluster, privilege):
    cluster.psql(sql_text=f"GRANT {privilege} ON propertyai.integration_resource_binding TO propertyai_async_worker;")
    try:
        with psycopg.connect(cluster.login_dsn(config.WORKER_LOGIN), row_factory=dict_row) as connection:
            with pytest.raises(pools.CleanerWorkerSessionError, match="EXCESS_BINDING_PRIVILEGE"):
                pool._normalize_and_verify(connection)
    finally:
        cluster.psql(sql_text=f"REVOKE {privilege} ON propertyai.integration_resource_binding FROM propertyai_async_worker;")
        # PostgreSQL REVOKE table UPDATE also removes individual column grants.
        columns = ",".join(pools._BINDING_COLUMNS)
        cluster.psql(sql_text=f"GRANT UPDATE({columns}) ON propertyai.integration_resource_binding TO propertyai_async_worker;")


def test_every_frozen_forbidden_binding_column_fails_closed(pool, cluster):
    with psycopg.connect(cluster.base_dsn) as connection:
        columns = [row[0] for row in connection.execute("""
            SELECT attname FROM pg_attribute
             WHERE attrelid='propertyai.integration_resource_binding'::regclass
               AND attnum>0 AND NOT attisdropped ORDER BY attnum
        """).fetchall()]
    forbidden = set(columns) - set(pools._BINDING_COLUMNS)
    assert forbidden and {"binding_id", "aggregate_id", "created_at"} <= forbidden
    for column in sorted(forbidden):
        cluster.psql(sql_text=f"GRANT UPDATE({column}) ON propertyai.integration_resource_binding TO propertyai_async_worker;")
        try:
            with psycopg.connect(cluster.login_dsn(config.WORKER_LOGIN), row_factory=dict_row) as connection:
                with pytest.raises(pools.CleanerWorkerSessionError, match="EXCESS_BINDING_PRIVILEGE"):
                    pool._normalize_and_verify(connection)
        finally:
            cluster.psql(sql_text=f"REVOKE UPDATE({column}) ON propertyai.integration_resource_binding FROM propertyai_async_worker;")


def test_binding_column_reference_and_login_read_grants_fail_closed(pool, cluster):
    cluster.psql(sql_text="GRANT REFERENCES(binding_id) ON propertyai.integration_resource_binding TO propertyai_async_worker;")
    try:
        with psycopg.connect(cluster.login_dsn(config.WORKER_LOGIN), row_factory=dict_row) as connection:
            with pytest.raises(pools.CleanerWorkerSessionError, match="EXCESS_BINDING_PRIVILEGE"):
                pool._normalize_and_verify(connection)
    finally:
        cluster.psql(sql_text="REVOKE REFERENCES(binding_id) ON propertyai.integration_resource_binding FROM propertyai_async_worker;")
    cluster.psql(sql_text="GRANT SELECT ON propertyai.integration_resource_binding TO propertyai_cleaner_worker;")
    try:
        with psycopg.connect(cluster.login_dsn(config.WORKER_LOGIN), row_factory=dict_row) as connection:
            with pytest.raises(pools.CleanerWorkerSessionError, match="DIRECT_BUSINESS_WRITE"):
                pool._normalize_and_verify(connection)
    finally:
        cluster.psql(sql_text="REVOKE SELECT ON propertyai.integration_resource_binding FROM propertyai_cleaner_worker;")


@pytest.mark.parametrize("privilege,object_sql", [
    ("EXECUTE", "FUNCTION propertyai.claim_integration_outbox(text,integer,integer)"),
    ("INSERT", "TABLE propertyai.integration_resource_binding"),
    ("UPDATE(external_uid)", "TABLE propertyai.integration_resource_binding"),
    ("SELECT", "TABLE propertyai.reservation"),
])
def test_object_grant_options_fail_closed(pool, cluster, privilege, object_sql):
    cluster.psql(sql_text=f"GRANT {privilege} ON {object_sql} TO propertyai_async_worker WITH GRANT OPTION;")
    try:
        with psycopg.connect(cluster.login_dsn(config.WORKER_LOGIN), row_factory=dict_row) as connection:
            with pytest.raises(pools.CleanerWorkerSessionError, match="OBJECT_GRANT_OPTION"):
                pool._normalize_and_verify(connection)
    finally:
        cluster.psql(sql_text=f"REVOKE GRANT OPTION FOR {privilege} ON {object_sql} FROM propertyai_async_worker;")


def test_exact_held_control_waits_without_restart_or_work():
    cause = GlobalWriterClientError("GLOBAL_PRODUCTION_WRITER_HELD")
    def run():
        raise ProductionWriterError("GLOBAL_WRITER_ACQUIRE_OR_REVALIDATE_FAILED") from cause
    results = []
    service.run_worker_loop(SimpleNamespace(run_once=run), max_cycles=3, sleep=lambda delay: None, observe=results.append)
    assert [r.status for r in results] == ["WAITING_CONTROL"] * 3


def test_other_lease_failure_not_hidden():
    def run(): raise ProductionWriterError("GLOBAL_WRITER_RELEASE_FAILED")
    with pytest.raises(ProductionWriterError):
        service.run_worker_loop(SimpleNamespace(run_once=run), max_cycles=1)


def test_health_file_binds_fresh_process_and_successful_idle(tmp_path):
    identity = SimpleNamespace(pid=123, process_incarnation_id="inc", product_build_commit="commit", product_build_identity="build")
    observe = service._health_observer(identity, {"PROPERTYAI_GLOBAL_WRITER_RUNTIME_IDENTITY_PATH": str(tmp_path / "W07.runtime.json")})
    observe(worker.OutboxRunResult(status="WAITING_CONTROL"))
    observe(worker.OutboxRunResult(status="IDLE"))
    result = json.loads((tmp_path / "W07.worker-health.json").read_text())
    assert result["pid"] == 123 and result["idle_cycles"] == 1
    assert result["writer_lease"] == "RELEASED"
    assert result["current_user"] == "propertyai_async_worker"

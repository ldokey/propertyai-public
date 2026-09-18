from __future__ import annotations

import copy
import http.client as http_client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from propertyai_core.adapters.postgres.rent_session_store import PostgresSessionStore
from propertyai_core.rent_runtime.artifact import persistent_migrator_plan
from propertyai_core.rent_runtime.common import LocalTarget, OperationalError, canonical, checked_file, clean_env, digest, write_new
from propertyai_core.rent_runtime.persistent_backup import load_persistent_backup_profile
from propertyai_core.rent_runtime.local_auth import fixed_local_subject
from propertyai_core.rent_runtime.staging import (EXTERNAL_ACTIVATION, PERSISTENT_STAGING,
                                                  persistent_target)
from propertyai_core.rent_runtime.web import RuntimeConfig, load_config, readiness
from propertyai_core.rent_runtime.worker import load_worker_config
from propertyai_core.tests.fixture_lifecycle import run_owned_command, terminate_owned_group, _group_exists
from propertyai_core.tests.postgres_stage_a_cluster import _LoopbackUnixBridge, _postgres_bin
from propertyai_core.tests.rent_finance_test_cluster import STABLE_INSTALL
from propertyai_core.web.auth_context import AuthorizedPrincipal, ServerPrincipalDirectory
from propertyai_core.web.auth_session import SessionService

from .helpers import bare_cluster, empty_admin, evidence, http, record, target_of


def _private_file(root: Path, name: str, text: str) -> Path:
    path = root / name
    write_new(path, text.encode())
    return path


def _web_config(root: Path, *, target_id: str, web_ref: Path, session_ref: Path,
                state_dir: Path, principal: AuthorizedPrincipal) -> dict:
    return {
        "environment": PERSISTENT_STAGING,
        "target_id": target_id,
        "database_connection_refs": {"WEB": str(web_ref), "SESSION": str(session_ref)},
        "runtime_role": "WEB",
        "auth_binding": {
            "kind": "PROVIDER_VERIFIED_SUBJECT_DIRECTORY",
            "issuer": {"kind": "LOCAL_TEST_FIXED_SUBJECT"},
            "principal": {
                "organization_id": str(principal.organization_id),
                "actor_party_id": str(principal.actor_party_id),
                "subject": principal.subject,
                "capabilities": sorted(principal.capabilities),
            },
        },
        "bind_host": "127.0.0.1",
        "bind_port": 0,
        "runtime_state_dir": str(state_dir),
        "scheduler": "OFF",
        "load_fixtures": False,
        "real_business_data_expected": False,
        "external_activation": EXTERNAL_ACTIVATION,
    }


def _worker_config(*, target_id: str, worker_ref: Path, organization_id) -> dict:
    return {
        "environment": PERSISTENT_STAGING,
        "target_id": target_id,
        "database_connection_ref": str(worker_ref),
        "runtime_role": "WORKER",
        "auth_binding": {"kind": "DATABASE_ROLE_BINDING", "role": "propertyai_rent_scheduler"},
        "organization_id": str(organization_id),
        "scheduler": "OFF",
        "max_items": 100,
        "max_attempts": 3,
        "real_business_data_expected": False,
        "external_activation": EXTERNAL_ACTIVATION,
    }


def _migrator_config(*, target_id: str, ref: Path) -> dict:
    return {
        "environment": PERSISTENT_STAGING,
        "target_id": target_id,
        "database_connection_ref": str(ref),
        "runtime_role": "MIGRATOR",
        "scheduler": "OFF",
        "real_business_data_expected": False,
        "external_activation": EXTERNAL_ACTIVATION,
    }


def _backup_config(
    *,
    target_id: str,
    ref: Path,
    host: str,
    port: int,
    database: str,
    server_version_num: int,
    pg_dump: Path,
    destination: Path,
) -> dict:
    return {
        "environment": PERSISTENT_STAGING,
        "target_id": target_id,
        "database_connection_ref": str(ref),
        "runtime_role": "BACKUP",
        "host": host,
        "port": port,
        "database": database,
        "server_version_num": server_version_num,
        "pg_dump_path": str(pg_dump),
        "pg_dump_sha256": digest(checked_file(pg_dump)),
        "backup_destination": str(destination),
        "scheduler": "OFF",
        "real_business_data_expected": False,
        "external_activation": EXTERNAL_ACTIVATION,
    }


def _write_config(root: Path, name: str, raw: dict) -> tuple[Path, str]:
    data = canonical(raw)
    path = root / name
    write_new(path, data)
    return path, digest(data)


def _http_exchange(port: int, path: str, *, method: str = "GET", body: bytes | None = None,
                   headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    connection = http_client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request(method, path, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        response_body = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        return response.status, response_headers, response_body
    finally:
        connection.close()


class _QueryResult:
    def __init__(self, *, one=None, many=None):
        self._one = one
        self._many = [] if many is None else many

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class _DatabaseIdentityConnection:
    def __init__(self, organization_id, database: str):
        self.organization_id = organization_id
        self.database = database

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, _params=None):
        sql_text = " ".join(statement.split())
        if "flyway_schema_history" in sql_text:
            return _QueryResult(many=[])
        if "rent_auth_session_get" in sql_text:
            return _QueryResult(many=[])
        if "rent_visible_org()" in sql_text and "current_database()" in sql_text:
            return _QueryResult(one=(self.organization_id, None, self.database))
        if "count(*) FROM propertyai.rent_contract" in sql_text:
            return _QueryResult(one=(0,))
        raise AssertionError(f"unexpected readiness SQL: {sql_text}")


def _persistent_web_config_with_dsn(tmp_path: Path, *, web_dsn: str):
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    web_ref = _private_file(tmp_path, "web.dsn", web_dsn)
    session_ref = _private_file(
        tmp_path, "session.dsn",
        "host=/tmp dbname=propertyai_rent_staging user=session password=SYNTHETIC_DB_SECRET",
    )
    actor_party_id = uuid4()
    principal = AuthorizedPrincipal(
        uuid4(), actor_party_id, fixed_local_subject(actor_party_id), frozenset({"READ", "WRITE"})
    )
    raw = _web_config(
        tmp_path, target_id="rent-stage-db-identity", web_ref=web_ref,
        session_ref=session_ref, state_dir=state_dir, principal=principal,
    )
    config_path, config_sha = _write_config(tmp_path, "web.json", raw)
    return config_path, config_sha, principal


def test_persistent_staging_web_config_uses_exact_dsn_database_and_fails_closed_on_missing_or_malformed(tmp_path):
    cases = {
        "valid": "host=/tmp dbname=propertyai_rent_staging user=web password=SYNTHETIC_DB_SECRET",
        "missing": "host=/tmp user=web password=SYNTHETIC_DB_SECRET",
        "malformed": "host=/tmp dbname='propertyai_rent_staging user=web password=SYNTHETIC_DB_SECRET",
    }
    for label, web_dsn in cases.items():
        root = tmp_path / label
        root.mkdir(mode=0o700)
        config_path, config_sha, _ = _persistent_web_config_with_dsn(root, web_dsn=web_dsn)
        if label == "valid":
            loaded = load_config(config_path, config_sha)
            assert loaded.target.expected_database("WEB") == "propertyai_rent_staging"
        else:
            with pytest.raises(OperationalError):
                load_config(config_path, config_sha)


@pytest.mark.parametrize(
    "actual_database,expected_result",
    [
        ("propertyai_rent_staging", "APPLICATION_READY"),
        ("postgres", "CONFIG_INVALID"),
    ],
)
def test_web_health_ready_database_identity_matches_exact_persistent_dsn(
    tmp_path, monkeypatch, capsys, actual_database, expected_result
):
    config_path, config_sha, principal = _persistent_web_config_with_dsn(
        tmp_path,
        web_dsn="host=/tmp dbname=propertyai_rent_staging user=web password=SYNTHETIC_DB_SECRET",
    )
    loaded = load_config(config_path, config_sha)

    def connect(_target, _role, *, autocommit=True):
        assert autocommit is True
        return _DatabaseIdentityConnection(principal.organization_id, actual_database)

    monkeypatch.setattr(type(loaded.target), "connect", connect)
    monkeypatch.setattr("propertyai_core.rent_runtime.web.role_check", lambda _conn, _group: None)

    assert readiness(loaded, {"migrations": []}) == expected_result
    output = capsys.readouterr()
    assert "SYNTHETIC_DB_SECRET" not in output.out
    assert "SYNTHETIC_DB_SECRET" not in output.err


def test_isolated_test_readiness_preserves_existing_postgres_database_identity(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="pa-stage-a-", dir="/tmp")).resolve()
    try:
        marker = {
            "root": str(root),
            "uid": root.stat().st_uid,
            "inode": root.stat().st_ino,
            "nonce": "a" * 48,
        }
        marker_path = root / ".propertyai-stage-a-owned.json"
        write_new(marker_path, canonical(marker))
        target = LocalTarget(root, digest(checked_file(marker_path)), 15432)
        principal = AuthorizedPrincipal(
            uuid4(), uuid4(), "synthetic:w5-db-identity-regression", frozenset({"READ"})
        )
        config = RuntimeConfig(
            target, "rent_test_db_identity", "rent_session_db_identity", principal, 0
        )

        def connect(_target, _login, *, autocommit=True):
            assert autocommit is True
            return _DatabaseIdentityConnection(principal.organization_id, "postgres")

        monkeypatch.setattr(LocalTarget, "connect", connect)
        monkeypatch.setattr("propertyai_core.rent_runtime.web.role_check", lambda _conn, _group: None)

        assert readiness(config, {"migrations": []}) == "APPLICATION_READY"
    finally:
        shutil.rmtree(root)


def test_persistent_staging_contract_is_explicit_secret_ref_only_and_production_fail_closed(package):
    root = Path(tempfile.mkdtemp(prefix="w5-r1-contract-", dir="/tmp")).resolve()
    try:
        state_dir = root / "state"
        state_dir.mkdir(mode=0o700)
        secret_canary = "SYNTHETIC_SECRET_CANARY_W5_R1"
        web_ref = _private_file(root, "web.dsn", f"host=/tmp dbname=postgres user=web password={secret_canary}")
        session_ref = _private_file(root, "session.dsn", f"host=/tmp dbname=postgres user=session password={secret_canary}")
        worker_ref = _private_file(root, "worker.dsn", f"host=/tmp dbname=postgres user=worker password={secret_canary}")
        flyway_ref = _private_file(root, "flyway-secret.conf", "flyway.url=jdbc:postgresql://127.0.0.1:5432/postgres\nflyway.user=propertyai_flyway\nflyway.password=" + secret_canary + "\n")
        actor_party_id = uuid4()
        principal = AuthorizedPrincipal(
            uuid4(), actor_party_id, fixed_local_subject(actor_party_id), frozenset({"READ", "WRITE"})
        )
        raw = _web_config(root, target_id="rent-stage-evaluator", web_ref=web_ref,
                          session_ref=session_ref, state_dir=state_dir, principal=principal)
        config_path, config_sha = _write_config(root, "web.json", raw)
        loaded = load_config(config_path, config_sha)
        assert loaded.environment == PERSISTENT_STAGING and loaded.target.target_id == "rent-stage-evaluator"
        assert loaded.principal.subject == principal.subject and not loaded.principal.subject.startswith("synthetic:")

        invalids = []
        for key in ("environment", "target_id", "database_connection_refs"):
            bad = copy.deepcopy(raw)
            bad.pop(key)
            invalids.append(bad)
        bad = copy.deepcopy(raw); bad["environment"] = "PRODUCTION"; invalids.append(bad)
        bad = copy.deepcopy(raw); bad["scheduler"] = "ON"; invalids.append(bad)
        bad = copy.deepcopy(raw); bad["target_id"] = ""; invalids.append(bad)
        bad = copy.deepcopy(raw); bad["database_connection_refs"] = {"WEB": str(web_ref)}; invalids.append(bad)
        bad = copy.deepcopy(raw); bad["database_connection_refs"]["WEB"] = "postgresql://implicit/default"; invalids.append(bad)
        bad = copy.deepcopy(raw); bad["reset_target"] = True; invalids.append(bad)
        bad = copy.deepcopy(raw); bad["auth_binding"]["principal"]["subject"] = "synthetic:forbidden"; invalids.append(bad)
        bad = copy.deepcopy(raw); bad["auth_binding"]["issuer"]["kind"] = "OIDC"; invalids.append(bad)
        bad = copy.deepcopy(raw); bad["auth_binding"]["issuer"]["client_secret"] = secret_canary; invalids.append(bad)
        bad = copy.deepcopy(raw); bad["bind_host"] = "0.0.0.0"; invalids.append(bad)
        bad = copy.deepcopy(raw); bad["auth_binding"]["principal"]["capabilities"].append("ADMIN"); invalids.append(bad)
        bad = copy.deepcopy(raw); bad["auth_binding"]["principal"]["subject"] = fixed_local_subject(uuid4()); invalids.append(bad)
        bad = copy.deepcopy(raw); bad["password"] = secret_canary; invalids.append(bad)
        for i, value in enumerate(invalids):
            path, sha = _write_config(root, f"invalid-{i}.json", value)
            with pytest.raises((OperationalError, KeyError, ValueError, FileNotFoundError)):
                load_config(path, sha)
        with pytest.raises(OperationalError):
            load_config(config_path, "0" * 64)

        worker_path, worker_sha = _write_config(
            root, "worker.json",
            _worker_config(target_id="rent-stage-evaluator", worker_ref=worker_ref,
                           organization_id=principal.organization_id),
        )
        worker = load_worker_config(worker_path, worker_sha)
        assert worker.environment == PERSISTENT_STAGING and worker.worker_login == "WORKER"

        migrator_path, migrator_sha = _write_config(
            root, "migrator.json", _migrator_config(target_id="rent-stage-evaluator", ref=flyway_ref)
        )
        argv = persistent_migrator_plan(package["root"], package["receipt"]["manifest_sha256"],
                                        STABLE_INSTALL, migrator_path, migrator_sha, "validate")
        assert secret_canary not in " ".join(argv)
        assert str(flyway_ref) in " ".join(argv)

        pg_dump = Path(_postgres_bin("pg_dump")).resolve()
        backup_raw = _backup_config(
            target_id="rent-stage-evaluator",
            ref=flyway_ref,
            host="127.0.0.1",
            port=5432,
            database="postgres",
            server_version_num=180006,
            pg_dump=pg_dump,
            destination=root / "backup",
        )
        backup_path, backup_sha = _write_config(root, "backup.json", backup_raw)
        backup_profile = load_persistent_backup_profile(backup_path, backup_sha)
        assert backup_profile.target_id == "rent-stage-evaluator"
        assert backup_profile.backup_destination == root / "backup"
        assert secret_canary not in repr(backup_profile)
        assert secret_canary.encode() not in checked_file(backup_path)
        bad_backup = copy.deepcopy(backup_raw)
        bad_backup["runtime_role"] = "MIGRATOR"
        bad_path, bad_sha = _write_config(root, "backup-invalid-role.json", bad_backup)
        with pytest.raises(OperationalError):
            load_persistent_backup_profile(bad_path, bad_sha)

        manifest = package["manifest"]
        assert manifest["format"] == "RENT_RUNTIME_V2"
        assert manifest["supported_environments"] == ["ISOLATED_TEST", "PERSISTENT_STAGING"]
        assert manifest["production_supported"] is False
        assert manifest["scheduler_default"] == "OFF"
        assert manifest["real_business_data_expected"] is False
        assert manifest["external_activation"] == EXTERNAL_ACTIVATION
        assert manifest["target_contract"]["PERSISTENT_STAGING"].startswith("EXPLICIT_EXTERNAL_POSTGRES")
        assert manifest["auth_contract"]["PERSISTENT_STAGING"] == "PROVIDER_VERIFIED_SUBJECT_DIRECTORY_NON_SYNTHETIC"
        assert manifest["roles"]["BACKUP"]["db_login"] == "propertyai_flyway"
        assert manifest["roles"]["BACKUP"]["effective_role"] == "propertyai_owner"
        assert manifest["roles"]["BACKUP"]["overwrite"] is False
        assert manifest["roles"]["BACKUP"]["prune"] is False
        assert secret_canary.encode() not in checked_file(evidence() / "artifact/runtime.tar")
        assert secret_canary.encode() not in checked_file(config_path)
        record("w5-r1-contract-negative.json", {
            "result": "PASS", "negative_cases": len(invalids) + 1,
            "supported_environments": manifest["supported_environments"],
            "production_supported": False, "scheduler_default": "OFF", "secret_in_artifact": False,
        })
    finally:
        shutil.rmtree(root)


def _run_persistent_flyway(package, cluster, config_root: Path, *, operation: str) -> None:
    bridge = _LoopbackUnixBridge(cluster.socket_dir / f".s.PGSQL.{cluster.port}")
    home, tmp = config_root / "flyway-home", config_root / "flyway-tmp"
    home.mkdir(mode=0o700, exist_ok=True)
    tmp.mkdir(mode=0o700, exist_ok=True)
    env = clean_env()
    env.update(HOME=str(home), TMPDIR=str(tmp), FLYWAY_USE_SYSTEM_PROXIES="false", LANG="C", LC_ALL="C",
               JAVA_ARGS=f"-XX:-UsePerfData -Djava.io.tmpdir={tmp} -Duser.home={home}")
    try:
        with bridge:
            host, port = bridge.bind_address
            secret = config_root / f"persistent-flyway-{operation}.conf"
            write_new(secret, (f"flyway.url=jdbc:postgresql://{host}:{port}/postgres?sslmode=disable\n"
                               "flyway.user=propertyai_flyway\nflyway.password=SYNTHETIC_W5_TEST_ONLY\n").encode())
            cfg, cfg_sha = _write_config(
                config_root, f"migrator-{operation}.json",
                _migrator_config(target_id="rent-stage-simulation", ref=secret),
            )
            argv = persistent_migrator_plan(package["root"], package["receipt"]["manifest_sha256"],
                                            STABLE_INSTALL, cfg, cfg_sha, operation)
            assert not any("SYNTHETIC_W5_TEST_ONLY" in arg for arg in argv)
            result = run_owned_command(argv, cwd=config_root, env=env, timeout=60)
            assert result.returncode == 0, result.stdout + result.stderr
    finally:
        assert bridge.closed


def _run_persistent_backup(package, cluster, config_root: Path) -> dict:
    bridge = _LoopbackUnixBridge(cluster.socket_dir / f".s.PGSQL.{cluster.port}")
    canary = "SYNTHETIC_W5_BACKUP_SECRET"
    pg_dump = Path(_postgres_bin("pg_dump")).resolve()
    destination = config_root / "persistent-backup"
    with target_of(cluster).connect(cluster.superuser) as conn:
        server_version_num = conn.info.server_version
    try:
        with bridge:
            host, port = bridge.bind_address
            secret = config_root / "persistent-backup-flyway.conf"
            write_new(
                secret,
                (
                    f"flyway.url=jdbc:postgresql://{host}:{port}/postgres?sslmode=disable\n"
                    "flyway.user=propertyai_flyway\n"
                    f"flyway.password={canary}\n"
                ).encode(),
            )
            config, config_sha = _write_config(
                config_root,
                "backup.json",
                _backup_config(
                    target_id="rent-stage-simulation",
                    ref=secret,
                    host=host,
                    port=port,
                    database="postgres",
                    server_version_num=server_version_num,
                    pg_dump=pg_dump,
                    destination=destination,
                ),
            )
            result = subprocess.run(
                [
                    str(package["python"]), "-m", "propertyai_core.rent_runtime", "backup",
                    "--config", str(config), "--config-sha256", config_sha,
                    "--manifest-sha256", package["receipt"]["manifest_sha256"],
                ],
                cwd=package["root"],
                env=clean_env(),
                capture_output=True,
                timeout=60,
            )
            assert result.returncode == 0 and result.stderr == b""
            assert canary.encode() not in result.stdout
            event = json.loads(result.stdout)
            assert event["process_role"] == "BACKUP" and event["result_class"] == "COMPLETED"
            receipt = json.loads(checked_file(destination / "backup-receipt.json"))
            assert receipt["action_state"] == "COMPLETED"
            assert receipt["source"]["source_class"] == PERSISTENT_STAGING
            assert receipt["source"]["target_id"] == "rent-stage-simulation"
            assert receipt["source"]["session_user"] == "propertyai_flyway"
            assert receipt["source"]["current_user"] == "propertyai_owner"
            assert receipt["source"]["server_version"] == server_version_num
            assert checked_file(destination / "database.dump", receipt["archive_sha256"]).startswith(b"PGDMP")
            second = subprocess.run(
                [
                    str(package["python"]), "-m", "propertyai_core.rent_runtime", "backup",
                    "--config", str(config), "--config-sha256", config_sha,
                    "--manifest-sha256", package["receipt"]["manifest_sha256"],
                ],
                cwd=package["root"],
                env=clean_env(),
                capture_output=True,
                timeout=60,
            )
            assert second.returncode == 78 and canary.encode() not in second.stdout
            return receipt
    finally:
        assert bridge.closed


def _business_rows(cluster) -> int:
    admin = {"authority_epoch", "flyway_schema_history", "organization", "party", "organization_member",
             "finance_ledger_scope", "rent_runtime_binding", "rent_auth_session"}
    with psycopg.connect(cluster.base_dsn) as conn:
        rows = conn.execute(
            "SELECT relname FROM pg_class WHERE relnamespace='propertyai'::regnamespace AND relkind='r' ORDER BY relname"
        ).fetchall()
        return sum(
            conn.execute(sql.SQL("SELECT count(*) FROM propertyai.{}").format(sql.Identifier(name))).fetchone()[0]
            for (name,) in rows if name not in admin
        )


def test_persistent_staging_simulation_migrator_web_auth_worker_zero_target(package):
    config_root = Path(tempfile.mkdtemp(prefix="w5-r1-persistent-sim-", dir="/tmp")).resolve()
    try:
        with bare_cluster() as cluster:
            # Evaluator-owned cluster lifecycle is outside runtime. Runtime receives only explicit refs.
            for rel in ("db/v2_2_1/bootstrap/001__privileged_roles_schema.sql",
                        "db/v2_2_1/bootstrap/002__rent_capability_roles.sql"):
                cluster.psql(sql_text=checked_file(package["root"] / rel).decode())
            _run_persistent_flyway(package, cluster, config_root, operation="migrate")
            _run_persistent_flyway(package, cluster, config_root, operation="validate")
            backup_receipt = _run_persistent_backup(package, cluster, config_root)
            assert backup_receipt["snapshot"]["tables"]["organization"]["count"] == 0

            ids = empty_admin(cluster)
            worker_login = "rent_scheduler_test_" + uuid4().hex[:10]
            cluster.psql(sql_text=f"CREATE ROLE {worker_login} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; "
                                  f"GRANT propertyai_rent_scheduler TO {worker_login}; "
                                  "INSERT INTO propertyai.rent_runtime_binding(login_name,organization_id,actor_party_id,principal_type,capability,data_environment,enabled) "
                                  f"VALUES('{worker_login}','{ids['organization_id']}',NULL,'SYSTEM','SCHEDULER','TEST',true);")
            session_login = "rent_session_" + uuid4().hex[:10]
            cluster.psql(sql_text=f"CREATE ROLE {session_login} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; "
                                  f"GRANT propertyai_app_runtime TO {session_login};")

            canary = "SYNTHETIC_W5_DSN_PASSWORD"
            web_ref = _private_file(config_root, "web.dsn", cluster.login_dsn(ids["login"]) + f" password={canary}")
            session_ref = _private_file(config_root, "session.dsn", cluster.login_dsn(session_login) + f" password={canary}")
            worker_ref = _private_file(config_root, "worker.dsn", cluster.login_dsn(worker_login) + f" password={canary}")
            target = persistent_target("rent-stage-simulation", {"WEB": str(web_ref), "SESSION": str(session_ref)},
                                       required_roles={"WEB", "SESSION"})
            principal = AuthorizedPrincipal(
                ids["organization_id"], ids["party_id"],
                fixed_local_subject(ids["party_id"]), frozenset({"READ", "WRITE"})
            )
            session_reader = SessionService(
                PostgresSessionStore(lambda: target.connect("SESSION")),
                ServerPrincipalDirectory([principal]),
            )

            state_dir = config_root / "state"
            state_dir.mkdir(mode=0o700)
            web_raw = _web_config(config_root, target_id="rent-stage-simulation", web_ref=web_ref,
                                  session_ref=session_ref, state_dir=state_dir, principal=principal)
            web_cfg, web_sha = _write_config(config_root, "web.json", web_raw)
            loaded = load_config(web_cfg, web_sha)
            assert readiness(loaded, package["manifest"]) == "APPLICATION_READY"
            assert loaded.local_issuer is not None
            assert loaded.local_issuer.verified_subject() == principal.subject
            assert _business_rows(cluster) == 0

            state = state_dir / "rent-web-simulation.json"
            web_argv = [
                str(package["python"]), "-m", "propertyai_core.rent_runtime", "web",
                "--config", str(web_cfg), "--config-sha256", web_sha,
                "--manifest-sha256", package["receipt"]["manifest_sha256"], "--state", str(state),
            ]

            def start_web():
                child = subprocess.Popen(
                    web_argv, cwd=package["root"], env=clean_env(), start_new_session=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                deadline = time.monotonic() + 15
                while not state.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert state.exists(), f"persistent web failed to start: {child.poll()}"
                return child, json.loads(checked_file(state))["port"]

            def stop_web(child):
                if child.poll() is None:
                    child.send_signal(signal.SIGTERM)
                try:
                    stdout, stderr = child.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    terminate_owned_group(child)
                    stdout, stderr = child.communicate(timeout=5)
                assert child.returncode == 0 and stderr == b""
                assert not state.exists() and not _group_exists(child.pid)
                return stdout

            token = None
            child, port = start_web()
            try:
                assert http(port, "/health/live")[0] == 200
                status, ready = http(port, "/health/ready")
                assert status == 200 and json.loads(ready)["result_class"] == "APPLICATION_READY"
                assert http(port, "/app")[0] == 401

                status, login_headers, login_body = _http_exchange(
                    port, "/auth/login/local", method="POST"
                )
                assert status == 303 and login_body == b""
                assert login_headers["location"] == "/app"
                cookie_header = login_headers["set-cookie"]
                for attribute in ("Path=/", "Secure", "HttpOnly", "SameSite=Strict"):
                    assert attribute in cookie_header
                cookie = SimpleCookie()
                cookie.load(cookie_header)
                token = cookie["rent_session"].value
                assert token and principal.subject.encode() not in login_body
                assert http(port, "/app", token=token)[0] == 200
                session_record = session_reader.resolve(token)
                assert session_record is not None and session_record.principal == principal
            finally:
                stdout = stop_web(child)
                assert canary.encode() not in stdout
                if token is not None:
                    assert token.encode() not in stdout

            # The next Web process must resolve the same durable PostgreSQL session.
            child, port = start_web()
            try:
                assert token is not None
                assert http(port, "/app", token=token)[0] == 200
                session_record = session_reader.resolve(token)
                assert session_record is not None
                status, logout_headers, logout_body = _http_exchange(
                    port,
                    "/auth/logout",
                    method="POST",
                    headers={
                        "Cookie": "rent_session=" + token,
                        "X-CSRF-Token": session_record.csrf_token,
                    },
                )
                assert status == 200 and json.loads(logout_body)["status"] == "LOGGED_OUT"
                cleared = logout_headers["set-cookie"]
                for attribute in ("Path=/", "Secure", "HttpOnly", "SameSite=Strict", "Max-Age=0"):
                    assert attribute in cleared
                assert session_reader.resolve(token) is None
                assert http(port, "/app", token=token)[0] == 401
            finally:
                stdout = stop_web(child)
                assert canary.encode() not in stdout
                if token is not None:
                    assert token.encode() not in stdout

            worker_raw = _worker_config(target_id="rent-stage-simulation", worker_ref=worker_ref,
                                        organization_id=ids["organization_id"])
            worker_cfg, worker_sha = _write_config(config_root, "worker.json", worker_raw)
            result = subprocess.run(
                [str(package["python"]), "-m", "propertyai_core.rent_runtime", "worker",
                 "--config", str(worker_cfg), "--config-sha256", worker_sha,
                 "--manifest-sha256", package["receipt"]["manifest_sha256"]],
                cwd=package["root"], env=clean_env(), capture_output=True, timeout=60,
            )
            assert result.returncode == 0 and result.stderr == b""
            payload = json.loads(result.stdout)
            assert payload["result_class"] == "ZERO_TARGET_NOOP"
            assert payload["worker_result"]["evaluated_count"] == 0
            assert payload["scheduler_default"] == "OFF" and payload["automatic_scheduler_activated"] is False
            assert _business_rows(cluster) == 0
            assert canary.encode() not in result.stdout
            record("w5-r1-persistent-staging-simulation.json", {
                "result": "PASS", "migrator": "MIGRATE_AND_VALIDATE_PASS",
                "web": "START_READY_LOCAL_LOGIN_RESTART_AUTHENTICATED_EMPTY_LOGOUT_STOP_PASS",
                "auth": "PERSISTENT_STAGING_LOCAL_TEST_FIXED_SUBJECT_DURABLE_POSTGRES_PASS",
                "worker": "ZERO_TARGET_NOOP", "worker_effect": 0,
                "business_rows": 0, "fixture_leakage": 0,
                "scheduler_default": "OFF", "scheduler_activated": False,
                "target_runtime_lifecycle_authority": False,
            })
    finally:
        shutil.rmtree(config_root)

"""Explicit synthetic, owned-target test operations. Never imported by runtime code."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import getpass
import http.client as http_client
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from propertyai_core.rent_runtime.artifact import build, materialize, migrator_plan, verify
from propertyai_core.rent_runtime.common import LocalTarget, canonical, checked_file, clean_env, digest, write_new
from propertyai_core.rent_runtime.recovery import role_facts, extension_facts
from propertyai_core.rent_runtime.web import RuntimeConfig, load_config
from propertyai_core.tests.fixture_lifecycle import FixtureLifecycle, run_owned_command, terminate_owned_group, _group_exists
from propertyai_core.tests.postgres_stage_a_cluster import DisposablePostgres, _LoopbackUnixBridge, _free_port, _postgres_bin
from propertyai_core.tests.rent_finance_test_cluster import start_disposable_rent_postgres, STABLE_INSTALL
from propertyai_core.adapters.postgres.rent_session_store import PostgresSessionStore
from propertyai_core.web.auth_context import AuthorizedPrincipal, ServerPrincipalDirectory
from propertyai_core.web.auth_session import SessionService

ROOT = Path(__file__).resolve().parents[3]
BASE = "9d85d8c84e1756e174cec1836a2719cdf7a6ab4c"


def evidence() -> Path:
    return Path(os.environ["W3B_EVIDENCE_ROOT"]).resolve()


def record(name: str, value: dict):
    write_new(evidence() / name, canonical(value))


def target_of(cluster) -> LocalTarget:
    marker = cluster.root / ".propertyai-stage-a-owned.json"
    value = LocalTarget(cluster.root, digest(checked_file(marker)), cluster.port)
    value.guard()
    return value


@contextmanager
def package_context():
    source = ({"commit": os.environ["W3B_SOURCE_COMMIT"]} if os.environ.get("W3B_SOURCE_COMMIT")
              else {"candidate_tree": os.environ["W3B_SOURCE_TREE"],
                    "base_commit": os.environ.get("W3B_CANDIDATE_BASE", BASE)})
    receipt = build(ROOT, evidence() / "artifact", **source)
    owned = Path(tempfile.mkdtemp(prefix="w3b-package-", dir="/tmp")).resolve()
    inode = owned.stat().st_ino
    location = owned / "release"
    try:
        manifest = materialize(evidence() / "artifact/runtime.tar", receipt["artifact_sha256"], location, receipt["manifest_sha256"])
        result = run_owned_command([str(Path.home() / ".local/bin/uv"), "sync", "--frozen", "--offline", "--no-dev", "--python", sys.executable],
                                   cwd=location, env=clean_env(), timeout=60)
        assert result.returncode == 0, result.stderr
        record("isolated-dependency-install.json", {"exit_code": result.returncode, "uv_lock_sha256": digest(checked_file(location / "uv.lock")),
                                                   "frozen": True, "offline": True, "no_dev": True})
        verify(location, receipt["manifest_sha256"])
        yield {"root": location, "receipt": receipt, "manifest": manifest, "source": source,
               "python": location / ".venv/bin/python"}
    finally:
        assert owned.stat().st_ino == inode and owned.parent == Path("/tmp").resolve() and owned.name.startswith("w3b-package-")
        shutil.rmtree(owned)
        record("artifact-materialization-cleanup.json", {"result": "PASS", "root": str(owned), "root_absent": not owned.exists()})


def start_cluster(cluster):
    cluster.lifecycle.guard()
    options = f"-p {cluster.port} -k {cluster.socket_dir} -c listen_addresses='' -c fsync=on -c synchronous_commit=on -c full_page_writes=on"
    run_owned_command([_postgres_bin("pg_ctl"), "-D", str(cluster.data_dir), "-l", str(cluster.root / "postgres.log"),
                       "-o", options, "-w", "-t", "15", "start"], timeout=20, check=True, allow_daemon=True)


@contextmanager
def bare_cluster():
    owner = FixtureLifecycle.create()
    cluster = DisposablePostgres(owner.root, owner.root / "data", owner.root / "sock", _free_port(), getpass.getuser(), lifecycle=owner)
    try:
        owner.guard()
        cluster.socket_dir.mkdir(mode=0o700)
        run_owned_command([_postgres_bin("initdb"), "-D", str(cluster.data_dir), "--auth=trust", "--no-locale", "--encoding=UTF8", "-U", cluster.superuser], timeout=20, check=True)
        start_cluster(cluster)
        yield cluster
    finally:
        cleanup = owner.finish(_postgres_bin("pg_ctl"))
        record("bare-" + owner.root.name + "-cleanup.json", cleanup)
        assert cleanup["classification"] == "PASS" and cleanup["root_absent"]


@contextmanager
def fresh_schema():
    binding = json.loads(checked_file(Path(os.environ["PROPERTYAI_RENT_TEST_BINDING_FILE"]), os.environ["PROPERTYAI_RENT_TEST_BINDING_SHA256"]))
    stage = next(s for s in binding["staging"] if s["scenario"] == "FRESH")
    provenance = binding["provenance"]
    fixture = None
    try:
        with start_disposable_rent_postgres(Path(stage["path"]), stage["sha256"], Path(provenance["path"]), provenance["sha256"], durability=True) as fixture:
            yield fixture
    finally:
        if fixture is not None and fixture.cleanup_report is not None:
            record("empty-cluster-cleanup.json", fixture.cleanup_report)
            assert fixture.cleanup_report["classification"] == "PASS" and fixture.cleanup_report["root_absent"]


def migrate_package(package, cluster, operation="migrate"):
    target_of(cluster).guard()
    bridge = _LoopbackUnixBridge(cluster.socket_dir / f".s.PGSQL.{cluster.port}")
    home, tmp = cluster.root / "w3b-flyway-home", cluster.root / "w3b-flyway-tmp"
    home.mkdir(mode=0o700, exist_ok=True)
    tmp.mkdir(mode=0o700, exist_ok=True)
    env = clean_env()
    env.update(HOME=str(home), TMPDIR=str(tmp), FLYWAY_USE_SYSTEM_PROXIES="false", LANG="C", LC_ALL="C",
               JAVA_ARGS=f"-XX:-UsePerfData -Djava.io.tmpdir={tmp} -Duser.home={home}")
    try:
        with bridge:
            assert bridge.upstream == cluster.socket_dir / f".s.PGSQL.{cluster.port}"
            argv = migrator_plan(package["root"], package["receipt"]["manifest_sha256"], STABLE_INSTALL,
                                 *bridge.bind_address, operation)
            result = run_owned_command(argv, cwd=cluster.root, env=env, timeout=60)
            record(f"flyway-{operation}-{uuid4().hex}.json", {"exit_code": result.returncode,
                  "source_tree": package["receipt"]["source_tree"], "operation": operation,
                  "stdout": result.stdout, "stderr": result.stderr, "upstream_owned_marker": target_of(cluster).marker_sha256})
            assert result.returncode == 0, result.stdout + result.stderr
    finally:
        assert bridge.closed


def create_session_binding(cluster, ids):
    login = "rent_session_" + uuid4().hex[:10]
    cluster.psql(sql_text=f"CREATE ROLE {login} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; GRANT propertyai_app_runtime TO {login};")
    principal = AuthorizedPrincipal(ids["organization_id"], ids["party_id"], "synthetic:w3b:verified", frozenset({"READ", "WRITE"}))
    service = SessionService(PostgresSessionStore(lambda: target_of(cluster).connect(login)), ServerPrincipalDirectory([principal]))
    return login, principal, service


def config_for(cluster, ids, session_login, principal):
    target = target_of(cluster)
    raw = {"environment": "ISOLATED_TEST", "target_root": str(cluster.root), "target_marker_sha256": target.marker_sha256,
           "pg_port": cluster.port, "web_login": ids["login"], "session_login": session_login,
           "principal": {"organization_id": str(principal.organization_id), "actor_party_id": str(principal.actor_party_id),
                         "subject": principal.subject, "capabilities": sorted(principal.capabilities)},
           "bind_host": "127.0.0.1", "bind_port": 0, "scheduler": "OFF", "load_fixtures": False}
    return raw


def worker_config_for(cluster, ids, *, max_items=100, max_attempts=3):
    target = target_of(cluster)
    return {"environment": "ISOLATED_TEST", "target_root": str(cluster.root),
            "target_marker_sha256": target.marker_sha256, "pg_port": cluster.port,
            "worker_login": ids["scheduler_login"], "organization_id": str(ids["organization_id"]),
            "scheduler": "OFF", "max_items": max_items, "max_attempts": max_attempts}


def empty_admin(cluster):
    """Admin bootstrap only: no property, unit, resident, account, contract, movement or receivable."""
    suffix = uuid4().hex[:10]
    org, party = uuid4(), uuid4()
    login = "rent_test_" + suffix
    cluster.psql(sql_text=f"""
        CREATE ROLE {login} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        GRANT propertyai_rent_runtime TO {login};
        INSERT INTO propertyai.organization(organization_id,organization_code,display_name,organization_status,data_environment)
        VALUES('{org}','W3B-{suffix}','Synthetic administrator scope','ACTIVE','TEST');
        INSERT INTO propertyai.party(party_id,party_code,display_name,data_environment)
        VALUES('{party}','W3B-{suffix}','Synthetic administrator','TEST');
        INSERT INTO propertyai.organization_member(organization_member_id,organization_id,party_id,membership_role,membership_status,joined_at)
        VALUES('{uuid4()}','{org}','{party}','OPERATOR','ACTIVE',transaction_timestamp());
        INSERT INTO propertyai.finance_ledger_scope(organization_id,revision) VALUES('{org}',0);
        INSERT INTO propertyai.rent_runtime_binding(login_name,organization_id,actor_party_id,principal_type,capability,data_environment,enabled)
        VALUES('{login}','{org}','{party}','PARTY','WRITE','TEST',true);
    """)
    return {"organization_id": org, "party_id": party, "login": login}


def create_scheduler_binding(cluster, organization_id):
    """TEST-only scheduler login/binding; no business fixture or Production authority."""
    login = "rent_scheduler_test_" + uuid4().hex[:10]
    cluster.psql(sql_text=f"""
        CREATE ROLE {login} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        GRANT propertyai_rent_scheduler TO {login};
        INSERT INTO propertyai.rent_runtime_binding(login_name,organization_id,actor_party_id,principal_type,capability,data_environment,enabled)
        VALUES('{login}','{organization_id}',NULL,'SYSTEM','SCHEDULER','TEST',true);
    """)
    return login


def clone_role_prerequisites(cluster, facts):
    """Explicit TEST-only restore prerequisite, with no password provisioning."""
    assert all(not any(r[3:]) for r in facts["roles"])
    with target_of(cluster).connect(cluster.superuser) as conn:
        for row in facts["roles"]:
            name, login, inherit, *_ = row
            assert name.startswith(("propertyai_", "rent_test_", "rent_session_", "rent_scheduler_test_"))
            conn.execute(sql.SQL("CREATE ROLE {} {} {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS").format(
                sql.Identifier(name), sql.SQL("LOGIN" if login else "NOLOGIN"), sql.SQL("INHERIT" if inherit else "NOINHERIT")))
        for role, member, admin, inherit, set_role in facts["memberships"]:
            conn.execute(sql.SQL("GRANT {} TO {} WITH ADMIN {}, INHERIT {}, SET {}").format(
                sql.Identifier(role), sql.Identifier(member), *(sql.SQL("TRUE" if v else "FALSE") for v in (admin, inherit, set_role))))
        assert role_facts(conn) == facts


def prepare_extensions(cluster, expected):
    """Explicit test-only source-bound extension bootstrap, not an automatic restore effect."""
    with target_of(cluster).connect(cluster.superuser) as conn:
        existing = {r[0] for r in extension_facts(conn)}
        for name, version, schema in expected:
            assert name in {"plpgsql", "btree_gist"} and schema in {"pg_catalog", "public"}
            if name not in existing:
                conn.execute(sql.SQL("CREATE EXTENSION {} WITH SCHEMA {} VERSION {}").format(
                    sql.Identifier(name), sql.Identifier(schema), sql.Literal(version)))
        assert extension_facts(conn) == expected


def http(port, path, *, token=None, method="GET", headers=None):
    h = dict(headers or {})
    if token:
        h["Cookie"] = "rent_session=" + token
    conn = http_client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(method, path, headers=h)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def worker_process(package, raw_config, cluster, label, *, dry_run=False, run_id=None, after=None):
    config_path = cluster.root / f"w3b-worker-config-{label}.json"
    config_bytes = canonical(raw_config)
    write_new(config_path, config_bytes)
    cursor_path = None
    cursor_bytes = None
    argv = [str(package["python"]), "-m", "propertyai_core.rent_runtime", "worker",
            "--config", str(config_path), "--config-sha256", digest(config_bytes),
            "--manifest-sha256", package["receipt"]["manifest_sha256"]]
    if dry_run:
        argv.append("--dry-run")
    if run_id is not None:
        argv.extend(["--run-id", str(run_id)])
    if after is not None:
        cursor_path = cluster.root / f"w3b-worker-cursor-{label}.json"
        cursor_bytes = canonical(after)
        write_new(cursor_path, cursor_bytes)
        argv.extend(["--after-file", str(cursor_path), "--after-sha256", digest(cursor_bytes)])
    child = subprocess.Popen(argv, cwd=package["root"], env=clean_env(), start_new_session=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        try:
            stdout, stderr = child.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            terminate_owned_group(child)
            child.communicate(timeout=5)
            raise AssertionError("WORKER_PROCESS_TIMEOUT") from None
        assert stderr == b"", stderr.decode(errors="replace")
        lines = [line for line in stdout.decode().splitlines() if line]
        assert len(lines) == 1, stdout.decode()
        payload = json.loads(lines[0])
        worker = payload.get("worker_result") or {}
        record(f"worker-{label}-receipt.json", {
            "exit_code": child.returncode, "process_group_absent": not _group_exists(child.pid),
            "result_class": payload.get("result_class"), "worker_result_class": worker.get("result_class"),
            "scan_complete": worker.get("scan_complete"), "created_count": worker.get("created_count"),
            "unknown_count": worker.get("unknown_count"), "failed_count": worker.get("failed_count"),
            "scheduler_default": payload.get("scheduler_default"),
            "automatic_scheduler_activated": payload.get("automatic_scheduler_activated"),
            "source_tree": package["receipt"]["source_tree"],
            "artifact_sha256": package["receipt"]["artifact_sha256"],
        })
        assert not _group_exists(child.pid)
        return child.returncode, payload
    finally:
        if child.poll() is None:
            terminate_owned_group(child)
        if config_path.exists() and checked_file(config_path) == config_bytes:
            config_path.unlink()
        if cursor_path is not None and cursor_path.exists() and checked_file(cursor_path) == cursor_bytes:
            cursor_path.unlink()


@contextmanager
def web_process(package, raw_config, cluster, label):
    config_path = cluster.root / f"w3b-config-{label}.json"
    state = cluster.root / f"w3b-web-{label}.json"
    config_bytes = canonical(raw_config)
    write_new(config_path, config_bytes)
    load_config(config_path, digest(config_bytes))
    argv = [str(package["python"]), "-m", "propertyai_core.rent_runtime", "web", "--config", str(config_path),
            "--config-sha256", digest(config_bytes), "--manifest-sha256", package["receipt"]["manifest_sha256"], "--state", str(state)]
    started = time.monotonic()
    child = subprocess.Popen(argv, cwd=package["root"], env=clean_env(), start_new_session=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not state.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert state.exists(), f"packaged process did not bind; exit={child.poll()}"
        port = json.loads(checked_file(state))["port"]
        yield port
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
        try:
            stdout, stderr = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            terminate_owned_group(child)
            raise AssertionError("WEB_CLEAN_STOP_TIMEOUT") from None
        events = [json.loads(line) for line in stdout.decode().splitlines()]
        record(f"web-{label}-receipt.json", {"exit_code": child.returncode, "state_absent": not state.exists(),
               "process_group_absent": not _group_exists(child.pid), "events": events,
               "stderr_bytes": len(stderr), "observed_seconds": round(time.monotonic() - started, 6),
               "source_tree": package["receipt"]["source_tree"], "artifact_sha256": package["receipt"]["artifact_sha256"]})
        assert child.returncode == 0 and not state.exists() and not _group_exists(child.pid), stdout.decode() + stderr.decode()
        assert stderr == b""
        assert all(set(e) == {"timestamp", "process_role", "correlation_id", "result_class", "error_class"} for e in events)
        assert raw_config["principal"]["subject"] not in stdout.decode()
        assert raw_config["principal"]["organization_id"] not in stdout.decode()

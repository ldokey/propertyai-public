from __future__ import annotations
import copy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
import pytest

from propertyai_core.adapters.postgres.rent_session_store import PostgresSessionStore
from propertyai_core.application.handlers.rent import BusinessDateProvider
from propertyai_core.rent_runtime.common import OperationalError, canonical, checked_file, digest, write_new
from propertyai_core.rent_runtime.recovery import backup, backup_freshness, logical_snapshot, restore
from propertyai_core.rent_runtime.web import RuntimeConfig, readiness
from propertyai_core.tests.fixture_lifecycle import run_owned_command
from propertyai_core.tests.postgres_stage_a_cluster import _postgres_bin
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_finance.p1_scenarios import run
from propertyai_core.tests.rent_finance.test_corrections import _setup_finance, _replacement_plan
from propertyai_core.web.auth_context import ServerPrincipalDirectory
from propertyai_core.web.auth_session import SessionService
from propertyai_core.web.rent_integration import PrincipalRentServiceResolver, RentDatabaseBinding, StaticRentDatabaseBindings
from .helpers import (bare_cluster, clone_role_prerequisites, config_for, create_scheduler_binding,
                      create_session_binding, empty_admin, evidence, fresh_schema, http, migrate_package,
                      prepare_extensions, record, start_cluster, target_of, web_process, worker_config_for,
                      worker_process)


def test_disposable_backup_independent_restore_finance_commands_sessions_privileges_and_partial_failure(package):
    fixture = None
    try:
        with operator_fixture(durability=True) as (fixture, service, ids):
            migrate_package(package, fixture.cluster)
            migrate_package(package, fixture.cluster, "validate")
            state = _setup_finance(service, ids, receipt_amount="500000", allocation_amount="500000")
            key = uuid4()
            correction = run(service, "adjustReceivable", {
                "delta": "-100000", "reason": "Synthetic recovery correction", "expected_version": state["receivable_version"],
                "expected_ledger_revision": state["ledger_revision"], "allocation_correction": _replacement_plan(state, "400000"),
                "reverse_adjustment_id": None}, target=UUID(state["receivable_id"]), key=key)
            assert correction["result"]["receivable_balances"][0]["balance"] == "0"
            assert correction["result"]["source_balances"][0]["principal"] == "500000"
            assert correction["result"]["source_balances"][0]["available"] == "100000"
            assert service.lookup_command("ADJUST_RECEIVABLE", key)["command"] == correction
            session_login, principal, sessions = create_session_binding(fixture.cluster, ids)
            active, revoked = sessions.issue(principal.subject), sessions.issue(principal.subject)
            assert sessions.revoke(revoked.session_id)
            past = datetime.now(timezone.utc) - timedelta(days=1)
            expired_service = SessionService(PostgresSessionStore(lambda: target_of(fixture.cluster).connect(session_login)),
                                             ServerPrincipalDirectory([principal]), ttl=timedelta(seconds=2), clock=lambda: past)
            expired = expired_service.issue(principal.subject)
            fixture.restart()
            assert sessions.resolve(active.token) is not None
            assert sessions.resolve(revoked.token) is None and sessions.resolve(expired.token) is None
            source = target_of(fixture.cluster)
            destination = evidence() / "backup"
            backed = backup(source, fixture.cluster.superuser, Path(_postgres_bin("pg_dump")), destination)
            assert backed["action_state"] == "COMPLETED" and backed["restore_proven"] is False
            backup_hash = digest(checked_file(destination / "backup-receipt.json"))
            with pytest.raises(OperationalError) as duplicate:
                backup(source, fixture.cluster.superuser, Path(_postgres_bin("pg_dump")), destination)
            assert duplicate.value.state == "FAILED_PRE_EFFECT"
            assert digest(checked_file(destination / "backup-receipt.json")) == backup_hash
            assert backup_freshness(destination, backup_hash, max_age_seconds=60) == "BACKUP_FRESH"
            assert backup_freshness(destination, backup_hash, max_age_seconds=60,
                                    now=datetime.fromisoformat(backed["created_at"]) + timedelta(seconds=61)) == "BACKUP_STALE"
            assert backup_freshness(evidence() / "missing-backup", backup_hash, max_age_seconds=60) == "BACKUP_MISSING"
            partial = evidence() / "partial-backup"
            partial.mkdir(mode=0o700)
            assert backup_freshness(partial, backup_hash, max_age_seconds=60) == "BACKUP_INVALID"
            assert backup_freshness(destination, "0" * 64, max_age_seconds=60) == "BACKUP_INVALID"
            with pytest.raises(OperationalError) as same:
                restore(source, fixture.cluster.superuser, Path(_postgres_bin("pg_restore")), destination, backup_hash)
            assert same.value.state == "FAILED_PRE_EFFECT"
            with bare_cluster() as restored_cluster:
                restored_target = target_of(restored_cluster)
                clone_role_prerequisites(restored_cluster, backed["snapshot"]["roles"])
                with pytest.raises(OperationalError) as missing_extension:
                    restore(restored_target, restored_cluster.superuser, Path(_postgres_bin("pg_restore")), destination, backup_hash)
                assert missing_extension.value.code == "RESTORE_EXTENSION_PREREQUISITES_INVALID"
                assert missing_extension.value.state == "FAILED_PRE_EFFECT"
                prepare_extensions(restored_cluster, backed["snapshot"]["extensions"])
                with pytest.raises(OperationalError) as hash_failure:
                    restore(restored_target, restored_cluster.superuser, Path(_postgres_bin("pg_restore")), destination, "0" * 64)
                assert hash_failure.value.state == "FAILED_PRE_EFFECT"
                assert not (restored_cluster.root / "w3b-restore-pending.json").exists()
                restored = restore(restored_target, restored_cluster.superuser, Path(_postgres_bin("pg_restore")), destination, backup_hash)
                assert restored["action_state"] == "RECOVERED" and restored["logical_integrity"] == "PASS"
                record("restore-receipt.json", restored)
                with restored_target.connect(restored_cluster.superuser) as conn:
                    assert logical_snapshot(conn) == backed["snapshot"]
                    conn.execute("GRANT SELECT ON propertyai.finance_command TO PUBLIC")
                    try:
                        assert logical_snapshot(conn) != backed["snapshot"]
                    finally:
                        conn.execute("REVOKE SELECT ON propertyai.finance_command FROM PUBLIC")
                    assert logical_snapshot(conn) == backed["snapshot"]
                migrate_package(package, restored_cluster, "validate")
                after_sessions = SessionService(PostgresSessionStore(lambda: restored_target.connect(session_login)), ServerPrincipalDirectory([principal]))
                assert after_sessions.resolve(active.token).session_id == active.session_id
                assert after_sessions.resolve(revoked.token) is None and after_sessions.resolve(expired.token) is None
                resolver = PrincipalRentServiceResolver(StaticRentDatabaseBindings({principal.subject: RentDatabaseBinding(
                    principal.organization_id, principal.actor_party_id, lambda: restored_target.connect(ids["login"]))}), BusinessDateProvider(lambda name: datetime.now(ZoneInfo(name)).date()))
                after_service = resolver.resolve(principal)
                assert after_service.lookup_command("ADJUST_RECEIVABLE", key)["command"] == correction
                balances = after_service.repository.read_rows("SELECT * FROM propertyai.v_rent_receivables WHERE receivable_id=%s", (UUID(state["receivable_id"]),))
                assert len(balances) == 1
                assert tuple(int(balances[0][k]) for k in ("effective_amount", "allocated", "balance")) == (400000, 400000, 0)
                history = after_service.repository.read_rows("SELECT delta FROM propertyai.finance_receivable_adjustment WHERE receivable_id=%s", (UUID(state["receivable_id"]),))
                assert [int(h["delta"]) for h in history] == [-100000]
                with restored_target.connect(ids["login"]) as conn:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        conn.execute("SET ROLE propertyai_owner")
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        conn.execute("SELECT * FROM propertyai.rent_auth_session")
                with restored_target.connect(session_login) as conn:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        conn.execute("SELECT * FROM propertyai.rent_auth_session")
                config = RuntimeConfig(restored_target, ids["login"], session_login, principal, 0)
                assert readiness(config, package["manifest"]) == "APPLICATION_READY"
                assert after_sessions.revoke(active.session_id)
                assert after_sessions.resolve(active.token) is None
                record("recovery-integrity.json", {"result": "PASS", "finance_balance": "MATCH", "command_receipt": "MATCH",
                       "session_state": "ACTIVE_REVOKED_EXPIRED_MATCH_AND_POST_RESTORE_REVOKE_PASS", "migration_history": "VALID",
                       "authorization_role_boundary": "PASS", "correction_history": "MATCH", "all_table_digests": "MATCH",
                       "snapshot_sha256": digest(canonical(backed["snapshot"])), "independent_cluster": True,
                       "representative_effective_allocated_balance": [400000, 400000, 0], "source_principal_available": [500000, 100000]})
            corrupt_dir = evidence() / "truncated-backup-test-only"
            corrupt_dir.mkdir(mode=0o700)
            truncated = checked_file(destination / "database.dump")[:len(checked_file(destination / "database.dump")) // 2]
            write_new(corrupt_dir / "database.dump", truncated)
            corrupted = copy.deepcopy(backed)
            corrupted.update(archive_sha256=digest(truncated), archive_bytes=len(truncated))
            write_new(corrupt_dir / "backup-receipt.json", canonical(corrupted))
            with bare_cluster() as failed_cluster:
                clone_role_prerequisites(failed_cluster, backed["snapshot"]["roles"])
                prepare_extensions(failed_cluster, backed["snapshot"]["extensions"])
                failed_target = target_of(failed_cluster)
                with pytest.raises(OperationalError) as partial:
                    restore(failed_target, failed_cluster.superuser, Path(_postgres_bin("pg_restore")), corrupt_dir,
                            digest(canonical(corrupted)))
                assert partial.value.state == "FAILED_UNKNOWN_EFFECT"
                assert (failed_cluster.root / "w3b-restore-pending.json").exists()
                with failed_target.connect(failed_cluster.superuser) as conn:
                    assert conn.execute("SELECT to_regnamespace('propertyai') IS NULL").fetchone()[0]
                assert readiness(RuntimeConfig(failed_target, ids["login"], session_login, principal, 0), package["manifest"]) == "MIGRATION_NOT_READY"
                record("partial-restore-fail-closed.json", {"result": "PASS", "action_state": partial.value.state,
                       "pending_guard_retained": True, "schema_absence_readback": True,
                       "post_failure_schema_absence_verified": True, "rollback_claimed": False, "readiness": "MIGRATION_NOT_READY"})
    finally:
        if fixture is not None and fixture.cleanup_report is not None:
            record("source-cluster-cleanup.json", fixture.cleanup_report)
            assert fixture.cleanup_report["classification"] == "PASS" and fixture.cleanup_report["root_absent"]


ADMIN_TABLES = frozenset({"authority_epoch", "flyway_schema_history", "organization", "party", "organization_member", "finance_ledger_scope",
                          "rent_runtime_binding", "rent_auth_session"})


def business_counts(cluster):
    with target_of(cluster).connect(cluster.superuser) as conn:
        tables = [r[0] for r in conn.execute("SELECT relname FROM pg_class WHERE relnamespace='propertyai'::regnamespace AND relkind='r' ORDER BY relname")]
        return {t: conn.execute(sql.SQL("SELECT count(*) FROM propertyai.{}").format(sql.Identifier(t))).fetchone()[0]
                for t in tables if t not in ADMIN_TABLES}


def test_clean_empty_packaged_web_restarts_health_fail_closed_no_fixture_leakage(package):
    with fresh_schema() as fixture:
        cluster = fixture.cluster
        migrate_package(package, cluster)
        migrate_package(package, cluster, "validate")
        ids = empty_admin(cluster)
        ids["scheduler_login"] = create_scheduler_binding(cluster, ids["organization_id"])
        session_login, principal, sessions = create_session_binding(cluster, ids)
        issued = sessions.issue(principal.subject)
        raw = config_for(cluster, ids, session_login, principal)
        with target_of(cluster).connect(cluster.superuser) as conn:
            control_before = conn.execute("SELECT to_jsonb(t)::text FROM propertyai.authority_epoch t ORDER BY scope_code").fetchall()
        assert len(control_before) == 1
        before = business_counts(cluster)
        assert all(n == 0 for n in before.values()), before
        config = RuntimeConfig(target_of(cluster), ids["login"], session_login, principal, 0)
        assert readiness(config, package["manifest"]) == "APPLICATION_READY"
        for label in ("empty-first", "empty-restart"):
            with web_process(package, raw, cluster, label) as port:
                assert http(port, "/health/live")[0] == 200
                status, ready = http(port, "/health/ready")
                assert status == 200 and json.loads(ready)["result_class"] == "APPLICATION_READY"
                assert http(port, "/app")[0] == 401
                status, empty = http(port, "/app", token=issued.token, headers={"Authorization": "Bearer SYNTHETIC_CANARY"})
                assert status == 200 and b'data-empty="true"' in empty
                assert http(port, "/rent", token=issued.token)[0] == 200
                if label == "empty-first":
                    with target_of(cluster).connect(cluster.superuser) as conn:
                        conn.execute("UPDATE propertyai.flyway_schema_history SET checksum=checksum+1 WHERE version='20260904.112'")
                    try:
                        status, failure = http(port, "/health/ready")
                        assert status == 503 and json.loads(failure)["result_class"] == "MIGRATION_NOT_READY"
                    finally:
                        with target_of(cluster).connect(cluster.superuser) as conn:
                            conn.execute("UPDATE propertyai.flyway_schema_history SET checksum=checksum-1 WHERE version='20260904.112'")
                    run_owned_command([_postgres_bin("pg_ctl"), "-D", str(cluster.data_dir), "-m", "fast", "-w", "-t", "15", "stop"], check=True, timeout=20)
                    try:
                        assert http(port, "/health/live")[0] == 200
                        status, failure = http(port, "/health/ready")
                        assert status == 503 and json.loads(failure)["result_class"] == "DATABASE_UNAVAILABLE"
                        status, failure = http(port, "/app", token=issued.token)
                        assert status == 503 and b'data-empty="true"' not in failure
                    finally:
                        start_cluster(cluster)
                    assert http(port, "/health/ready")[0] == 200
                    assert http(port, "/app", token=issued.token)[0] == 200
        worker_exit, worker = worker_process(package, worker_config_for(cluster, ids), cluster, "clean-empty-zero")
        assert worker_exit == 0 and worker["result_class"] == "ZERO_TARGET_NOOP"
        assert worker["worker_result"]["evaluated_count"] == 0 and worker["worker_result"]["scan_complete"] is True
        assert worker["scheduler_default"] == "OFF" and worker["automatic_scheduler_activated"] is False
        with target_of(cluster).connect(cluster.superuser) as conn:
            assert conn.execute("SELECT to_jsonb(t)::text FROM propertyai.authority_epoch t ORDER BY scope_code").fetchall() == control_before
        after = business_counts(cluster)
        assert before == after and all(n == 0 for n in after.values())
        for path in evidence().glob("web-*-receipt.json"):
            log = checked_file(path)
            assert issued.token.encode() not in log and issued.csrf_token.encode() not in log and b"SYNTHETIC_CANARY" not in log
        record("clean-empty-receipt.json", {"result": "PASS", "business_domain_rows": sum(after.values()), "table_counts": after,
               "allowed_admin_tables": sorted(ADMIN_TABLES), "migration_control_seed_unchanged": True, "fixture_leakage": 0, "scheduler_default": "OFF",
               "authenticated_empty_ui": "PASS", "db_failure_not_empty": "PASS", "migration_drift_not_ready": "PASS",
               "source_tree": package["receipt"]["source_tree"], "artifact_sha256": package["receipt"]["artifact_sha256"],
               "two_clean_start_stop_cycles": "PASS", "durable_session_after_db_restart": "PASS"})

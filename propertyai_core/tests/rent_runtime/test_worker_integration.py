from __future__ import annotations

import copy
import json
from pathlib import Path
from uuid import UUID

from propertyai_core.rent_runtime.common import canonical, checked_file, digest
from propertyai_core.rent_runtime.recovery import backup, logical_snapshot, restore
from propertyai_core.tests.postgres_stage_a_cluster import _postgres_bin
from propertyai_core.tests.rent_finance.p1_fixture import operator_fixture
from propertyai_core.tests.rent_billing_worker.test_recurring_postgres import count_receivables, seed

from .helpers import (bare_cluster, clone_role_prerequisites, evidence, migrate_package,
                      prepare_extensions, record, target_of, worker_config_for, worker_process)


def _issue_command_count(fixture) -> int:
    return fixture.query("SELECT count(*) FROM propertyai.finance_command WHERE command_type='ISSUE_RENT'")[0][0]


def _command_receipt_count(fixture) -> int:
    return fixture.query("SELECT count(*) FROM propertyai.command_receipt")[0][0]


def _seed_current_cycle(fixture, service, ids):
    return seed(fixture, service, ids, start="2026-09-01", end="2027-01-01",
                due_month="2026-09", due_day=10)


def test_packaged_worker_config_dry_run_zero_effect_real_cycle_and_process_restart(package):
    with operator_fixture(with_scheduler=True, durability=True) as (fixture, service, ids):
        raw = worker_config_for(fixture.cluster, ids)

        zero_exit, zero = worker_process(package, raw, fixture.cluster, "zero-target")
        assert zero_exit == 0 and zero["result_class"] == "ZERO_TARGET_NOOP"
        assert zero["worker_result"]["evaluated_count"] == 0 and zero["worker_result"]["scan_complete"] is True
        assert zero["scheduler_default"] == "OFF" and zero["automatic_scheduler_activated"] is False
        assert count_receivables(fixture) == _issue_command_count(fixture) == 0

        contract = _seed_current_cycle(fixture, service, ids)
        receivables_before = count_receivables(fixture)
        issue_before = _issue_command_count(fixture)
        receipts_before = _command_receipt_count(fixture)
        dry_exit, dry = worker_process(package, raw, fixture.cluster, "dry-run", dry_run=True)
        assert dry_exit == 0 and dry["result_class"] == "DRY_RUN_SUCCESS"
        assert dry["worker_result"]["would_create_count"] == 1
        assert count_receivables(fixture) == receivables_before
        assert _issue_command_count(fixture) == issue_before
        assert _command_receipt_count(fixture) == receipts_before

        run_exit, first = worker_process(package, raw, fixture.cluster, "real-first")
        assert run_exit == 0 and first["result_class"] == "COMPLETED"
        assert first["worker_result"]["created_count"] == 1
        assert count_receivables(fixture) == 1 and _issue_command_count(fixture) == 1

        # A new packaged process is the restart boundary. The same exact cycle must
        # reconcile the durable command receipt rather than create a second effect.
        repeat_exit, repeated = worker_process(package, raw, fixture.cluster, "real-restart")
        assert repeat_exit == 0 and repeated["result_class"] == "COMPLETED"
        assert repeated["worker_result"]["created_count"] == 0
        disposition = next(d for d in repeated["worker_result"]["dispositions"]
                           if d["contract_id"] == contract["contract_id"])
        assert disposition["disposition"] == "ALREADY_ISSUED"
        assert count_receivables(fixture) == 1 and _issue_command_count(fixture) == 1

        # New worker logs/results must fail closed without echoing arbitrary config fields.
        canary = "SYNTHETIC_PASSWORD_BEARER_ACCOUNT_CANARY"
        invalid = copy.deepcopy(raw)
        invalid["password"] = canary
        invalid_exit, refused = worker_process(package, invalid, fixture.cluster, "invalid-config")
        assert invalid_exit == 78 and refused["result_class"] == "CONFIG_ERROR"
        assert refused["worker_result"] is None and canary not in json.dumps(refused)

        record("i3-worker-runtime-cycle.json", {
            "result": "PASS", "zero_target": "PASS", "dry_run_mutation": 0,
            "created_receivables": 1, "issue_commands": 1, "restart_duplicate_effect": 0,
            "config_fail_closed": True, "log_canary_redacted": True, "scheduler_default": "OFF",
            "automatic_scheduler_activated": False,
        })


def test_packaged_worker_scan_continuation_preserves_cursor_and_completes(package):
    with operator_fixture(with_scheduler=True, durability=True) as (fixture, service, ids):
        contracts = [_seed_current_cycle(fixture, service, ids) for _ in range(4)]
        raw = worker_config_for(fixture.cluster, ids, max_items=2)
        first_exit, first = worker_process(package, raw, fixture.cluster, "page-1")
        assert first_exit == 3 and first["result_class"] == "PARTIAL_SCAN_REQUIRES_CONTINUATION"
        assert first["continuation_required"] is True and first["next_cursor"] is not None
        assert first["worker_result"]["scan_complete"] is False
        assert first["worker_result"]["next_cursor"] == first["next_cursor"]

        second_exit, second = worker_process(package, raw, fixture.cluster, "page-2", after=first["next_cursor"])
        assert second_exit == 0 and second["result_class"] == "COMPLETED"
        assert second["continuation_required"] is False and second["next_cursor"] is None
        assert second["worker_result"]["scan_complete"] is True
        assert first["worker_result"]["created_count"] + second["worker_result"]["created_count"] == 4
        assert count_receivables(fixture) == len(contracts) and _issue_command_count(fixture) == len(contracts)

        record("i3-worker-scan-continuation.json", {
            "result": "PASS", "first_exit": 3, "continuation_preserved": True,
            "final_scan_complete": True, "effective_receivables": len(contracts),
            "duplicate_effect": 0, "scheduler_default": "OFF",
        })


def test_packaged_worker_idempotency_survives_backup_and_independent_restore(package):
    with operator_fixture(with_scheduler=True, durability=True) as (fixture, service, ids):
        # Cross-bundle oracle starts from the integrated W3 runtime schema, not the
        # earlier Finance fixture boundary. W3-B's packaged migrator owns this step.
        migrate_package(package, fixture.cluster)
        migrate_package(package, fixture.cluster, "validate")
        _seed_current_cycle(fixture, service, ids)
        raw = worker_config_for(fixture.cluster, ids)
        first_exit, first = worker_process(package, raw, fixture.cluster, "backup-source-effect")
        assert first_exit == 0 and first["worker_result"]["created_count"] == 1
        assert count_receivables(fixture) == 1 and _issue_command_count(fixture) == 1
        source_target = target_of(fixture.cluster)
        backup_dir = evidence() / "i3-worker-backup"
        backed = backup(source_target, fixture.cluster.superuser, Path(_postgres_bin("pg_dump")), backup_dir)
        receipt_sha = digest(checked_file(backup_dir / "backup-receipt.json"))

        with bare_cluster() as restored_cluster:
            restored_target = target_of(restored_cluster)
            clone_role_prerequisites(restored_cluster, backed["snapshot"]["roles"])
            prepare_extensions(restored_cluster, backed["snapshot"]["extensions"])
            restored = restore(restored_target, restored_cluster.superuser, Path(_postgres_bin("pg_restore")),
                               backup_dir, receipt_sha)
            assert restored["action_state"] == "RECOVERED" and restored["logical_integrity"] == "PASS"
            migrate_package(package, restored_cluster, "validate")
            restored_ids = {**ids}
            restored_raw = worker_config_for(restored_cluster, restored_ids)
            with restored_target.connect(restored_cluster.superuser) as conn:
                before = logical_snapshot(conn)
                assert conn.execute("SELECT count(*) FROM propertyai.finance_receivable").fetchone()[0] == 1
                assert conn.execute("SELECT count(*) FROM propertyai.finance_command WHERE command_type='ISSUE_RENT'").fetchone()[0] == 1
                assert conn.execute("SELECT count(*) FROM propertyai.command_receipt WHERE command_type='ISSUE_RENT'").fetchone()[0] == 1

            repeat_exit, repeated = worker_process(package, restored_raw, restored_cluster, "backup-restored-repeat")
            assert repeat_exit == 0 and repeated["result_class"] == "COMPLETED"
            assert repeated["worker_result"]["created_count"] == 0
            with restored_target.connect(restored_cluster.superuser) as conn:
                after = logical_snapshot(conn)
                assert conn.execute("SELECT count(*) FROM propertyai.finance_receivable").fetchone()[0] == 1
                assert conn.execute("SELECT count(*) FROM propertyai.finance_command WHERE command_type='ISSUE_RENT'").fetchone()[0] == 1
                assert conn.execute("SELECT count(*) FROM propertyai.command_receipt WHERE command_type='ISSUE_RENT'").fetchone()[0] == 1
            assert before == after

            record("i3-backup-restore-worker-idempotency.json", {
                "result": "PASS", "backup_action": backed["action_state"],
                "restore_action": restored["action_state"], "logical_integrity": restored["logical_integrity"],
                "receivables": 1, "issue_commands": 1, "issue_command_receipts": 1,
                "restored_repeat_created": 0, "post_repeat_logical_snapshot_unchanged": True,
                "snapshot_sha256": digest(canonical(after)), "scheduler_default": "OFF",
            })

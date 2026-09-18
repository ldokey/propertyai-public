from __future__ import annotations

import hashlib
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from propertyai_core.adapters.sqlite.store import SQLiteStore
from propertyai_core.adapters.testing.effects import FailClosedEffectAdapter
from propertyai_core.application.commands.cleaning import RecordCleaningDayConfirmationCommand
from propertyai_core.application.handlers.cleaning import RecordCleaningDayConfirmationHandler
from propertyai_core.config.flags import FeatureFlags
from propertyai_core.domain.models import CommandEnvelope
from propertyai_core.domain.states import CommandState, InvalidStateTransition
from propertyai_core.runtime.worker import SystemTestWorker


def enabled_flags() -> FeatureFlags:
    return FeatureFlags(
        application_gate_enabled=True,
        system_test_enabled=True,
        durable_outbox_enabled=True,
        cleaning_day_command_enabled=True,
        production_writes_enabled=False,
        health_restart_guard_enabled=False,
    )


def envelope(
    *,
    command_id: str | None = None,
    idempotency_key: str = "system-test-cleaning-confirmation-1",
    cleaning_id: str = "synthetic-cleaning-001",
    data_environment: str = "TEST",
    source_channel: str = "SYSTEM_TEST",
    note: str | None = None,
) -> CommandEnvelope:
    payload = {"cleaning_id": cleaning_id, "confirmed": True}
    if note is not None:
        payload["note"] = note
    return CommandEnvelope(
        command_id=command_id or str(uuid.uuid4()),
        command_type="RecordCleaningDayConfirmationCommand",
        actor_party_id="synthetic-party-001",
        actor_role="SYSTEM_TEST_WORKER",
        requested_at="2026-08-03T00:00:00+00:00",
        source_channel=source_channel,
        source_message_ref="synthetic-message-001",
        idempotency_key=idempotency_key,
        data_environment=data_environment,
        expected_version=0,
        payload=payload,
    )


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    value = SQLiteStore(tmp_path / "application-gate.sqlite3", data_environment="TEST")
    value.migrate()
    return value


def submit(store: SQLiteStore, value: CommandEnvelope):
    handler = RecordCleaningDayConfirmationHandler(store, enabled_flags())
    return handler.handle(RecordCleaningDayConfirmationCommand(value))


def test_migration_first_run_and_replay_are_idempotent(tmp_path: Path) -> None:
    value = SQLiteStore(tmp_path / "migration.sqlite3", data_environment="TEST")
    value.migrate()
    value.migrate()
    with value.connect() as connection:
        versions = connection.execute("SELECT version, name FROM schema_migration").fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert [(row[0], row[1]) for row in versions] == [(1, "initial_durable_application_gate")]
    assert {
        "schema_migration",
        "command_execution",
        "command_result",
        "outbox_effect",
        "effect_dependency",
        "effect_attempt",
        "audit_record",
        "worker_lease",
    }.issubset(tables)


def test_sqlite_safety_pragmas(store: SQLiteStore) -> None:
    settings = store.pragmas()
    assert settings["journal_mode"].lower() == "wal"
    assert settings["foreign_keys"] == 1
    assert settings["busy_timeout"] >= 5000
    assert settings["synchronous"] >= 1


def test_same_idempotency_key_and_payload_reuses_result(store: SQLiteStore) -> None:
    first = envelope(command_id="command-original")
    accepted = submit(store, first)
    adapter = FailClosedEffectAdapter()
    worker = SystemTestWorker(store, adapter, worker_id="worker-one")
    assert worker.run_once()
    assert worker.run_once()

    duplicate = envelope(command_id="command-duplicate")
    reused = submit(store, duplicate)
    assert accepted.command_id == "command-original"
    assert reused.command_id == "command-original"
    assert reused.reused is True
    assert reused.status == "SUCCEEDED"
    assert len(store.effect_rows("command-original")) == 2


def test_same_idempotency_key_with_different_payload_conflicts(store: SQLiteStore) -> None:
    original = envelope(command_id="command-original")
    submit(store, original)
    conflict = submit(
        store,
        envelope(command_id="command-conflict", cleaning_id="synthetic-cleaning-002"),
    )
    assert conflict.code == "REJECTED_STATE_CONFLICT"
    assert conflict.command_id == "command-original"
    assert len(store.effect_rows("command-original")) == 2


def test_production_environment_is_rejected_without_persistence(store: SQLiteStore) -> None:
    result = submit(store, envelope(data_environment="PRODUCTION"))
    assert result.status == "REJECTED"
    assert result.code == "APPLICATION_GATE_CLOSED"
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM command_execution").fetchone()[0] == 0


def test_flags_default_and_unknown_values_are_off() -> None:
    defaults = FeatureFlags.from_environment({})
    unknown = FeatureFlags.from_environment(
        {
            "PROPERTYAI_APPLICATION_GATE_ENABLED": "unexpected",
            "PROPERTYAI_SYSTEM_TEST_ENABLED": "",
            "PROPERTYAI_PRODUCTION_WRITES_ENABLED": "false",
        }
    )
    assert not any(defaults.__dict__.values())
    assert not any(unknown.__dict__.values())


def test_dependency_blocks_notification_until_record_succeeds(store: SQLiteStore) -> None:
    result = submit(store, envelope(command_id="command-dependency"))
    first = store.claim_next_effect("worker-one")
    assert first is not None
    assert first.effect_type == "TEST_RECORD_CONFIRMATION"
    rows = {row["effect_type"]: row for row in store.effect_rows(result.command_id)}
    assert rows["TEST_BUILD_NOTIFICATION"]["status"] == "PENDING"
    store.complete_effect(first)
    second = store.claim_next_effect("worker-one")
    assert second is not None
    assert second.effect_type == "TEST_BUILD_NOTIFICATION"


def test_two_workers_have_one_atomic_claim(store: SQLiteStore) -> None:
    submit(store, envelope(command_id="command-concurrent"))

    def claim(worker_id: str):
        return store.claim_next_effect(worker_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, ("worker-one", "worker-two")))
    assert sum(item is not None for item in claims) == 1


def test_lease_heartbeat_extends_effect_and_worker_lease(store: SQLiteStore) -> None:
    submit(store, envelope(command_id="command-heartbeat"))
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    claim = store.claim_next_effect("worker-one", now=start, lease_seconds=10)
    assert claim is not None
    assert store.heartbeat(claim, now=start + timedelta(seconds=5), lease_seconds=20)
    with store.connect() as connection:
        effect = connection.execute(
            "SELECT heartbeat_at, lease_until FROM outbox_effect WHERE effect_id = ?",
            (claim.effect_id,),
        ).fetchone()
        lease = connection.execute(
            "SELECT heartbeat_at, lease_until FROM worker_lease WHERE lease_name = ?",
            ("propertyai-system-test-worker",),
        ).fetchone()
    assert effect["heartbeat_at"] == lease["heartbeat_at"]
    assert effect["lease_until"] == lease["lease_until"]
    assert effect["lease_until"] > claim.lease_until


def test_stale_running_requires_explicit_recovery_policy(store: SQLiteStore) -> None:
    submit(store, envelope(command_id="command-stale"))
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    claim = store.claim_next_effect("worker-one", now=start, lease_seconds=10)
    assert claim is not None
    assert store.recover_stale(now=start + timedelta(seconds=9), policy="retry") == 0
    assert store.recover_stale(now=start + timedelta(seconds=11), policy="retry") == 1
    row = next(row for row in store.effect_rows("command-stale") if row["effect_id"] == claim.effect_id)
    assert row["status"] == "READY"
    assert row["lease_owner"] is None


def test_retry_wait_records_next_attempt_at(store: SQLiteStore) -> None:
    submit(store, envelope(command_id="command-retry"))
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    adapter = FailClosedEffectAdapter({"TEST_RECORD_CONFIRMATION": 1})
    worker = SystemTestWorker(
        store, adapter, worker_id="worker-one", max_attempts=3, retry_delay_seconds=7
    )
    assert worker.run_once(now=start)
    row = next(
        row
        for row in store.effect_rows("command-retry")
        if row["effect_type"] == "TEST_RECORD_CONFIRMATION"
    )
    assert row["status"] == "RETRY_WAIT"
    assert row["attempt_count"] == 1
    assert row["next_attempt_at"] is not None
    assert not worker.run_once(now=start + timedelta(seconds=6))
    assert worker.run_once(now=start + timedelta(seconds=8))


def test_all_effects_succeeded_marks_command_succeeded(store: SQLiteStore) -> None:
    submit(store, envelope(command_id="command-success"))
    worker = SystemTestWorker(store, FailClosedEffectAdapter(), worker_id="worker-one")
    assert worker.run_once()
    assert worker.run_once()
    assert store.command_snapshot("command-success")["status"] == "SUCCEEDED"
    assert {row["status"] for row in store.effect_rows("command-success")} == {"SUCCEEDED"}


def test_effect_failure_cannot_mark_command_succeeded(store: SQLiteStore) -> None:
    submit(store, envelope(command_id="command-failure"))
    adapter = FailClosedEffectAdapter({"TEST_RECORD_CONFIRMATION": 1})
    worker = SystemTestWorker(store, adapter, worker_id="worker-one", max_attempts=1)
    assert worker.run_once()
    assert store.command_snapshot("command-failure")["status"] == "OPERATOR_REQUIRED"
    assert any(row["status"] == "DEAD_LETTER" for row in store.effect_rows("command-failure"))


def test_ambiguous_external_adapter_result_is_reconciliation_only_and_not_blind_retried(store: SQLiteStore) -> None:
    submit(store, envelope(command_id="command-ambiguous"))

    class AmbiguousAdapter:
        def __init__(self):
            self.calls = 0

        def execute(self, _claim):
            self.calls += 1
            raise TimeoutError("external acknowledgement unknown")

    adapter = AmbiguousAdapter()
    worker = SystemTestWorker(store, adapter, worker_id="worker-one")
    assert worker.run_once()
    rows = store.effect_rows("command-ambiguous")
    first = next(row for row in rows if row["effect_type"] == "TEST_RECORD_CONFIRMATION")
    assert first["status"] == "PENDING_RECONCILIATION"
    assert first["next_attempt_at"] is None
    assert first["last_error_code"] == "EXTERNAL_EFFECT_UNCERTAIN:TimeoutError"
    assert store.command_snapshot("command-ambiguous")["status"] == "PENDING_RECONCILIATION"
    assert not worker.run_once()
    assert adapter.calls == 1


def test_testing_adapter_has_zero_external_calls(store: SQLiteStore) -> None:
    submit(store, envelope(command_id="command-external-zero"))
    adapter = FailClosedEffectAdapter()
    worker = SystemTestWorker(store, adapter, worker_id="worker-one")
    worker.run_once()
    worker.run_once()
    assert adapter.external_call_count == 0
    assert sum(adapter.call_counts.values()) == 2


def test_secret_and_pii_are_not_written_to_audit(store: SQLiteStore) -> None:
    sensitive_markers = ("SYNTHETIC_SECRET_MARKER", "SYNTHETIC_PII_MARKER")
    submit(
        store,
        envelope(
            command_id="command-audit-redaction",
            note=" ".join(sensitive_markers),
        ),
    )
    audit = store.audit_text()
    assert all(marker not in audit for marker in sensitive_markers)


def test_legacy_runtime_and_plist_sentinels_are_unchanged(tmp_path: Path) -> None:
    runtime_json = tmp_path / "legacy-runtime.json"
    source_plist = tmp_path / "legacy-source.plist"
    runtime_json.write_text('{"state":"legacy"}', encoding="utf-8")
    source_plist.write_text("<plist><dict/></plist>", encoding="utf-8")

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = (digest(runtime_json), digest(source_plist))
    value = SQLiteStore(tmp_path / "isolated-test.sqlite3", data_environment="TEST")
    value.migrate()
    submit(value, envelope(command_id="command-fingerprint"))
    worker = SystemTestWorker(value, FailClosedEffectAdapter(), worker_id="worker-one")
    worker.run_once()
    worker.run_once()
    after = (digest(runtime_json), digest(source_plist))
    assert after == before


def test_store_rejects_non_test_database_environment(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="restricted to TEST"):
        SQLiteStore(tmp_path / "production.sqlite3", data_environment="PRODUCTION")
    assert not (tmp_path / "production.sqlite3").exists()


def test_store_rejects_database_path_outside_pytest_tmp_path() -> None:
    forbidden = Path.cwd() / "propertyai-production.sqlite3"
    with pytest.raises(ValueError, match="system temporary directory"):
        SQLiteStore(forbidden, data_environment="TEST")
    assert not forbidden.exists()


def test_unique_constraints_are_enforced(store: SQLiteStore) -> None:
    submit(store, envelope(command_id="command-unique"))
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO worker_lease(lease_name, lease_owner, lease_until, heartbeat_at)
                VALUES ('duplicate-lease', 'one', '2026-08-03', '2026-08-03')
                """
            )
            connection.execute(
                """
                INSERT INTO worker_lease(lease_name, lease_owner, lease_until, heartbeat_at)
                VALUES ('duplicate-lease', 'two', '2026-08-03', '2026-08-03')
                """
            )


def test_invalid_transition_raises_and_persists_safe_audit(store: SQLiteStore) -> None:
    submit(store, envelope(command_id="command-invalid-transition"))
    worker = SystemTestWorker(store, FailClosedEffectAdapter(), worker_id="worker-one")
    worker.run_once()
    worker.run_once()
    with pytest.raises(InvalidStateTransition):
        with store.transaction(immediate=True) as connection:
            store._transition_command(
                connection,
                "command-invalid-transition",
                CommandState.RUNNING,
                "2026-08-03T00:00:00+00:00",
            )
    assert "INVALID_TRANSITION" in store.audit_text()

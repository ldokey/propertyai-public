"""Frozen Batch A integration attacks. Critical nodes must execute, never skip."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from adcp_global_writer_client import GlobalWriterLeaseClient
from propertyai_core import global_writer
from propertyai_core.adapters.postgres.repository import PostgresCleanerTransaction
from propertyai_core.runtime import cleaner_pg_outbox_service as service
from propertyai_core.runtime.outbox_reconciliation import (
    GeneralOutboxReconciliation, OutboxReconciliationError, ProviderEvidence,
    ProviderEvidenceClass, row_binding_sha256,
)
from propertyai_core.runtime.cleaner_topology import CleanerRuntimeTopology, topology_contract
from propertyai_core.tests.batch_a_failure_harness import (
    batch_a_cluster, batch_a, dsn, emit, install_coordinator, wait_until,
)
from propertyai_core.tests.test_postgres_stage_a import make_receipt, snapshot_for

pytestmark = pytest.mark.real_global_writer
UTC = timezone.utc


def resolve(harness, message, *, provider=None, retry=False, **overrides):
    row = harness.row(message.outbox_id)
    return GeneralOutboxReconciliation(harness.worker_repository, provider or harness.ledger).resolve(
        message.outbox_id, expected_attempt=overrides.get("attempt", row.attempt_count),
        expected_lease_fence=overrides.get("fence", row.lease_fence),
        diagnostic_owner="batch-a:recovery", allow_confirmed_no_effect_retry=retry)


def test_shared_harness_real_fsync_dcs_worker_ledger_and_cleanup(batch_a):
    h = batch_a
    message = h.enqueue()
    result = h.finish(h.spawn())
    row = h.full_row(message.outbox_id)
    assert result["result"]["status"] == "COMPLETED"
    assert row["outbox_status"] == "SUCCEEDED" and row["attempt_count"] == row["lease_fence"] == 1
    assert row["lease_owner"] is row["lease_until"] is None
    assert len(h.ledger.effects(message.outbox_id)) == 1
    with GlobalWriterLeaseClient(h.case / "control.sqlite3") as client:
        assert client.get()["state"] == "FREE"
    emit("SHARED_HARNESS_PASS", row=asdict(h.row(message.outbox_id)), effects=h.ledger.effects(message.outbox_id))


@pytest.mark.parametrize("checkpoint,outcome,effect_count", [
    ("after_claim", "success", 0),
    ("before_provider", "success", 0),
    ("after_provider", "success", 1),
    ("ambiguous_response", "ambiguous", 1),
])
def test_sigkill_restart_reclaim_and_no_duplicate_effect(batch_a, checkpoint, outcome, effect_count):
    h = batch_a
    message = h.enqueue()
    child = h.spawn(checkpoint=checkpoint, outcome=outcome)
    before = h.full_row(message.outbox_id)
    assert before["outbox_status"] == ("RUNNING" if checkpoint == "after_claim" else "PENDING_RECONCILIATION")
    assert len(h.ledger.effects(message.outbox_id)) == effect_count
    h.kill(child)
    if checkpoint == "after_claim":
        wait_until(lambda: h.full_row(message.outbox_id)["lease_until"] < datetime.now(UTC), timeout=3)
    restarted = h.spawn()
    assert restarted.pid != child.pid
    restart = h.finish(restarted)
    assert restart["result"]["status"] in {"IDLE", "PENDING_RECONCILIATION"}
    pending = h.row(message.outbox_id)
    assert pending.outbox_status == "PENDING_RECONCILIATION"
    assert len(h.ledger.effects(message.outbox_id)) == effect_count
    assert pending.attempt_count == pending.lease_fence == (2 if checkpoint == "after_claim" else 1)
    if checkpoint == "after_claim":
        assert h.worker_repository.complete_outbox(message.outbox_id, worker_id="batch-a-worker", lease_fence=1) is False
    # Complete known effects without send; absence authorizes exactly one next try.
    resolved = resolve(h, message, retry=effect_count == 0)
    if effect_count:
        assert resolved.durable_status == "SUCCEEDED"
        assert h.finish(h.spawn())["result"]["status"] == "IDLE"
    else:
        assert resolved.durable_status == "FAILED_RETRYABLE"
        assert h.finish(h.spawn())["result"]["status"] == "COMPLETED"
    assert len(h.ledger.effects(message.outbox_id)) == 1
    emit("CRASH_RESTART_PASS", checkpoint=checkpoint, killed_pid=child.pid,
         restart_pid=restarted.pid, duplicate_effect_count=0, row=asdict(h.row(message.outbox_id)))


def test_postgres_immediate_restart_committed_atomic_rows_survive_rollback_absent(batch_a):
    h = batch_a
    committed, rolled_back = h.message(), h.message()
    receipt_ok = make_receipt(key="BATCH-A:COMMIT:" + uuid4().hex)
    receipt_absent = make_receipt(key="BATCH-A:ROLLBACK:" + uuid4().hex)
    with h.repository.transaction() as tx:
        tx.register_command_receipt(receipt_ok)
        event_id = tx.append_domain_event_once(command_id=receipt_ok.command_id,
            aggregate_type=committed.aggregate_type, aggregate_id=committed.aggregate_id, aggregate_version=1,
            event_type=committed.event_type, actor_party_id=None, payload=committed.payload,
            occurred_at=receipt_ok.decided_at)
        committed = replace(committed, domain_event_id=event_id)
        tx.enqueue_outbox(committed)
    with pytest.raises(RuntimeError, match="rollback-disposable"):
        with h.repository.transaction() as tx:
            tx.register_command_receipt(receipt_absent)
            absent_event_id = tx.append_domain_event_once(command_id=receipt_absent.command_id,
                aggregate_type=rolled_back.aggregate_type, aggregate_id=rolled_back.aggregate_id, aggregate_version=1,
                event_type=rolled_back.event_type, actor_party_id=None, payload=rolled_back.payload,
                occurred_at=receipt_absent.decided_at)
            tx.enqueue_outbox(replace(rolled_back, domain_event_id=absent_event_id))
            raise RuntimeError("rollback-disposable")
    child = h.spawn(checkpoint="after_provider")
    h.kill(child)
    pending_before = h.row(committed.outbox_id)
    h.restart_postgres()
    assert h.row(committed.outbox_id) == pending_before
    assert h.row(rolled_back.outbox_id) is None
    with h.repository.transaction() as tx:
        assert tx._connection.execute("SELECT domain_event_id FROM propertyai.domain_event WHERE command_id=%s", (receipt_ok.command_id,)).fetchone()["domain_event_id"] == event_id
        assert tx._connection.execute("SELECT domain_event_id FROM propertyai.domain_event WHERE command_id=%s", (receipt_absent.command_id,)).fetchone() is None
        assert tx.find_command_receipt(receipt_ok.authority_scope_code, receipt_ok.command_type, receipt_ok.idempotency_key) == receipt_ok.command_id
        assert tx.find_command_receipt(receipt_absent.authority_scope_code, receipt_absent.command_type, receipt_absent.idempotency_key) is None
    assert resolve(h, committed).durable_status == "SUCCEEDED"
    assert h.finish(h.spawn())["result"]["status"] == "IDLE"
    assert len(h.ledger.effects(committed.outbox_id)) == 1
    emit("FSYNC_DURABILITY_PASS", committed_outbox=str(committed.outbox_id),
         committed_command=str(receipt_ok.command_id), absent_outbox=str(rolled_back.outbox_id),
         absent_command=str(receipt_absent.command_id), committed_event=event_id,
         absent_event=absent_event_id, duplicate_effect_count=0)


@pytest.mark.parametrize("classification", list(ProviderEvidenceClass))
def test_general_evidence_classification_and_explicit_retry(batch_a, classification):
    h = batch_a
    message = h.enqueue()
    child = h.spawn(checkpoint="before_provider")
    h.kill(child)
    before = h.row(message.outbox_id)
    evidence = ProviderEvidence(classification, row_binding_sha256(before), datetime.now(UTC), "synthetic-proof",
        external_effect_id="fake:existing" if classification is ProviderEvidenceClass.CONFIRMED_EFFECT else None,
        complete_no_effect_proof=True, in_flight_effects_excluded=True)
    provider = SimpleNamespace(inspect=lambda row: evidence)
    result = resolve(h, message, provider=provider)
    if classification is ProviderEvidenceClass.CONFIRMED_EFFECT:
        assert result.applied and h.row(message.outbox_id).outbox_status == "SUCCEEDED"
    else:
        assert not result.applied and h.row(message.outbox_id) == before
    assert h.ledger.effects(message.outbox_id) == []
    assert result.diagnostic_owner == "batch-a:recovery" and len(result.evidence_sha256) == 64


@pytest.mark.parametrize("damage", ["row", "attempt", "fence", "old", "future", "naive", "no_effect_inflight", "no_effect_incomplete", "missing_receipt"])
def test_reconciliation_malformed_wrong_or_stale_evidence_cannot_mutate(batch_a, damage):
    h = batch_a
    message = h.enqueue()
    child = h.spawn(checkpoint="before_provider")
    h.kill(child)
    before = h.row(message.outbox_id)
    evidence = h.ledger.inspect(before)
    kwargs = {}
    if damage == "row": evidence = replace(evidence, row_binding_sha256="0" * 64)
    elif damage == "attempt": kwargs["attempt"] = before.attempt_count + 1
    elif damage == "fence": kwargs["fence"] = before.lease_fence + 1
    elif damage == "old": evidence = replace(evidence, observed_at=datetime.now(UTC) - timedelta(hours=1))
    elif damage == "future": evidence = replace(evidence, observed_at=datetime.now(UTC) + timedelta(hours=1))
    elif damage == "naive": evidence = replace(evidence, observed_at=datetime.now())
    elif damage == "no_effect_inflight": evidence = replace(evidence, in_flight_effects_excluded=False)
    elif damage == "no_effect_incomplete": evidence = replace(evidence, complete_no_effect_proof=False)
    elif damage == "missing_receipt": evidence = replace(evidence, classification=ProviderEvidenceClass.CONFIRMED_EFFECT)
    with pytest.raises(OutboxReconciliationError):
        resolve(h, message, provider=SimpleNamespace(inspect=lambda row: evidence), retry=True, **kwargs)
    assert h.row(message.outbox_id) == before and h.ledger.effects(message.outbox_id) == []


def test_no_effect_retry_permit_cannot_survive_an_extra_reclaim(batch_a):
    h = batch_a
    message = h.enqueue()
    h.kill(h.spawn(checkpoint="before_provider"))
    assert resolve(h, message, retry=True).durable_status == "FAILED_RETRYABLE"
    child = h.spawn(checkpoint="after_claim")
    h.kill(child)
    wait_until(lambda: h.full_row(message.outbox_id)["lease_until"] < datetime.now(UTC), timeout=3)
    assert h.finish(h.spawn())["result"]["status"] == "PENDING_RECONCILIATION"
    assert h.row(message.outbox_id).attempt_count == 3
    assert not h.ledger.effects(message.outbox_id)


@pytest.mark.parametrize("history", [None, "consumed-cutover-operation", "completed", "malformed-old-flag"])
def test_normal_startup_ignores_historical_cutover_objects_without_provider_access(batch_a, history):
    h = batch_a
    result = h.finish(h.spawn(mode="startup", history=history, outcome="unavailable"))
    assert result["results"][0]["status"] == "IDLE"


@pytest.mark.parametrize("outcome,expected", [("success", "COMPLETED"), ("unavailable", "PENDING_RECONCILIATION"), ("ambiguous", "PENDING_RECONCILIATION")])
def test_normal_startup_actionable_work_provider_state_is_not_admission_authority(batch_a, outcome, expected):
    h = batch_a
    message = h.enqueue()
    result = h.finish(h.spawn(mode="startup", history="consumed", outcome=outcome))
    assert result["results"][0]["status"] == expected
    assert len(h.ledger.effects(message.outbox_id)) <= 1


@pytest.mark.parametrize("damage", ["source", "config", "pid"])
def test_actual_runtime_identity_mismatch_prevents_claim_or_send(batch_a, damage):
    h = batch_a
    message = h.enqueue()
    child = h.spawn(damage=damage)
    h.finish(child, expected=1)
    assert h.row(message.outbox_id).outbox_status == "PENDING"
    assert h.row(message.outbox_id).attempt_count == 0
    assert not h.ledger.effects(message.outbox_id)


def _forced_unique_overlap(h, operation, winner_value, loser_value):
    """Hold winner uncommitted; observe PostgreSQL loser lock wait before release."""
    winner_ready, release_winner, loser_started = threading.Event(), threading.Event(), threading.Event()
    pids = {}
    def winner():
        with h.repository.transaction() as tx:
            pids["winner"] = tx._connection.info.backend_pid
            result = operation(tx, winner_value)
            winner_ready.set()
            assert release_winner.wait(10)
            return result
    def loser():
        assert winner_ready.wait(10)
        with h.repository.transaction() as tx:
            pids["loser"] = tx._connection.info.backend_pid
            loser_started.set()
            return operation(tx, loser_value)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.submit(winner), executor.submit(loser)
        try:
            assert loser_started.wait(10)
            with psycopg.connect(dsn(h.config, h.config["superuser"]), autocommit=True) as observer:
                def blocked():
                    row = observer.execute("SELECT pg_blocking_pids(%s),state,wait_event_type,wait_event FROM pg_stat_activity WHERE pid=%s", (pids["loser"], pids["loser"])).fetchone()
                    return row if row and pids["winner"] in row[0] and row[1] == "active" and row[2] == "Lock" else None
                overlap = wait_until(blocked)
                emit("FORCED_POSTGRES_OVERLAP", winner_pid=pids["winner"], loser_pid=pids["loser"], overlap=overlap)
        finally:
            release_winner.set()
        return first.result(timeout=10), second.result(timeout=10)


def test_forced_duplicate_command_receipt_winner_loser_identity(batch_a):
    h = batch_a
    value = make_receipt(key="BATCH-A:RACE:" + uuid4().hex)
    first, second = _forced_unique_overlap(h, lambda tx, item: tx.register_command_receipt(item), value, replace(value, command_id=uuid4()))
    assert first.command_id == second.command_id == value.command_id
    assert first.reused is False and second.reused is True
    with h.repository.transaction() as tx:
        assert tx._connection.execute("SELECT count(*) AS n FROM propertyai.command_receipt WHERE idempotency_key=%s", (value.idempotency_key,)).fetchone()["n"] == 1


def test_forced_duplicate_outbox_same_event_identity(batch_a):
    h = batch_a
    value = h.message()
    other = h.message(idempotency_key=value.idempotency_key, aggregate_id=value.aggregate_id, payload=value.payload)
    first, second = _forced_unique_overlap(h, lambda tx, item: tx.enqueue_outbox(item), value, other)
    assert first.outbox_id == second.outbox_id == value.outbox_id
    assert first.reused is False and second.reused is True
    assert h.row(other.outbox_id) is None
    assert h.row(value.outbox_id).attempt_count == h.row(value.outbox_id).lease_fence == 0


def test_forced_first_create_converges_one_identity_and_revision(batch_a):
    h = batch_a
    value = snapshot_for(h.repository)
    first, second = _forced_unique_overlap(h, lambda tx, item: tx.ingest_canonical_reservation(item), value, value)
    assert first.reservation_id == second.reservation_id == value.reservation_id
    assert first.created and not second.created and first.source_version == second.source_version == 1
    with h.repository.transaction() as tx:
        assert tx._connection.execute("SELECT count(*) AS n FROM propertyai.reservation WHERE reservation_id=%s", (value.reservation_id,)).fetchone()["n"] == 1


def test_forced_claim_skip_locked_and_stale_fence_after_reclaim(batch_a):
    h = batch_a
    message = h.enqueue()
    with psycopg.connect(dsn(h.config, h.config["superuser"]), row_factory=dict_row) as winner:
        winner.execute("SET ROLE propertyai_async_worker")
        claim = winner.execute("SELECT * FROM propertyai.claim_integration_outbox('winner',1,1)").fetchone()
        assert claim["outbox_id"] == message.outbox_id
        # Winner transaction is still open and owns the row lock while the actual
        # second repository executes its conflicting SKIP LOCKED claim operation.
        loser = h.worker_repository.claim_outbox("loser", limit=1, lease_seconds=1)
        assert not loser
        emit("FORCED_CLAIM_OVERLAP", winner_pid=winner.info.backend_pid, row_locked=True, loser_count=0)
    wait_until(lambda: h.full_row(message.outbox_id)["lease_until"] < datetime.now(UTC), timeout=3)
    successor = h.worker_repository.claim_outbox("successor", limit=1, lease_seconds=1)[0]
    assert successor.attempt_count == successor.lease_fence == 2
    assert h.worker_repository.complete_outbox(message.outbox_id, worker_id="winner", lease_fence=1) is False
    assert h.worker_repository.mark_outbox_pending_reconciliation(message.outbox_id, worker_id="winner", lease_fence=1, error_code="stale") is False
    assert h.full_row(message.outbox_id)["lease_owner"] == "successor"


def test_forced_reconciliation_cas_only_one_resolution_wins(batch_a):
    h = batch_a
    message = h.enqueue()
    h.kill(h.spawn(checkpoint="before_provider"))
    before = h.row(message.outbox_id)
    with psycopg.connect(dsn(h.config, h.config["superuser"]), row_factory=dict_row) as first:
        first.execute("SET ROLE propertyai_async_worker")
        assert first.execute("SELECT propertyai.resolve_outbox_reconciliation(%s,%s,'SUCCEEDED','fake:winner','test',0) AS applied", (message.outbox_id, before.lease_fence)).fetchone()["applied"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(h.worker_repository.resolve_outbox_reconciliation_exact, before,
                                     resolution="SUCCEEDED", external_effect_id="fake:loser", error_code="test")
            try:
                with psycopg.connect(dsn(h.config, h.config["superuser"]), autocommit=True) as observer:
                    overlap = wait_until(lambda: observer.execute("SELECT pid,pg_blocking_pids(pid) FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))", (first.info.backend_pid,)).fetchall())
                    emit("FORCED_RECONCILIATION_OVERLAP", winner_pid=first.info.backend_pid, blocked=overlap)
            finally:
                first.commit()
            assert future.result(timeout=10) is None
    assert h.row(message.outbox_id).external_effect_id == "fake:winner"


def test_expected_topology_includes_non_cleaner_and_excludes_retired_writers():
    value = topology_contract(CleanerRuntimeTopology.POST_CUTOVER_PG)
    assert set(value.services) == {"telegram_cleaner", "gmail_readonly", "health_monitor", "cleaner_pg_outbox"}
    assert {"com.propertyai.cleaning-operations", "com.propertyai.cleaning-completion"} <= value.retired_labels
    assert not set(value.recovery_targets) & value.retired_labels


@pytest.mark.parametrize("authority,topology", [("LEGACY", "POST_CUTOVER_PG"), ("POSTGRES", "PRE_CUTOVER"), ("POSTGRES", "AUTO")])
def test_w07_wrong_authority_or_topology_never_constructs_pool(monkeypatch, authority, topology):
    monkeypatch.setattr(service, "PostgresWorkerPool", lambda **kwargs: pytest.fail("pool construction forbidden"))
    with pytest.raises(RuntimeError):
        service.build_worker(environment={service.AUTHORITY_ENV: authority, "PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY": topology})


@pytest.mark.parametrize("writer", ["W04", "W05"])
@pytest.mark.parametrize("environment", [
    {"PROPERTYAI_CLEANER_AUTHORITY": "POSTGRES"},
    {"PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY": "POST_CUTOVER_PG"},
    {"PROPERTYAI_CLEANER_AUTHORITY": "LEGACY", "PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY": "POST_CUTOVER_PG"},
    {"PROPERTYAI_CLEANER_AUTHORITY": "POSTGRES", "PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY": "PRE_CUTOVER"},
    {"PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY": "INVALID"},
])
def test_accidental_legacy_entrypoint_cannot_read_credentials_or_send(monkeypatch, writer, environment):
    from telegram_approval import send_cleaning_operations, send_due_completions
    module = send_cleaning_operations if writer == "W04" else send_due_completions
    for key in ("PROPERTYAI_CLEANER_AUTHORITY", "PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(module, "_validate_send_role_config", lambda: pytest.fail("retired service reached credential/provider boundary"))
    with pytest.raises(RuntimeError, match="RETIRED_CLEANER|CONFIG_INVALID"):
        module.run(send=True)


@pytest.mark.parametrize("writer", ["W01", "W02", "W03", "W06", "W07"])
def test_topology_negative_guard_does_not_reject_other_current_services(writer):
    from propertyai_core.runtime.cleaner_topology import assert_legacy_cleaner_writer_allowed
    assert_legacy_cleaner_writer_allowed(writer, {"PROPERTYAI_CLEANER_AUTHORITY": "POSTGRES",
                                                "PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY": "POST_CUTOVER_PG"})


def test_reconciliation_loses_real_dcs_lease_during_evidence_no_mutation(batch_a):
    h = batch_a
    message = h.enqueue()
    h.kill(h.spawn(checkpoint="before_provider"))
    before = h.row(message.outbox_id)
    def inspect(row):
        evidence = h.ledger.inspect(row)
        h.expire_global_lease()
        return evidence
    with pytest.raises(global_writer.ProductionWriterError):
        resolve(h, message, provider=SimpleNamespace(inspect=inspect), retry=True)
    assert h.row(message.outbox_id) == before and not h.ledger.effects(message.outbox_id)


def test_provider_ledger_wrong_business_effect_is_conflict_not_success(batch_a):
    h = batch_a
    message = h.enqueue()
    h.kill(h.spawn(checkpoint="before_provider"))
    before = h.row(message.outbox_id)
    h.ledger.record(replace(before, aggregate_id=uuid4()))
    result = resolve(h, message)
    assert result.classification is ProviderEvidenceClass.CONFLICTING_EVIDENCE
    assert not result.applied and h.row(message.outbox_id) == before


def test_worker_repeated_reconciliation_does_not_resolve_completed_row_again(batch_a):
    h = batch_a
    message = h.enqueue()
    h.kill(h.spawn(checkpoint="after_provider"))
    assert resolve(h, message).applied
    before = h.row(message.outbox_id)
    with pytest.raises(OutboxReconciliationError, match="EXPECTATION_MISMATCH"):
        resolve(h, message)
    assert h.row(message.outbox_id) == before and len(h.ledger.effects(message.outbox_id)) == 1


def test_actual_calendar_noop_receipt_completes_without_external_effect(batch_a):
    from propertyai_core.runtime.cleaner_projection_adapters import CalendarCurrentStateProjectionAdapter
    from propertyai_core.runtime.postgres_outbox_worker import CleanerPostgresOutboxWorker
    h = batch_a
    message = h.enqueue(event_type="RESERVATION_CANCELLED", destination_type="CALENDAR_PROJECTION", payload={})
    sentinel = SimpleNamespace(find=lambda *a: pytest.fail("no Cleaning means no provider access"))
    adapter = CalendarCurrentStateProjectionAdapter(h.worker_repository, sentinel)
    result = CleanerPostgresOutboxWorker(h.worker_repository, adapter, worker_id="batch-a-worker").run_once()
    row = h.row(message.outbox_id)
    assert result.status == "COMPLETED" and row.outbox_status == "SUCCEEDED"
    assert row.external_effect_id is None and row.last_error_code.startswith("DELIVERY_NO_EFFECT_CONFIRMED:")
    assert h.ledger.effects(message.outbox_id) == []


def test_projection_receipt_port_and_lost_commit_ack_recovered_from_exact_durable_row(batch_a):
    from propertyai_core.runtime.cleaner_projection_adapters import ProjectionDeliveryReceipt
    from propertyai_core.runtime.postgres_outbox_worker import CleanerPostgresOutboxWorker
    from propertyai_core.adapters.postgres.repository import PostgresOutboxWorkerRepository
    h = batch_a
    message = h.enqueue()
    class LostAcknowledgement(PostgresOutboxWorkerRepository):
        def resolve_outbox_reconciliation_exact(self, *args, **kwargs):
            assert super().resolve_outbox_reconciliation_exact(*args, **kwargs) is not None
            raise OSError("synthetic-commit-ack-lost")
    repository = LostAcknowledgement(h.worker_pool)
    def deliver(claim):
        return ProjectionDeliveryReceipt(h.ledger.record(h.row(claim.outbox_id)))
    worker = CleanerPostgresOutboxWorker(repository, SimpleNamespace(deliver=deliver), worker_id="batch-a-worker")
    result = worker.run_once()
    assert result.status == "COMPLETED" and result.diagnostic_code == "COMPLETED_BY_DURABLE_READBACK"
    assert h.row(message.outbox_id).outbox_status == "SUCCEEDED"
    assert h.finish(h.spawn())["result"]["status"] == "IDLE"
    assert len(h.ledger.effects(message.outbox_id)) == 1


def test_confirmed_no_effect_at_attempt_limit_is_terminal_not_rescheduled(batch_a):
    h = batch_a
    message = h.enqueue(max_attempts=1)
    h.kill(h.spawn(checkpoint="before_provider"))
    result = resolve(h, message, retry=True)
    assert result.durable_status == "DEAD_LETTER"
    assert "OWNER:batch-a:recovery" in h.row(message.outbox_id).last_error_code
    assert h.finish(h.spawn())["result"]["status"] == "IDLE"
    assert not h.ledger.effects(message.outbox_id)


@pytest.mark.parametrize("writer", ["W04", "W05"])
def test_cached_legacy_coordinator_revalidates_retirement_before_writer_acquire(batch_a, monkeypatch, writer):
    h = batch_a
    monkeypatch.delenv("PROPERTYAI_CLEANER_AUTHORITY", raising=False)
    monkeypatch.delenv("PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY", raising=False)
    install_coordinator(h.config, writer_code=writer)
    with GlobalWriterLeaseClient(h.case / "control.sqlite3") as client:
        before = client.get()
    monkeypatch.setenv("PROPERTYAI_CLEANER_AUTHORITY", "POSTGRES")
    with pytest.raises(global_writer.ProductionWriterError, match="RETIRED_CLEANER_WRITER_FORBIDDEN"):
        with global_writer.mutation_scope(writer, unit_id="accidental-legacy-start",
                operation_class="TEST_LEGACY_NEGATIVE", target="owned-only"):
            pytest.fail("retired writer entered mutation scope")
    with GlobalWriterLeaseClient(h.case / "control.sqlite3") as client:
        assert client.get() == before


def test_w03_missing_pg_service_fails_before_credentials_and_legacy_fallback(monkeypatch):
    from propertyai_core.config.cleaner_authority import CleanerAuthorityConfig
    from telegram_approval import cleaner_bot_runtime
    monkeypatch.setattr(cleaner_bot_runtime.CleanerCredentialProvider, "bot_context",
                        lambda self: pytest.fail("missing PG service reached provider credential"))
    with pytest.raises(RuntimeError, match="requires the Product PG application service"):
        cleaner_bot_runtime.build_poller(environment={}, authority_config=CleanerAuthorityConfig(
            authority="POSTGRES", pg_ingress_enabled=True, authority_epoch=2), postgres_service=None)


def test_w01_pg_unavailable_cannot_fallback_to_legacy_business_writer(monkeypatch, tmp_path):
    from propertyai_core.config.cleaner_authority import CleanerAuthorityConfig
    from gmail_ingest import booking_bridge
    mapping = tmp_path / "synthetic-mappings.json"
    mapping.write_text('{"listings": {}}')
    queue = tmp_path / "queue"
    queue.mkdir()
    (queue / "synthetic.json").write_text('{"status":"PENDING_NOTION_READ"}')
    monkeypatch.setattr(booking_bridge, "MAPPINGS_PATH", mapping)
    monkeypatch.setattr(booking_bridge, "QUEUE_DIR", queue)
    monkeypatch.setattr(booking_bridge, "process_one", lambda *a, **k: pytest.fail("unsafe legacy fallback"))
    @contextmanager
    def unavailable(authority):
        raise RuntimeError("test-owned-pg-unavailable")
        yield
    monkeypatch.setattr(booking_bridge, "build_cleaner_postgres_application", unavailable)
    with pytest.raises(RuntimeError, match="test-owned-pg-unavailable"):
        booking_bridge.process_pending(authority_config=CleanerAuthorityConfig(
            authority="POSTGRES", pg_ingress_enabled=True, authority_epoch=2))


def test_fake_provider_no_effect_proof_requires_reaped_owned_workers(batch_a):
    h = batch_a
    message = h.enqueue()
    child = h.spawn(checkpoint="before_provider")
    row = h.row(message.outbox_id)
    assert child.poll() is None and not h.ledger.effects(message.outbox_id)
    live_evidence = h.ledger.inspect(row)
    assert live_evidence.classification is ProviderEvidenceClass.AMBIGUOUS
    assert not live_evidence.complete_no_effect_proof and not live_evidence.in_flight_effects_excluded
    h.kill(child)
    reaped_evidence = h.ledger.inspect(row)
    assert reaped_evidence.classification is ProviderEvidenceClass.CONFIRMED_NO_EFFECT
    assert reaped_evidence.complete_no_effect_proof and reaped_evidence.in_flight_effects_excluded

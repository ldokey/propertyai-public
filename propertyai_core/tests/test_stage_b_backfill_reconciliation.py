from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import count
from uuid import UUID, uuid4

import pytest

from propertyai_core.stage_b.backfill import BackfillEngine, InMemoryMigrationRepository
from propertyai_core.stage_b.models import (
    CanonicalRecord,
    ConcurrentMigrationError,
    ImmutableIdentityConflict,
    MigrationObligation,
    OldSnapshotError,
    Population,
    ReviewRequired,
    SnapshotManifest,
    SourceObservation,
)
from propertyai_core.stage_b.normalize import semantic_hash
from propertyai_core.stage_b.obligations import EFFECT_ONLY, STATE_MUTATING, obligation
from propertyai_core.stage_b.snapshot import FreshSourceValidator
from propertyai_core.stage_b.reconciliation import (
    BLOCKING_GATES,
    ReadinessTracker,
    ReconciliationContext,
    ReconciliationEngine,
    evaluate_final_delta_preflight,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 5, 1, tzinfo=UTC)
_MANIFEST_SEQUENCE = count()


def record(
    source_key: str,
    aggregate_id: UUID,
    payload: dict,
    *,
    entity_type: str = "reservation",
    immutable_fields=(
        "reservation_id",
        "reservation_code",
        "property_id",
        "rental_unit_id",
        "source_channel",
        "external_reservation_id",
    ),
):
    return CanonicalRecord(
        entity_type,
        aggregate_id,
        source_key,
        semantic_hash(payload),
        payload,
        tuple(immutable_fields),
    )


def reservation_payload(reservation_id=None, status="CONFIRMED", checkout=None):
    reservation_id = reservation_id or uuid4()
    return {
        "reservation_id": reservation_id,
        "reservation_code": f"RSV-{reservation_id.hex}",
        "property_id": uuid4(),
        "rental_unit_id": None,
        "source_channel": "LEGACY_MASTER",
        "external_reservation_id": f"LEG-{reservation_id.hex}",
        "reservation_status": status,
        "check_in_at": NOW,
        "check_out_at": checkout or NOW + timedelta(days=2),
    }


class _FixtureSnapshotSource:
    def __init__(self, source_type: str, observations):
        self.source_type = source_type
        self.runtime_reference = "fixture-backfill:" + source_type
        self.rows = {
            observation.durable_identity: observation
            for observation in observations
        }
        self.scan_count = 0

    def scan(self):
        self.scan_count += 1
        return tuple(self.rows[key] for key in sorted(self.rows))

    def point_read(self, durable_identity):
        return self.rows.get(durable_identity)

    @property
    def file_content_hashes(self):
        return {}


def _fixture_runtime_reference(observations) -> str:
    source_types = sorted({observation.source_type for observation in observations.values()})
    if not source_types:
        source_types = ["empty"]
    return "|".join("fixture-backfill:" + source_type for source_type in source_types)


def _fixture_validator(manifest: SnapshotManifest) -> FreshSourceValidator:
    grouped = {}
    for observation in manifest.observations.values():
        grouped.setdefault(observation.source_type, []).append(observation)
    if not grouped:
        grouped["empty"] = []
    return FreshSourceValidator(
        [_FixtureSnapshotSource(source_type, observations) for source_type, observations in sorted(grouped.items())],
        frozen_snapshot=manifest,
    )


class _SourceBackedBackfillHarness:
    def __init__(self, repository):
        self.repository = repository

    def apply(self, manifest, population, **kwargs):
        return BackfillEngine(
            self.repository, fresh_source_validator=_fixture_validator(manifest)
        ).apply(manifest, population, **kwargs)


def backfill_engine(repository):
    return _SourceBackedBackfillHarness(repository)


def manifest_for(records, *, predecessor=None, snapshot_id=None):
    observations = {
        item.source_key: SourceObservation(
            item.source_key.split(":", 1)[0],
            item.source_key.split(":", 1)[1],
            item.payload,
        )
        for item in records
    }
    hashes = {item.source_key: item.semantic_hash for item in records}
    scan_started = NOW + timedelta(seconds=10 * next(_MANIFEST_SEQUENCE))
    return SnapshotManifest(
        run_id=uuid4(),
        snapshot_id=snapshot_id or uuid4(),
        predecessor_snapshot_id=predecessor,
        scan_started_at=scan_started,
        scan_completed_at=scan_started + timedelta(seconds=1),
        source_runtime_reference=_fixture_runtime_reference(observations),
        source_row_identities=tuple(observations),
        source_semantic_hashes=hashes,
        file_content_hashes={},
        manifest_hash=semantic_hash(
            {
                "source_row_identities": sorted(observations),
                "source_semantic_hashes": hashes,
                "file_content_hashes": {},
            }
        ),
        observations=observations,
    )


def test_idempotent_apply_and_reservation_revision_semantics():
    repository = InMemoryMigrationRepository()
    payload = reservation_payload()
    item = record("reservation:one", payload["reservation_id"], payload)
    snapshot = manifest_for([item])
    engine = backfill_engine(repository)
    first = engine.apply(snapshot, Population((item,)))
    second = engine.apply(snapshot, Population((item,)))
    assert (first.applied, first.noop) == (1, 0)
    assert (second.applied, second.noop) == (0, 1)
    assert repository.targets[item.aggregate_id]["source_version"] == 1

    changed_payload = dict(payload, reservation_status="CANCELLED")
    changed = record(item.source_key, item.aggregate_id, changed_payload)
    next_snapshot = manifest_for([changed], predecessor=snapshot.snapshot_id)
    third = engine.apply(next_snapshot, Population((changed,)))
    assert third.applied == 1
    assert repository.targets[item.aggregate_id]["source_version"] == 2
    assert engine.apply(next_snapshot, Population((changed,))).noop == 1
    assert repository.targets[item.aggregate_id]["source_version"] == 2


def test_immutable_identity_conflict_fails_closed():
    repository = InMemoryMigrationRepository()
    payload = reservation_payload()
    item = record("reservation:one", payload["reservation_id"], payload)
    first = manifest_for([item])
    backfill_engine(repository).apply(first, Population((item,)))
    conflict_payload = dict(payload, reservation_code="DIFFERENT")
    conflict = record(item.source_key, item.aggregate_id, conflict_payload)
    second = manifest_for([conflict], predecessor=first.snapshot_id)
    with pytest.raises(ImmutableIdentityConflict):
        backfill_engine(repository).apply(second, Population((conflict,)))


def test_crash_resume_uses_persisted_receipts_and_bindings():
    repository = InMemoryMigrationRepository()
    payloads = [reservation_payload(), reservation_payload()]
    records = tuple(
        record(f"reservation:{index}", payload["reservation_id"], payload)
        for index, payload in enumerate(payloads)
    )
    snapshot = manifest_for(records)
    engine = backfill_engine(repository)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        engine.apply(snapshot, Population(records), crash_after=1)
    resumed = engine.apply(snapshot, Population(records))
    assert (resumed.applied, resumed.noop) == (1, 1)
    assert len(repository.receipts) == 2
    assert len(repository.bindings) == 2


def test_old_snapshot_and_concurrent_writer_are_rejected():
    repository = InMemoryMigrationRepository()
    payload = reservation_payload()
    item = record("reservation:one", payload["reservation_id"], payload)
    first = manifest_for([item])
    engine = backfill_engine(repository)
    engine.apply(first, Population((item,)))
    changed = record(
        item.source_key,
        item.aggregate_id,
        dict(payload, check_out_at=payload["check_out_at"] + timedelta(hours=1)),
    )
    second = manifest_for([changed], predecessor=first.snapshot_id)
    engine.apply(second, Population((changed,)))
    with pytest.raises(OldSnapshotError):
        engine.apply(first, Population((item,)))

    repository.targets[item.aggregate_id]["reservation_status"] = "CANCELLED"
    with pytest.raises(ConcurrentMigrationError):
        engine.apply(second, Population((changed,)))


def test_preexisting_unbound_target_is_rejected():
    repository = InMemoryMigrationRepository()
    payload = reservation_payload()
    item = record("reservation:one", payload["reservation_id"], payload)
    repository.targets[item.aggregate_id] = dict(payload, source_version=1)
    with pytest.raises(ConcurrentMigrationError):
        backfill_engine(repository).apply(manifest_for([item]), Population((item,)))


def test_r01_to_r15_clean_fixture_and_r15e_is_informational():
    repository = InMemoryMigrationRepository()
    payload = reservation_payload()
    item = record("reservation:one", payload["reservation_id"], payload)
    snapshot = manifest_for([item])
    effect = obligation("effect:one", EFFECT_ONLY, "legacy delivery pending")
    population = Population((item,), (effect,))
    backfill_engine(repository).apply(snapshot, population)
    report = ReconciliationEngine(repository).reconcile(snapshot, population)
    assert tuple(gate.gate for gate in report.gates) == BLOCKING_GATES
    assert all(gate.blocking_count == 0 for gate in report.gates)
    assert report.clean
    assert report.effect_only_pending_count == 1


@pytest.mark.parametrize(
    ("context", "gate"),
    [
        (ReconciliationContext(snapshot_integrity_findings=("race",)), "R01_SOURCE_SNAPSHOT_INTEGRITY"),
        (ReconciliationContext(relation_findings=("broken fk",)), "R03_REFERENCE_AND_RELATION_INTEGRITY"),
        (ReconciliationContext(economics_findings=("fee mismatch",)), "R11_ECONOMICS_EQUIVALENCE"),
        (ReconciliationContext(privilege_findings=("SET ROLE allowed",)), "R13_PRE_CUTOVER_ZERO_EFFECT_AND_PRIVILEGE"),
    ],
)
def test_reconciliation_negative_context_fixtures(context, gate):
    repository = InMemoryMigrationRepository()
    snapshot = manifest_for([])
    report = ReconciliationEngine(repository).reconcile(snapshot, Population(()), context=context)
    by_gate = {item.gate: item for item in report.gates}
    assert by_gate[gate].blocking_count == 1
    assert not report.clean


def test_r02_r12_r14_r15_negative_fixtures():
    repository = InMemoryMigrationRepository()
    payload = reservation_payload()
    item = record("reservation:one", payload["reservation_id"], payload)
    snapshot = manifest_for([item])
    population = Population(
        (item,),
        (obligation("reservation:one", STATE_MUTATING, "missing assignment ancestry"),),
        ("collision",),
    )
    # Seed without passing the collision-bearing population to the apply engine.
    backfill_engine(repository).apply(snapshot, Population((item,)))
    repository.targets[item.aggregate_id]["reservation_status"] = "CANCELLED"
    report = ReconciliationEngine(repository).reconcile(snapshot, population)
    blocked = {gate.gate for gate in report.gates if gate.blocking_count}
    assert "R02_DETERMINISTIC_IDENTITY_COLLISION" in blocked
    assert "R12_UNATTRIBUTED_OBSERVED_MUTATION_COUNT" in blocked
    assert "R15_UNRESOLVED_STATE_MUTATING_OBLIGATION" in blocked

    changed = record(item.source_key, item.aggregate_id, dict(payload, reservation_status="CANCELLED"))
    next_snapshot = manifest_for([changed], predecessor=snapshot.snapshot_id)
    report = ReconciliationEngine(repository).reconcile(next_snapshot, Population((changed,)))
    by_gate = {gate.gate: gate for gate in report.gates}
    assert by_gate["R14_UNAPPLIED_SOURCE_DELTA"].blocking_count == 1


def test_r13_zero_effect_and_authority_proof_negative_fixture():
    repository = InMemoryMigrationRepository(
        authority_epoch_mutations=1,
        domain_event_mutations=1,
        scheduled_action_mutations=1,
        outbox_mutations=1,
        migration_role_safe=False,
    )
    snapshot = manifest_for([])
    report = ReconciliationEngine(repository).reconcile(snapshot, Population(()))
    gate = next(
        item for item in report.gates if item.gate == "R13_PRE_CUTOVER_ZERO_EFFECT_AND_PRIVILEGE"
    )
    assert gate.blocking_count == 5


def test_r14_detects_removed_source_binding_until_tombstone_is_handled():
    repository = InMemoryMigrationRepository()
    payload = reservation_payload()
    item = record("reservation:one", payload["reservation_id"], payload)
    first = manifest_for([item])
    backfill_engine(repository).apply(first, Population((item,)))
    empty = manifest_for([], predecessor=first.snapshot_id)
    report = ReconciliationEngine(repository).reconcile(empty, Population(()))
    r14 = next(gate for gate in report.gates if gate.gate == "R14_UNAPPLIED_SOURCE_DELTA")
    assert r14.blocking_count == 1
    assert "deletion/tombstone" in r14.facts[0]


def test_s0_s1_s2_delta_flow_and_two_independent_clean_passes():
    repository = InMemoryMigrationRepository()
    engine = backfill_engine(repository)
    reconciler = ReconciliationEngine(repository)
    tracker = ReadinessTracker()
    payload = reservation_payload()
    previous = None
    manifests = []
    versions = []
    for offset in (0, 1, 2):
        current_payload = dict(
            payload, check_out_at=payload["check_out_at"] + timedelta(hours=offset)
        )
        item = record("reservation:one", payload["reservation_id"], current_payload)
        manifest = manifest_for(
            [item], predecessor=None if previous is None else previous.snapshot_id
        )
        engine.apply(manifest, Population((item,)))
        report = reconciler.reconcile(manifest, Population((item,)))
        assert report.clean
        tracker.record(manifest, report)
        manifests.append(manifest)
        versions.append(repository.targets[item.aggregate_id]["source_version"])
        previous = manifest
    assert versions == [1, 2, 3]
    readiness = tracker.readiness()
    assert readiness.ready
    assert readiness.clean_cycle_count == 3
    final = reconciler.reconcile(manifests[-1], Population((item,)))
    r14 = next(gate for gate in final.gates if gate.gate == "R14_UNAPPLIED_SOURCE_DELTA")
    assert r14.blocking_count == 0
    assert repository.safety_counters()["authority_epoch_mutations"] == 0
    assert repository.safety_counters()["domain_event_mutations"] == 0
    assert repository.safety_counters()["scheduled_action_mutations"] == 0
    assert repository.safety_counters()["outbox_mutations"] == 0
    preflight = evaluate_final_delta_preflight(
        freeze_acquired=True, final_report=final, final_unapplied_delta_count=0
    )
    assert preflight.decision == "READY_FOR_CONTROLLED_CUTOVER"
    assert preflight.postgres_authority_activation_allowed is False
    aborted = evaluate_final_delta_preflight(
        freeze_acquired=False, final_report=final, final_unapplied_delta_count=0
    )
    assert aborted.decision == "ABORT_CUTOVER"


def test_readiness_requires_the_last_two_cycles_to_be_clean():
    repository = InMemoryMigrationRepository()
    snapshot = manifest_for([])
    reconciler = ReconciliationEngine(repository)
    backfill_engine(repository).apply(snapshot, Population(()))
    clean = reconciler.reconcile(snapshot, Population(()))
    tracker = ReadinessTracker()
    tracker.record(snapshot, clean)
    second = manifest_for([], predecessor=snapshot.snapshot_id)
    backfill_engine(repository).apply(second, Population(()))
    dirty = reconciler.reconcile(
        second,
        Population(()),
        context=ReconciliationContext(snapshot_integrity_findings=("race",)),
    )
    tracker.record(second, dirty)
    third = manifest_for([], predecessor=second.snapshot_id)
    backfill_engine(repository).apply(third, Population(()))
    tracker.record(third, reconciler.reconcile(third, Population(())))
    assert tracker.readiness().ready is False


def test_source_key_target_remap_is_rejected_and_reconciliation_blocks_lineage():
    repository = InMemoryMigrationRepository()
    first_payload = reservation_payload()
    first = record("reservation:remap", first_payload["reservation_id"], first_payload)
    first_manifest = manifest_for([first])
    backfill_engine(repository).apply(first_manifest, Population((first,)))

    second_payload = reservation_payload()
    second = record("reservation:remap", second_payload["reservation_id"], second_payload)
    second_manifest = manifest_for([second], predecessor=first_manifest.snapshot_id)
    with pytest.raises(ImmutableIdentityConflict, match="remap"):
        backfill_engine(repository).apply(second_manifest, Population((second,)))

    binding = repository.binding_for(first.source_key)
    assert binding is not None and binding.aggregate_id == first.aggregate_id
    assert repository.targets.get(second.aggregate_id) is None
    report = ReconciliationEngine(repository).reconcile(second_manifest, Population((second,)))
    blocked = {gate.gate for gate in report.gates if gate.blocking_count}
    assert {
        "R10_LINEAGE_AND_IDEMPOTENCY",
        "R12_UNATTRIBUTED_OBSERVED_MUTATION_COUNT",
        "R14_UNAPPLIED_SOURCE_DELTA",
    } <= blocked


def test_unknown_obligation_category_fails_closed_in_r15():
    repository = InMemoryMigrationRepository()
    manifest = manifest_for([])
    backfill_engine(repository).apply(manifest, Population(()))
    unknown = MigrationObligation(
        obligation_id=uuid4(),
        source_key="legacy:unknown",
        category="UNKNOWN_KIND",
        reason="constructor-level adversarial category",
    )
    report = ReconciliationEngine(repository).reconcile(
        manifest, Population((), (unknown,))
    )
    r15 = next(
        gate for gate in report.gates
        if gate.gate == "R15_UNRESOLVED_STATE_MUTATING_OBLIGATION"
    )
    assert r15.blocking_count == 1
    assert "UNKNOWN_KIND" in r15.facts[0]
    assert report.effect_only_pending_count == 0
    assert not report.clean


def test_population_authority_findings_block_before_any_write():
    repository = InMemoryMigrationRepository()
    manifest = manifest_for([])
    population = Population((), authority_findings=("arbitrary source claimed reservation",))
    with pytest.raises(ImmutableIdentityConflict, match="source authority rejected"):
        backfill_engine(repository).apply(manifest, population)
    assert not repository.snapshot_receipts
    assert not repository.targets


def test_later_delta_cannot_silently_renumber_persisted_local_ancestry():
    repository = InMemoryMigrationRepository()
    cleaning_id = uuid4()
    revision_id = uuid4()
    immutable = (
        "campaign_id",
        "cleaning_id",
        "schedule_revision_id",
        "campaign_no",
        "base_fee_krw",
        "replacement_urgency",
        "urgent_premium_krw",
        "total_agreed_fee_krw",
        "opened_at",
    )
    first_id = uuid4()
    first_payload = {
        "campaign_id": first_id,
        "cleaning_id": cleaning_id,
        "schedule_revision_id": revision_id,
        "campaign_no": 1,
        "base_fee_krw": 50_000,
        "replacement_urgency": "NORMAL",
        "urgent_premium_krw": 0,
        "total_agreed_fee_krw": 50_000,
        "opened_at": NOW + timedelta(hours=2),
    }
    first = record(
        "campaign:first",
        first_id,
        first_payload,
        entity_type="cleaning_offer_campaign",
        immutable_fields=immutable,
    )
    frozen = manifest_for([first])
    backfill_engine(repository).apply(frozen, Population((first,)))
    receipts_before = set(repository.snapshot_receipts)

    older_id = uuid4()
    older_payload = dict(
        first_payload,
        campaign_id=older_id,
        campaign_no=1,
        opened_at=NOW + timedelta(hours=1),
    )
    older = record(
        "campaign:older",
        older_id,
        older_payload,
        entity_type="cleaning_offer_campaign",
        immutable_fields=immutable,
    )
    renumbered_payload = dict(first_payload, campaign_no=2)
    renumbered = record(
        first.source_key,
        first.aggregate_id,
        renumbered_payload,
        entity_type="cleaning_offer_campaign",
        immutable_fields=immutable,
    )
    delta = manifest_for([older, renumbered], predecessor=frozen.snapshot_id)
    with pytest.raises(ReviewRequired, match="would renumber persisted ancestry"):
        backfill_engine(repository).apply(delta, Population((older, renumbered)))
    assert repository.targets[first_id]["campaign_no"] == 1
    assert older_id not in repository.targets
    assert repository.snapshot_receipts == receipts_before

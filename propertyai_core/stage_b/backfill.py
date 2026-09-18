from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Mapping, Protocol
from uuid import UUID

from .models import (
    ApplyDisposition,
    ApplyResult,
    BackfillSummary,
    CanonicalRecord,
    ConcurrentMigrationError,
    ImmutableIdentityConflict,
    OldSnapshotError,
    Population,
    ReviewRequired,
    SnapshotIntegrityError,
    SnapshotManifest,
    records_in_dependency_order,
)
from .normalize import canonical_value, reservation_semantics, semantic_hash
from .population import reservation_next_version
from .snapshot import FreshSourceValidator


@dataclass(frozen=True)
class BindingState:
    source_key: str
    aggregate_id: UUID
    semantic_hash: str
    target_hash: str
    snapshot_id: UUID
    replay_cursor: str
    aggregate_type: str | None = None
    resource_code: str | None = None


class MigrationRepository(Protocol):
    def record_snapshot(self, manifest: SnapshotManifest) -> None: ...

    def snapshot_receipt_exists(self, snapshot_id: UUID) -> bool: ...

    def apply_record(
        self,
        record: CanonicalRecord,
        *,
        snapshot_id: UUID,
        predecessor_snapshot_id: UUID | None,
        run_id: UUID,
        observed_at: datetime,
    ) -> ApplyResult: ...

    def binding_for(self, source_key: str) -> BindingState | None: ...

    def all_bindings(self) -> Mapping[str, BindingState]: ...

    def target_payload(self, record: CanonicalRecord) -> Mapping[str, object] | None: ...

    def receipt_exists(self, snapshot_id: UUID, source_key: str) -> bool: ...

    def safety_counters(self) -> Mapping[str, int | bool]: ...


@dataclass
class InMemoryMigrationRepository:
    """Synthetic reference implementation for deterministic offline testing."""

    targets: dict[UUID, dict[str, object]] = field(default_factory=dict)
    target_types: dict[UUID, str] = field(default_factory=dict)
    bindings: dict[str, BindingState] = field(default_factory=dict)
    receipts: set[tuple[UUID, str]] = field(default_factory=set)
    snapshot_receipts: set[UUID] = field(default_factory=set)
    authority_epoch_mutations: int = 0
    domain_event_mutations: int = 0
    scheduled_action_mutations: int = 0
    outbox_mutations: int = 0
    unattributed_mutations: int = 0
    migration_role_safe: bool = True

    def binding_for(self, source_key: str) -> BindingState | None:
        return self.bindings.get(source_key)

    def all_bindings(self) -> Mapping[str, BindingState]:
        return dict(self.bindings)

    def target_payload(self, record: CanonicalRecord) -> Mapping[str, object] | None:
        return self.targets.get(record.aggregate_id)

    def receipt_exists(self, snapshot_id: UUID, source_key: str) -> bool:
        return (snapshot_id, source_key) in self.receipts

    def record_snapshot(self, manifest: SnapshotManifest) -> None:
        self.snapshot_receipts.add(manifest.snapshot_id)

    def snapshot_receipt_exists(self, snapshot_id: UUID) -> bool:
        return snapshot_id in self.snapshot_receipts

    def apply_record(
        self,
        record: CanonicalRecord,
        *,
        snapshot_id: UUID,
        predecessor_snapshot_id: UUID | None,
        run_id: UUID,
        observed_at: datetime,
    ) -> ApplyResult:
        del run_id, observed_at
        binding = self.bindings.get(record.source_key)
        if binding is not None:
            if binding.aggregate_id != record.aggregate_id:
                raise ImmutableIdentityConflict(
                    f"source key target aggregate remap rejected: {record.source_key}"
                )
            if binding.aggregate_type not in (None, record.entity_type):
                raise ImmutableIdentityConflict(
                    f"source key aggregate type remap rejected: {record.source_key}"
                )
            if binding.resource_code not in (None, record.entity_type):
                raise ImmutableIdentityConflict(
                    f"source key resource code remap rejected: {record.source_key}"
                )
            if binding.snapshot_id not in {snapshot_id, predecessor_snapshot_id}:
                raise OldSnapshotError(
                    f"binding is at {binding.snapshot_id}, not predecessor {predecessor_snapshot_id}"
                )
        existing = self.targets.get(record.aggregate_id)
        if existing is not None and binding is None:
            raise ConcurrentMigrationError(
                f"target exists without an attributed Stage B binding: {record.source_key}"
            )
        if existing is not None:
            for field_name in record.immutable_fields:
                if canonical_value(existing.get(field_name)) != canonical_value(
                    record.payload.get(field_name)
                ):
                    raise ImmutableIdentityConflict(
                        f"immutable target conflict for {record.entity_type}.{field_name}"
                    )
        payload = dict(record.payload)
        version: int | None = None
        if record.entity_type == "reservation":
            existing_version = None if existing is None else int(existing["source_version"])
            existing_semantics = None if existing is None else reservation_semantics(existing)
            version, changed = reservation_next_version(
                existing_version, existing_semantics, reservation_semantics(payload)
            )
            payload["source_version"] = version
        else:
            changed = existing is None or canonical_value(existing) != canonical_value(payload)
        target_hash = semantic_hash(payload)
        same_source = binding is not None and binding.semantic_hash == record.semantic_hash
        if same_source and existing is not None and binding.target_hash != semantic_hash(existing):
            raise ConcurrentMigrationError(
                f"target changed without an attributed Stage B binding: {record.source_key}"
            )
        if changed:
            self.targets[record.aggregate_id] = payload
            self.target_types[record.aggregate_id] = record.entity_type
        self.receipts.add((snapshot_id, record.source_key))
        self.bindings[record.source_key] = BindingState(
            record.source_key,
            record.aggregate_id,
            record.semantic_hash,
            semantic_hash(self.targets[record.aggregate_id]),
            snapshot_id,
            f"{snapshot_id}:{record.source_key}",
            record.entity_type,
            record.entity_type,
        )
        return ApplyResult(
            record.source_key,
            record.aggregate_id,
            ApplyDisposition.APPLIED if changed else ApplyDisposition.NOOP,
            record.semantic_hash,
            snapshot_id,
            version,
        )

    def safety_counters(self) -> Mapping[str, int | bool]:
        return {
            "authority_epoch_mutations": self.authority_epoch_mutations,
            "domain_event_mutations": self.domain_event_mutations,
            "scheduled_action_mutations": self.scheduled_action_mutations,
            "outbox_mutations": self.outbox_mutations,
            "unattributed_mutations": self.unattributed_mutations,
            "migration_role_safe": self.migration_role_safe,
        }


class BackfillEngine:
    def __init__(
        self,
        repository: MigrationRepository,
        *,
        fresh_source_validator: FreshSourceValidator | None = None,
    ) -> None:
        if fresh_source_validator is None:
            raise SnapshotIntegrityError(
                "fresh source validator bound to authoritative adapters is required for Stage B apply"
            )
        if not isinstance(fresh_source_validator, FreshSourceValidator):
            raise SnapshotIntegrityError(
                "Stage B apply freshness authority must be a FreshSourceValidator"
            )
        self.repository = repository
        self.fresh_source_validator = fresh_source_validator

    def _validate_fresh_observation(self, manifest: SnapshotManifest) -> SnapshotManifest:
        # FreshSourceValidator performs a new complete extraction through trusted
        # source adapters and returns the apply manifest bound to that extraction.
        # Never continue with the caller object merely because semantics match.
        return self.fresh_source_validator.validate(manifest)

    def _validate_local_sequence_stability(self, population: Population) -> None:
        sequence_fields = {
            "cleaning_offer_campaign": "campaign_no",
            "cleaning_assignment": "assignment_no",
        }
        for record in population.records:
            sequence_field = sequence_fields.get(record.entity_type)
            if sequence_field is None:
                continue
            if self.repository.binding_for(record.source_key) is None:
                continue
            target = self.repository.target_payload(record)
            if target is None:
                continue
            if canonical_value(target.get(sequence_field)) != canonical_value(
                record.payload.get(sequence_field)
            ):
                raise ReviewRequired(
                    f"retained {record.entity_type} local sequence would renumber "
                    f"persisted ancestry: {record.source_key}"
                )

    def apply(
        self,
        manifest: SnapshotManifest,
        population: Population,
        *,
        crash_after: int | None = None,
    ) -> BackfillSummary:
        if population.identity_collisions:
            raise ImmutableIdentityConflict("; ".join(population.identity_collisions))
        if population.authority_findings:
            raise ImmutableIdentityConflict(
                "source authority rejected: " + "; ".join(population.authority_findings)
            )
        # A frozen snapshot may only cross the executable apply boundary after a
        # genuinely new complete source extraction proves exact equality.  From
        # this point onward only the validator-bound extraction provenance is used.
        trusted_manifest = self._validate_fresh_observation(manifest)
        # A later delta must fail closed rather than silently renumber already
        # persisted Cleaner-local campaign/assignment ancestry.
        self._validate_local_sequence_stability(population)
        # Snapshot receipts are immutable idempotency evidence.  The first
        # successful apply records the actual validator extraction provenance;
        # later replays still perform a new validation, but do not conflict with
        # or rewrite that already-authorized receipt.
        if not self.repository.snapshot_receipt_exists(trusted_manifest.snapshot_id):
            self.repository.record_snapshot(trusted_manifest)
        ordered = records_in_dependency_order(population.records)
        deferred_cleaning_records: list[CanonicalRecord] = []
        load_records: list[CanonicalRecord] = []
        for record in ordered:
            if record.entity_type == "cleaning_job" and record.payload.get(
                "current_schedule_revision_id"
            ) is not None:
                bootstrap_payload = dict(record.payload)
                bootstrap_payload["current_schedule_revision_id"] = None
                load_records.append(replace(record, payload=bootstrap_payload))
                deferred_cleaning_records.append(record)
            else:
                load_records.append(record)
        results: list[ApplyResult] = []
        physical_applies = 0
        for record in tuple(load_records) + tuple(deferred_cleaning_records):
            if record.source_key not in trusted_manifest.observations:
                raise ValueError(f"record source is absent from snapshot: {record.source_key}")
            if record.semantic_hash != trusted_manifest.source_semantic_hashes[record.source_key]:
                raise SnapshotIntegrityError(
                    f"population record is not bound to trusted extraction: {record.source_key}"
                )
            # Preserve the existing per-row TOCTOU check, but bind it to the
            # validator-produced extraction that is also persisted/applied.
            self.fresh_source_validator.validate_point_read(trusted_manifest, record.source_key)
            result = self.repository.apply_record(
                record,
                snapshot_id=trusted_manifest.snapshot_id,
                predecessor_snapshot_id=trusted_manifest.predecessor_snapshot_id,
                run_id=trusted_manifest.run_id,
                observed_at=trusted_manifest.scan_completed_at,
            )
            physical_applies += 1
            if record not in deferred_cleaning_records or record.payload.get(
                "current_schedule_revision_id"
            ) is not None:
                # The bootstrap cleaning row is an internal FK-cycle phase. The
                # final source-row result replaces it in operator-facing counts.
                prior_index = next(
                    (i for i, item in enumerate(results) if item.source_key == result.source_key),
                    None,
                )
                if prior_index is None:
                    results.append(result)
                else:
                    results[prior_index] = result
            if crash_after is not None and physical_applies >= crash_after:
                raise RuntimeError("synthetic crash after persisted apply boundary")
        return BackfillSummary(
            snapshot_id=trusted_manifest.snapshot_id,
            applied=sum(item.disposition == ApplyDisposition.APPLIED for item in results),
            noop=sum(item.disposition == ApplyDisposition.NOOP for item in results),
            review_required=len(population.obligations),
            results=tuple(results),
        )


__all__ = [
    "BackfillEngine",
    "BindingState",
    "InMemoryMigrationRepository",
    "MigrationRepository",
]

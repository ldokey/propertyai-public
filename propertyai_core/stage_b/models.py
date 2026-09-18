from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from uuid import UUID


class StageBError(RuntimeError):
    """Base class for fail-closed Stage B errors."""


class SnapshotIntegrityError(StageBError):
    pass


class SnapshotConvergenceError(SnapshotIntegrityError):
    pass


class StaleSnapshotRowError(SnapshotIntegrityError):
    pass


class IdentityConflictError(StageBError):
    pass


class AmbiguousIdentityError(IdentityConflictError):
    pass


class ImmutableIdentityConflict(IdentityConflictError):
    pass


class OldSnapshotError(StageBError):
    pass


class ConcurrentMigrationError(StageBError):
    pass


class ReconstructionError(StageBError):
    pass


class ReviewRequired(ReconstructionError):
    pass


class ApplyDisposition(str, Enum):
    APPLIED = "APPLIED"
    NOOP = "NOOP"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class GateStatus(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    INFORMATIONAL = "INFORMATIONAL"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class SourceObservation:
    source_type: str
    durable_identity: str
    semantic_payload: Mapping[str, Any]
    source_ref: str | None = None
    last_edited_time: datetime | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.source_type.strip() or not self.durable_identity.strip():
            raise ValueError("source_type and durable_identity must be nonblank")
        object.__setattr__(self, "semantic_payload", _frozen_mapping(self.semantic_payload))

    @property
    def row_key(self) -> str:
        return f"{self.source_type}:{self.durable_identity}"


@dataclass(frozen=True)
class SnapshotManifest:
    run_id: UUID
    snapshot_id: UUID
    predecessor_snapshot_id: UUID | None
    scan_started_at: datetime
    scan_completed_at: datetime
    source_runtime_reference: str
    source_row_identities: tuple[str, ...]
    source_semantic_hashes: Mapping[str, str]
    file_content_hashes: Mapping[str, str]
    manifest_hash: str
    observations: Mapping[str, SourceObservation]
    convergence_passes: int = 2
    independent_extraction_id: UUID | None = None
    extraction_evidence_hash: str | None = None

    def __post_init__(self) -> None:
        from .identity import extraction_identity
        from .normalize import semantic_hash

        if self.scan_completed_at < self.scan_started_at:
            raise ValueError("scan_completed_at precedes scan_started_at")
        identities = tuple(sorted(self.source_row_identities))
        if len(identities) != len(set(identities)):
            raise SnapshotIntegrityError("duplicate source row identity in manifest")
        if set(identities) != set(self.source_semantic_hashes):
            raise SnapshotIntegrityError("manifest identity/hash coverage differs")
        if set(identities) != set(self.observations):
            raise SnapshotIntegrityError("manifest identity/observation coverage differs")
        if self.convergence_passes < 2:
            raise SnapshotIntegrityError("snapshot was not proven by two identical manifests")
        evidence_hash = semantic_hash(
            {
                "manifest_hash": self.manifest_hash,
                "source_runtime_reference": self.source_runtime_reference,
                "source_row_identities": identities,
                "source_semantic_hashes": dict(self.source_semantic_hashes),
                "file_content_hashes": dict(self.file_content_hashes),
                "scan_started_at": self.scan_started_at,
                "scan_completed_at": self.scan_completed_at,
            }
        )
        if self.extraction_evidence_hash not in (None, evidence_hash):
            raise SnapshotIntegrityError("extraction evidence hash is not bound to snapshot evidence")
        expected_extraction_id = extraction_identity(evidence_hash)
        if self.independent_extraction_id not in (None, expected_extraction_id):
            raise SnapshotIntegrityError("independent extraction id is not bound to extraction evidence")
        object.__setattr__(self, "source_row_identities", identities)
        object.__setattr__(self, "source_semantic_hashes", _frozen_mapping(self.source_semantic_hashes))
        object.__setattr__(self, "file_content_hashes", _frozen_mapping(self.file_content_hashes))
        object.__setattr__(self, "observations", MappingProxyType(dict(self.observations)))
        object.__setattr__(self, "extraction_evidence_hash", evidence_hash)
        object.__setattr__(self, "independent_extraction_id", expected_extraction_id)


@dataclass(frozen=True)
class CanonicalRecord:
    entity_type: str
    aggregate_id: UUID
    source_key: str
    semantic_hash: str
    payload: Mapping[str, Any]
    immutable_fields: tuple[str, ...]
    dependencies: tuple[UUID, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.entity_type.strip() or not self.source_key.strip():
            raise ValueError("entity_type and source_key must be nonblank")
        if not self.semantic_hash.strip():
            raise ValueError("semantic_hash must be nonblank")
        missing = [name for name in self.immutable_fields if name not in self.payload]
        if missing:
            raise ValueError(f"immutable fields absent from payload: {missing}")
        object.__setattr__(self, "payload", _frozen_mapping(self.payload))
        object.__setattr__(self, "provenance", _frozen_mapping(self.provenance))


@dataclass(frozen=True)
class Population:
    records: tuple[CanonicalRecord, ...]
    obligations: tuple["MigrationObligation", ...] = ()
    identity_collisions: tuple[str, ...] = ()
    authority_findings: tuple[str, ...] = ()

    def by_source_key(self) -> Mapping[str, CanonicalRecord]:
        return MappingProxyType({record.source_key: record for record in self.records})


@dataclass(frozen=True)
class MigrationObligation:
    obligation_id: UUID
    source_key: str
    category: str
    reason: str
    resolved: bool = False
    effect_only: bool = False


@dataclass(frozen=True)
class ApplyResult:
    source_key: str
    aggregate_id: UUID
    disposition: ApplyDisposition
    semantic_hash: str
    snapshot_id: UUID
    source_version: int | None = None


@dataclass(frozen=True)
class BackfillSummary:
    snapshot_id: UUID
    applied: int
    noop: int
    review_required: int
    results: tuple[ApplyResult, ...]


@dataclass(frozen=True)
class GateEvidence:
    gate: str
    status: GateStatus
    blocking_count: int
    facts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciliationReport:
    snapshot_id: UUID
    generated_at: datetime
    gates: tuple[GateEvidence, ...]
    effect_only_pending_count: int

    @property
    def blocking_count(self) -> int:
        return sum(gate.blocking_count for gate in self.gates)

    @property
    def clean(self) -> bool:
        return self.blocking_count == 0


@dataclass(frozen=True)
class SnapshotDelta:
    predecessor_snapshot_id: UUID
    snapshot_id: UUID
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(frozen=True)
class CleanCycleEvidence:
    snapshot_id: UUID
    independent_extraction_id: UUID
    extraction_evidence_hash: str
    predecessor_snapshot_id: UUID | None
    report: ReconciliationReport


@dataclass(frozen=True)
class CutoverReadiness:
    ready: bool
    clean_cycle_count: int
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class FinalDeltaPreflight:
    decision: str
    freeze_acquired: bool
    final_unapplied_delta_count: int
    blocking_count: int
    postgres_authority_activation_allowed: bool = False


def records_in_dependency_order(records: Sequence[CanonicalRecord]) -> tuple[CanonicalRecord, ...]:
    """Stable topological order; fail closed on missing/cyclic dependencies."""
    remaining = list(records)
    known_ids = {record.aggregate_id for record in remaining}
    emitted: set[UUID] = set()
    ordered: list[CanonicalRecord] = []
    while remaining:
        ready = [
            record
            for record in remaining
            if all(dep in emitted or dep not in known_ids for dep in record.dependencies)
        ]
        if not ready:
            raise ReconstructionError("cyclic canonical record dependencies")
        ready.sort(key=lambda record: (record.entity_type, str(record.aggregate_id)))
        for record in ready:
            ordered.append(record)
            emitted.add(record.aggregate_id)
            remaining.remove(record)
    return tuple(ordered)


__all__ = [name for name in globals() if not name.startswith("_")]

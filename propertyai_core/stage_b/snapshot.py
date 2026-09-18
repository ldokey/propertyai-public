from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol, Sequence
from uuid import UUID, uuid4

from .identity import deterministic_id
from .models import (
    SnapshotConvergenceError,
    SnapshotDelta,
    SnapshotIntegrityError,
    SnapshotManifest,
    SourceObservation,
    StaleSnapshotRowError,
    utc_now,
)
from .normalize import semantic_hash


class SnapshotSource(Protocol):
    runtime_reference: str

    def scan(self) -> Sequence[SourceObservation]: ...

    def point_read(self, durable_identity: str) -> SourceObservation | None: ...

    @property
    def file_content_hashes(self) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class _Scan:
    started_at: datetime
    completed_at: datetime
    observations: Mapping[str, SourceObservation]
    semantic_hashes: Mapping[str, str]
    file_hashes: Mapping[str, str]
    fingerprint: str


class ConvergentSnapshotBuilder:
    def __init__(self, sources: Sequence[SnapshotSource], *, max_scans: int = 6) -> None:
        if not sources:
            raise ValueError("at least one snapshot source is required")
        if max_scans < 2:
            raise ValueError("max_scans must allow two consecutive observations")
        self.sources = tuple(sources)
        self.max_scans = max_scans

    @property
    def runtime_reference(self) -> str:
        return "|".join(sorted(source.runtime_reference for source in self.sources))

    def _scan_once(self) -> _Scan:
        started_at = utc_now()
        observations: dict[str, SourceObservation] = {}
        file_hashes: dict[str, str] = {}
        for source in self.sources:
            for observation in source.scan():
                if observation.row_key in observations:
                    raise SnapshotIntegrityError(
                        f"duplicate source row across adapters: {observation.row_key}"
                    )
                observations[observation.row_key] = observation
            for key, value in source.file_content_hashes.items():
                if key in file_hashes and file_hashes[key] != value:
                    raise SnapshotIntegrityError(f"duplicate file evidence identity: {key}")
                file_hashes[key] = value
        semantic_hashes = {
            key: semantic_hash(observation.semantic_payload)
            for key, observation in observations.items()
        }
        fingerprint = semantic_hash(
            {
                "source_row_identities": sorted(observations),
                "source_semantic_hashes": semantic_hashes,
                "file_content_hashes": file_hashes,
            }
        )
        return _Scan(
            started_at,
            utc_now(),
            observations,
            semantic_hashes,
            file_hashes,
            fingerprint,
        )

    def capture(
        self,
        *,
        run_id: UUID | None = None,
        predecessor_snapshot_id: UUID | None = None,
        independent_extraction_id: UUID | None = None,
    ) -> SnapshotManifest:
        run_id = run_id or uuid4()
        previous: _Scan | None = None
        for _ in range(self.max_scans):
            current = self._scan_once()
            if previous is not None and previous.fingerprint == current.fingerprint:
                snapshot_id = deterministic_id(
                    f"snapshot:{run_id}:{predecessor_snapshot_id or 'ROOT'}:{current.fingerprint}"
                )
                return SnapshotManifest(
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    predecessor_snapshot_id=predecessor_snapshot_id,
                    scan_started_at=previous.started_at,
                    scan_completed_at=current.completed_at,
                    source_runtime_reference=self.runtime_reference,
                    source_row_identities=tuple(current.observations),
                    source_semantic_hashes=current.semantic_hashes,
                    file_content_hashes=current.file_hashes,
                    manifest_hash=current.fingerprint,
                    observations=current.observations,
                    convergence_passes=2,
                    independent_extraction_id=independent_extraction_id,
                )
            previous = current
        raise SnapshotConvergenceError(
            f"no two consecutive identical manifests after {self.max_scans} scans"
        )

    def validate_point_read(self, manifest: SnapshotManifest, source_key: str) -> None:
        expected = manifest.source_semantic_hashes.get(source_key)
        observation = manifest.observations.get(source_key)
        if expected is None or observation is None:
            raise StaleSnapshotRowError(f"snapshot row is absent: {source_key}")
        matching_sources = [
            source
            for source in self.sources
            if source_key.startswith(f"{getattr(source, 'source_type', '')}:")
        ]
        if len(matching_sources) != 1:
            raise SnapshotIntegrityError(f"source adapter is ambiguous for {source_key}")
        current = matching_sources[0].point_read(observation.durable_identity)
        if current is None or current.row_key != source_key:
            raise StaleSnapshotRowError(f"source row disappeared before apply: {source_key}")
        if semantic_hash(current.semantic_payload) != expected:
            raise StaleSnapshotRowError(f"source row changed before apply: {source_key}")
        if observation.content_hash is not None and current.content_hash != observation.content_hash:
            raise StaleSnapshotRowError(f"source file changed before apply: {source_key}")

    def validate_fresh_observation(self, manifest: SnapshotManifest) -> SnapshotManifest:
        """Perform a new full extraction and prove exact membership + semantic/file equality."""
        current = self.capture(predecessor_snapshot_id=manifest.snapshot_id)
        validate_independent_snapshot_observation(manifest, current)
        return current


class FreshSourceValidator:
    """Executable freshness authority bound to source adapters and one frozen snapshot."""

    def __init__(
        self,
        sources: Sequence[SnapshotSource],
        *,
        frozen_snapshot: SnapshotManifest,
        max_scans: int = 6,
    ) -> None:
        # Both the source adapters and the frozen snapshot are supplied by trusted
        # executable wiring.  The apply caller may identify that exact frozen
        # snapshot, but cannot mint replacement scan bounds/extraction provenance.
        if not isinstance(frozen_snapshot, SnapshotManifest):
            raise SnapshotIntegrityError(
                "FreshSourceValidator requires a trusted frozen SnapshotManifest"
            )
        self._builder = ConvergentSnapshotBuilder(sources, max_scans=max_scans)
        self._frozen_snapshot = frozen_snapshot

    @property
    def sources(self) -> tuple[SnapshotSource, ...]:
        return self._builder.sources

    @property
    def frozen_snapshot(self) -> SnapshotManifest:
        return self._frozen_snapshot

    def validate(self, apply_manifest: SnapshotManifest) -> SnapshotManifest:
        # Always perform the real source extraction first.  A repackaged caller
        # manifest therefore cannot turn a cheap identity/type failure into a
        # substitute for the mandatory executable freshness observation.
        current = self._builder.validate_fresh_observation(self._frozen_snapshot)
        if apply_manifest != self._frozen_snapshot:
            raise SnapshotIntegrityError(
                "apply manifest is not the validator-bound frozen extraction"
            )
        # Keep the frozen business-snapshot lineage while replacing every piece
        # of extraction provenance with the actual validator-produced extraction.
        # This makes persisted/apply provenance equal to the real source scan,
        # without introducing a synthetic extra business snapshot in the chain.
        return SnapshotManifest(
            run_id=self._frozen_snapshot.run_id,
            snapshot_id=self._frozen_snapshot.snapshot_id,
            predecessor_snapshot_id=self._frozen_snapshot.predecessor_snapshot_id,
            scan_started_at=current.scan_started_at,
            scan_completed_at=current.scan_completed_at,
            source_runtime_reference=current.source_runtime_reference,
            source_row_identities=current.source_row_identities,
            source_semantic_hashes=current.source_semantic_hashes,
            file_content_hashes=current.file_content_hashes,
            manifest_hash=current.manifest_hash,
            observations=current.observations,
            convergence_passes=current.convergence_passes,
            independent_extraction_id=current.independent_extraction_id,
            extraction_evidence_hash=current.extraction_evidence_hash,
        )

    def validate_point_read(self, trusted_manifest: SnapshotManifest, source_key: str) -> None:
        self._builder.validate_point_read(trusted_manifest, source_key)


def validate_independent_snapshot_observation(
    frozen: SnapshotManifest, current: SnapshotManifest
) -> None:
    if current.predecessor_snapshot_id != frozen.snapshot_id:
        raise SnapshotIntegrityError("fresh validation is not bound to the frozen snapshot")
    if current.source_runtime_reference != frozen.source_runtime_reference:
        raise SnapshotIntegrityError("fresh validation runtime reference differs")
    if current.scan_started_at < frozen.scan_completed_at:
        raise SnapshotIntegrityError("fresh validation observation predates frozen extraction completion")
    if current.extraction_evidence_hash == frozen.extraction_evidence_hash:
        raise SnapshotIntegrityError("same cached extraction cannot validate itself")
    if current.independent_extraction_id == frozen.independent_extraction_id:
        raise SnapshotIntegrityError("fresh validation extraction identity was replayed")
    if tuple(current.source_row_identities) != tuple(frozen.source_row_identities):
        raise StaleSnapshotRowError("fresh validation source membership differs")
    if dict(current.source_semantic_hashes) != dict(frozen.source_semantic_hashes):
        raise StaleSnapshotRowError("fresh validation semantic hashes differ")
    if dict(current.file_content_hashes) != dict(frozen.file_content_hashes):
        raise StaleSnapshotRowError("fresh validation file/path membership evidence differs")


def snapshot_delta(previous: SnapshotManifest, current: SnapshotManifest) -> SnapshotDelta:
    if current.predecessor_snapshot_id != previous.snapshot_id:
        raise SnapshotIntegrityError("delta snapshots do not form a predecessor chain")
    previous_keys = set(previous.source_semantic_hashes)
    current_keys = set(current.source_semantic_hashes)
    return SnapshotDelta(
        predecessor_snapshot_id=previous.snapshot_id,
        snapshot_id=current.snapshot_id,
        added=tuple(sorted(current_keys - previous_keys)),
        changed=tuple(
            sorted(
                key
                for key in previous_keys & current_keys
                if previous.source_semantic_hashes[key] != current.source_semantic_hashes[key]
            )
        ),
        removed=tuple(sorted(previous_keys - current_keys)),
    )


__all__ = [
    "ConvergentSnapshotBuilder",
    "FreshSourceValidator",
    "SnapshotSource",
    "snapshot_delta",
    "validate_independent_snapshot_observation",
]

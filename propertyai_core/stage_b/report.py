from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ReconciliationReport, SnapshotManifest
from .normalize import canonical_value


def report_document(report: ReconciliationReport) -> dict[str, Any]:
    return {
        "snapshot_id": str(report.snapshot_id),
        "generated_at": canonical_value(report.generated_at),
        "blocking_count": report.blocking_count,
        "clean": report.clean,
        "gates": [
            {
                "gate": gate.gate,
                "status": gate.status.value,
                "blocking_count": gate.blocking_count,
                "facts": list(gate.facts),
            }
            for gate in report.gates
        ],
        "R15E_EFFECT_ONLY_PENDING_COUNT": report.effect_only_pending_count,
        "production_business_authority": "LEGACY",
        "postgres_authority": "OFF",
        "draft_requires_human_review": True,
    }


def manifest_document(manifest: SnapshotManifest) -> dict[str, Any]:
    return {
        "run_id": str(manifest.run_id),
        "snapshot_id": str(manifest.snapshot_id),
        "predecessor_snapshot_id": (
            None
            if manifest.predecessor_snapshot_id is None
            else str(manifest.predecessor_snapshot_id)
        ),
        "scan_started_at": canonical_value(manifest.scan_started_at),
        "scan_completed_at": canonical_value(manifest.scan_completed_at),
        "source_runtime_reference": manifest.source_runtime_reference,
        "source_row_identities": list(manifest.source_row_identities),
        "source_semantic_hashes": dict(manifest.source_semantic_hashes),
        "file_content_hashes": dict(manifest.file_content_hashes),
        "manifest_hash": manifest.manifest_hash,
        "convergence_passes": manifest.convergence_passes,
        "independent_extraction_id": (
            None
            if manifest.independent_extraction_id is None
            else str(manifest.independent_extraction_id)
        ),
        "observations": [
            {
                "source_type": observation.source_type,
                "durable_identity": observation.durable_identity,
                "semantic_payload": canonical_value(observation.semantic_payload),
                "source_ref": observation.source_ref,
                "last_edited_time": canonical_value(observation.last_edited_time),
                "content_hash": observation.content_hash,
            }
            for observation in manifest.observations.values()
        ],
        "production_business_authority": "LEGACY",
        "postgres_authority": "OFF",
        "draft_requires_human_review": True,
    }


def write_report(report: ReconciliationReport, path: Path, *, output_root: Path) -> Path:
    output_root = output_root.resolve()
    target = path.resolve()
    if target != output_root and output_root not in target.parents:
        raise ValueError("reviewable reports may only be written under outputs/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report_document(report), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def write_manifest(manifest: SnapshotManifest, path: Path, *, output_root: Path) -> Path:
    output_root = output_root.resolve()
    target = path.resolve()
    if target != output_root and output_root not in target.parents:
        raise ValueError("reviewable manifests may only be written under outputs/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(manifest_document(manifest), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return target


__all__ = ["manifest_document", "report_document", "write_manifest", "write_report"]

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import UUID

from propertyai_core.adapters.postgres.stage_b_pool import (
    PostgresStageBMigrationConfig,
    PostgresStageBMigrationPool,
)
from propertyai_core.adapters.postgres.stage_b_repository import (
    PostgresStageBMigrationRepository,
)

from .backfill import BackfillEngine
from .models import SnapshotIntegrityError, SnapshotManifest, SourceObservation
from .normalize import normalize_datetime, semantic_hash
from .population import PopulationBuilder
from .reconciliation import ReconciliationEngine
from .report import report_document
from .snapshot import FreshSourceValidator, SnapshotSource


def _within(path: Path, root: Path) -> Path:
    target = path.resolve()
    root = root.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path must be under {root}")
    return target


def load_manifest(path: Path, *, input_root: Path) -> SnapshotManifest:
    target = _within(path, input_root)
    raw = json.loads(target.read_text(encoding="utf-8"))
    observations: dict[str, SourceObservation] = {}
    for item in raw["observations"]:
        observation = SourceObservation(
            source_type=item["source_type"],
            durable_identity=item["durable_identity"],
            semantic_payload=item["semantic_payload"],
            source_ref=item.get("source_ref"),
            last_edited_time=(
                None
                if item.get("last_edited_time") is None
                else normalize_datetime(item["last_edited_time"])
            ),
            content_hash=item.get("content_hash"),
        )
        if observation.row_key in observations:
            raise SnapshotIntegrityError("manifest JSON contains duplicate source rows")
        observations[observation.row_key] = observation
    hashes = {
        key: semantic_hash(observation.semantic_payload)
        for key, observation in observations.items()
    }
    supplied_hashes = raw["source_semantic_hashes"]
    if hashes != supplied_hashes:
        raise SnapshotIntegrityError("manifest semantic hashes do not match observations")
    expected_manifest_hash = semantic_hash(
        {
            "source_row_identities": sorted(observations),
            "source_semantic_hashes": hashes,
            "file_content_hashes": raw.get("file_content_hashes", {}),
        }
    )
    if raw["manifest_hash"] != expected_manifest_hash:
        raise SnapshotIntegrityError("manifest hash is invalid")
    return SnapshotManifest(
        run_id=UUID(raw["run_id"]),
        snapshot_id=UUID(raw["snapshot_id"]),
        predecessor_snapshot_id=(
            None
            if raw.get("predecessor_snapshot_id") is None
            else UUID(raw["predecessor_snapshot_id"])
        ),
        scan_started_at=normalize_datetime(raw["scan_started_at"]),
        scan_completed_at=normalize_datetime(raw["scan_completed_at"]),
        source_runtime_reference=raw["source_runtime_reference"],
        source_row_identities=tuple(raw["source_row_identities"]),
        source_semantic_hashes=hashes,
        file_content_hashes=raw.get("file_content_hashes", {}),
        manifest_hash=raw["manifest_hash"],
        observations=observations,
        convergence_passes=int(raw["convergence_passes"]),
        independent_extraction_id=(
            None
            if raw.get("independent_extraction_id") is None
            else UUID(raw["independent_extraction_id"])
        ),
        extraction_evidence_hash=raw.get("extraction_evidence_hash"),
    )


def _write_json(document: Mapping[str, Any], path: Path, *, output_root: Path) -> None:
    target = _within(path, output_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _config() -> PostgresStageBMigrationConfig:
    return PostgresStageBMigrationConfig.from_environment(os.environ)



SourceAdapterFactory = Callable[[], FreshSourceValidator]


def _configured_fresh_source_validator(
    source_adapter_factory: SourceAdapterFactory | None = None,
    *,
    frozen_snapshot: SnapshotManifest | None = None,
    environment: Mapping[str, str] | None = None,
) -> FreshSourceValidator:
    """Resolve trusted executable freshness authority before any PostgreSQL access."""
    if source_adapter_factory is None:
        env = os.environ if environment is None else environment
        if (
            env.get("PROPERTYAI_DATA_ENVIRONMENT") == "PRODUCTION"
            and env.get("PROPERTYAI_CLEANER_POSTGRES_MODE") == "PRE_CUTOVER_MIGRATION"
        ):
            if frozen_snapshot is None:
                raise SnapshotIntegrityError(
                    "Production Stage B source wiring requires the frozen snapshot"
                )
            from .sources.production import production_source_adapter_factory

            validator = production_source_adapter_factory(
                frozen_snapshot, environment=env
            )
        else:
            # TEST/DEVELOPMENT retains the accepted explicit-injection behavior.
            raise SnapshotIntegrityError(
                "Stage B CLI fresh authoritative source adapters are not configured"
            )
    else:
        validator = source_adapter_factory()
    if not isinstance(validator, FreshSourceValidator):
        raise SnapshotIntegrityError(
            "Stage B CLI executable source wiring must return a FreshSourceValidator "
            "bound to trusted frozen extraction provenance"
        )
    return validator


def _reject_cached_manifest_backfill() -> None:
    # Kept as an explicit regression hook for cached-manifest authority attacks.
    raise SnapshotIntegrityError(
        "caller-supplied cached manifests cannot satisfy Stage B apply freshness"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PropertyAI Stage B offline migration foundation")
    parser.add_argument("--input-root", type=Path, default=Path("inputs"))
    parser.add_argument("--output-root", type=Path, default=Path("outputs"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-manifest")
    validate.add_argument("manifest", type=Path)
    validate.add_argument("output", type=Path)
    for name in ("backfill", "reconcile"):
        command = subparsers.add_parser(name)
        command.add_argument("manifest", type=Path)
        command.add_argument("output", type=Path)
        if name == "backfill":
            command.add_argument("--execute", action="store_true", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    source_adapter_factory: SourceAdapterFactory | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    manifest = load_manifest(args.manifest, input_root=args.input_root)
    population = PopulationBuilder().build(manifest)
    if args.command == "validate-manifest":
        _write_json(
            {
                "snapshot_id": str(manifest.snapshot_id),
                "manifest_hash": manifest.manifest_hash,
                "source_row_count": len(manifest.source_row_identities),
                "population_record_count": len(population.records),
                "review_required_count": len(population.obligations)
                + len(population.identity_collisions)
                + len(population.authority_findings),
                "production_business_authority": "LEGACY",
                "postgres_authority": "OFF",
                "draft_requires_human_review": True,
            },
            args.output,
            output_root=args.output_root,
        )
        return 0
    fresh_source_validator = None
    if args.command == "backfill":
        # Trusted executable wiring must construct a validator already bound to
        # real source adapters. Missing wiring is a hard failure before DB access.
        fresh_source_validator = _configured_fresh_source_validator(
            source_adapter_factory, frozen_snapshot=manifest
        )
    with PostgresStageBMigrationPool(_config()) as pool:
        repository = PostgresStageBMigrationRepository(pool)
        if args.command == "backfill":
            summary = BackfillEngine(
                repository, fresh_source_validator=fresh_source_validator
            ).apply(manifest, population)
            report = ReconciliationEngine(repository).reconcile(manifest, population)
            document = report_document(report)
            document["apply_summary"] = {
                "applied": summary.applied,
                "noop": summary.noop,
                "review_required": summary.review_required,
            }
        else:
            report = ReconciliationEngine(repository).reconcile(manifest, population)
            document = report_document(report)
    _write_json(document, args.output, output_root=args.output_root)
    return 0 if report.clean else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "load_manifest", "main"]

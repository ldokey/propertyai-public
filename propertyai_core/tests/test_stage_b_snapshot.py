from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Mapping
from uuid import uuid4

import pytest

from propertyai_core.stage_b.backfill import BackfillEngine, InMemoryMigrationRepository
from propertyai_core.stage_b.models import (
    CanonicalRecord,
    Population,
    SnapshotConvergenceError,
    SnapshotIntegrityError,
    SourceObservation,
    StaleSnapshotRowError,
)
from propertyai_core.stage_b.snapshot import (
    ConvergentSnapshotBuilder,
    FreshSourceValidator,
    snapshot_delta,
    validate_independent_snapshot_observation,
)
import propertyai_core.stage_b.cli as stage_b_cli
import propertyai_core.stage_b.snapshot as stage_b_snapshot
from propertyai_core.stage_b.cli import _reject_cached_manifest_backfill
from propertyai_core.stage_b.sources.files import FileSnapshotSource, read_stable_file
from propertyai_core.stage_b.sources.notion import NotionPage, NotionSnapshotSource


class SequenceSource:
    source_type = "legacy"
    runtime_reference = "synthetic-runtime-v1"

    def __init__(self, values):
        self.values = list(values)
        self.index = 0
        self.current = values[0]

    def scan(self):
        self.current = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return (SourceObservation("legacy", "row", self.current),)

    def point_read(self, durable_identity):
        assert durable_identity == "row"
        return SourceObservation("legacy", "row", self.current)

    @property
    def file_content_hashes(self):
        return {}


def test_two_consecutive_identical_full_manifests_are_required():
    source = SequenceSource([{"v": 1}, {"v": 2}, {"v": 3}, {"v": 3}])
    manifest = ConvergentSnapshotBuilder([source], max_scans=4).capture()
    assert manifest.convergence_passes == 2
    assert manifest.observations["legacy:row"].semantic_payload["v"] == 3
    assert source.index == 4


def test_nonconvergent_same_row_repeated_changes_fail_closed():
    source = SequenceSource([{"v": 1}, {"v": 2}, {"v": 3}])
    with pytest.raises(SnapshotConvergenceError):
        ConvergentSnapshotBuilder([source], max_scans=3).capture()


def test_point_reread_rejects_stale_source_row():
    source = SequenceSource([{"v": 1}, {"v": 1}])
    builder = ConvergentSnapshotBuilder([source])
    manifest = builder.capture()
    source.current = {"v": 2}
    with pytest.raises(StaleSnapshotRowError):
        builder.validate_point_read(manifest, "legacy:row")


def test_notion_complete_pagination_and_duplicate_rejection():
    page_id = "12345678-1234-4234-9234-123456789abc"
    calls = []

    def fetch(cursor):
        calls.append(cursor)
        if cursor is None:
            return NotionPage(({"id": page_id, "properties": {"v": 1}},), "next", True)
        return NotionPage(({"id": "22345678-1234-4234-9234-123456789abc"},), None, False)

    source = NotionSnapshotSource(
        "notion",
        fetch,
        lambda page: {"id": page},
        semantic_normalizer=lambda row: row.get("properties", {}),
        runtime_reference="notion-db:test",
    )
    assert len(source.scan()) == 2
    assert calls == [None, "next"]

    duplicate = NotionSnapshotSource(
        "notion",
        lambda cursor: NotionPage(
            ({"id": page_id}, {"id": page_id}), None, False
        ),
        lambda page: {"id": page},
        semantic_normalizer=lambda row: row,
        runtime_reference="notion-db:test",
    )
    with pytest.raises(SnapshotIntegrityError):
        duplicate.scan()


def test_notion_cursor_loop_is_rejected():
    source = NotionSnapshotSource(
        "notion",
        lambda cursor: NotionPage((), "same", True),
        lambda page: None,
        semantic_normalizer=lambda row: row,
        runtime_reference="notion-db:test",
    )
    with pytest.raises(SnapshotIntegrityError):
        source.scan()


def test_stable_file_source_captures_content_hash(tmp_path):
    path = tmp_path / "cleaners.json"
    path.write_text(json.dumps({"cleaners": [1, 2]}), encoding="utf-8")
    stable = read_stable_file(path)
    assert stable.content_hash
    source = FileSnapshotSource(
        "runtime-file",
        {"cleaners": path},
        semantic_normalizer=lambda identity, value: value,
        runtime_reference="runtime:test",
    )
    observation = source.scan()[0]
    assert source.file_content_hashes[observation.row_key] == observation.content_hash


def test_file_replacement_race_is_detected(tmp_path, monkeypatch):
    path = tmp_path / "cleaners.json"
    path.write_text("{}", encoding="utf-8")
    actual_fstat = os.fstat
    calls = 0

    class Changed:
        pass

    def changing_fstat(fd):
        nonlocal calls
        calls += 1
        value = actual_fstat(fd)
        if calls != 2:
            return value
        fake = Changed()
        for name in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"):
            setattr(fake, name, getattr(value, name))
        fake.st_mtime_ns += 1
        return fake

    monkeypatch.setattr(os, "fstat", changing_fstat)
    with pytest.raises(SnapshotIntegrityError):
        read_stable_file(path)


def test_snapshot_delta_requires_exact_chain_and_reports_changes():
    source = SequenceSource([{"v": 1}, {"v": 1}])
    builder = ConvergentSnapshotBuilder([source])
    first = builder.capture()
    source.values = [{"v": 2}, {"v": 2}]
    source.index = 0
    second = builder.capture(predecessor_snapshot_id=first.snapshot_id)
    delta = snapshot_delta(first, second)
    assert delta.changed == ("legacy:row",)
    assert not delta.added and not delta.removed


class MutableRowsSource:
    source_type = "legacy-rows"
    runtime_reference = "synthetic-runtime-complete-membership-v1"

    def __init__(self, rows):
        self.rows = dict(rows)
        self.scan_count = 0

    def scan(self):
        self.scan_count += 1
        return tuple(
            SourceObservation(self.source_type, key, value)
            for key, value in sorted(self.rows.items())
        )

    def point_read(self, durable_identity):
        value = self.rows.get(durable_identity)
        return None if value is None else SourceObservation(self.source_type, durable_identity, value)

    @property
    def file_content_hashes(self):
        return {}


def test_fresh_complete_extraction_detects_change_add_remove_and_accepts_unchanged():
    source = MutableRowsSource({"a": {"v": 1}, "b": {"v": 2}})
    builder = ConvergentSnapshotBuilder([source])
    frozen = builder.capture()

    unchanged = builder.validate_fresh_observation(frozen)
    assert unchanged.source_semantic_hashes == frozen.source_semantic_hashes
    assert unchanged.independent_extraction_id != frozen.independent_extraction_id
    assert unchanged.extraction_evidence_hash != frozen.extraction_evidence_hash

    source.rows["a"] = {"v": 9}
    with pytest.raises(StaleSnapshotRowError, match="semantic"):
        builder.validate_fresh_observation(frozen)
    source.rows["a"] = {"v": 1}

    source.rows["c"] = {"v": 3}
    with pytest.raises(StaleSnapshotRowError, match="membership"):
        builder.validate_fresh_observation(frozen)
    source.rows.pop("c")

    source.rows.pop("b")
    with pytest.raises(StaleSnapshotRowError, match="membership"):
        builder.validate_fresh_observation(frozen)


def test_same_cached_extraction_and_arbitrary_extraction_identity_are_rejected():
    source = MutableRowsSource({"a": {"v": 1}})
    frozen = ConvergentSnapshotBuilder([source]).capture()
    with pytest.raises(SnapshotIntegrityError):
        validate_independent_snapshot_observation(frozen, frozen)
    with pytest.raises(SnapshotIntegrityError, match="independent extraction id"):
        replace(frozen, independent_extraction_id=uuid4())
    with pytest.raises(SnapshotIntegrityError, match="cached manifest"):
        _reject_cached_manifest_backfill()


def test_file_snapshot_evidence_includes_original_resolved_path_and_directory_membership(tmp_path):
    authoritative = tmp_path / "runtime"
    authoritative.mkdir()
    target = authoritative / "target.json"
    target.write_text('{"v":1}', encoding="utf-8")
    link = authoritative / "cleaners.json"
    link.symlink_to(target.name)
    stable = read_stable_file(link)
    assert stable.original_path == link.absolute()
    assert stable.resolved_path == target.resolve()
    assert stable.original_lstat_identity != stable.resolved_stat_identity

    source = FileSnapshotSource(
        "runtime-file",
        {"cleaners": link},
        semantic_normalizer=lambda identity, value: value,
        runtime_reference="runtime:test",
        authoritative_directories=(authoritative,),
    )
    source.scan()
    evidence = dict(source.file_content_hashes)
    assert "runtime-file:cleaners" in evidence
    assert "path:runtime-file:cleaners" in evidence
    assert f"directory:{authoritative.absolute()}" in evidence
    before_directory = evidence[f"directory:{authoritative.absolute()}"]
    (authoritative / "unexpected.json").write_text("{}", encoding="utf-8")
    source.scan()
    after_directory = source.file_content_hashes[f"directory:{authoritative.absolute()}"]
    assert before_directory != after_directory


def _fresh_apply_fixture():
    aggregate_id = uuid4()
    payload = {
        "organization_id": aggregate_id,
        "organization_code": "ORG-FRESH-BOUNDARY",
        "display_name": "Fresh Boundary",
        "organization_status": "ACTIVE",
        "data_environment": "TEST",
    }
    source = MutableRowsSource({"a": payload})
    frozen = ConvergentSnapshotBuilder([source]).capture()
    key = "legacy-rows:a"
    item = CanonicalRecord(
        "organization",
        aggregate_id,
        key,
        frozen.source_semantic_hashes[key],
        payload,
        ("organization_id", "organization_code", "data_environment"),
    )
    return source, frozen, item


def _assert_no_apply_writes(repository: InMemoryMigrationRepository) -> None:
    assert repository.targets == {}
    assert repository.bindings == {}
    assert repository.receipts == set()
    assert repository.snapshot_receipts == set()


def test_apply_requires_fresh_source_validator_before_first_write():
    _, _, _ = _fresh_apply_fixture()
    repository = InMemoryMigrationRepository()
    with pytest.raises(SnapshotIntegrityError, match="fresh source validator"):
        BackfillEngine(repository)
    _assert_no_apply_writes(repository)


@pytest.mark.parametrize("attack", ["changed", "added", "removed"])
def test_apply_source_rescan_attacks_reject_before_write(attack):
    source, frozen, item = _fresh_apply_fixture()
    repository = InMemoryMigrationRepository()
    validator = FreshSourceValidator([source], frozen_snapshot=frozen)
    if attack == "changed":
        source.rows["a"] = dict(source.rows["a"], display_name="Changed")
    elif attack == "added":
        source.rows["b"] = dict(
            source.rows["a"],
            organization_id=uuid4(),
            organization_code="ORG-NEW",
        )
    else:
        source.rows.pop("a")

    before = source.scan_count
    with pytest.raises((SnapshotIntegrityError, StaleSnapshotRowError)):
        BackfillEngine(
            repository, fresh_source_validator=validator
        ).apply(frozen, Population((item,)))
    assert source.scan_count >= before + 2
    _assert_no_apply_writes(repository)


def test_repackaged_cached_extraction_cannot_authorize_apply(monkeypatch):
    source, frozen, item = _fresh_apply_fixture()
    repository = InMemoryMigrationRepository()
    validator = FreshSourceValidator([source], frozen_snapshot=frozen)
    started = frozen.scan_completed_at + timedelta(seconds=1)
    repackaged = replace(
        frozen,
        run_id=uuid4(),
        snapshot_id=uuid4(),
        scan_started_at=started,
        scan_completed_at=started + timedelta(seconds=1),
        independent_extraction_id=None,
        extraction_evidence_hash=None,
    )
    assert repackaged.observations == frozen.observations
    assert repackaged.source_row_identities == frozen.source_row_identities
    assert repackaged.source_semantic_hashes == frozen.source_semantic_hashes
    assert repackaged.file_content_hashes == frozen.file_content_hashes
    assert repackaged.scan_started_at > frozen.scan_completed_at
    assert repackaged.extraction_evidence_hash != frozen.extraction_evidence_hash
    assert repackaged.independent_extraction_id != frozen.independent_extraction_id

    # Make the validator's real extraction unequivocally later than the fake scan
    # bounds, so rejection cannot be satisfied by the timestamp freshness check.
    fresh_at = repackaged.scan_completed_at + timedelta(seconds=1)
    monkeypatch.setattr(stage_b_snapshot, "utc_now", lambda: fresh_at)
    before = source.scan_count
    with pytest.raises(
        SnapshotIntegrityError, match="validator-bound frozen extraction"
    ):
        BackfillEngine(repository, fresh_source_validator=validator).apply(
            repackaged, Population((item,))
        )
    assert source.scan_count >= before + 2
    _assert_no_apply_writes(repository)


def test_same_cached_object_does_not_establish_freshness_but_real_rescans_do():
    source, frozen, item = _fresh_apply_fixture()
    repository = InMemoryMigrationRepository()
    validator = FreshSourceValidator([source], frozen_snapshot=frozen)
    before = source.scan_count
    engine = BackfillEngine(repository, fresh_source_validator=validator)
    first = engine.apply(frozen, Population((item,)))
    after_first = source.scan_count
    second = engine.apply(frozen, Population((item,)))
    assert first.applied == 1
    assert second.noop == 1
    assert after_first >= before + 2
    assert source.scan_count >= after_first + 2


def test_real_second_same_state_extraction_has_distinct_evidence_and_passes():
    source, frozen, _ = _fresh_apply_fixture()
    validator = FreshSourceValidator([source], frozen_snapshot=frozen)
    before = source.scan_count
    current = validator.validate(frozen)
    assert source.scan_count >= before + 2
    assert current.source_semantic_hashes == frozen.source_semantic_hashes
    assert current.extraction_evidence_hash != frozen.extraction_evidence_hash
    assert current.independent_extraction_id != frozen.independent_extraction_id


def test_apply_real_unchanged_reextraction_passes_and_binds_actual_provenance(monkeypatch):
    source, frozen, item = _fresh_apply_fixture()

    class RecordingRepository(InMemoryMigrationRepository):
        recorded_manifest = None

        def record_snapshot(self, manifest):
            self.recorded_manifest = manifest
            super().record_snapshot(manifest)

    repository = RecordingRepository()
    validator = FreshSourceValidator([source], frozen_snapshot=frozen)
    fresh_at = frozen.scan_completed_at + timedelta(seconds=10)
    monkeypatch.setattr(stage_b_snapshot, "utc_now", lambda: fresh_at)
    before = source.scan_count
    summary = BackfillEngine(
        repository, fresh_source_validator=validator
    ).apply(frozen, Population((item,)))
    assert source.scan_count >= before + 2
    assert summary.applied == 1
    assert item.aggregate_id in repository.targets
    applied_manifest = repository.recorded_manifest
    assert applied_manifest is not None
    assert applied_manifest.snapshot_id == frozen.snapshot_id
    assert applied_manifest.predecessor_snapshot_id == frozen.predecessor_snapshot_id
    assert applied_manifest.scan_started_at == fresh_at
    assert applied_manifest.scan_completed_at == fresh_at
    assert applied_manifest.extraction_evidence_hash != frozen.extraction_evidence_hash
    assert applied_manifest.independent_extraction_id != frozen.independent_extraction_id



class _CleanReport:
    clean = True


class _CleanReconciliation:
    def __init__(self, repository):
        self.repository = repository

    def reconcile(self, manifest, population):
        return _CleanReport()


def _patch_cli_offline_dependencies(monkeypatch, frozen, population, repository, state):
    monkeypatch.setattr(stage_b_cli, "load_manifest", lambda *args, **kwargs: frozen)

    class PopulationFactory:
        def build(self, manifest):
            assert manifest is frozen
            return population

    class Pool:
        def __init__(self, config):
            state["pool_constructed"] += 1

        def __enter__(self):
            state["pool_entered"] += 1
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(stage_b_cli, "PopulationBuilder", PopulationFactory)
    monkeypatch.setattr(stage_b_cli, "PostgresStageBMigrationPool", Pool)
    monkeypatch.setattr(stage_b_cli, "PostgresStageBMigrationRepository", lambda pool: repository)
    monkeypatch.setattr(stage_b_cli, "ReconciliationEngine", _CleanReconciliation)
    monkeypatch.setattr(stage_b_cli, "report_document", lambda report: {})
    monkeypatch.setattr(stage_b_cli, "_write_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(stage_b_cli, "_config", lambda: object())


def test_cli_backfill_without_configured_source_adapters_fails_before_db(monkeypatch):
    source, frozen, item = _fresh_apply_fixture()
    repository = InMemoryMigrationRepository()
    population = Population((item,))
    state = {"pool_constructed": 0, "pool_entered": 0}
    _patch_cli_offline_dependencies(monkeypatch, frozen, population, repository, state)

    before = source.scan_count
    with pytest.raises(SnapshotIntegrityError, match="source adapters are not configured"):
        stage_b_cli.main(["backfill", "manifest.json", "report.json", "--execute"])
    assert source.scan_count == before
    assert state == {"pool_constructed": 0, "pool_entered": 0}
    _assert_no_apply_writes(repository)


def test_cli_backfill_with_executable_source_adapter_factory_performs_real_rescan(monkeypatch):
    source, frozen, item = _fresh_apply_fixture()
    repository = InMemoryMigrationRepository()
    population = Population((item,))
    state = {"pool_constructed": 0, "pool_entered": 0}
    _patch_cli_offline_dependencies(monkeypatch, frozen, population, repository, state)
    factory_calls = 0

    def source_adapter_factory():
        nonlocal factory_calls
        factory_calls += 1
        return FreshSourceValidator([source], frozen_snapshot=frozen)

    before = source.scan_count
    result = stage_b_cli.main(
        ["backfill", "manifest.json", "report.json", "--execute"],
        source_adapter_factory=source_adapter_factory,
    )
    assert result == 0
    assert factory_calls == 1
    assert source.scan_count >= before + 2
    assert state == {"pool_constructed": 1, "pool_entered": 1}
    assert item.aggregate_id in repository.targets


def test_cli_has_no_caller_supplied_current_manifest_backfill_option():
    with pytest.raises(SystemExit):
        stage_b_cli.build_parser().parse_args(
            [
                "backfill",
                "frozen.json",
                "report.json",
                "--execute",
                "--point-read-manifest",
                "fresh-looking.json",
            ]
        )

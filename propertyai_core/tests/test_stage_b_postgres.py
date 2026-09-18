from __future__ import annotations

import shutil
from dataclasses import replace
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import dict_row

from propertyai_core.adapters.postgres.stage_b_pool import (
    PostgresRoleMismatchError,
    PostgresStageBMigrationConfig,
    PostgresStageBMigrationPool,
    _EXPECTED_FLYWAY_STATE,
)
from propertyai_core.adapters.postgres.stage_b_repository import (
    MutationAttributionObservation,
    PostgresStageBMigrationRepository,
    normalize_mutation_attribution_category,
)
from propertyai_core.stage_b.backfill import BackfillEngine
from propertyai_core.stage_b.models import (
    CanonicalRecord,
    ImmutableIdentityConflict,
    MigrationObligation,
    OldSnapshotError,
    Population,
    SnapshotManifest,
    SourceObservation,
)
from propertyai_core.stage_b.normalize import semantic_hash
from propertyai_core.stage_b.population import (
    IMMUTABLE_FIELDS,
    minimal_exact_assignment_ancestry_batch,
)
from propertyai_core.stage_b.snapshot import FreshSourceValidator
from propertyai_core.stage_b.reconciliation import ReconciliationEngine
from propertyai_core.tests.postgres_stage_a_cluster import start_disposable_postgres


REPO_ROOT = Path(__file__).resolve().parents[2]
ROLE = "propertyai_stage_b_migration"
UTC = timezone.utc
NOW = datetime(2026, 9, 5, 1, tzinfo=UTC)


@pytest.fixture
def stage_b_cluster():
    cluster = start_disposable_postgres()
    try:
        cluster.psql(file=REPO_ROOT / "db" / "stage_b" / "provision_migration_role.sql")
        verified = cluster.psql(
            user=ROLE,
            file=REPO_ROOT / "db" / "stage_b" / "verify_migration_role.sql",
        )
        assert "STAGE_B_MIGRATION_ROLE_VERIFIED" in verified.stdout
        yield cluster
    finally:
        cluster.cleanup()


@pytest.fixture
def stage_b_pool(stage_b_cluster):
    config = PostgresStageBMigrationConfig(
        dsn=stage_b_cluster.login_dsn(ROLE),
        expected_session_user=ROLE,
        expected_database="postgres",
        data_environment="TEST",
        min_size=1,
        max_size=4,
    )
    with PostgresStageBMigrationPool(config) as pool:
        yield pool


class _FixtureSnapshotSource:
    def __init__(self, source_type: str, observations):
        self.source_type = source_type
        self.runtime_reference = "fixture-postgres:" + source_type
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
    return "|".join("fixture-postgres:" + source_type for source_type in source_types)


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


def canonical_record(entity_type, aggregate_id, source_key, payload, immutable, dependencies=()):
    return CanonicalRecord(
        entity_type,
        aggregate_id,
        source_key,
        semantic_hash(payload),
        payload,
        tuple(immutable),
        tuple(dependencies),
    )


def full_population(*, reservation_status="CONFIRMED", predecessor=None):
    organization_id = uuid4()
    property_id = uuid4()
    rental_unit_id = uuid4()
    reservation_id = uuid4()
    organization = canonical_record(
        "organization",
        organization_id,
        "organization:production",
        {
            "organization_id": organization_id,
            "organization_code": "PAI-PROD-SEED",
            "display_name": "Synthetic Organization",
            "organization_status": "ACTIVE",
            "data_environment": "TEST",
        },
        ("organization_id", "organization_code", "data_environment"),
    )
    property_record = canonical_record(
        "property",
        property_id,
        "property:one",
        {
            "property_id": property_id,
            "organization_id": organization_id,
            "property_code": "PROP-SYNTHETIC-1",
            "display_name": "Synthetic Property",
            "timezone_name": "Asia/Seoul",
            "active": True,
        },
        ("property_id", "organization_id", "property_code"),
        (organization_id,),
    )
    unit = canonical_record(
        "rental_unit",
        rental_unit_id,
        "rental-unit:one",
        {
            "rental_unit_id": rental_unit_id,
            "rental_unit_code": "UNIT-SYNTHETIC-1",
            "property_id": property_id,
            "display_name": "Synthetic Unit",
            "active": True,
        },
        ("rental_unit_id", "property_id", "rental_unit_code"),
        (property_id,),
    )
    reservation = canonical_record(
        "reservation",
        reservation_id,
        "reservation:one",
        {
            "reservation_id": reservation_id,
            "reservation_code": "RSV-SYNTHETIC-1",
            "property_id": property_id,
            "rental_unit_id": rental_unit_id,
            "source_channel": "LEGACY_MASTER",
            "external_reservation_id": "LEGACY-SYNTHETIC-1",
            "reservation_status": reservation_status,
            "check_in_at": NOW,
            "check_out_at": NOW + timedelta(days=2),
        },
        (
            "reservation_id",
            "reservation_code",
            "property_id",
            "rental_unit_id",
            "source_channel",
            "external_reservation_id",
        ),
        (property_id, rental_unit_id),
    )
    records = (organization, property_record, unit, reservation)
    observations = {
        item.source_key: SourceObservation(
            item.source_key.split(":", 1)[0],
            item.source_key.split(":", 1)[1],
            item.payload,
        )
        for item in records
    }
    hashes = {item.source_key: item.semantic_hash for item in records}
    manifest = SnapshotManifest(
        uuid4(),
        uuid4(),
        predecessor,
        NOW,
        NOW + timedelta(seconds=1),
        _fixture_runtime_reference(observations),
        tuple(observations),
        hashes,
        {},
        semantic_hash({"source_row_identities": sorted(observations), "source_semantic_hashes": hashes, "file_content_hashes": {}}),
        observations,
    )
    return manifest, Population(records)


def test_pre_cutover_pool_is_direct_login_with_no_authority_role(stage_b_pool):
    with stage_b_pool._connection() as connection:
        row = connection.execute(
            "SELECT session_user, current_user, current_setting('propertyai.cleaner_postgres_mode') AS mode"
        ).fetchone()
        assert row == {
            "session_user": ROLE,
            "current_user": ROLE,
            "mode": "PRE_CUTOVER_MIGRATION",
        }
        for authority_role in (
            "propertyai_owner",
            "propertyai_migrator",
            "propertyai_app_runtime",
            "propertyai_async_worker",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                connection.execute(f"SET ROLE {authority_role}")
            connection.rollback()


def test_positive_dml_idempotency_revision_and_reconciliation(stage_b_pool):
    repository = PostgresStageBMigrationRepository(stage_b_pool, mutation_attribution_observer=lambda: ())
    manifest, population = full_population()
    first = backfill_engine(repository).apply(manifest, population)
    assert first.applied == 4
    replay = backfill_engine(repository).apply(manifest, population)
    assert replay.noop == 4
    reservation = population.records[-1]
    assert repository.target_payload(reservation)["source_version"] == 1
    report = ReconciliationEngine(repository).reconcile(manifest, population)
    assert report.clean, [
        (gate.gate, gate.facts) for gate in report.gates if gate.blocking_count
    ]

    changed_records = list(population.records)
    previous = changed_records[-1]
    changed_payload = dict(previous.payload, reservation_status="CANCELLED")
    changed_records[-1] = canonical_record(
        previous.entity_type,
        previous.aggregate_id,
        previous.source_key,
        changed_payload,
        previous.immutable_fields,
        previous.dependencies,
    )
    observations = {
        item.source_key: SourceObservation(
            item.source_key.split(":", 1)[0],
            item.source_key.split(":", 1)[1],
            item.payload,
        )
        for item in changed_records
    }
    hashes = {item.source_key: item.semantic_hash for item in changed_records}
    next_manifest = SnapshotManifest(
        uuid4(),
        uuid4(),
        manifest.snapshot_id,
        NOW,
        NOW + timedelta(seconds=1),
        _fixture_runtime_reference(observations),
        tuple(observations),
        hashes,
        {},
        semantic_hash({"source_row_identities": sorted(observations), "source_semantic_hashes": hashes, "file_content_hashes": {}}),
        observations,
    )
    next_population = Population(tuple(changed_records))
    summary = backfill_engine(repository).apply(next_manifest, next_population)
    assert summary.applied == 1 and summary.noop == 3
    assert repository.target_payload(changed_records[-1])["source_version"] == 2
    assert ReconciliationEngine(repository).reconcile(next_manifest, next_population).clean


def test_full_cleaner_graph_positive_dml_and_current_revision_cycle(stage_b_pool):
    repository = PostgresStageBMigrationRepository(stage_b_pool, mutation_attribution_observer=lambda: ())
    ids = {name: uuid4() for name in (
        "organization", "property", "unit", "party", "external", "roster",
        "reservation", "cleaning", "revision", "campaign", "candidate", "assignment",
        "unavailability", "reassignment",
    )}
    suffix = ids["organization"].hex
    start = NOW + timedelta(days=4)
    end = start + timedelta(hours=2)
    definitions = [
        ("organization", ids["organization"], {"organization_id": ids["organization"], "organization_code": f"ORG-{suffix}", "display_name": "Graph Org", "organization_status": "ACTIVE", "data_environment": "TEST"}, ("organization_id", "organization_code", "data_environment"), ()),
        ("property", ids["property"], {"property_id": ids["property"], "organization_id": ids["organization"], "property_code": f"PROP-{suffix}", "display_name": "Graph Property", "timezone_name": "Asia/Seoul", "active": True}, ("property_id", "organization_id", "property_code"), (ids["organization"],)),
        ("rental_unit", ids["unit"], {"rental_unit_id": ids["unit"], "rental_unit_code": f"UNIT-{suffix}", "property_id": ids["property"], "display_name": "Graph Unit", "active": True}, ("rental_unit_id", "property_id", "rental_unit_code"), (ids["property"],)),
        ("party", ids["party"], {"party_id": ids["party"], "party_code": f"CLEANER-{suffix}", "display_name": "Synthetic Cleaner", "data_environment": "TEST", "active": True}, ("party_id", "party_code", "data_environment"), ()),
        ("cleaner_profile", ids["party"], {"cleaner_party_id": ids["party"], "operational_status": "ACTIVE", "max_daily_work_minutes": 480, "max_daily_jobs": 4}, ("cleaner_party_id",), (ids["party"],)),
        ("external_identity", ids["external"], {"external_identity_id": ids["external"], "party_id": ids["party"], "provider": "TELEGRAM", "provider_user_id": f"tg-{suffix}", "provider_chat_id": None, "bound_at": NOW, "revoked_at": None, "source_ref": "synthetic"}, ("external_identity_id", "party_id", "provider", "provider_user_id", "bound_at"), (ids["party"],)),
        ("cleaner_property_roster", ids["roster"], {"roster_id": ids["roster"], "cleaner_party_id": ids["party"], "property_id": ids["property"], "roster_status": "ACTIVE", "offer_tier": 1, "priority_within_tier": 0, "eligible_from": NOW, "eligible_until": None}, ("roster_id", "cleaner_party_id", "property_id"), (ids["party"], ids["property"])),
        ("reservation", ids["reservation"], {"reservation_id": ids["reservation"], "reservation_code": f"RSV-{suffix}", "property_id": ids["property"], "rental_unit_id": ids["unit"], "source_channel": "LEGACY_MASTER", "external_reservation_id": f"LEG-{suffix}", "reservation_status": "CONFIRMED", "check_in_at": NOW, "check_out_at": start}, ("reservation_id", "reservation_code", "property_id", "rental_unit_id", "source_channel", "external_reservation_id"), (ids["property"], ids["unit"])),
        ("cleaning_job", ids["cleaning"], {"cleaning_id": ids["cleaning"], "cleaning_code": f"CLN-{suffix}", "reservation_id": ids["reservation"], "property_id": ids["property"], "rental_unit_id": ids["unit"], "schedule_source_type": "RESERVATION_CHECKOUT", "cleaning_status": "OFFERING", "current_schedule_revision_id": ids["revision"]}, ("cleaning_id", "cleaning_code", "reservation_id", "property_id", "rental_unit_id", "schedule_source_type"), (ids["reservation"], ids["property"], ids["unit"])),
        ("cleaning_schedule_revision", ids["revision"], {"schedule_revision_id": ids["revision"], "cleaning_id": ids["cleaning"], "revision_no": 1, "service_window_start_at": start, "service_deadline_at": end + timedelta(hours=1), "required_work_minutes": 120, "source_checkout_at": start, "source_reservation_version": 1, "change_reason_code": "LEGACY_EXACT_RECONSTRUCTION", "source_command_id": None}, ("schedule_revision_id", "cleaning_id", "revision_no", "service_window_start_at", "service_deadline_at", "required_work_minutes", "source_checkout_at", "source_reservation_version"), (ids["cleaning"],)),
        ("cleaning_offer_campaign", ids["campaign"], {"campaign_id": ids["campaign"], "cleaning_id": ids["cleaning"], "schedule_revision_id": ids["revision"], "campaign_no": 1, "campaign_status": "CLOSED", "open_tier_floor": 1, "max_tier": 1, "tier_expand_after_minutes": None, "acceptance_cutoff_at": NOW + timedelta(days=3), "base_fee_krw": 50000, "replacement_urgency": "NORMAL", "urgent_premium_krw": 0, "total_agreed_fee_krw": 50000, "urgent_premium_policy_version": None, "opened_at": NOW + timedelta(days=1), "closed_at": NOW + timedelta(days=1, minutes=10), "closed_reason_code": "ACCEPTED"}, ("campaign_id", "cleaning_id", "schedule_revision_id", "campaign_no", "base_fee_krw", "replacement_urgency", "urgent_premium_krw", "total_agreed_fee_krw", "opened_at"), (ids["cleaning"], ids["revision"])),
        ("cleaning_offer_candidate", ids["candidate"], {"offer_candidate_id": ids["candidate"], "campaign_id": ids["campaign"], "cleaner_party_id": ids["party"], "tier_no": 1, "candidate_status": "ACCEPTED", "proposal_version": 1, "proposed_start_at": start, "proposed_end_at": end, "proposed_buffer_before_minutes": 0, "proposed_buffer_after_minutes": 0, "buffer_basis": "NOT_APPLIED", "buffer_policy_ref": None, "evaluated_at": NOW + timedelta(days=1), "declined_at": None, "accepted_at": NOW + timedelta(days=1, minutes=10)}, ("offer_candidate_id", "campaign_id", "cleaner_party_id", "tier_no", "evaluated_at"), (ids["campaign"], ids["party"])),
        ("cleaning_assignment", ids["assignment"], {"assignment_id": ids["assignment"], "cleaning_id": ids["cleaning"], "assignment_no": 1, "schedule_revision_id": ids["revision"], "campaign_id": ids["campaign"], "offer_candidate_id": ids["candidate"], "accepted_proposal_version": 1, "cleaner_party_id": ids["party"], "assignment_source": "OFFER_ACCEPTED", "assignment_status": "RELEASED", "booked_at": NOW + timedelta(days=1, minutes=10), "scheduled_start_at": start, "scheduled_end_at": end, "work_minutes_snapshot": 120, "travel_buffer_before_minutes": 0, "travel_buffer_after_minutes": 0, "buffer_basis": "NOT_APPLIED", "buffer_policy_ref": None, "base_fee_krw": 50000, "replacement_urgency": "NORMAL", "urgent_premium_krw": 0, "total_agreed_fee_krw": 50000, "urgent_premium_policy_version": None, "ended_at": NOW + timedelta(days=2), "end_reason_code": "EARLY_UNAVAILABLE"}, ("assignment_id", "cleaning_id", "assignment_no", "schedule_revision_id", "campaign_id", "offer_candidate_id", "accepted_proposal_version", "cleaner_party_id", "assignment_source", "booked_at", "scheduled_start_at", "scheduled_end_at", "work_minutes_snapshot", "base_fee_krw", "replacement_urgency", "urgent_premium_krw", "total_agreed_fee_krw"), (ids["cleaning"], ids["revision"], ids["campaign"], ids["candidate"], ids["party"])),
        ("cleaner_unavailability_case", ids["unavailability"], {"unavailability_id": ids["unavailability"], "cleaning_id": ids["cleaning"], "schedule_revision_id": ids["revision"], "original_assignment_id": ids["assignment"], "cleaner_party_id": ids["party"], "case_status": "CONFIRMED", "availability_classification": "EARLY_UNAVAILABLE", "replacement_urgency": "NORMAL", "reason_code": "SYNTHETIC_UNAVAILABLE", "reason_text": None, "occurred_at": NOW + timedelta(days=2)}, ("unavailability_id", "cleaning_id", "schedule_revision_id", "original_assignment_id", "cleaner_party_id", "occurred_at"), (ids["cleaning"], ids["revision"], ids["assignment"], ids["party"])),
        ("cleaner_reassignment_request", ids["reassignment"], {"reassignment_request_id": ids["reassignment"], "unavailability_id": ids["unavailability"], "cleaning_id": ids["cleaning"], "cleaner_party_id": ids["party"], "original_assignment_id": ids["assignment"], "requested_schedule_revision_id": ids["revision"], "request_no": 1, "request_status": "CONTINUE_REPLACEMENT", "requested_at": NOW + timedelta(days=2), "decided_at": NOW + timedelta(days=2, minutes=5), "decision_code": "REPLACEMENT_REQUIRED"}, ("reassignment_request_id", "unavailability_id", "cleaning_id", "cleaner_party_id", "original_assignment_id", "requested_schedule_revision_id", "request_no", "requested_at"), (ids["unavailability"], ids["cleaning"], ids["party"], ids["assignment"], ids["revision"])),
    ]
    records = tuple(
        canonical_record(entity, aggregate_id, f"{entity}:graph-{index}", payload, immutable, dependencies)
        for index, (entity, aggregate_id, payload, immutable, dependencies) in enumerate(definitions)
    )
    observations = {
        item.source_key: SourceObservation(*item.source_key.split(":", 1), item.payload)
        for item in records
    }
    hashes = {item.source_key: item.semantic_hash for item in records}
    manifest = SnapshotManifest(
        uuid4(), uuid4(), None, NOW, NOW + timedelta(seconds=1), _fixture_runtime_reference(observations),
        tuple(observations), hashes, {}, semantic_hash({"source_row_identities": sorted(observations), "source_semantic_hashes": hashes, "file_content_hashes": {}}), observations,
    )
    population = Population(records)
    summary = backfill_engine(repository).apply(manifest, population)
    assert summary.applied == len(records)
    report = ReconciliationEngine(repository).reconcile(manifest, population)
    assert report.clean, [
        (gate.gate, gate.facts) for gate in report.gates if gate.blocking_count
    ]
    with stage_b_pool._connection() as connection:
        stored = connection.execute(
            "SELECT current_schedule_revision_id FROM propertyai.cleaning_job WHERE cleaning_id=%s",
            (ids["cleaning"],),
        ).fetchone()
        assert stored["current_schedule_revision_id"] == ids["revision"]
        busy = connection.execute(
            "SELECT lower(busy_window) AS starts, upper(busy_window) AS ends FROM propertyai.cleaning_assignment WHERE assignment_id=%s",
            (ids["assignment"],),
        ).fetchone()
        assert busy == {"starts": start, "ends": end}


def test_actual_negative_dml_and_zero_effect_authority_proof(stage_b_pool):
    repository = PostgresStageBMigrationRepository(stage_b_pool, mutation_attribution_observer=lambda: ())
    before = repository.safety_counters()
    assert before["migration_role_safe"] is True
    with stage_b_pool._connection() as connection:
        forbidden = (
            "UPDATE propertyai.authority_epoch SET current_epoch=current_epoch",
            "INSERT INTO propertyai.domain_event(aggregate_type,aggregate_id,event_type,command_id,payload,occurred_at) VALUES ('X',gen_random_uuid(),'X',gen_random_uuid(),'{}',clock_timestamp())",
            "INSERT INTO propertyai.business_scheduled_action(scheduled_action_id,action_type,aggregate_type,aggregate_id,due_at,available_at,idempotency_key,payload,max_attempts) VALUES (gen_random_uuid(),'X','X',gen_random_uuid(),clock_timestamp(),clock_timestamp(),'forbidden-action','{}',1)",
            "INSERT INTO propertyai.integration_outbox(outbox_id,event_type,aggregate_type,aggregate_id,destination_type,available_at,idempotency_key,payload,max_attempts) VALUES (gen_random_uuid(),'X','X',gen_random_uuid(),'X',clock_timestamp(),'forbidden-outbox','{}',1)",
        )
        for statement in forbidden:
            with pytest.raises(errors.InsufficientPrivilege):
                connection.execute(statement)
            connection.rollback()
    after = repository.safety_counters()
    assert after["authority_epoch_mutations"] == 0
    assert after["domain_event_mutations"] == 0
    assert after["scheduled_action_mutations"] == 0
    assert after["outbox_mutations"] == 0


def _stage_b_config(cluster, *, expected_database="postgres"):
    return PostgresStageBMigrationConfig(
        dsn=cluster.login_dsn(ROLE),
        expected_session_user=ROLE,
        expected_database=expected_database,
        data_environment="TEST",
        min_size=1,
        max_size=1,
    )


def _run_connection_guard(cluster, *, expected_database="postgres"):
    config = _stage_b_config(cluster, expected_database=expected_database)
    pool = PostgresStageBMigrationPool(config)
    with psycopg.connect(config.dsn, row_factory=dict_row) as connection:
        pool._normalize_and_verify(connection)


def _manifest_for_records(records, *, predecessor=None, scan_offset=20):
    observations = {
        item.source_key: SourceObservation(*item.source_key.split(":", 1), item.payload)
        for item in records
    }
    hashes = {item.source_key: item.semantic_hash for item in records}
    started = NOW + timedelta(seconds=scan_offset)
    return SnapshotManifest(
        uuid4(),
        uuid4(),
        predecessor,
        started,
        started + timedelta(seconds=1),
        _fixture_runtime_reference(observations),
        tuple(observations),
        hashes,
        {},
        semantic_hash(
            {
                "source_row_identities": sorted(observations),
                "source_semantic_hashes": hashes,
                "file_content_hashes": {},
            }
        ),
        observations,
    )


def test_exact_database_schema_and_flyway_contract(stage_b_cluster):
    _run_connection_guard(stage_b_cluster)
    rows = stage_b_cluster.psql(
        user=ROLE,
        sql_text="SELECT version, script, checksum, success FROM propertyai.flyway_schema_history ORDER BY version",
    ).stdout
    for version, script, checksum, success in _EXPECTED_FLYWAY_STATE:
        assert version in rows and script in rows and str(checksum) in rows
        assert success is True


def test_wrong_database_identity_fails_closed(stage_b_cluster):
    with pytest.raises(PostgresRoleMismatchError, match="database_name"):
        _run_connection_guard(stage_b_cluster, expected_database="not_the_stage_b_database")


def test_missing_propertyai_schema_fails_closed(stage_b_cluster):
    stage_b_cluster.psql(sql_text="ALTER SCHEMA propertyai RENAME TO propertyai_missing")
    with pytest.raises(PostgresRoleMismatchError, match="schema is absent"):
        _run_connection_guard(stage_b_cluster)


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM propertyai.flyway_schema_history WHERE version='20260904.108'",
        "UPDATE propertyai.flyway_schema_history SET checksum=checksum+1 WHERE version='20260904.107'",
    ],
    ids=["seven_of_eight", "tampered_checksum"],
)
def test_incomplete_or_tampered_flyway_history_fails_closed(stage_b_cluster, mutation):
    stage_b_cluster.psql(sql_text=mutation)
    with pytest.raises(PostgresRoleMismatchError, match="Flyway state mismatch"):
        _run_connection_guard(stage_b_cluster)


def test_postgres_source_key_target_remap_is_rejected_before_second_target_write(stage_b_pool):
    repository = PostgresStageBMigrationRepository(
        stage_b_pool, mutation_attribution_observer=lambda: ()
    )
    first_manifest, population = full_population()
    backfill_engine(repository).apply(first_manifest, population)
    first = population.records[-1]
    second_id = uuid4()
    second_payload = dict(
        first.payload,
        reservation_id=second_id,
        reservation_code=f"RSV-{second_id.hex}",
        external_reservation_id=f"LEG-{second_id.hex}",
    )
    second = canonical_record(
        "reservation",
        second_id,
        first.source_key,
        second_payload,
        first.immutable_fields,
        first.dependencies,
    )
    second_manifest = _manifest_for_records(
        (second,), predecessor=first_manifest.snapshot_id, scan_offset=30
    )
    with pytest.raises(ImmutableIdentityConflict, match="remap"):
        backfill_engine(repository).apply(second_manifest, Population((second,)))
    binding = repository.binding_for(first.source_key)
    assert binding is not None and binding.aggregate_id == first.aggregate_id
    assert repository.target_payload(second) is None
    report = ReconciliationEngine(repository).reconcile(second_manifest, Population((second,)))
    blocked = {gate.gate for gate in report.gates if gate.blocking_count}
    assert {"R10_LINEAGE_AND_IDEMPOTENCY", "R12_UNATTRIBUTED_OBSERVED_MUTATION_COUNT", "R14_UNAPPLIED_SOURCE_DELTA"} <= blocked


def test_postgres_old_snapshot_and_newer_snapshot_semantics(stage_b_pool):
    repository = PostgresStageBMigrationRepository(
        stage_b_pool, mutation_attribution_observer=lambda: ()
    )
    first_manifest, population = full_population()
    backfill_engine(repository).apply(first_manifest, population)
    reservation = population.records[-1]
    changed = canonical_record(
        reservation.entity_type,
        reservation.aggregate_id,
        reservation.source_key,
        dict(reservation.payload, reservation_status="CANCELLED"),
        reservation.immutable_fields,
        reservation.dependencies,
    )
    second_manifest = _manifest_for_records(
        (changed,), predecessor=first_manifest.snapshot_id, scan_offset=40
    )
    backfill_engine(repository).apply(second_manifest, Population((changed,)))
    assert repository.target_payload(changed)["source_version"] == 2
    with pytest.raises(OldSnapshotError):
        backfill_engine(repository).apply(first_manifest, population)


def _insert_protected_rows(cluster, suffix):
    cluster.psql(
        sql_text=f"""
        WITH cmd AS (
            INSERT INTO propertyai.command_receipt(
                command_id, authority_scope_code, command_type, idempotency_key,
                request_payload, source_channel_code, principal_type, authority_epoch,
                decided_at
            ) VALUES (
                gen_random_uuid(), 'CLEANER_SCHEDULING', 'BASELINE_TEST', 'cmd-{suffix}',
                '{{}}', 'MIGRATION', 'MIGRATION', 0, clock_timestamp()
            ) RETURNING command_id
        )
        INSERT INTO propertyai.domain_event(
            aggregate_type, aggregate_id, event_type, command_id, payload, occurred_at
        ) SELECT 'TEST', gen_random_uuid(), 'TEST_EVENT', command_id, '{{}}', clock_timestamp() FROM cmd;
        INSERT INTO propertyai.business_scheduled_action(
            scheduled_action_id, action_type, aggregate_type, aggregate_id,
            due_at, available_at, idempotency_key, payload, max_attempts
        ) VALUES (
            gen_random_uuid(), 'TEST', 'TEST', gen_random_uuid(), clock_timestamp(),
            clock_timestamp(), 'action-{suffix}', '{{}}', 1
        );
        INSERT INTO propertyai.integration_outbox(
            outbox_id, event_type, aggregate_type, aggregate_id, destination_type,
            available_at, idempotency_key, payload, max_attempts
        ) VALUES (
            gen_random_uuid(), 'TEST', 'TEST', gen_random_uuid(), 'TEST',
            clock_timestamp(), 'outbox-{suffix}', '{{}}', 1
        );
        """
    )


def test_r13_uses_repository_baseline_deltas_not_absolute_protected_row_counts(stage_b_cluster):
    _insert_protected_rows(stage_b_cluster, "before")
    config = _stage_b_config(stage_b_cluster)
    with PostgresStageBMigrationPool(config) as pool:
        repository = PostgresStageBMigrationRepository(
            pool, mutation_attribution_observer=lambda: ()
        )
        baseline = repository.safety_counters()
        assert baseline["domain_event_mutations"] == 0
        assert baseline["scheduled_action_mutations"] == 0
        assert baseline["outbox_mutations"] == 0
        _insert_protected_rows(stage_b_cluster, "after")
        after = repository.safety_counters()
        assert after["domain_event_mutations"] == 1
        assert after["scheduled_action_mutations"] == 1
        assert after["outbox_mutations"] == 1
        manifest = _manifest_for_records((), scan_offset=50)
        report = ReconciliationEngine(repository).reconcile(manifest, Population(()))
        r13 = next(g for g in report.gates if g.gate == "R13_PRE_CUTOVER_ZERO_EFFECT_AND_PRIVILEGE")
        assert r13.blocking_count >= 3


def test_r12_mutation_attribution_is_fail_closed_when_evidence_unavailable(stage_b_pool):
    manifest = _manifest_for_records((), scan_offset=60)
    unavailable = PostgresStageBMigrationRepository(stage_b_pool)
    report = ReconciliationEngine(unavailable).reconcile(manifest, Population(()))
    r12 = next(g for g in report.gates if g.gate == "R12_UNATTRIBUTED_OBSERVED_MUTATION_COUNT")
    assert r12.blocking_count == 1

    canonical_and_aliases = (
        ("KNOWN_APPLICATION_WRITER", "KNOWN_APPLICATION_WRITER"),
        ("VERIFIED_KNOWN_APPLICATION_WRITER", "KNOWN_APPLICATION_WRITER"),
        ("ALLOWLISTED_HUMAN_NOTION_ACTOR", "ALLOWLISTED_HUMAN_NOTION_ACTOR"),
        ("VERIFIED_MAINTENANCE", "VERIFIED_MAINTENANCE"),
        ("VERIFIED_MAINTENANCE_EVIDENCE", "VERIFIED_MAINTENANCE"),
    )
    for supplied, canonical in canonical_and_aliases:
        observation = MutationAttributionObservation(supplied, f"test:{supplied}")
        assert observation.category == canonical
        repository = PostgresStageBMigrationRepository(
            stage_b_pool, mutation_attribution_observer=lambda observation=observation: (observation,)
        )
        assert repository.safety_counters()["unattributed_mutations"] == 0
        clean = ReconciliationEngine(repository).reconcile(manifest, Population(()))
        r12 = next(g for g in clean.gates if g.gate == "R12_UNATTRIBUTED_OBSERVED_MUTATION_COUNT")
        assert r12.blocking_count == 0

    unattributed = MutationAttributionObservation("UNATTRIBUTED", "test:unattributed")
    repository = PostgresStageBMigrationRepository(
        stage_b_pool, mutation_attribution_observer=lambda: (unattributed,)
    )
    assert repository.safety_counters()["unattributed_mutations"] == 1

    unknown = MutationAttributionObservation("UNKNOWN_LABEL", "test:unknown")
    assert unknown.category == "UNATTRIBUTED"
    assert normalize_mutation_attribution_category("UNKNOWN_LABEL") == "UNATTRIBUTED"
    repository = PostgresStageBMigrationRepository(
        stage_b_pool, mutation_attribution_observer=lambda: (unknown,)
    )
    assert repository.safety_counters()["unattributed_mutations"] == 1

    def failing_observer():
        raise RuntimeError("observer unavailable")

    failed = PostgresStageBMigrationRepository(
        stage_b_pool, mutation_attribution_observer=failing_observer
    )
    assert failed.safety_counters()["unattributed_mutations"] == 1


def test_empty_population_table_dml_is_denied(stage_b_pool):
    with stage_b_pool._connection() as connection:
        for statement in (
            "INSERT INTO propertyai.organization_member DEFAULT VALUES",
            "UPDATE propertyai.organization_member SET membership_status=membership_status",
            "INSERT INTO propertyai.cleaner_schedule_block DEFAULT VALUES",
            "UPDATE propertyai.cleaner_schedule_block SET cancelled_at=cancelled_at",
        ):
            with pytest.raises(errors.InsufficientPrivilege):
                connection.execute(statement)
            connection.rollback()


@pytest.mark.parametrize(
    "grant_sql",
    [
        f"GRANT CREATE ON SCHEMA propertyai TO {ROLE}",
        f"GRANT DELETE ON propertyai.property TO {ROLE}",
        f"GRANT TRUNCATE ON propertyai.property TO {ROLE}",
        f"GRANT TRIGGER ON propertyai.property TO {ROLE}",
        f"GRANT INSERT ON propertyai.organization_member TO {ROLE}",
        f"GRANT UPDATE (membership_status) ON propertyai.organization_member TO {ROLE}",
        f"GRANT EXECUTE ON FUNCTION propertyai.lock_and_verify_authority_epoch(text,bigint) TO {ROLE}",
        f"CREATE ROLE stage_b_extra_drift NOLOGIN; GRANT stage_b_extra_drift TO {ROLE}",
        f"GRANT propertyai_app_runtime TO {ROLE}",
    ],
    ids=[
        "schema_create", "delete", "truncate", "trigger", "forbidden_insert",
        "forbidden_update", "function_execute", "extra_membership", "set_authority_role",
    ],
)
def test_privilege_verifier_detects_each_forbidden_drift(stage_b_cluster, grant_sql):
    stage_b_cluster.psql(sql_text=grant_sql)
    with pytest.raises(subprocess.CalledProcessError):
        stage_b_cluster.psql(
            user=ROLE,
            file=REPO_ROOT / "db" / "stage_b" / "verify_migration_role.sql",
        )


def test_postgres_reconciliation_unknown_obligation_category_blocks_r15(stage_b_pool):
    repository = PostgresStageBMigrationRepository(
        stage_b_pool, mutation_attribution_observer=lambda: ()
    )
    manifest = _manifest_for_records((), scan_offset=70)
    obligation = MigrationObligation(
        obligation_id=uuid4(),
        source_key="legacy:unknown-pg",
        category="UNKNOWN_KIND",
        reason="postgres-backed reconciliation adversarial category",
    )
    report = ReconciliationEngine(repository).reconcile(
        manifest, Population((), (obligation,))
    )
    r15 = next(g for g in report.gates if g.gate == "R15_UNRESOLVED_STATE_MUTATING_OBLIGATION")
    assert r15.blocking_count == 1
    assert "UNKNOWN_KIND" in r15.facts[0]
    assert report.effect_only_pending_count == 0
    assert not report.clean


def test_postgres_multiple_retained_campaign_assignment_sequences_are_unique(stage_b_pool):
    repository = PostgresStageBMigrationRepository(
        stage_b_pool, mutation_attribution_observer=lambda: ()
    )
    _, base_population = full_population()
    records = list(base_population.records)
    organization, property_record, unit, reservation = records
    cleaning_id = uuid4()
    revision_id = uuid4()
    suffix = cleaning_id.hex
    start = NOW + timedelta(days=5)

    cleaning = canonical_record(
        "cleaning_job",
        cleaning_id,
        "cleaning_job:multi-retained",
        {
            "cleaning_id": cleaning_id,
            "cleaning_code": f"CLN-MULTI-{suffix}",
            "reservation_id": reservation.aggregate_id,
            "property_id": property_record.aggregate_id,
            "rental_unit_id": unit.aggregate_id,
            "schedule_source_type": "RESERVATION_CHECKOUT",
            "cleaning_status": "OFFERING",
            "current_schedule_revision_id": revision_id,
        },
        IMMUTABLE_FIELDS["cleaning_job"],
        (reservation.aggregate_id, property_record.aggregate_id, unit.aggregate_id),
    )
    revision = canonical_record(
        "cleaning_schedule_revision",
        revision_id,
        "cleaning_schedule_revision:multi-retained",
        {
            "schedule_revision_id": revision_id,
            "cleaning_id": cleaning_id,
            "revision_no": 1,
            "service_window_start_at": start,
            "service_deadline_at": start + timedelta(days=4),
            "required_work_minutes": 120,
            "source_checkout_at": None,
            "source_reservation_version": None,
            "change_reason_code": "LEGACY_EXACT_RECONSTRUCTION",
            "source_command_id": None,
        },
        IMMUTABLE_FIELDS["cleaning_schedule_revision"],
        (cleaning_id,),
    )
    records.extend((cleaning, revision))

    cleaner_ids = [uuid4() for _ in range(3)]
    for index, cleaner_id in enumerate(cleaner_ids):
        records.extend(
            (
                canonical_record(
                    "party",
                    cleaner_id,
                    f"party:multi-{index}",
                    {
                        "party_id": cleaner_id,
                        "party_code": f"CLEANER-MULTI-{suffix}-{index}",
                        "display_name": f"Cleaner {index}",
                        "data_environment": "TEST",
                        "active": True,
                    },
                    IMMUTABLE_FIELDS["party"],
                ),
                canonical_record(
                    "cleaner_profile",
                    cleaner_id,
                    f"cleaner_profile:multi-{index}",
                    {
                        "cleaner_party_id": cleaner_id,
                        "operational_status": "ACTIVE",
                        "max_daily_work_minutes": 480,
                        "max_daily_jobs": 4,
                    },
                    IMMUTABLE_FIELDS["cleaner_profile"],
                    (cleaner_id,),
                ),
            )
        )

    pages = (
        "12345678-1234-4234-9234-123456789abc",
        "22345678-1234-4234-9234-123456789abc",
        "32345678-1234-4234-9234-123456789abc",
    )
    actions = []
    for index, (page_id, cleaner_id) in enumerate(zip(pages, cleaner_ids)):
        offered = NOW + timedelta(hours=index)
        accepted = offered + timedelta(minutes=5)
        scheduled_start = start + timedelta(days=index)
        actions.append(
            {
                "assignment_page_id": page_id,
                "cleaning_id": cleaning_id,
                "schedule_revision_id": revision_id,
                "cleaner_party_id": cleaner_id,
                "persisted_actual_action": True,
                "offered_at": offered,
                "accepted_at": accepted,
                "acceptance_cutoff_at": offered + timedelta(minutes=30),
                "scheduled_start_at": scheduled_start,
                "scheduled_end_at": scheduled_start + timedelta(hours=2),
                "base_fee_krw": 50_000,
                "urgent_premium_krw": 0,
                "assignment_status": "RELEASED",
                "ended_at": accepted + timedelta(minutes=1),
                "end_reason_code": "REASSIGNED",
                "campaign_no": 77,
                "assignment_no": 88,
            }
        )
    ancestry = minimal_exact_assignment_ancestry_batch(actions)
    assert [row["campaign"]["campaign_no"] for row in ancestry] == [1, 2, 3]
    assert [row["assignment"]["assignment_no"] for row in ancestry] == [1, 2, 3]

    for index, row in enumerate(ancestry):
        campaign = row["campaign"]
        candidate = row["candidate"]
        assignment = row["assignment"]
        records.extend(
            (
                canonical_record(
                    "cleaning_offer_campaign",
                    campaign["campaign_id"],
                    f"campaign:multi-{index}",
                    campaign,
                    IMMUTABLE_FIELDS["cleaning_offer_campaign"],
                    (cleaning_id, revision_id),
                ),
                canonical_record(
                    "cleaning_offer_candidate",
                    candidate["offer_candidate_id"],
                    f"candidate:multi-{index}",
                    candidate,
                    IMMUTABLE_FIELDS["cleaning_offer_candidate"],
                    (campaign["campaign_id"], candidate["cleaner_party_id"]),
                ),
                canonical_record(
                    "cleaning_assignment",
                    assignment["assignment_id"],
                    f"assignment:multi-{index}",
                    assignment,
                    IMMUTABLE_FIELDS["cleaning_assignment"],
                    (
                        cleaning_id,
                        revision_id,
                        campaign["campaign_id"],
                        candidate["offer_candidate_id"],
                        assignment["cleaner_party_id"],
                    ),
                ),
            )
        )

    manifest = _manifest_for_records(tuple(records), scan_offset=80)
    summary = backfill_engine(repository).apply(manifest, Population(tuple(records)))
    assert summary.applied == len(records)
    with stage_b_pool._connection() as connection:
        campaigns = connection.execute(
            "SELECT campaign_no FROM propertyai.cleaning_offer_campaign "
            "WHERE cleaning_id=%s ORDER BY campaign_no",
            (cleaning_id,),
        ).fetchall()
        assignments = connection.execute(
            "SELECT assignment_no FROM propertyai.cleaning_assignment "
            "WHERE cleaning_id=%s ORDER BY assignment_no",
            (cleaning_id,),
        ).fetchall()
    assert [row["campaign_no"] for row in campaigns] == [1, 2, 3]
    assert [row["assignment_no"] for row in assignments] == [1, 2, 3]

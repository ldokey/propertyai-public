from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from propertyai_core.stage_b.models import ReviewRequired, SnapshotManifest, SourceObservation
from propertyai_core.stage_b.population import (
    DOMAIN_SOURCE_AUTHORITY,
    ENTITY_PRIMARY_KEYS,
    PopulationBuilder,
    ScheduleWindow,
    minimal_exact_assignment_ancestry,
    minimal_exact_assignment_ancestry_batch,
    reconstruct_schedule_revisions,
    reconstruct_runtime_telegram_projection,
    require_reassignment_evidence,
    require_unavailability_evidence,
    reservation_next_version,
)
from propertyai_core.stage_b.normalize import semantic_hash


UTC = timezone.utc
BASE = datetime(2026, 9, 10, 1, tzinfo=UTC)


def test_domain_authority_is_specific_not_global_precedence():
    assert DOMAIN_SOURCE_AUTHORITY["roster"] == "NOTION_CLEANER_PROPERTY_ACCESS"
    assert DOMAIN_SOURCE_AUTHORITY["reservation"] == "CANONICAL_RESERVATION_PROJECTION"
    assert DOMAIN_SOURCE_AUTHORITY["assignment"].startswith("ASSIGNMENT_HISTORY_19")
    assert len(set(DOMAIN_SOURCE_AUTHORITY.values())) == len(DOMAIN_SOURCE_AUTHORITY)


def test_minimal_assignment_ancestry_retains_exactly_one_candidate():
    action = {
        "assignment_page_id": "12345678-1234-4234-9234-123456789abc",
        "cleaning_id": uuid4(),
        "schedule_revision_id": uuid4(),
        "cleaner_party_id": uuid4(),
        "persisted_actual_action": True,
        "offered_at": BASE,
        "accepted_at": BASE + timedelta(minutes=5),
        "acceptance_cutoff_at": BASE + timedelta(hours=1),
        "scheduled_start_at": BASE + timedelta(days=1),
        "scheduled_end_at": BASE + timedelta(days=1, hours=2),
        "base_fee_krw": 50_000,
        "urgent_premium_krw": 0,
        "unoffered_candidates": [str(uuid4()), str(uuid4())],
        "campaign_no": 77,
        "assignment_no": 88,
    }
    ancestry = minimal_exact_assignment_ancestry(action)
    assert set(ancestry) == {"campaign", "candidate", "assignment"}
    assert ancestry["campaign"]["max_tier"] == 1
    assert ancestry["candidate"]["tier_no"] == 1
    assert ancestry["candidate"]["proposal_version"] == 1
    assert ancestry["assignment"]["work_minutes_snapshot"] == 120
    assert ancestry["campaign"]["campaign_no"] == 1
    assert ancestry["assignment"]["assignment_no"] == 1
    assert "unoffered_candidates" not in str(ancestry)


def test_missing_active_assignment_ancestry_is_review_required():
    with pytest.raises(ReviewRequired):
        minimal_exact_assignment_ancestry({"persisted_actual_action": False})


def test_unproven_premium_economics_are_review_required():
    action = {
        "assignment_page_id": "12345678-1234-4234-9234-123456789abc",
        "cleaning_id": uuid4(),
        "schedule_revision_id": uuid4(),
        "cleaner_party_id": uuid4(),
        "persisted_actual_action": True,
        "offered_at": BASE,
        "accepted_at": BASE + timedelta(minutes=5),
        "acceptance_cutoff_at": BASE + timedelta(hours=1),
        "scheduled_start_at": BASE + timedelta(days=1),
        "scheduled_end_at": BASE + timedelta(days=1, hours=2),
        "base_fee_krw": 50_000,
        "urgent_premium_krw": 10_000,
    }
    with pytest.raises(ReviewRequired):
        minimal_exact_assignment_ancestry(action)


def test_schedule_revisions_are_exact_distinct_local_and_contiguous():
    cleaning_id = uuid4()
    first = ScheduleWindow(
        BASE,
        BASE + timedelta(hours=3),
        180,
        frozenset({"ACCEPTED_ASSIGNMENT"}),
    )
    duplicate = ScheduleWindow(
        BASE,
        BASE + timedelta(hours=3),
        180,
        frozenset({"CURRENT_CLEANING"}),
    )
    second = ScheduleWindow(
        BASE + timedelta(days=1),
        BASE + timedelta(days=1, hours=3),
        180,
        frozenset({"CURRENT_CLEANING"}),
    )
    revisions = reconstruct_schedule_revisions(cleaning_id, [second, duplicate, first])
    assert [row["revision_no"] for row in revisions] == [1, 2]
    assert all(row["source_checkout_at"] is None for row in revisions)
    assert all(row["source_reservation_version"] is None for row in revisions)


def test_schedule_checkout_provenance_is_never_half_fabricated():
    with pytest.raises(ValueError):
        ScheduleWindow(
            BASE,
            BASE + timedelta(hours=2),
            60,
            frozenset({"CURRENT_CLEANING"}),
            source_checkout_at=BASE,
        )


def test_reservation_revision_algorithm_has_no_caller_selected_version():
    state = {
        "reservation_status": "CONFIRMED",
        "check_in_at": BASE,
        "check_out_at": BASE + timedelta(days=2),
    }
    assert reservation_next_version(None, None, state) == (1, True)
    assert reservation_next_version(1, state, dict(state)) == (1, False)
    changed = dict(state, reservation_status="CANCELLED")
    assert reservation_next_version(1, state, changed) == (2, True)
    changed_again = dict(changed, check_out_at=BASE + timedelta(days=3))
    assert reservation_next_version(2, changed, changed_again) == (3, True)


def test_runtime_telegram_projection_requires_exact_party_and_roster_match():
    page = "12345678-1234-4234-9234-123456789abc"
    party_id = uuid4()
    projected = reconstruct_runtime_telegram_projection(
        [
            {
                "identity_id": "forbidden-mutable-registry-id",
                "nickname": "ignored",
                "party_notion_page_id": page,
                "telegram_user_id": "10001",
                "bound_at": BASE,
            }
        ],
        [{"notion_page_id": page, "party_id": party_id, "display_name": "ignored"}],
        [{"party_notion_page_id": page}],
    )
    assert projected[0]["party_id"] == party_id
    assert projected[0]["provider_user_id"] == "10001"
    assert "identity_id" not in projected[0]
    with pytest.raises(ReviewRequired):
        reconstruct_runtime_telegram_projection(
            [{"party_notion_page_id": page, "telegram_user_id": "10001", "bound_at": BASE}],
            [{"notion_page_id": page, "party_id": party_id}],
            [],
        )


def test_unavailability_and_reassignment_require_domain_specific_evidence():
    with pytest.raises(ReviewRequired):
        require_unavailability_evidence({"assignment_end_evidence": True})
    with pytest.raises(ReviewRequired):
        require_reassignment_evidence({"durable_request_evidence": True})
    assert require_unavailability_evidence(
        {"assignment_end_evidence": True, "unavailable_action_evidence": True}
    )
    assert require_reassignment_evidence(
        {"durable_request_evidence": True, "committed_decision_evidence": True}
    )


def test_schedule_window_required_work_minutes_is_exactly_derived():
    derived = ScheduleWindow(
        BASE,
        BASE + timedelta(hours=2),
        None,
        frozenset({"CURRENT_CLEANING"}),
    )
    assert derived.required_work_minutes == 120
    with pytest.raises(ValueError, match="mismatch"):
        ScheduleWindow(
            BASE,
            BASE + timedelta(hours=2),
            60,
            frozenset({"CURRENT_CLEANING"}),
        )
    with pytest.raises(ValueError, match="whole-minute"):
        ScheduleWindow(
            BASE,
            BASE + timedelta(hours=2, seconds=30),
            None,
            frozenset({"CURRENT_CLEANING"}),
        )
    with pytest.raises(ValueError, match="positive"):
        ScheduleWindow(BASE, BASE, None, frozenset({"CURRENT_CLEANING"}))


def _population_for(source_type: str, entity_type: str, payload: dict):
    raw = {"entity_type": entity_type, **payload}
    observation = SourceObservation(source_type, "source-row", raw)
    key = observation.row_key
    digest = semantic_hash(raw)
    manifest_hash = semantic_hash(
        {
            "source_row_identities": [key],
            "source_semantic_hashes": {key: digest},
            "file_content_hashes": {},
        }
    )
    manifest = SnapshotManifest(
        run_id=uuid4(),
        snapshot_id=uuid4(),
        predecessor_snapshot_id=None,
        scan_started_at=BASE,
        scan_completed_at=BASE + timedelta(seconds=1),
        source_runtime_reference="authority:test",
        source_row_identities=(key,),
        source_semantic_hashes={key: digest},
        file_content_hashes={},
        manifest_hash=manifest_hash,
        observations={key: observation},
    )
    return PopulationBuilder().build(manifest)


def test_domain_source_authority_is_enforced_before_canonical_record_creation():
    reservation_id = uuid4()
    reservation_payload = {
        "reservation_id": reservation_id,
        "reservation_code": "RSV-AUTH",
        "property_id": uuid4(),
        "rental_unit_id": None,
        "source_channel": "LEGACY_MASTER",
        "external_reservation_id": "LEG-AUTH",
    }
    candidate_payload = {
        "offer_candidate_id": uuid4(),
        "campaign_id": uuid4(),
        "cleaner_party_id": uuid4(),
        "tier_no": 1,
        "evaluated_at": BASE,
    }
    roster_payload = {
        "roster_id": uuid4(),
        "cleaner_party_id": uuid4(),
        "property_id": uuid4(),
    }
    cleaning_payload = {
        "cleaning_id": uuid4(),
        "cleaning_code": "CLN-AUTH",
        "reservation_id": uuid4(),
        "property_id": uuid4(),
        "rental_unit_id": None,
        "schedule_source_type": "RESERVATION_CHECKOUT",
    }

    for entity_type, payload in (
        ("reservation", reservation_payload),
        ("cleaning_offer_candidate", candidate_payload),
        ("cleaner_property_roster", roster_payload),
    ):
        denied = _population_for("ARBITRARY_SOURCE", entity_type, payload)
        assert denied.records == ()
        assert denied.authority_findings

    assert len(_population_for("CANONICAL_RESERVATION_PROJECTION", "reservation", reservation_payload).records) == 1
    assert len(_population_for("CLEANING_PROJECTION", "cleaning_job", cleaning_payload).records) == 1
    assert len(
        _population_for(
            "ASSIGNMENT_HISTORY_19_PLUS_EXACT_EXECUTION_EVIDENCE",
            "cleaning_offer_candidate",
            candidate_payload,
        ).records
    ) == 1
    assert len(
        _population_for(
            "NOTION_CLEANER_PROPERTY_ACCESS",
            "cleaner_property_roster",
            roster_payload,
        ).records
    ) == 1


def test_empty_population_tables_are_not_stage_b_entity_write_surfaces():
    assert "organization_member" not in ENTITY_PRIMARY_KEYS
    assert "cleaner_schedule_block" not in ENTITY_PRIMARY_KEYS


def _retained_action(
    page_id: str,
    cleaning_id: UUID,
    *,
    offered_at: datetime,
    accepted_at: datetime,
    campaign_no: int = 77,
    assignment_no: int = 88,
):
    return {
        "assignment_page_id": page_id,
        "cleaning_id": cleaning_id,
        "schedule_revision_id": uuid4(),
        "cleaner_party_id": uuid4(),
        "persisted_actual_action": True,
        "offered_at": offered_at,
        "accepted_at": accepted_at,
        "acceptance_cutoff_at": accepted_at + timedelta(hours=1),
        "scheduled_start_at": BASE + timedelta(days=3),
        "scheduled_end_at": BASE + timedelta(days=3, hours=2),
        "base_fee_krw": 50_000,
        "urgent_premium_krw": 0,
        "campaign_no": campaign_no,
        "assignment_no": assignment_no,
    }


@pytest.mark.parametrize("count", [1, 2, 3])
def test_retained_assignment_batch_derives_contiguous_local_sequences(count):
    cleaning_id = uuid4()
    actions = [
        _retained_action(
            str(uuid4()),
            cleaning_id,
            offered_at=BASE + timedelta(minutes=index * 10),
            accepted_at=BASE + timedelta(minutes=index * 10 + 5),
        )
        for index in range(count)
    ]
    ancestry = minimal_exact_assignment_ancestry_batch(actions)
    assert [row["campaign"]["campaign_no"] for row in ancestry] == list(
        range(1, count + 1)
    )
    assert [row["assignment"]["assignment_no"] for row in ancestry] == list(
        range(1, count + 1)
    )
    assert 77 not in [row["campaign"]["campaign_no"] for row in ancestry]
    assert 88 not in [row["assignment"]["assignment_no"] for row in ancestry]


def test_retained_assignment_batch_tie_breaks_by_durable_source_identity():
    cleaning_id = uuid4()
    lower = "12345678-1234-4234-9234-123456789abc"
    higher = "22345678-1234-4234-9234-123456789abc"
    actions = [
        _retained_action(higher, cleaning_id, offered_at=BASE, accepted_at=BASE),
        _retained_action(lower, cleaning_id, offered_at=BASE, accepted_at=BASE),
    ]
    ancestry = minimal_exact_assignment_ancestry_batch(actions)
    assert [row["campaign"]["campaign_no"] for row in ancestry] == [2, 1]
    assert [row["assignment"]["assignment_no"] for row in ancestry] == [2, 1]


def test_retained_assignment_batch_sequences_restart_for_each_cleaning():
    actions = [
        _retained_action(str(uuid4()), uuid4(), offered_at=BASE, accepted_at=BASE),
        _retained_action(str(uuid4()), uuid4(), offered_at=BASE, accepted_at=BASE),
    ]
    ancestry = minimal_exact_assignment_ancestry_batch(actions)
    assert [row["campaign"]["campaign_no"] for row in ancestry] == [1, 1]
    assert [row["assignment"]["assignment_no"] for row in ancestry] == [1, 1]


def test_population_builder_derives_local_sequences_from_complete_retained_batch():
    source_type = "ASSIGNMENT_HISTORY_19_PLUS_EXACT_EXECUTION_EVIDENCE"
    cleaning_id = uuid4()
    revision_id = uuid4()
    cleaner_id = uuid4()
    campaign_rows = []
    assignment_rows = []
    for index, durable in enumerate(("campaign-b", "campaign-a")):
        campaign_id = uuid4()
        campaign_rows.append(
            SourceObservation(
                source_type,
                durable,
                {
                    "entity_type": "cleaning_offer_campaign",
                    "campaign_id": campaign_id,
                    "cleaning_id": cleaning_id,
                    "schedule_revision_id": revision_id,
                    "campaign_no": 77,
                    "base_fee_krw": 50_000,
                    "replacement_urgency": "NORMAL",
                    "urgent_premium_krw": 0,
                    "total_agreed_fee_krw": 50_000,
                    "opened_at": BASE,
                },
            )
        )
        assignment_rows.append(
            SourceObservation(
                source_type,
                f"assignment-{durable[-1]}",
                {
                    "entity_type": "cleaning_assignment",
                    "assignment_id": uuid4(),
                    "cleaning_id": cleaning_id,
                    "assignment_no": 88,
                    "schedule_revision_id": revision_id,
                    "campaign_id": campaign_id,
                    "offer_candidate_id": uuid4(),
                    "accepted_proposal_version": 1,
                    "cleaner_party_id": cleaner_id,
                    "assignment_source": "OFFER_ACCEPTED",
                    "booked_at": BASE,
                    "scheduled_start_at": BASE + timedelta(days=1 + index),
                    "scheduled_end_at": BASE + timedelta(days=1 + index, hours=2),
                    "work_minutes_snapshot": 120,
                    "base_fee_krw": 50_000,
                    "replacement_urgency": "NORMAL",
                    "urgent_premium_krw": 0,
                    "total_agreed_fee_krw": 50_000,
                },
            )
        )
    observations = {row.row_key: row for row in campaign_rows + assignment_rows}
    hashes = {key: semantic_hash(row.semantic_payload) for key, row in observations.items()}
    manifest = SnapshotManifest(
        run_id=uuid4(),
        snapshot_id=uuid4(),
        predecessor_snapshot_id=None,
        scan_started_at=BASE,
        scan_completed_at=BASE + timedelta(seconds=1),
        source_runtime_reference="authority:retained-batch",
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
    population = PopulationBuilder().build(manifest)
    assert population.identity_collisions == ()
    campaigns = sorted(
        (record for record in population.records if record.entity_type == "cleaning_offer_campaign"),
        key=lambda record: record.source_key,
    )
    assignments = sorted(
        (record for record in population.records if record.entity_type == "cleaning_assignment"),
        key=lambda record: record.source_key,
    )
    assert [record.payload["campaign_no"] for record in campaigns] == [1, 2]
    assert [record.payload["assignment_no"] for record in assignments] == [1, 2]
    assert all(record.payload["campaign_no"] != 77 for record in campaigns)
    assert all(record.payload["assignment_no"] != 88 for record in assignments)


def test_retained_batch_missing_ordering_evidence_is_review_required():
    action = _retained_action(
        "42345678-1234-4234-9234-123456789abc",
        uuid4(),
        offered_at=BASE,
        accepted_at=BASE + timedelta(minutes=5),
    )
    action.pop("offered_at")
    with pytest.raises(ReviewRequired):
        minimal_exact_assignment_ancestry_batch([action])

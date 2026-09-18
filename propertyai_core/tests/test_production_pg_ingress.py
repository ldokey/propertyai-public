from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
from uuid import UUID, uuid4

import pytest

from propertyai_core.adapters.postgres.production_pool import (
    PostgresProductionConfig,
    PostgresProductionPool,
)
from propertyai_core.adapters.postgres.repository import PostgresCleanerRepository
from propertyai_core.application.cleaner_pg import (
    AcceptAssignmentCommand,
    CommandMetadata,
    CompleteCleaningCommand,
    FirstAuthoritativeReservationAlreadyPresent,
    MarkUnavailableCommand,
    OpenAssignmentOfferCommand,
    PostgresCleanerApplicationService,
    ReassignOriginalCommand,
    RequestReassignmentCommand,
    ReservationIngressCommand,
)
from propertyai_core.config.cleaner_authority import (
    AUTHORITY_ENV,
    CleanerAuthorityConfig,
    CleanerAuthorityConfigError,
    PG_AUTHORITY_EPOCH_ENV,
    PG_DATABASE_ENV,
    PG_DSN_PATH_ENV,
    PG_INGRESS_ENV,
    PG_SESSION_USER_ENV,
)
from propertyai_core.stage_b.identity import notion_identity
from propertyai_core.tests.outbox_semantics import (
    assert_outbox_exact, expected_event_rows, m1_expected_events,
)
from propertyai_core.tests.postgres_stage_a_cluster import (
    APP_LOGIN,
    start_disposable_postgres,
)


UTC = timezone.utc
PROPERTY_PAGE_ID = "11111111-1111-4111-8111-111111111111"
RENTAL_PAGE_ID = "22222222-2222-4222-8222-222222222222"
PROPERTY_ID = notion_identity("property", PROPERTY_PAGE_ID)
RENTAL_UNIT_ID = notion_identity("rental-unit", RENTAL_PAGE_ID)


@pytest.fixture(scope="module")
def production_pg():
    cluster = start_disposable_postgres()
    pool = None
    try:
        pool = PostgresProductionPool(
            PostgresProductionConfig(
                dsn=cluster.login_dsn(APP_LOGIN),
                expected_session_user=APP_LOGIN,
                expected_database="postgres",
            )
        )
        pool.open()
        organization_id = uuid4()
        cleaner_party_id = uuid4()
        cluster.psql(
            sql_text=f"""
            INSERT INTO propertyai.organization(
                organization_id, organization_code, display_name, organization_status, data_environment
            ) VALUES ('{organization_id}', 'C2-ORG', 'C2 Org', 'ACTIVE', 'PRODUCTION');
            INSERT INTO propertyai.property(
                property_id, organization_id, property_code, display_name, timezone_name
            ) VALUES ('{PROPERTY_ID}', '{organization_id}', 'C2-PROP', 'C2 Property', 'Asia/Seoul');
            INSERT INTO propertyai.rental_unit(
                rental_unit_id, rental_unit_code, property_id, display_name
            ) VALUES ('{RENTAL_UNIT_ID}', 'C2-UNIT', '{PROPERTY_ID}', 'C2 Unit');
            INSERT INTO propertyai.party(
                party_id, party_code, display_name, data_environment
            ) VALUES ('{cleaner_party_id}', 'C2-CLEANER', 'C2 Cleaner', 'PRODUCTION');
            INSERT INTO propertyai.cleaner_profile(
                cleaner_party_id, operational_status, max_daily_work_minutes, max_daily_jobs
            ) VALUES ('{cleaner_party_id}', 'ACTIVE', 600, 4);
            """
        )
        yield cluster, pool, cleaner_party_id
    finally:
        try:
            if pool is not None:
                pool.close()
        finally:
            cluster.cleanup()


def _metadata(key: str, *, at: datetime, source_event: str | None = None, actor: UUID | None = None):
    return CommandMetadata(
        idempotency_key=key,
        source_channel_code="AIRBNB" if actor is None else "TELEGRAM",
        source_stream_key="HM-C2" if actor is None else "telegram-user:1001",
        source_event_id=source_event or key,
        authority_epoch=1,
        decided_at=at,
        actor_party_id=actor,
    )


def _reservation_command(
    key: str,
    *,
    external_id: str,
    reservation_code: str,
    at: datetime,
    checkout_day: int = 3,
) -> ReservationIngressCommand:
    check_in = datetime(2026, 10, 1, 15, tzinfo=UTC)
    check_out = datetime(2026, 10, checkout_day, 11, tzinfo=UTC)
    return ReservationIngressCommand(
        metadata=_metadata(key, at=at, source_event=key),
        source_channel="AIRBNB",
        external_reservation_id=external_id,
        reservation_code=reservation_code,
        property_id=PROPERTY_ID,
        rental_unit_id=RENTAL_UNIT_ID,
        reservation_status="CONFIRMED",
        check_in_at=check_in,
        check_out_at=check_out,
        cleaning_code=f"CLEANING-AIRBNB-{reservation_code}",
        service_window_start_at=check_out,
        service_deadline_at=check_out + timedelta(hours=4),
        required_work_minutes=240,
    )


def test_production_authority_defaults_and_invalid_combinations(tmp_path: Path):
    default = CleanerAuthorityConfig.from_environment({})
    assert default.uses_legacy is True
    assert default.uses_postgres is False
    assert default.pg_dsn_path is None

    with pytest.raises(CleanerAuthorityConfigError, match="LEGACY_WITH_PG_INGRESS_FORBIDDEN"):
        CleanerAuthorityConfig.from_environment({PG_INGRESS_ENV: "true"})
    with pytest.raises(CleanerAuthorityConfigError, match="POSTGRES_WITH_PG_INGRESS_DISABLED"):
        CleanerAuthorityConfig.from_environment({AUTHORITY_ENV: "POSTGRES"})
    with pytest.raises(CleanerAuthorityConfigError, match="UNKNOWN_CLEANER_AUTHORITY"):
        CleanerAuthorityConfig.from_environment({AUTHORITY_ENV: "OTHER"})

    with pytest.raises(CleanerAuthorityConfigError, match="CLEANER_AUTHORITY_MALFORMED"):
        CleanerAuthorityConfig.from_environment({AUTHORITY_ENV: ""})

    dsn = tmp_path / "pg.dsn"
    dsn.write_text("host=/tmp dbname=unused user=unused\n")
    dsn.chmod(0o600)
    config = CleanerAuthorityConfig.from_environment(
        {
            AUTHORITY_ENV: "POSTGRES",
            PG_INGRESS_ENV: "true",
            PG_DSN_PATH_ENV: str(dsn),
            PG_SESSION_USER_ENV: "app-login",
            PG_DATABASE_ENV: "propertyai",
            PG_AUTHORITY_EPOCH_ENV: "1",
        }
    )
    assert config.uses_postgres is True
    assert config.authority_epoch == 1


def test_concurrent_first_ingest_converges_to_one_durable_reservation_identity(production_pg):
    _cluster, pool, _cleaner = production_pg
    service = PostgresCleanerApplicationService(PostgresCleanerRepository(pool))
    at = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)
    external = f"CONCURRENT-{uuid4()}"
    code = f"AIR-{uuid4()}"
    commands = (
        _reservation_command(f"concurrent-a:{external}", external_id=external, reservation_code=code, at=at),
        _reservation_command(f"concurrent-b:{external}", external_id=external, reservation_code=code, at=at),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(service.ingest_reservation, commands))

    assert len({result.reservation_id for result in results}) == 1
    with pool._connection() as connection:
        row = connection.execute(
            """
            SELECT count(*) AS row_count, count(DISTINCT reservation_id) AS distinct_ids
              FROM propertyai.reservation
             WHERE source_channel='AIRBNB' AND external_reservation_id=%s
            """,
            (external,),
        ).fetchone()
    assert row == {"row_count": 1, "distinct_ids": 1}


def test_strict_first_ingest_uses_read_only_absence_then_unique_receipt_race_fence(
    production_pg,
):
    _cluster, pool, _cleaner = production_pg
    repository = PostgresCleanerRepository(pool)
    service = PostgresCleanerApplicationService(repository)
    at = datetime(2026, 9, 9, 3, 30, tzinfo=UTC)
    external = f"FIRST-{uuid4()}"
    code = f"AIR-{uuid4()}"
    command = _reservation_command(
        f"first-strict:{external}",
        external_id=external,
        reservation_code=code,
        at=at,
    )

    with pool._connection() as connection:
        privileges = connection.execute(
            """
            SELECT current_user,
                   has_table_privilege(current_user, 'propertyai.command_receipt', 'SELECT')
                       AS can_select,
                   has_table_privilege(current_user, 'propertyai.command_receipt', 'UPDATE')
                       AS can_update
            """
        ).fetchone()
        assert privileges == {
            "current_user": "propertyai_app_runtime",
            "can_select": True,
            "can_update": False,
        }
    with repository.transaction() as tx:
        assert tx.find_command_receipt(
            "CLEANER_SCHEDULING", "RESERVATION_INGRESS", command.metadata.idempotency_key
        ) is None

    def execute():
        try:
            return service.ingest_reservation(command, require_absent=True)
        except FirstAuthoritativeReservationAlreadyPresent as error:
            return type(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: execute(), range(2)))

    assert sum(result is FirstAuthoritativeReservationAlreadyPresent for result in results) == 1
    assert sum(result is not FirstAuthoritativeReservationAlreadyPresent for result in results) == 1
    with pool._connection() as connection:
        facts = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM propertyai.command_receipt
                WHERE authority_scope_code='CLEANER_SCHEDULING'
                  AND command_type='RESERVATION_INGRESS'
                  AND idempotency_key=%s) AS receipt_count,
              (SELECT count(*) FROM propertyai.reservation
                WHERE source_channel='AIRBNB' AND external_reservation_id=%s) AS reservation_count,
              (SELECT count(*) FROM propertyai.integration_outbox o
                 JOIN propertyai.domain_event e ON e.domain_event_id=o.domain_event_id
                WHERE e.event_type='RESERVATION_INGESTED'
                  AND e.payload->>'external_reservation_id'=%s) AS outbox_count,
              (SELECT count(*) FROM propertyai.integration_outbox o
                 JOIN propertyai.domain_event e ON e.domain_event_id=o.domain_event_id
                WHERE e.event_type='RESERVATION_INGESTED'
                  AND e.payload->>'external_reservation_id'=%s
                  AND o.destination_type='TELEGRAM_PROJECTION') AS telegram_count
            """,
            (command.metadata.idempotency_key, external, external, external),
        ).fetchone()
    assert facts == {
        "receipt_count": 1,
        "reservation_count": 1,
        "outbox_count": 2,
        "telegram_count": 0,
    }


def test_synthetic_m1_lifecycle_idempotency_and_outbox_atomicity(production_pg):
    _cluster, pool, cleaner_party_id = production_pg
    # Select the complete new effect set, not a WHERE aggregate_id filter that
    # could hide a wrong aggregate, recipient, event or destination.
    with pool._connection() as connection:
        before_outbox = {row["outbox_id"] for row in connection.execute(
            "SELECT outbox_id FROM propertyai.integration_outbox").fetchall()}
        before_events = {row["domain_event_id"] for row in connection.execute(
            "SELECT domain_event_id FROM propertyai.domain_event").fetchall()}
    service = PostgresCleanerApplicationService(PostgresCleanerRepository(pool))
    at = datetime(2026, 9, 9, 4, 0, tzinfo=UTC)
    external = f"M1-{uuid4()}"
    code = f"AIR-{uuid4()}"

    reservation_command = _reservation_command(
        f"m1-reservation:{external}", external_id=external, reservation_code=code, at=at
    )
    reservation = service.ingest_reservation(reservation_command)
    replay = service.ingest_reservation(reservation_command)
    assert replay.reservation_id == reservation.reservation_id
    assert replay.cleaning_id == reservation.cleaning_id
    assert reservation.source_version == replay.source_version == 1
    assert reservation.cleaning_id is not None

    offer_command = OpenAssignmentOfferCommand(
        metadata=_metadata("m1-offer", at=at + timedelta(minutes=1), actor=cleaner_party_id),
        cleaning_id=reservation.cleaning_id,
        cleaner_party_id=cleaner_party_id,
        base_fee_krw=55_000,
        acceptance_cutoff_at=at + timedelta(hours=2),
    )
    offer = service.open_assignment_offer(offer_command)
    assert service.open_assignment_offer(offer_command) == offer

    accept_command = AcceptAssignmentCommand(
        metadata=_metadata("m1-accept", at=at + timedelta(minutes=2), actor=cleaner_party_id),
        campaign_id=offer.campaign_id,
        offer_candidate_id=offer.offer_candidate_id,
        cleaner_party_id=cleaner_party_id,
    )
    assignment_id = service.accept_assignment(accept_command)
    assert service.accept_assignment(accept_command) == assignment_id

    unavailable_id = service.mark_unavailable(
        MarkUnavailableCommand(
            metadata=_metadata("m1-unavailable", at=at + timedelta(minutes=3), actor=cleaner_party_id),
            assignment_id=assignment_id,
            availability_classification="EARLY_UNAVAILABLE",
            replacement_urgency="NORMAL",
            reason_code="PERSONAL",
        )
    )
    request_id = service.request_reassignment(
        RequestReassignmentCommand(
            metadata=_metadata("m1-reassign-request", at=at + timedelta(minutes=4), actor=cleaner_party_id),
            unavailability_id=unavailable_id,
        )
    )
    replacement_assignment_id = service.reassign_original(
        ReassignOriginalCommand(
            metadata=_metadata("m1-reassign-original", at=at + timedelta(minutes=5), actor=cleaner_party_id),
            reassignment_request_id=request_id,
        )
    )
    cleaning_id = service.complete_cleaning(
        CompleteCleaningCommand(
            metadata=_metadata("m1-complete", at=at + timedelta(minutes=6), actor=cleaner_party_id),
            assignment_id=replacement_assignment_id,
        )
    )
    assert cleaning_id == reservation.cleaning_id
    assert service.complete_cleaning(
        CompleteCleaningCommand(
            metadata=_metadata("m1-complete", at=at + timedelta(minutes=6), actor=cleaner_party_id),
            assignment_id=replacement_assignment_id,
        )
    ) == cleaning_id

    with pool._connection() as connection:
        reservation_row = connection.execute(
            "SELECT count(*) AS n, count(DISTINCT reservation_id) AS ids FROM propertyai.reservation WHERE source_channel='AIRBNB' AND external_reservation_id=%s",
            (external,),
        ).fetchone()
        cleaning = connection.execute(
            "SELECT cleaning_status FROM propertyai.cleaning_job WHERE cleaning_id=%s",
            (cleaning_id,),
        ).fetchone()
        assignments = connection.execute(
            "SELECT assignment_status, assignment_source FROM propertyai.cleaning_assignment WHERE cleaning_id=%s ORDER BY assignment_no",
            (cleaning_id,),
        ).fetchall()
        outbox = [row for row in connection.execute(
            "SELECT * FROM propertyai.integration_outbox").fetchall()
            if row["outbox_id"] not in before_outbox]
        events = [row for row in connection.execute(
            "SELECT * FROM propertyai.domain_event").fetchall()
            if row["domain_event_id"] not in before_events]
        specs = m1_expected_events(reservation_command, reservation, offer, assignment_id,
                                   unavailable_id, request_id, replacement_assignment_id, cleaner_party_id)
        keys = [spec["key"] for spec in specs]
        receipts = connection.execute(
            "SELECT command_id,idempotency_key FROM propertyai.command_receipt WHERE idempotency_key=ANY(%s)",
            (keys,),
        ).fetchall()

    assert len(receipts) == len(specs) == len(events) == 7
    by_key = {row["idempotency_key"]: row["command_id"] for row in receipts}
    expected_outbox = []
    for spec in specs:
        event_rows = [row for row in events if row["command_id"] == by_key[spec["key"]]]
        assert len(event_rows) == 1
        event = event_rows[0]
        assert (event["event_type"], event["aggregate_type"], str(event["aggregate_id"]), event["payload"]) == (
            spec["event"], spec["aggregate_type"], spec["aggregate_id"], spec["payload"])
        expected_outbox.extend(expected_event_rows(
            event=spec["event"], aggregate_type=spec["aggregate_type"], aggregate_id=spec["aggregate_id"],
            command_id=by_key[spec["key"]], domain_event_id=event["domain_event_id"], payload=spec["payload"],
            destinations=spec["destinations"], recipient=spec["recipient"]))
    assert len(expected_outbox) == 12
    assert_outbox_exact(outbox, expected_outbox)
    # Complete multiset also requires absence of unintended destinations.
    assert not [r for r in outbox if r["event_type"] == "RESERVATION_INGESTED"
                and r["destination_type"] == "TELEGRAM_PROJECTION"]

    assert reservation_row == {"n": 1, "ids": 1}
    assert cleaning == {"cleaning_status": "COMPLETED"}
    assert assignments == [
        {"assignment_status": "RELEASED", "assignment_source": "OFFER_ACCEPTED"},
        {"assignment_status": "COMPLETED", "assignment_source": "ORIGINAL_REASSIGNED"},
    ]


def test_reservation_schedule_update_with_hard_booking_opens_reconciliation(production_pg):
    _cluster, pool, cleaner_party_id = production_pg
    service = PostgresCleanerApplicationService(PostgresCleanerRepository(pool))
    at = datetime(2026, 9, 9, 5, 0, tzinfo=UTC)
    external = f"UPDATE-{uuid4()}"
    code = f"AIR-{uuid4()}"
    initial = _reservation_command(
        f"update-initial:{external}", external_id=external, reservation_code=code, at=at
    )
    reservation = service.ingest_reservation(initial)
    assert reservation.cleaning_id is not None
    assert reservation.schedule_revision_id is not None

    offer = service.open_assignment_offer(
        OpenAssignmentOfferCommand(
            metadata=_metadata("update-offer", at=at + timedelta(minutes=1), actor=cleaner_party_id),
            cleaning_id=reservation.cleaning_id,
            cleaner_party_id=cleaner_party_id,
            base_fee_krw=55_000,
            acceptance_cutoff_at=at + timedelta(hours=2),
        )
    )
    assignment_id = service.accept_assignment(
        AcceptAssignmentCommand(
            metadata=_metadata("update-accept", at=at + timedelta(minutes=2), actor=cleaner_party_id),
            campaign_id=offer.campaign_id,
            offer_candidate_id=offer.offer_candidate_id,
            cleaner_party_id=cleaner_party_id,
        )
    )

    changed_template = _reservation_command(
        f"update-new:{external}",
        external_id=external,
        reservation_code=code,
        at=at + timedelta(minutes=3),
        checkout_day=4,
    )
    changed = service.ingest_reservation(changed_template)
    assert changed.reservation_id == reservation.reservation_id
    assert changed.source_version == 2
    assert changed.schedule_revision_id != reservation.schedule_revision_id

    with pool._connection() as connection:
        reconciliation = connection.execute(
            """
            SELECT hard_booked_assignment_id, base_assignment_revision_id,
                   target_schedule_revision_id, case_version, reconciliation_status, reason_code
              FROM propertyai.cleaning_schedule_reconciliation
             WHERE cleaning_id=%s
            """,
            (reservation.cleaning_id,),
        ).fetchall()
    assert reconciliation == [
        {
            "hard_booked_assignment_id": assignment_id,
            "base_assignment_revision_id": reservation.schedule_revision_id,
            "target_schedule_revision_id": changed.schedule_revision_id,
            "case_version": 1,
            "reconciliation_status": "PENDING",
            "reason_code": "RESERVATION_SOURCE_UPDATED",
        }
    ]

    retry = service.ingest_reservation(changed_template)
    assert retry.source_version == 2
    assert retry.schedule_revision_id == changed.schedule_revision_id
    with pool._connection() as connection:
        counts = connection.execute(
            """
            SELECT count(*) AS rows, max(case_version) AS max_version
              FROM propertyai.cleaning_schedule_reconciliation
             WHERE cleaning_id=%s
            """,
            (reservation.cleaning_id,),
        ).fetchone()
    assert counts == {"rows": 1, "max_version": 1}


def test_reservation_cancellation_closes_open_offer_without_hard_booking(production_pg):
    _cluster, pool, cleaner_party_id = production_pg
    service = PostgresCleanerApplicationService(PostgresCleanerRepository(pool))
    at = datetime(2026, 9, 9, 6, 0, tzinfo=UTC)
    external = f"CANCEL-{uuid4()}"
    code = f"AIR-{uuid4()}"
    initial = _reservation_command(
        f"cancel-initial:{external}", external_id=external, reservation_code=code, at=at
    )
    reservation = service.ingest_reservation(initial)
    assert reservation.cleaning_id is not None
    offer = service.open_assignment_offer(
        OpenAssignmentOfferCommand(
            metadata=_metadata("cancel-offer", at=at + timedelta(minutes=1), actor=cleaner_party_id),
            cleaning_id=reservation.cleaning_id,
            cleaner_party_id=cleaner_party_id,
            base_fee_krw=55_000,
            acceptance_cutoff_at=at + timedelta(hours=2),
        )
    )

    cancelled = replace(
        initial,
        metadata=_metadata(
            f"cancel-event:{external}",
            at=at + timedelta(minutes=2),
            source_event=f"cancel:{external}",
        ),
        reservation_status="CANCELLED",
    )
    result = service.ingest_reservation(cancelled)
    assert result.source_version == 2

    with pool._connection() as connection:
        campaign = connection.execute(
            "SELECT campaign_status, closed_reason_code FROM propertyai.cleaning_offer_campaign WHERE campaign_id=%s",
            (offer.campaign_id,),
        ).fetchone()
        candidate = connection.execute(
            "SELECT candidate_status FROM propertyai.cleaning_offer_candidate WHERE offer_candidate_id=%s",
            (offer.offer_candidate_id,),
        ).fetchone()
        cleaning = connection.execute(
            "SELECT cleaning_status FROM propertyai.cleaning_job WHERE cleaning_id=%s",
            (reservation.cleaning_id,),
        ).fetchone()
        open_campaigns = connection.execute(
            "SELECT count(*) AS n FROM propertyai.cleaning_offer_campaign WHERE cleaning_id=%s AND campaign_status='OPEN'",
            (reservation.cleaning_id,),
        ).fetchone()
        hard_booked = connection.execute(
            "SELECT count(*) AS n FROM propertyai.cleaning_assignment WHERE cleaning_id=%s AND assignment_status='HARD_BOOKED'",
            (reservation.cleaning_id,),
        ).fetchone()
    assert campaign == {
        "campaign_status": "CANCELLED",
        "closed_reason_code": "RESERVATION_CANCELLED",
    }
    assert candidate == {"candidate_status": "DECLINED"}
    assert cleaning == {"cleaning_status": "CANCELLED"}
    assert open_campaigns == {"n": 0}
    assert hard_booked == {"n": 0}


def test_repeated_reassignment_requests_increment_request_number(production_pg):
    _cluster, pool, cleaner_party_id = production_pg
    service = PostgresCleanerApplicationService(PostgresCleanerRepository(pool))
    at = datetime(2026, 9, 9, 7, 0, tzinfo=UTC)
    external = f"REASSIGN-NO-{uuid4()}"
    code = f"AIR-{uuid4()}"
    reservation = service.ingest_reservation(
        _reservation_command(
            f"request-no-initial:{external}",
            external_id=external,
            reservation_code=code,
            at=at,
            checkout_day=6,
        )
    )
    assert reservation.cleaning_id is not None

    def offer_accept(prefix: str, minute: int):
        offer = service.open_assignment_offer(
            OpenAssignmentOfferCommand(
                metadata=_metadata(
                    f"{prefix}-offer", at=at + timedelta(minutes=minute), actor=cleaner_party_id
                ),
                cleaning_id=reservation.cleaning_id,
                cleaner_party_id=cleaner_party_id,
                base_fee_krw=55_000,
                acceptance_cutoff_at=at + timedelta(hours=3),
            )
        )
        return service.accept_assignment(
            AcceptAssignmentCommand(
                metadata=_metadata(
                    f"{prefix}-accept",
                    at=at + timedelta(minutes=minute + 1),
                    actor=cleaner_party_id,
                ),
                campaign_id=offer.campaign_id,
                offer_candidate_id=offer.offer_candidate_id,
                cleaner_party_id=cleaner_party_id,
            )
        )

    first_assignment = offer_accept("request-no-first", 1)
    first_unavailable = service.mark_unavailable(
        MarkUnavailableCommand(
            metadata=_metadata(
                "request-no-first-unavailable",
                at=at + timedelta(minutes=3),
                actor=cleaner_party_id,
            ),
            assignment_id=first_assignment,
            availability_classification="EARLY_UNAVAILABLE",
            replacement_urgency="NORMAL",
        )
    )
    first_request = service.request_reassignment(
        RequestReassignmentCommand(
            metadata=_metadata(
                "request-no-first-request",
                at=at + timedelta(minutes=4),
                actor=cleaner_party_id,
            ),
            unavailability_id=first_unavailable,
        )
    )
    second_assignment = service.reassign_original(
        ReassignOriginalCommand(
            metadata=_metadata(
                "request-no-first-reassign",
                at=at + timedelta(minutes=5),
                actor=cleaner_party_id,
            ),
            reassignment_request_id=first_request,
        )
    )
    second_unavailable = service.mark_unavailable(
        MarkUnavailableCommand(
            metadata=_metadata(
                "request-no-second-unavailable",
                at=at + timedelta(minutes=6),
                actor=cleaner_party_id,
            ),
            assignment_id=second_assignment,
            availability_classification="EARLY_UNAVAILABLE",
            replacement_urgency="NORMAL",
        )
    )
    service.request_reassignment(
        RequestReassignmentCommand(
            metadata=_metadata(
                "request-no-second-request",
                at=at + timedelta(minutes=7),
                actor=cleaner_party_id,
            ),
            unavailability_id=second_unavailable,
        )
    )

    with pool._connection() as connection:
        rows = connection.execute(
            """
            SELECT request_no, request_status
              FROM propertyai.cleaner_reassignment_request
             WHERE cleaning_id=%s
             ORDER BY request_no
            """,
            (reservation.cleaning_id,),
        ).fetchall()
    assert rows == [
        {"request_no": 1, "request_status": "REASSIGNED_ORIGINAL"},
        {"request_no": 2, "request_status": "REQUESTED"},
    ]

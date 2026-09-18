from __future__ import annotations

from datetime import date

from gmail_ingest.cleaning_reconcile import (
    CONFLICT,
    MATCHED,
    MISSING,
    ReadOnlyNotionClient,
    classify_related_cleanings,
    reconcile_pages,
    related_cleaning_query_filter,
)


AS_OF = date(2026, 8, 30)
PROPERTY_ID = "property-jj"
UNIT_ID = "unit-jj"


def _reservation(*, page_id="r1", status="확정", checkout="2026-09-01", environment="PRODUCTION", registration="APPROVED"):
    return {
        "id": page_id,
        "properties": {
            "데이터 환경": {"select": {"name": environment}},
            "등록 상태": {"select": {"name": registration}},
            "상태": {"select": {"name": status}},
            "체크아웃": {"date": {"start": checkout}},
            "연결 집": {"relation": [{"id": PROPERTY_ID}]},
            "연결 운영상품": {"relation": [{"id": UNIT_ID}]},
        },
    }


def _run(reservation, cleanings):
    return reconcile_pages(
        [reservation],
        as_of=AS_OF,
        related_cleaning_lookup=lambda _reservation_id: cleanings,
        property_page_id=PROPERTY_ID,
        rental_unit_page_id=UNIT_ID,
    )


def test_exactly_one_related_cleaning_is_matched():
    result = _run(_reservation(), [{"id": "c1"}])
    assert result.processed_count == 1
    assert result.matched_count == 1
    assert result.missing_count == 0
    assert result.conflict_count == 0
    assert result.business_write_count == 0


def test_no_related_cleaning_is_missing_candidate_only():
    result = _run(_reservation(), [])
    assert result.missing_count == 1
    assert result.create_candidate_count == 1
    assert result.business_write_count == 0


def test_duplicate_related_cleanings_are_conflict():
    result = _run(_reservation(), [{"id": "c1"}, {"id": "c2"}])
    assert result.conflict_count == 1
    assert result.business_write_count == 0


def test_cancelled_or_noncanonical_reservation_is_excluded():
    called = False

    def lookup(_reservation_id):
        nonlocal called
        called = True
        return []

    result = reconcile_pages(
        [_reservation(status="취소")],
        as_of=AS_OF,
        related_cleaning_lookup=lookup,
        property_page_id=PROPERTY_ID,
        rental_unit_page_id=UNIT_ID,
    )
    assert result.processed_count == 0
    assert result.skipped_count == 1
    assert called is False


def test_read_only_client_exposes_no_business_mutation_method(tmp_path):
    client = ReadOnlyNotionClient(tmp_path / "token")
    assert hasattr(client, "query_data_source")
    for forbidden in ("create", "update", "patch", "delete", "apply"):
        assert not hasattr(client, forbidden)


def test_classification_is_deterministic_by_related_count():
    assert classify_related_cleanings([]) == MISSING
    assert classify_related_cleanings([{"id": "c1"}]) == MATCHED
    assert classify_related_cleanings([{"id": "c1"}, {"id": "c2"}]) == CONFLICT


def test_cleaning_lookup_filter_is_reservation_identity_based():
    reservation_id = "reservation-canonical-id"
    query_filter = related_cleaning_query_filter(reservation_id)
    assert query_filter == {
        "property": "관련 Reservation",
        "relation": {"contains": reservation_id},
    }
    assert "점검일" not in str(query_filter)

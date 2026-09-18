"""Test-only effect oracle: exact multiset, never a producer-derived expectation.

Ignore row allocation IDs/timestamps and object key order, but retain every domain
payload value, recipient/resource, effect kind, event/aggregate binding, durable
idempotency identity and attempt/fence. Multiplicity is intentional: set() would
hide duplicate external effects. No provider is invoked by this helper.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
import json
from typing import Any


SEMANTIC_FIELDS = (
    "event_type", "aggregate_type", "aggregate_id", "destination_type",
    "destination_ref", "domain_event_id", "idempotency_key", "payload",
    "outbox_status", "attempt_count", "max_attempts", "lease_fence",
)


def canonical_effect(row: Mapping[str, Any]) -> str:
    missing = set(SEMANTIC_FIELDS) - row.keys()
    if missing:
        raise AssertionError(f"OUTBOX_SEMANTIC_FIELDS_MISSING:{sorted(missing)}")
    value = {key: row[key] for key in SEMANTIC_FIELDS}
    value["aggregate_id"] = str(value["aggregate_id"])
    if not isinstance(value["payload"], dict):
        raise AssertionError("OUTBOX_PAYLOAD_OBJECT_REQUIRED")
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise AssertionError("OUTBOX_CANONICAL_PAYLOAD_INVALID") from error


def assert_outbox_exact(actual: Iterable[Mapping[str, Any]], expected: Iterable[Mapping[str, Any]]) -> None:
    actual_set = Counter(canonical_effect(row) for row in actual)
    expected_set = Counter(canonical_effect(row) for row in expected)
    assert actual_set == expected_set, (
        "OUTBOX_SEMANTIC_SET_MISMATCH",
        {"missing": dict(expected_set - actual_set), "unexpected": dict(actual_set - expected_set)},
    )


def expected_event_rows(*, event: str, aggregate_type: str, aggregate_id: object,
                        command_id: object, domain_event_id: int, payload: dict,
                        destinations: tuple[str, ...], recipient: dict | None = None) -> list[dict]:
    """Build explicit test data, with destinations supplied by the test, not production."""
    result = []
    for destination in destinations:
        body = dict(payload)
        if destination == "TELEGRAM_PROJECTION":
            assert recipient is not None
            body["telegram_effect"] = {"effect_kind": event, "recipient": dict(recipient), "body": dict(payload)}
        result.append({
            "event_type": event, "aggregate_type": aggregate_type, "aggregate_id": str(aggregate_id),
            "destination_type": destination, "destination_ref": None, "domain_event_id": domain_event_id,
            "idempotency_key": f"CLEANER_OUTBOX:{command_id}:{event}:{destination}",
            "payload": body, "outbox_status": "PENDING", "attempt_count": 0, "max_attempts": 5, "lease_fence": 0,
        })
    return result


def m1_expected_events(command, reservation, offer, assignment_id, unavailable_id,
                       request_id, replacement_assignment_id, cleaner_party_id) -> list[dict]:
    """Independent M1 domain oracle. Do not import EVENT_DESTINATIONS or call _emit.

    Dynamic IDs are workflow return values. Expected content/routing is specified
    here so a producer bug cannot edit the expectation along with actual rows.
    """
    cleaning = str(reservation.cleaning_id)
    cleaner = str(cleaner_party_id)
    party = {"kind": "PARTY", "identity": cleaner}
    reservation_payload = {
        "source_channel": "AIRBNB", "external_reservation_id": command.external_reservation_id,
        "reservation_code": command.reservation_code, "property_id": str(command.property_id),
        "rental_unit_id": str(command.rental_unit_id), "reservation_status": "CONFIRMED",
        "check_in_at": command.check_in_at.isoformat(), "check_out_at": command.check_out_at.isoformat(),
        "cleaning_code": command.cleaning_code, "service_window_start_at": command.service_window_start_at.isoformat(),
        "service_deadline_at": command.service_deadline_at.isoformat(), "required_work_minutes": 240,
        "reservation_id": str(reservation.reservation_id), "source_version": 1,
        "cleaning_id": cleaning, "schedule_revision_id": str(reservation.schedule_revision_id),
        "schedule_reconciliation_id": None, "cancelled_campaign_id": None,
    }
    from datetime import timedelta
    specs = [
        (command.metadata.idempotency_key, "RESERVATION_INGESTED", "RESERVATION", reservation.reservation_id,
         ("NOTION_PROJECTION", "CALENDAR_PROJECTION"), None, reservation_payload),
        ("m1-offer", "CLEANING_ASSIGNMENT_OFFER_OPENED", "CLEANING", reservation.cleaning_id,
         ("TELEGRAM_PROJECTION",), party, {
             "cleaning_id": cleaning, "cleaner_party_id": cleaner, "base_fee_krw": 55000,
             "replacement_urgency": "NORMAL", "urgent_premium_krw": 0,
             "urgent_premium_policy_version": None, "tier_no": 1,
             "acceptance_cutoff_at": (command.metadata.decided_at + timedelta(hours=2)).isoformat(),
             "campaign_id": str(offer.campaign_id), "offer_candidate_id": str(offer.offer_candidate_id),
             "schedule_revision_id": str(reservation.schedule_revision_id),
             "proposed_start_at": command.service_window_start_at.isoformat(),
             "proposed_end_at": (command.service_window_start_at + timedelta(minutes=240)).isoformat(),
         }),
        ("m1-accept", "CLEANING_ASSIGNMENT_ACCEPTED", "CLEANING_ASSIGNMENT", assignment_id,
         ("NOTION_PROJECTION", "TELEGRAM_PROJECTION"), party, {
             "campaign_id": str(offer.campaign_id), "offer_candidate_id": str(offer.offer_candidate_id),
             "cleaner_party_id": cleaner, "assignment_id": str(assignment_id), "cleaning_id": cleaning,
         }),
        ("m1-unavailable", "CLEANER_UNAVAILABLE_CONFIRMED", "CLEANER_UNAVAILABILITY", unavailable_id,
         ("NOTION_PROJECTION", "TELEGRAM_PROJECTION"), party, {
             "assignment_id": str(assignment_id), "availability_classification": "EARLY_UNAVAILABLE",
             "replacement_urgency": "NORMAL", "reason_code": "PERSONAL", "reason_text": None,
             "unavailability_id": str(unavailable_id), "cleaning_id": cleaning, "cleaner_party_id": cleaner,
         }),
        ("m1-reassign-request", "CLEANER_REASSIGNMENT_REQUESTED", "CLEANER_REASSIGNMENT", request_id,
         ("TELEGRAM_PROJECTION",), {"kind": "OPS", "identity": "PROPERTYAI_OPS"}, {
             "unavailability_id": str(unavailable_id), "reassignment_request_id": str(request_id),
             "cleaning_id": cleaning, "cleaner_party_id": cleaner,
         }),
        ("m1-reassign-original", "ORIGINAL_CLEANER_REASSIGNED", "CLEANING_ASSIGNMENT", replacement_assignment_id,
         ("NOTION_PROJECTION", "TELEGRAM_PROJECTION"), party, {
             "reassignment_request_id": str(request_id), "assignment_id": str(replacement_assignment_id),
             "cleaning_id": cleaning, "cleaner_party_id": cleaner,
         }),
        ("m1-complete", "CLEANING_COMPLETED", "CLEANING", reservation.cleaning_id,
         ("NOTION_PROJECTION", "TELEGRAM_PROJECTION"), party, {
             "assignment_id": str(replacement_assignment_id), "cleaning_id": cleaning, "cleaner_party_id": cleaner,
         }),
    ]
    return [dict(key=key, event=event, aggregate_type=kind, aggregate_id=str(identity),
                 destinations=destinations, recipient=recipient, payload=payload)
            for key, event, kind, identity, destinations, recipient, payload in specs]

from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from propertyai_core.tests.outbox_semantics import SEMANTIC_FIELDS, assert_outbox_exact, expected_event_rows


def sample():
    return expected_event_rows(
        event="CLEANING_ASSIGNMENT_OFFER_OPENED", aggregate_type="CLEANING", aggregate_id=UUID(int=1),
        command_id=UUID(int=2), domain_event_id=3,
        payload={"cleaning_id": str(UUID(int=1)), "base_fee_krw": 55000,
                 "resource": {"rental_unit_id": str(UUID(int=4)), "version": 1}},
        destinations=("TELEGRAM_PROJECTION",), recipient={"kind": "PARTY", "identity": str(UUID(int=5))})


@pytest.mark.parametrize("damage", [
    "wrong-destination", "wrong-recipient", "wrong-resource", "missing-event", "extra-event",
    "duplicate-event", "wrong-payload", "wrong-aggregate-id", "wrong-aggregate-type",
    "wrong-provider-effect", "wrong-event", "wrong-domain-event", "wrong-idempotency",
    "stale-attempt", "stale-fence", "wrong-status", "wrong-destination-ref", "wrong-effect-body",
    "typed-payload-drift", "unexpected-null",
])
def test_semantic_exact_set_rejects_meaningful_wrong_effect(damage):
    expected = sample()
    actual = deepcopy(expected)
    row = actual[0]
    if damage == "wrong-destination": row["destination_type"] = "NOTION_PROJECTION"
    elif damage == "wrong-recipient": row["payload"]["telegram_effect"]["recipient"]["identity"] = str(UUID(int=999))
    elif damage == "wrong-resource": row["payload"]["resource"]["rental_unit_id"] = str(UUID(int=999))
    elif damage == "missing-event": actual.clear()
    elif damage == "extra-event":
        extra = deepcopy(row); extra["idempotency_key"] = "unexpected-but-unique"; actual.append(extra)
    elif damage == "duplicate-event": actual.append(deepcopy(row))
    elif damage == "wrong-payload": row["payload"]["base_fee_krw"] = 55001
    elif damage == "wrong-aggregate-id": row["aggregate_id"] = str(UUID(int=999))
    elif damage == "wrong-aggregate-type": row["aggregate_type"] = "RESERVATION"
    elif damage == "wrong-provider-effect": row["payload"]["telegram_effect"]["effect_kind"] = "CLEANING_COMPLETED"
    elif damage == "wrong-event": row["event_type"] = "CLEANING_COMPLETED"
    elif damage == "wrong-domain-event": row["domain_event_id"] = 999
    elif damage == "wrong-idempotency": row["idempotency_key"] += ":different"
    elif damage == "stale-attempt": row["attempt_count"] = 2
    elif damage == "stale-fence": row["lease_fence"] = 7
    elif damage == "wrong-status": row["outbox_status"] = "SUCCEEDED"
    elif damage == "wrong-destination-ref": row["destination_ref"] = "foreign-target"
    elif damage == "wrong-effect-body": row["payload"]["telegram_effect"]["body"]["base_fee_krw"] = 1
    elif damage == "typed-payload-drift": row["payload"]["resource"]["version"] = True
    elif damage == "unexpected-null": row["payload"]["resource"] = None
    else: raise AssertionError(damage)
    with pytest.raises(AssertionError, match="OUTBOX_SEMANTIC_SET_MISMATCH"):
        assert_outbox_exact(actual, expected)


@pytest.mark.parametrize("missing", SEMANTIC_FIELDS)
def test_semantic_oracle_does_not_default_missing_binding_fields(missing):
    actual = sample()
    actual[0].pop(missing)
    with pytest.raises(AssertionError, match="FIELDS_MISSING"):
        assert_outbox_exact(actual, sample())


def test_semantic_exact_set_ignores_only_incidental_representation():
    expected = sample()
    actual = deepcopy(expected)
    actual[0] = dict(reversed(list(actual[0].items())))
    actual[0]["payload"] = dict(reversed(list(actual[0]["payload"].items())))
    actual[0]["outbox_id"] = UUID(int=888)
    actual[0]["created_at"] = "incidental-allocation-time"
    actual[0]["aggregate_id"] = UUID(int=1)
    assert_outbox_exact(actual, expected)


def test_expected_absence_is_positive_and_does_not_allow_extra_effects():
    assert_outbox_exact([], [])
    with pytest.raises(AssertionError):
        assert_outbox_exact(sample(), [])


def test_the_old_count_and_unique_key_check_would_pass_wrong_recipient():
    expected = sample()
    actual = deepcopy(expected)
    actual[0]["payload"]["telegram_effect"]["recipient"]["identity"] = "wrong-party"
    assert len(actual) > 0
    assert len(actual) == len({row["idempotency_key"] for row in actual})
    with pytest.raises(AssertionError):
        assert_outbox_exact(actual, expected)

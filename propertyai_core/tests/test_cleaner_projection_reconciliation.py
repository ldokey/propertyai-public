from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from uuid import UUID

import pytest

from propertyai_core.ports.cleaner_repository import OutboxReconciliationState
from propertyai_core.runtime import cleaner_projection_reconciliation as reconciliation
from propertyai_core.runtime.cleaner_projection_adapters import ExternalResourceSnapshot


class FakeRepository:
    def __init__(self):
        payload = {
            "reservation_id": str(reconciliation.RESERVATION_ID),
            "cleaning_id": str(reconciliation.CLEANING_ID),
        }
        self.rows = {
            outbox_id: OutboxReconciliationState(
                outbox_id=outbox_id,
                domain_event_id=reconciliation.DOMAIN_EVENT_ID,
                event_type="RESERVATION_INGESTED",
                aggregate_type="RESERVATION",
                aggregate_id=reconciliation.RESERVATION_ID,
                destination_type=destination,
                destination_ref=None,
                outbox_status="PENDING_RECONCILIATION",
                idempotency_key=(
                    f"CLEANER_OUTBOX:{reconciliation.COMMAND_ID}:"
                    f"RESERVATION_INGESTED:{destination}"
                ),
                payload=payload,
                attempt_count=1,
                max_attempts=5,
                lease_fence=1,
                external_effect_id=None,
                last_error_code="EXTERNAL_EFFECT_UNCERTAIN:Test",
            )
            for outbox_id, destination in reconciliation.EXPECTED_ROWS.items()
        }
        self.bindings = {}
        self.resolved = []
        self.reject_resolution_once = False

    def read_outbox_reconciliation(self, outbox_id):
        return self.rows.get(outbox_id)

    def reservation_projection_state(self, reservation_id):
        assert reservation_id == reconciliation.RESERVATION_ID
        return {
            "reservation_id": reservation_id,
            "reservation_code": "HM3MSAA2TA",
            "reservation_status": "CONFIRMED",
            "check_in_at": "2026-09-22T06:00:00+00:00",
            "check_out_at": "2026-09-25T02:00:00+00:00",
            "source_version": 1,
        }

    def cleaning_projection_state(self, cleaning_id):
        assert cleaning_id == reconciliation.CLEANING_ID
        return {
            "cleaning_id": cleaning_id,
            "cleaning_code": "CLEANING-AIRBNB-HM3MSAA2TA",
            "cleaning_status": "PLANNED",
            "service_window_start_at": "2026-09-25T02:00:00+00:00",
            "service_deadline_at": "2026-09-25T06:00:00+00:00",
            "revision_no": 1,
            "reservation_code": "HM3MSAA2TA",
        }

    def resource_binding(self, **identity):
        key = tuple(identity[name] for name in (
            "aggregate_type", "aggregate_id", "destination_type", "resource_code"
        ))
        return self.bindings.get(key)

    def bind_resource(self, **values):
        key = tuple(values[name] for name in (
            "aggregate_type", "aggregate_id", "destination_type", "resource_code"
        ))
        existing = self.bindings.get(key)
        if existing is not None and existing["external_resource_id"] != values["external_resource_id"]:
            raise RuntimeError("RESOURCE_BINDING_IDENTITY_CONFLICT")
        self.bindings[key] = dict(values)
        return self.bindings[key]

    def rebind_resource(self, **values):
        key = tuple(values[name] for name in (
            "aggregate_type", "aggregate_id", "destination_type", "resource_code"
        ))
        current = self.bindings[key]
        assert current["external_resource_id"] == values["expected_external_resource_id"]
        current.update(values)
        current["external_resource_id"] = values["new_external_resource_id"]
        return current

    def resolve_outbox_reconciliation(
        self, outbox_id, *, expected_lease_fence, resolution,
        external_effect_id, error_code, retry_delay_seconds,
    ):
        if self.reject_resolution_once:
            self.reject_resolution_once = False
            return False
        row = self.rows[outbox_id]
        if row.outbox_status != "PENDING_RECONCILIATION" or row.lease_fence != expected_lease_fence:
            return False
        assert resolution == "SUCCEEDED"
        assert retry_delay_seconds == 0
        self.rows[outbox_id] = replace(
            row,
            outbox_status="SUCCEEDED",
            external_effect_id=external_effect_id,
            last_error_code=error_code,
        )
        self.resolved.append(outbox_id)
        return True


class FakeNotionClient:
    def __init__(self, *, reservation=None, cleaning=None):
        self.resources = {
            "RESERVATION": list(reservation or []),
            "CLEANING": list(cleaning or []),
        }
        self.effects = []

    def find(self, resource_kind, identity):
        return tuple(self.resources[resource_kind])

    def read(self, resource_kind, external_id):
        return next((item for item in self.resources[resource_kind] if item.external_id == external_id), None)

    def create(self, resource_kind, identity, desired):
        self.effects.append(("create", resource_kind))
        item = ExternalResourceSnapshot(f"notion-{resource_kind.lower()}", dict(desired), external_version="v1")
        self.resources[resource_kind].append(item)
        return item

    def update(self, resource_kind, external_id, desired):
        self.effects.append(("update", resource_kind))
        item = ExternalResourceSnapshot(external_id, dict(desired), external_version="v2")
        self.resources[resource_kind] = [
            item if old.external_id == external_id else old
            for old in self.resources[resource_kind]
        ]
        return item


class FakeCalendarClient:
    def __init__(self, resources=None):
        self.resources = list(resources or [])
        self.effects = []

    def find(self, identity): return tuple(self.resources)
    def read(self, external_id):
        return next((item for item in self.resources if item.external_id == external_id), None)
    def create(self, identity, desired):
        self.effects.append("create")
        item = ExternalResourceSnapshot("calendar-cleaning", dict(desired), external_uid="uid", external_version="v1")
        self.resources.append(item)
        return item
    def update(self, external_id, desired):
        self.effects.append("update")
        item = ExternalResourceSnapshot(external_id, dict(desired), external_uid="uid", external_version="v2")
        self.resources = [item if old.external_id == external_id else old for old in self.resources]
        return item
    def retire(self, external_id): raise AssertionError("retire not expected")


@contextmanager
def _scope(*args, **kwargs):
    yield


def _service(repo=None, notion=None, calendar=None):
    repo = repo or FakeRepository()
    notion = notion or FakeNotionClient()
    calendar = calendar or FakeCalendarClient()
    return repo, notion, calendar, reconciliation.ExactProjectionReconciliation(
        repo, notion_client=notion, calendar_client=calendar
    )


def test_plan_classifies_unique_drifted_notion_and_absent_calendar_as_not_applied():
    legacy = ExternalResourceSnapshot(
        "legacy-reservation",
        {
            "reservation_id": "legacy-id",
            "reservation_code": "HM3MSAA2TA",
            "reservation_status": "CONFIRMED",
            "check_in_at": "2026-09-22T00:00:00",
            "check_out_at": "2026-09-25T00:00:00",
        },
    )
    _, _, _, service = _service(notion=FakeNotionClient(reservation=[legacy]))
    plans = {plan.destination_type: plan for plan in service.plan()}
    assert plans["CALENDAR_PROJECTION"].classification == reconciliation.NOT_EXTERNALLY_APPLIED
    assert plans["NOTION_PROJECTION"].classification == reconciliation.NOT_EXTERNALLY_APPLIED
    assert plans["NOTION_PROJECTION"].external_resource_ids == ("legacy-reservation",)


def test_apply_reconciles_exact_two_without_incrementing_attempt_or_fence(monkeypatch):
    monkeypatch.setattr(reconciliation, "mutation_scope", _scope)
    monkeypatch.setattr(reconciliation, "assert_current_production_writer", lambda: None)
    legacy = ExternalResourceSnapshot(
        "legacy-reservation",
        {
            "reservation_id": "legacy-id",
            "reservation_code": "HM3MSAA2TA",
            "reservation_status": "CONFIRMED",
            "check_in_at": "2026-09-22T00:00:00",
            "check_out_at": "2026-09-25T00:00:00",
        },
    )
    repo, notion, calendar, service = _service(
        notion=FakeNotionClient(reservation=[legacy])
    )
    plans = service.apply(operation_id=reconciliation.OPERATION_ID)
    assert {plan.classification for plan in plans} == {reconciliation.NOT_EXTERNALLY_APPLIED}
    assert notion.effects == [("update", "RESERVATION"), ("create", "CLEANING")]
    assert calendar.effects == ["create"]
    assert set(repo.resolved) == set(reconciliation.EXPECTED_ROWS)
    assert all(
        row.outbox_status == "SUCCEEDED"
        and row.attempt_count == 1
        and row.lease_fence == 1
        and row.external_effect_id
        for row in repo.rows.values()
    )

    effects = (list(notion.effects), list(calendar.effects))
    again = service.apply(operation_id=reconciliation.OPERATION_ID)
    assert {plan.classification for plan in again} == {reconciliation.ALREADY_EXTERNALLY_APPLIED}
    assert (notion.effects, calendar.effects) == effects


def test_ambiguity_stops_both_rows_before_lease_or_effect(monkeypatch):
    entered = []
    @contextmanager
    def forbidden_scope(*args, **kwargs):
        entered.append(True)
        yield
    monkeypatch.setattr(reconciliation, "mutation_scope", forbidden_scope)
    duplicate = [
        ExternalResourceSnapshot("reservation-1", {}),
        ExternalResourceSnapshot("reservation-2", {}),
    ]
    repo, notion, calendar, service = _service(
        notion=FakeNotionClient(reservation=duplicate)
    )
    with pytest.raises(reconciliation.ProjectionReconciliationError, match="AMBIGUOUS"):
        service.apply(operation_id=reconciliation.OPERATION_ID)
    assert entered == []
    assert notion.effects == [] and calendar.effects == [] and repo.resolved == []


def test_lost_local_resolution_is_safely_reconciled_from_fresh_external_readback(monkeypatch):
    monkeypatch.setattr(reconciliation, "mutation_scope", _scope)
    monkeypatch.setattr(reconciliation, "assert_current_production_writer", lambda: None)
    repo, notion, calendar, service = _service()
    # Calendar delivers first; simulate acknowledgement loss at its local CAS.
    repo.reject_resolution_once = True
    with pytest.raises(reconciliation.ProjectionReconciliationError, match="CAS_REJECTED"):
        service.apply(operation_id=reconciliation.OPERATION_ID)
    assert calendar.effects == ["create"]
    assert repo.rows[UUID("d4b4db22-6ecc-4232-b3a5-ca033334b2aa")].outbox_status == "PENDING_RECONCILIATION"

    service.apply(operation_id=reconciliation.OPERATION_ID)
    assert calendar.effects == ["create"]
    assert all(row.outbox_status == "SUCCEEDED" for row in repo.rows.values())

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from propertyai_core.application.cleaner_pg import EVENT_DESTINATIONS
from propertyai_core.ports.cleaner_repository import OutboxClaim
from propertyai_core.runtime.cleaner_projection_adapters import (
    CALENDAR_RESOURCE_CODE,
    CalendarCurrentStateProjectionAdapter,
    DestinationProjectionRouter,
    ExternalResourceSnapshot,
    NotionCurrentStateProjectionAdapter,
    ProjectionPendingReconciliation,
    TelegramSealedEffectAdapter,
)
from propertyai_core.runtime.cleaner_topology import CleanerRuntimeTopology, topology_contract
from propertyai_core.runtime import postgres_outbox_worker as worker_mod


RESERVATION_ID = UUID("11111111-1111-4111-8111-111111111111")
CLEANING_ID = UUID("22222222-2222-4222-8222-222222222222")
PARTY_ID = UUID("33333333-3333-4333-8333-333333333333")
OUTBOX_ID = UUID("44444444-4444-4444-8444-444444444444")


def _claim(
    *,
    event="RESERVATION_INGESTED",
    destination="NOTION_PROJECTION",
    payload=None,
    aggregate_type="RESERVATION",
    aggregate_id=RESERVATION_ID,
    attempt_count=1,
    external_effect_id=None,
):
    return OutboxClaim(
        outbox_id=OUTBOX_ID,
        event_type=event,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        destination_type=destination,
        destination_ref=None,
        payload=payload or {},
        attempt_count=attempt_count,
        max_attempts=5,
        lease_owner="w07-test",
        lease_until=datetime.now(timezone.utc),
        lease_fence=7,
        external_effect_id=external_effect_id,
    )


class FakeState:
    def __init__(self):
        self.reservation = {
            "reservation_id": RESERVATION_ID,
            "reservation_code": "HMTEST001",
            "reservation_status": "CONFIRMED",
            "check_in_at": datetime(2026, 9, 20, tzinfo=timezone.utc),
            "check_out_at": datetime(2026, 9, 22, tzinfo=timezone.utc),
            "source_version": 1,
        }
        self.cleaning = {
            "cleaning_id": CLEANING_ID,
            "cleaning_code": "CLEANING-HMTEST001",
            "reservation_id": RESERVATION_ID,
            "cleaning_status": "PLANNED",
            "revision_no": 1,
            "service_window_start_at": datetime(2026, 9, 22, 2, tzinfo=timezone.utc),
            "service_deadline_at": datetime(2026, 9, 22, 5, tzinfo=timezone.utc),
            "required_work_minutes": 120,
        }
        self.bindings = {}
        self.bind_calls = []

    def reservation_projection_state(self, reservation_id):
        assert reservation_id == RESERVATION_ID
        return dict(self.reservation)

    def cleaning_projection_state(self, cleaning_id):
        assert cleaning_id == CLEANING_ID
        return dict(self.cleaning)

    def resource_binding(self, **key):
        return self.bindings.get((key["aggregate_type"], key["aggregate_id"], key["destination_type"], key["resource_code"]))

    def bind_resource(self, **value):
        key = (value["aggregate_type"], value["aggregate_id"], value["destination_type"], value["resource_code"])
        existing = self.bindings.get(key)
        if existing is not None and existing["external_resource_id"] != value["external_resource_id"]:
            raise AssertionError("binding identity changed")
        row = dict(value)
        self.bindings[key] = row
        self.bind_calls.append(row)
        return row

    def rebind_resource(self, **value):
        key = (value["aggregate_type"], value["aggregate_id"], value["destination_type"], value["resource_code"])
        existing = self.bindings.get(key)
        assert existing is not None
        assert existing["external_resource_id"] == value["expected_external_resource_id"]
        row = dict(value)
        row["external_resource_id"] = row.pop("new_external_resource_id")
        row.pop("expected_external_resource_id")
        self.bindings[key] = row
        self.bind_calls.append(row)
        return row


class FakeNotion:
    def __init__(self):
        self.resources = {}
        self.creates = []
        self.updates = []
        self.create_raises = False
        self.find_override = None

    def _key(self, kind, identity):
        return kind, tuple(sorted(identity.items()))

    def find(self, kind, identity):
        if self.find_override is not None:
            return list(self.find_override)
        key = self._key(kind, identity)
        return [value for value in self.resources.values() if value.state.get("_identity") == key]

    def read(self, kind, external_id):
        del kind
        return self.resources.get(external_id)

    def create(self, kind, identity, desired):
        self.creates.append((kind, dict(identity), dict(desired)))
        if self.create_raises:
            raise RuntimeError("uncertain create")
        external_id = f"notion-{kind.lower()}-{len(self.creates)}"
        state = dict(desired)
        snapshot = ExternalResourceSnapshot(external_id, state)
        # Keep identity outside the externally compared accepted field set.
        self.resources[external_id] = snapshot
        self._identities = getattr(self, "_identities", {})
        self._identities[self._key(kind, identity)] = external_id
        return snapshot

    def update(self, kind, external_id, desired):
        del kind
        self.updates.append((external_id, dict(desired)))
        self.resources[external_id] = ExternalResourceSnapshot(external_id, dict(desired))
        return self.resources[external_id]

    def find(self, kind, identity):  # noqa: F811 - explicit identity index for the fake
        if self.find_override is not None:
            return list(self.find_override)
        external_id = getattr(self, "_identities", {}).get(self._key(kind, identity))
        return [] if external_id is None else [self.resources[external_id]]


class FakeCalendar:
    def __init__(self):
        self.resources = {}
        self.identities = {}
        self.creates = []
        self.updates = []
        self.retires = []
        self.find_override = None

    @staticmethod
    def _key(identity):
        return tuple(sorted(identity.items()))

    def find(self, identity):
        if self.find_override is not None:
            return list(self.find_override)
        external_id = self.identities.get(self._key(identity))
        return [] if external_id is None else [self.resources[external_id]]

    def read(self, external_id):
        return self.resources.get(external_id)

    def create(self, identity, desired):
        self.creates.append((dict(identity), dict(desired)))
        external_id = f"gcal-{len(self.creates)}"
        self.identities[self._key(identity)] = external_id
        self.resources[external_id] = ExternalResourceSnapshot(external_id, dict(desired), external_uid=f"uid-{external_id}")
        return self.resources[external_id]

    def update(self, external_id, desired):
        self.updates.append((external_id, dict(desired)))
        self.resources[external_id] = ExternalResourceSnapshot(external_id, dict(desired), external_uid=f"uid-{external_id}")
        return self.resources[external_id]

    def retire(self, external_id):
        self.retires.append(external_id)
        self.resources.pop(external_id, None)
        for key, value in list(self.identities.items()):
            if value == external_id:
                self.identities.pop(key)


class FakeTelegram:
    def __init__(self, *, raises=False):
        self.raises = raises
        self.sent = []
        self.resolved = []

    def resolve_recipient(self, effect):
        recipient = effect["recipient"]
        resolved = f"telegram-chat:{recipient['identity']}"
        self.resolved.append(resolved)
        return resolved

    def send(self, effect, *, recipient_identity, delivery_identity):
        self.sent.append((dict(effect), recipient_identity, delivery_identity))
        if self.raises:
            raise RuntimeError("provider outcome ambiguous")
        return "tg-message-42"


def test_actual_event_type_matrix_is_explicit_and_matches_w07_allowlist():
    expected = {
        "RESERVATION_INGESTED": ("NOTION_PROJECTION", "CALENDAR_PROJECTION"),
        "RESERVATION_CANCELLED": ("NOTION_PROJECTION", "CALENDAR_PROJECTION"),
        "CLEANING_ASSIGNMENT_OFFER_OPENED": ("TELEGRAM_PROJECTION",),
        "CLEANING_ASSIGNMENT_ACCEPTED": ("NOTION_PROJECTION", "TELEGRAM_PROJECTION"),
        "CLEANING_ASSIGNMENT_DECLINED": ("TELEGRAM_PROJECTION",),
        "CLEANER_UNAVAILABLE_CONFIRMED": ("NOTION_PROJECTION", "TELEGRAM_PROJECTION"),
        "CLEANER_REASSIGNMENT_REQUESTED": ("TELEGRAM_PROJECTION",),
        "ORIGINAL_CLEANER_REASSIGNED": ("NOTION_PROJECTION", "TELEGRAM_PROJECTION"),
        "CLEANER_REPLACEMENT_CONTINUES": ("TELEGRAM_PROJECTION",),
        "CLEANING_COMPLETED": ("NOTION_PROJECTION", "TELEGRAM_PROJECTION"),
    }
    assert EVENT_DESTINATIONS == expected
    assert worker_mod.ALLOWED_EVENT_DESTINATIONS == {
        (event, destination)
        for event, destinations in expected.items()
        for destination in destinations
    }


def test_generic_reservation_ingest_has_zero_telegram_effect_and_no_x3_fanout():
    assert EVENT_DESTINATIONS["RESERVATION_INGESTED"] == (
        "NOTION_PROJECTION", "CALENDAR_PROJECTION"
    )
    assert "TELEGRAM_PROJECTION" not in EVENT_DESTINATIONS["RESERVATION_INGESTED"]
    assert max(map(len, EVENT_DESTINATIONS.values())) == 2


def test_notion_binding_create_update_noop_uses_only_notion_state():
    state = FakeState()
    notion = FakeNotion()
    adapter = NotionCurrentStateProjectionAdapter(state, notion)
    claim = _claim(payload={})

    first = adapter.deliver(claim)
    assert first.external_effect_id == "notion:notion-reservation-1"
    assert len(notion.creates) == 1 and notion.updates == []
    assert state.bind_calls[-1]["destination_type"] == "NOTION"
    assert state.bind_calls[-1]["resource_code"] == "RESERVATION"

    adapter.deliver(claim)
    assert len(notion.creates) == 1 and notion.updates == []

    state.reservation["source_version"] = 2
    state.reservation["reservation_status"] = "CANCELLED"
    adapter.deliver(_claim(event="RESERVATION_CANCELLED"))
    assert len(notion.updates) == 1


def test_notion_ambiguous_create_outcome_stops_for_reconciliation_without_blind_retry():
    state = FakeState()
    notion = FakeNotion()
    notion.create_raises = True
    notion.find_override = []
    adapter = NotionCurrentStateProjectionAdapter(state, notion)
    with pytest.raises(ProjectionPendingReconciliation, match="NOTION_CREATE_OUTCOME_AMBIGUOUS"):
        adapter.deliver(_claim())
    assert len(notion.creates) == 1
    assert notion.updates == []
    assert state.bind_calls == []


def test_calendar_create_update_noop_and_recreate_are_binding_based():
    state = FakeState()
    calendar = FakeCalendar()
    adapter = CalendarCurrentStateProjectionAdapter(state, calendar)
    claim = _claim(destination="CALENDAR_PROJECTION", payload={"cleaning_id": str(CLEANING_ID)})

    first = adapter.deliver(claim)
    assert first.external_effect_id == "gcal-1"
    assert len(calendar.creates) == 1 and calendar.updates == []
    assert state.bind_calls[-1]["resource_code"] == CALENDAR_RESOURCE_CODE

    adapter.deliver(claim)
    assert len(calendar.creates) == 1 and calendar.updates == []

    state.cleaning["revision_no"] = 2
    state.cleaning["service_deadline_at"] = datetime(2026, 9, 22, 6, tzinfo=timezone.utc)
    adapter.deliver(claim)
    assert len(calendar.updates) == 1

    bound = state.resource_binding(
        aggregate_type="CLEANING", aggregate_id=CLEANING_ID,
        destination_type="CALENDAR", resource_code=CALENDAR_RESOURCE_CODE,
    )
    calendar.resources.pop(bound["external_resource_id"])
    for key in list(calendar.identities):
        calendar.identities.pop(key)
    adapter.deliver(claim)
    assert len(calendar.creates) == 2


def test_calendar_orphan_during_cancel_is_not_silently_deleted():
    state = FakeState()
    state.cleaning["cleaning_status"] = "CANCELLED"
    orphan = ExternalResourceSnapshot("orphan-1", dict(state.cleaning))
    calendar = FakeCalendar()
    calendar.find_override = [orphan]
    adapter = CalendarCurrentStateProjectionAdapter(state, calendar)
    claim = _claim(
        event="RESERVATION_CANCELLED",
        destination="CALENDAR_PROJECTION",
        payload={"cleaning_id": str(CLEANING_ID)},
    )
    with pytest.raises(ProjectionPendingReconciliation, match="CALENDAR_ORPHAN"):
        adapter.deliver(claim)
    assert calendar.retires == []


def test_calendar_explicit_cancel_retires_only_exact_bound_event():
    state = FakeState()
    calendar = FakeCalendar()
    adapter = CalendarCurrentStateProjectionAdapter(state, calendar)
    active = _claim(destination="CALENDAR_PROJECTION", payload={"cleaning_id": str(CLEANING_ID)})
    adapter.deliver(active)
    state.cleaning["cleaning_status"] = "CANCELLED"
    cancelled = _claim(
        event="RESERVATION_CANCELLED",
        destination="CALENDAR_PROJECTION",
        payload={"cleaning_id": str(CLEANING_ID)},
    )
    adapter.deliver(cancelled)
    assert calendar.retires == ["gcal-1"]
    assert state.bind_calls[-1]["sync_status"] == "RETIRED"


def test_telegram_success_receipt_and_stable_delivery_identity():
    client = FakeTelegram()
    adapter = TelegramSealedEffectAdapter(client)
    effect = {
        "effect_kind": "CLEANING_ASSIGNMENT_OFFER_OPENED",
        "recipient": {"kind": "PARTY", "identity": str(PARTY_ID)},
    }
    claim = _claim(
        event="CLEANING_ASSIGNMENT_OFFER_OPENED",
        destination="TELEGRAM_PROJECTION",
        aggregate_type="CLEANING",
        aggregate_id=CLEANING_ID,
        payload={"telegram_effect": effect},
    )
    receipt = adapter.deliver(claim)
    assert receipt.external_effect_id == "tg-message-42"
    assert len(client.sent) == 1
    assert client.sent[0][1] == f"telegram-chat:{PARTY_ID}"
    assert client.sent[0][2] == adapter.delivery_identity(
        claim, effect, f"telegram-chat:{PARTY_ID}"
    )


def test_telegram_prior_attempt_without_receipt_never_blind_resends():
    client = FakeTelegram()
    adapter = TelegramSealedEffectAdapter(client)
    claim = _claim(
        event="CLEANING_COMPLETED",
        destination="TELEGRAM_PROJECTION",
        aggregate_type="CLEANING",
        aggregate_id=CLEANING_ID,
        attempt_count=2,
        payload={
            "telegram_effect": {
                "effect_kind": "CLEANING_COMPLETED",
                "recipient": {"kind": "PARTY", "identity": str(PARTY_ID)},
            }
        },
    )
    with pytest.raises(ProjectionPendingReconciliation, match="PRIOR_ATTEMPT"):
        adapter.deliver(claim)
    assert client.sent == []


def test_destination_router_has_zero_cross_destination_effects():
    calls = {"notion": 0, "calendar": 0, "telegram": 0}

    class Adapter:
        def __init__(self, name): self.name = name
        def deliver(self, claim):
            calls[self.name] += 1
            return SimpleNamespace(external_effect_id=self.name)

    router = DestinationProjectionRouter(
        notion=Adapter("notion"), calendar=Adapter("calendar"), telegram=Adapter("telegram")
    )
    router.deliver(_claim(destination="NOTION_PROJECTION"))
    assert calls == {"notion": 1, "calendar": 0, "telegram": 0}
    router.deliver(_claim(destination="CALENDAR_PROJECTION", payload={"cleaning_id": str(CLEANING_ID)}))
    assert calls == {"notion": 1, "calendar": 1, "telegram": 0}
    router.deliver(_claim(
        event="CLEANING_COMPLETED", destination="TELEGRAM_PROJECTION",
        aggregate_type="CLEANING", aggregate_id=CLEANING_ID,
        payload={"telegram_effect": {"effect_kind": "CLEANING_COMPLETED", "recipient": {"kind": "PARTY", "identity": str(PARTY_ID)}}},
    ))
    assert calls == {"notion": 1, "calendar": 1, "telegram": 1}


def test_w07_crash_restart_path_marks_telegram_reconciliation_without_resend(monkeypatch):
    from propertyai_core.tests.batch_a_failure_harness import MemoryOutboxWorkerPort
    @contextmanager
    def scope(*args, **kwargs): yield
    monkeypatch.setattr(worker_mod, "mutation_scope", scope)
    monkeypatch.setattr(worker_mod, "assert_current_production_writer", lambda: None)
    claim = _claim(event="CLEANING_COMPLETED", destination="TELEGRAM_PROJECTION", aggregate_type="CLEANING", aggregate_id=CLEANING_ID, attempt_count=2,
        payload={"telegram_effect": {"effect_kind": "CLEANING_COMPLETED", "recipient": {"kind": "PARTY", "identity": str(PARTY_ID)}}})
    repo = MemoryOutboxWorkerPort(claim)
    client = FakeTelegram()
    result = worker_mod.CleanerPostgresOutboxWorker(repo, TelegramSealedEffectAdapter(client), worker_id="w07-test").run_once()
    assert result.status == "PENDING_RECONCILIATION" and client.sent == []
    assert repo.state.last_error_code == "RECLAIMED_OUTCOME_REQUIRES_EVIDENCE"
    assert repo.calls == [("CLAIM", 7), ("INTENT", 7)]


def test_w06_post_topology_requires_w07_and_cannot_resurrect_w04_w05():
    contract = topology_contract(CleanerRuntimeTopology.POST_CUTOVER_PG)
    assert contract.services["cleaner_pg_outbox"] == ("com.propertyai.cleaner-pg-outbox", True)
    assert "com.propertyai.cleaner-pg-outbox" in contract.recovery_targets
    assert "com.propertyai.cleaning-operations" not in contract.recovery_targets
    assert "com.propertyai.cleaning-completion" not in contract.recovery_targets


def test_w07_complete_fence_failure_after_effect_is_reconciliation_only(monkeypatch):
    from propertyai_core.tests.batch_a_failure_harness import MemoryOutboxWorkerPort
    @contextmanager
    def scope(*args, **kwargs): yield
    monkeypatch.setattr(worker_mod, "mutation_scope", scope)
    monkeypatch.setattr(worker_mod, "assert_current_production_writer", lambda: None)
    claim = _claim(event="CLEANING_COMPLETED", destination="TELEGRAM_PROJECTION", aggregate_type="CLEANING", aggregate_id=CLEANING_ID,
        payload={"telegram_effect": {"effect_kind": "CLEANING_COMPLETED", "recipient": {"kind": "PARTY", "identity": str(PARTY_ID)}}})
    repo = MemoryOutboxWorkerPort(claim, resolution_allowed=False)
    delivered = []
    def deliver(value):
        assert repo.state.outbox_status == "PENDING_RECONCILIATION"
        delivered.append(value)
        return SimpleNamespace(external_effect_id="provider-message-42")
    result = worker_mod.CleanerPostgresOutboxWorker(repo, SimpleNamespace(deliver=deliver), worker_id="w07-test").run_once()
    assert result.status == "PENDING_RECONCILIATION" and len(delivered) == 1
    assert repo.failed == [] and repo.state.outbox_status == "PENDING_RECONCILIATION"
    assert result.external_effect_id == "provider-message-42"
    assert result.diagnostic_code == "EXTERNAL_EFFECT_UNCERTAIN:CleanerOutboxDeliveryError"
    assert repo.calls == [("CLAIM", 7), ("INTENT", 7), ("RESOLVE", 7)]


def test_w07_provider_success_writer_loss_preserves_durable_intent_and_reports_receipt(monkeypatch):
    from propertyai_core.tests.batch_a_failure_harness import MemoryOutboxWorkerPort
    @contextmanager
    def scope(*args, **kwargs): yield
    monkeypatch.setattr(worker_mod, "mutation_scope", scope)
    delivered = []
    def writer_assertion():
        if delivered: raise RuntimeError("writer lost")
    monkeypatch.setattr(worker_mod, "assert_current_production_writer", writer_assertion)
    claim = _claim(event="CLEANING_COMPLETED", destination="TELEGRAM_PROJECTION", aggregate_type="CLEANING", aggregate_id=CLEANING_ID,
        payload={"telegram_effect": {"effect_kind": "CLEANING_COMPLETED", "recipient": {"kind": "PARTY", "identity": str(PARTY_ID)}}})
    repo = MemoryOutboxWorkerPort(claim)
    def deliver(value):
        assert repo.state.outbox_status == "PENDING_RECONCILIATION"
        delivered.append(value)
        return SimpleNamespace(external_effect_id="provider-message-42")
    result = worker_mod.CleanerPostgresOutboxWorker(repo, SimpleNamespace(deliver=deliver), worker_id="w07-test").run_once()
    assert result.status == "PENDING_RECONCILIATION" and result.external_effect_id == "provider-message-42"
    assert repo.failed == [] and repo.calls == [("CLAIM", 7), ("INTENT", 7)]
    assert repo.state.outbox_status == "PENDING_RECONCILIATION" and repo.state.lease_fence == 7


def test_w07_ambiguous_effect_writer_loss_never_blind_retries(monkeypatch):
    from propertyai_core.tests.batch_a_failure_harness import MemoryOutboxWorkerPort
    @contextmanager
    def scope(*args, **kwargs): yield
    monkeypatch.setattr(worker_mod, "mutation_scope", scope)
    monkeypatch.setattr(worker_mod, "assert_current_production_writer", lambda: None)
    claim = _claim(event="CLEANING_COMPLETED", destination="TELEGRAM_PROJECTION", aggregate_type="CLEANING", aggregate_id=CLEANING_ID,
        payload={"telegram_effect": {"effect_kind": "CLEANING_COMPLETED", "recipient": {"kind": "PARTY", "identity": str(PARTY_ID)}}})
    repo = MemoryOutboxWorkerPort(claim)
    def deliver(value):
        assert repo.state.outbox_status == "PENDING_RECONCILIATION"
        raise TimeoutError("provider acknowledgement unknown")
    result = worker_mod.CleanerPostgresOutboxWorker(repo, SimpleNamespace(deliver=deliver), worker_id="w07-test").run_once()
    assert result.status == "PENDING_RECONCILIATION" and result.external_effect_id is None
    assert repo.failed == [] and repo.calls == [("CLAIM", 7), ("INTENT", 7)]


def test_w07_superseded_claim_cannot_reconcile_or_enable_retry(monkeypatch):
    from propertyai_core.tests.batch_a_failure_harness import MemoryOutboxWorkerPort
    @contextmanager
    def scope(*args, **kwargs): yield
    monkeypatch.setattr(worker_mod, "mutation_scope", scope)
    monkeypatch.setattr(worker_mod, "assert_current_production_writer", lambda: None)
    claim = _claim(event="CLEANING_COMPLETED", destination="TELEGRAM_PROJECTION", aggregate_type="CLEANING", aggregate_id=CLEANING_ID,
        payload={"telegram_effect": {"effect_kind": "CLEANING_COMPLETED", "recipient": {"kind": "PARTY", "identity": str(PARTY_ID)}}})
    repo = MemoryOutboxWorkerPort(claim, intent_allowed=False)
    adapter = SimpleNamespace(deliver=lambda claim: pytest.fail("stale intent reached provider"))
    with pytest.raises(worker_mod.CleanerOutboxDeliveryError, match="OUTBOX_DELIVERY_INTENT_FENCE_REJECTED"):
        worker_mod.CleanerPostgresOutboxWorker(repo, adapter, worker_id="w07-test").run_once()
    assert repo.calls == [("CLAIM", 7), ("INTENT", 7)] and repo.failed == []

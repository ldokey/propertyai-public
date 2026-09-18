from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from propertyai_core.ports.cleaner_repository import OutboxClaim
from propertyai_core.runtime.cleaner_topology import (
    CleanerRuntimeTopology,
    topology_contract,
    topology_contract_from_env,
)
from propertyai_core.runtime import postgres_outbox_worker as worker_mod


def _claim(*, destination="NOTION_PROJECTION", event="RESERVATION_INGESTED"):
    return OutboxClaim(
        outbox_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        event_type=event,
        aggregate_type="RESERVATION",
        aggregate_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        destination_type=destination,
        destination_ref=None,
        payload={"reservation_code": "HMFIRST001"},
        attempt_count=1,
        max_attempts=5,
        lease_owner="w07-test",
        lease_until=datetime.now(timezone.utc),
        lease_fence=3,
    )


def test_post_cutover_topology_retires_w04_w05_and_adds_w07():
    contract = topology_contract(CleanerRuntimeTopology.POST_CUTOVER_PG)
    labels = {label for label, _ in contract.services.values()}
    assert "com.propertyai.cleaning-operations" not in labels
    assert "com.propertyai.cleaning-completion" not in labels
    assert "com.propertyai.cleaner-pg-outbox" in labels
    assert "com.propertyai.gmail-readonly" in labels
    assert "com.propertyai.telegram-cleaner" in labels
    assert "com.propertyai.cleaning-operations" in contract.retired_labels
    assert "com.propertyai.cleaning-completion" in contract.retired_labels


def test_pre_cutover_topology_preserves_legacy_w04_w05_and_retires_w07():
    contract = topology_contract(CleanerRuntimeTopology.PRE_CUTOVER)
    labels = {label for label, _ in contract.services.values()}
    assert "com.propertyai.cleaning-operations" in labels
    assert "com.propertyai.cleaning-completion" in labels
    assert "com.propertyai.cleaner-pg-outbox" not in labels
    assert "com.propertyai.cleaner-pg-outbox" in contract.retired_labels


def test_invalid_topology_fails_closed():
    with pytest.raises(RuntimeError, match="INVALID_CLEANER_RUNTIME_TOPOLOGY"):
        topology_contract_from_env({"PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY": "AUTO"})


def _patch_writer(monkeypatch):
    entered = []
    @contextmanager
    def scope(writer, **kwargs):
        entered.append((writer, kwargs))
        yield
    monkeypatch.setattr(worker_mod, "mutation_scope", scope)
    monkeypatch.setattr(worker_mod, "assert_current_production_writer", lambda: None)
    return entered


def test_outbox_worker_claims_exactly_one_completes_fenced(monkeypatch):
    from propertyai_core.tests.batch_a_failure_harness import MemoryOutboxWorkerPort
    entered = _patch_writer(monkeypatch)
    claim = _claim()
    repo = MemoryOutboxWorkerPort(claim)
    class Adapter:
        def deliver(self, value):
            assert value == claim and repo.state.outbox_status == "PENDING_RECONCILIATION"
            assert repo.calls == [("CLAIM", 3), ("INTENT", 3)]
            return worker_mod.DeliveryReceipt("notion:page-1")
    result = worker_mod.CleanerPostgresOutboxWorker(repo, Adapter(), worker_id="w07-test").run_once()
    assert result.status == "COMPLETED" and repo.state.outbox_status == "SUCCEEDED"
    assert repo.state.external_effect_id == "notion:page-1" and repo.failed == []
    assert repo.calls == [("CLAIM", 3), ("INTENT", 3), ("RESOLVE", 3)]
    assert entered[0][0] == "W07"


def test_outbox_worker_external_failure_requires_reconciliation_not_blind_retry(monkeypatch):
    from propertyai_core.tests.batch_a_failure_harness import MemoryOutboxWorkerPort
    _patch_writer(monkeypatch)
    repo = MemoryOutboxWorkerPort(_claim())
    class Adapter:
        def deliver(self, claim):
            assert repo.state.outbox_status == "PENDING_RECONCILIATION"
            raise RuntimeError("transport")
    result = worker_mod.CleanerPostgresOutboxWorker(repo, Adapter(), worker_id="w07-test").run_once()
    assert result.status == "PENDING_RECONCILIATION" and repo.failed == []
    assert repo.state.last_error_code == "DELIVERY_INTENT_UNRESOLVED"
    assert result.diagnostic_code == "EXTERNAL_EFFECT_UNCERTAIN:RuntimeError"
    assert repo.calls == [("CLAIM", 3), ("INTENT", 3)]


def test_outbox_worker_rejects_unallowlisted_dispatch_without_adapter(monkeypatch):
    _patch_writer(monkeypatch)
    claim = _claim(destination="ARBITRARY_URL", event="RUN_COMMAND")
    class Repo:
        def __init__(self): self.failed=0
        def claim_outbox(self, *args, **kwargs): return [claim]
        def complete_outbox(self, *args, **kwargs): pytest.fail("complete forbidden")
        def fail_outbox(self, *args, **kwargs): self.failed += 1; return True
    class Adapter:
        def deliver(self, _claim): pytest.fail("adapter forbidden")
    repo=Repo()
    result=worker_mod.CleanerPostgresOutboxWorker(repo, Adapter(), worker_id="w07-test").run_once()
    assert result.status == "RETRY_SCHEDULED"
    assert repo.failed == 1


def test_outbox_worker_idle_has_no_external_delivery(monkeypatch):
    _patch_writer(monkeypatch)
    class Repo:
        def claim_outbox(self, *args, **kwargs): return []
    class Adapter:
        def deliver(self, _claim): pytest.fail("adapter forbidden")
    assert worker_mod.CleanerPostgresOutboxWorker(Repo(), Adapter(), worker_id="w07-test").run_once().status == "IDLE"


def test_first_authoritative_service_rejects_reused_command_receipt_inside_transaction():
    from contextlib import contextmanager
    from datetime import timedelta
    from propertyai_core.application.cleaner_pg import (
        CommandMetadata,
        FirstAuthoritativeReservationAlreadyPresent,
        PostgresCleanerApplicationService,
        ReservationIngressCommand,
    )
    now = datetime.now(timezone.utc)
    command = ReservationIngressCommand(
        metadata=CommandMetadata(
            idempotency_key="AIRBNB_RESERVATION_EVENT:" + "a" * 64,
            source_channel_code="AIRBNB",
            source_stream_key="HMFIRST001",
            source_event_id="a" * 64,
            authority_epoch=9,
            decided_at=now,
        ),
        source_channel="AIRBNB",
        external_reservation_id="HMFIRST001",
        reservation_code="HMFIRST001",
        property_id=UUID("11111111-1111-4111-8111-111111111111"),
        rental_unit_id=UUID("22222222-2222-4222-8222-222222222222"),
        reservation_status="CONFIRMED",
        check_in_at=now,
        check_out_at=now + timedelta(days=2),
        cleaning_code="CLEANING-AIRBNB-HMFIRST001",
        service_window_start_at=now + timedelta(days=2),
        service_deadline_at=now + timedelta(days=2, hours=3),
        required_work_minutes=120,
    )

    class Tx:
        def verify_authority_epoch(self, scope, epoch):
            assert (scope, epoch) == ("CLEANER_SCHEDULING", 9)
            return epoch
        def find_command_receipt(self, scope, command_type, key):
            assert (scope, command_type, key) == (
                "CLEANER_SCHEDULING", "RESERVATION_INGRESS", command.metadata.idempotency_key
            )
            return UUID("33333333-3333-4333-8333-333333333333")
        def find_reservation_by_source_code(self, *_args):
            pytest.fail("reservation absence check must not follow an existing receipt")
        def register_command_receipt(self, _receipt):
            pytest.fail("receipt insert forbidden after existing-receipt precheck")

    class Repo:
        @contextmanager
        def transaction(self):
            yield Tx()

    with pytest.raises(FirstAuthoritativeReservationAlreadyPresent, match="FIRST_PG_COMMAND_RECEIPT_ALREADY_PRESENT"):
        PostgresCleanerApplicationService(Repo()).ingest_reservation(command, require_absent=True)


def test_first_authoritative_service_rejects_existing_airbnb_reservation_inside_transaction():
    from contextlib import contextmanager
    from datetime import timedelta
    from propertyai_core.application.cleaner_pg import (
        CommandMetadata,
        FirstAuthoritativeReservationAlreadyPresent,
        PostgresCleanerApplicationService,
        ReservationIngressCommand,
    )
    now = datetime.now(timezone.utc)
    command = ReservationIngressCommand(
        metadata=CommandMetadata(
            idempotency_key="AIRBNB_RESERVATION_EVENT:" + "b" * 64,
            source_channel_code="AIRBNB",
            source_stream_key="HMFIRST002",
            source_event_id="b" * 64,
            authority_epoch=9,
            decided_at=now,
        ),
        source_channel="AIRBNB",
        external_reservation_id="HMFIRST002",
        reservation_code="HMFIRST002",
        property_id=UUID("11111111-1111-4111-8111-111111111111"),
        rental_unit_id=None,
        reservation_status="CONFIRMED",
        check_in_at=now,
        check_out_at=now + timedelta(days=2),
        cleaning_code="CLEANING-AIRBNB-HMFIRST002",
        service_window_start_at=now + timedelta(days=2),
        service_deadline_at=now + timedelta(days=2, hours=3),
        required_work_minutes=120,
    )

    class Tx:
        def verify_authority_epoch(self, scope, epoch):
            assert (scope, epoch) == ("CLEANER_SCHEDULING", 9)
            return epoch
        def find_command_receipt(self, scope, command_type, key):
            assert (scope, command_type, key) == (
                "CLEANER_SCHEDULING", "RESERVATION_INGRESS", command.metadata.idempotency_key
            )
            return None
        def find_reservation_by_source_code(self, source_channel, reservation_code):
            assert (source_channel, reservation_code) == ("AIRBNB", "HMFIRST002")
            return {"reservation_id": UUID("44444444-4444-4444-8444-444444444444")}
        def register_command_receipt(self, _receipt):
            pytest.fail("receipt insert forbidden before reservation absence passes")

    class Repo:
        @contextmanager
        def transaction(self):
            yield Tx()

    with pytest.raises(FirstAuthoritativeReservationAlreadyPresent, match="FIRST_PG_RESERVATION_ALREADY_PRESENT"):
        PostgresCleanerApplicationService(Repo()).ingest_reservation(command, require_absent=True)


def test_first_authoritative_service_proves_both_absences_before_receipt_mutation():
    from contextlib import contextmanager
    from datetime import timedelta
    from propertyai_core.application.cleaner_pg import (
        CommandMetadata, PostgresCleanerApplicationService, ReservationIngressCommand,
    )

    class FirstMutationObserved(RuntimeError):
        pass

    now = datetime.now(timezone.utc)
    command = ReservationIngressCommand(
        metadata=CommandMetadata(
            idempotency_key="AIRBNB_RESERVATION_EVENT:" + "c" * 64,
            source_channel_code="AIRBNB", source_stream_key="HMFIRST003",
            source_event_id="c" * 64, authority_epoch=9, decided_at=now,
        ),
        source_channel="AIRBNB", external_reservation_id="HMFIRST003",
        reservation_code="HMFIRST003",
        property_id=UUID("11111111-1111-4111-8111-111111111111"), rental_unit_id=None,
        reservation_status="CONFIRMED", check_in_at=now, check_out_at=now + timedelta(days=2),
        cleaning_code="CLEANING-AIRBNB-HMFIRST003",
        service_window_start_at=now + timedelta(days=2),
        service_deadline_at=now + timedelta(days=2, hours=3), required_work_minutes=120,
    )
    sequence = []

    class Tx:
        def verify_authority_epoch(self, scope, epoch):
            sequence.append("AUTHORITY_LOCK")
            assert (scope, epoch) == ("CLEANER_SCHEDULING", 9)
            return epoch
        def find_command_receipt(self, *_args):
            sequence.append("RECEIPT_ABSENCE_CHECK")
            return None
        def find_reservation_by_source_code(self, *_args):
            sequence.append("RESERVATION_ABSENCE_CHECK")
            return None
        def register_command_receipt(self, _receipt):
            sequence.append("FIRST_MUTATING_STATEMENT")
            raise FirstMutationObserved

    class Repo:
        @contextmanager
        def transaction(self):
            yield Tx()

    with pytest.raises(FirstMutationObserved):
        PostgresCleanerApplicationService(Repo()).ingest_reservation(command, require_absent=True)
    assert sequence.index("RECEIPT_ABSENCE_CHECK") < sequence.index("RESERVATION_ABSENCE_CHECK")
    assert sequence.index("RESERVATION_ABSENCE_CHECK") < sequence.index("FIRST_MUTATING_STATEMENT")


def test_first_authoritative_receipt_race_loss_stops_before_business_mutation():
    from contextlib import contextmanager
    from datetime import timedelta
    from propertyai_core.application.cleaner_pg import (
        CommandMetadata, FirstAuthoritativeReservationAlreadyPresent,
        PostgresCleanerApplicationService, ReservationIngressCommand,
    )
    from propertyai_core.ports.cleaner_repository import CommandReceiptResult

    now = datetime.now(timezone.utc)
    command = ReservationIngressCommand(
        metadata=CommandMetadata(
            idempotency_key="AIRBNB_RESERVATION_EVENT:" + "d" * 64,
            source_channel_code="AIRBNB", source_stream_key="HMFIRST004",
            source_event_id="d" * 64, authority_epoch=9, decided_at=now,
        ),
        source_channel="AIRBNB", external_reservation_id="HMFIRST004",
        reservation_code="HMFIRST004",
        property_id=UUID("11111111-1111-4111-8111-111111111111"), rental_unit_id=None,
        reservation_status="CONFIRMED", check_in_at=now, check_out_at=now + timedelta(days=2),
        cleaning_code="CLEANING-AIRBNB-HMFIRST004",
        service_window_start_at=now + timedelta(days=2),
        service_deadline_at=now + timedelta(days=2, hours=3), required_work_minutes=120,
    )
    sequence = []

    class Tx:
        def verify_authority_epoch(self, *_args): return 9
        def find_command_receipt(self, *_args):
            sequence.append("RECEIPT_ABSENCE_CHECK"); return None
        def find_reservation_by_source_code(self, *_args):
            sequence.append("RESERVATION_ABSENCE_CHECK"); return None
        def register_command_receipt(self, receipt):
            sequence.append("RECEIPT_RACE_LOST")
            return CommandReceiptResult(receipt.command_id, True)
        def find_external_reservation(self, *_args):
            pytest.fail("business lookup/mutation forbidden after receipt-race loss")

    class Repo:
        @contextmanager
        def transaction(self): yield Tx()

    with pytest.raises(FirstAuthoritativeReservationAlreadyPresent, match="FIRST_PG_COMMAND_RECEIPT_ALREADY_PRESENT"):
        PostgresCleanerApplicationService(Repo()).ingest_reservation(command, require_absent=True)
    assert sequence == ["RECEIPT_ABSENCE_CHECK", "RESERVATION_ABSENCE_CHECK", "RECEIPT_RACE_LOST"]

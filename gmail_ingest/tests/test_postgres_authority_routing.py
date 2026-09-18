from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from gmail_ingest import booking_bridge
from gmail_ingest.postgres_ingress import normalize_airbnb_reservation_command
from propertyai_core.config.cleaner_authority import CleanerAuthorityConfig
from propertyai_core.stage_b.identity import notion_identity


PROPERTY_PAGE_ID = "11111111-1111-4111-8111-111111111111"
RENTAL_PAGE_ID = "22222222-2222-4222-8222-222222222222"


def _mapping():
    return {
        "nickname": "C2 House",
        "property_page_id": PROPERTY_PAGE_ID,
        "rental_unit_page_id": RENTAL_PAGE_ID,
        "check_in_time": "15:00",
        "check_out_time": "11:00",
        "cleaning_end_time": "15:00",
    }


def _event(source_hash="a" * 64):
    return {
        "event_type": "BOOKING_CONFIRMED",
        "source_message_hash": source_hash,
        "listing_id": "listing-1",
        "check_in": "2026-10-01",
        "check_out": "2026-10-03",
    }


def _write_mappings(tmp_path: Path):
    path = tmp_path / "mappings.json"
    path.write_text(json.dumps({"listings": {"listing-1": _mapping()}}))
    return path


def test_airbnb_normalizer_uses_provider_business_key_not_gmail_hash():
    from datetime import datetime, timezone

    command = normalize_airbnb_reservation_command(
        event=_event("b" * 64),
        reservation_code="HMABC123",
        mapping=_mapping(),
        authority_epoch=7,
        decided_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    assert command.source_channel == "AIRBNB"
    assert command.external_reservation_id == "HMABC123"
    assert command.reservation_code == "HMABC123"
    assert command.metadata.source_event_id == "b" * 64
    assert command.metadata.source_stream_key == "HMABC123"
    assert "b" * 64 not in str(command.property_id)
    assert "b" * 64 not in str(command.rental_unit_id)


def test_airbnb_normalizer_accepts_exact_authoritative_uuid_v8_mapping_ids():
    from datetime import datetime, timezone

    property_page_id = "11111111-1111-8111-9111-111111111111"
    rental_page_id = "22222222-2222-8222-a222-222222222222"
    mapping = {
        **_mapping(),
        "property_page_id": property_page_id,
        "rental_unit_page_id": rental_page_id,
    }
    command = normalize_airbnb_reservation_command(
        event=_event("8" * 64),
        reservation_code="HMUUIDV8001",
        mapping=mapping,
        authority_epoch=12,
        decided_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
    )

    assert command.property_id == notion_identity("property", property_page_id)
    assert command.rental_unit_id == notion_identity("rental-unit", rental_page_id)


def test_process_pending_legacy_route_never_calls_postgres(monkeypatch, tmp_path: Path):
    queue = tmp_path / "queue"
    queue.mkdir()
    (queue / "one.json").write_text('{}')
    monkeypatch.setattr(booking_bridge, "QUEUE_DIR", queue)
    monkeypatch.setattr(booking_bridge, "MAPPINGS_PATH", _write_mappings(tmp_path))
    calls = []
    monkeypatch.setattr(booking_bridge, "process_one", lambda *a, **k: calls.append("legacy") or "completed")

    def forbidden(*_a, **_k):
        raise AssertionError("POSTGRES route must not execute under LEGACY authority")

    monkeypatch.setattr(booking_bridge, "process_one_postgres", forbidden)
    counts = booking_bridge.process_pending(authority_config=CleanerAuthorityConfig())
    assert calls == ["legacy"]
    assert counts["completed"] == 1


def test_process_pending_postgres_route_never_calls_legacy(monkeypatch, tmp_path: Path):
    queue = tmp_path / "queue"
    queue.mkdir()
    (queue / "one.json").write_text('{}')
    monkeypatch.setattr(booking_bridge, "QUEUE_DIR", queue)
    monkeypatch.setattr(booking_bridge, "MAPPINGS_PATH", _write_mappings(tmp_path))
    service = object()
    calls = []

    def forbidden(*_a, **_k):
        raise AssertionError("legacy route must not execute under POSTGRES authority")

    def pg(path, mappings, *, service: object, authority_epoch: int, mode: str):
        calls.append((path.name, authority_epoch, mappings["listing-1"]["nickname"]))
        return "completed"

    monkeypatch.setattr(booking_bridge, "process_one", forbidden)
    monkeypatch.setattr(booking_bridge, "process_one_postgres", pg)
    authority = CleanerAuthorityConfig(
        authority="POSTGRES", pg_ingress_enabled=True, authority_epoch=9
    )
    counts = booking_bridge.process_pending(
        authority_config=authority,
        postgres_service=service,
    )
    assert calls == [("one.json", 9, "C2 House")]
    assert counts["completed"] == 1


def test_postgres_item_failure_is_fail_closed_without_legacy_fallback(monkeypatch, tmp_path: Path):
    record_path = tmp_path / "item.json"
    record_path.write_text(json.dumps({"status": "PENDING_NOTION_READ", "source_message_hash": "c" * 64}))
    mappings = {"listing-1": _mapping()}
    monkeypatch.setattr(
        booking_bridge,
        "fresh_source",
        lambda _hash: (_event("c" * 64), "HMFAIL123", "2026-09-09T00:00:00Z"),
    )

    def persist(path, value):
        path.write_text(json.dumps(value))

    monkeypatch.setattr(booking_bridge, "persist_authoritative_result", persist)
    for name in ("create_reservation", "create_cleaning", "ensure_calendar_event", "finalize_cleaning"):
        monkeypatch.setattr(
            booking_bridge,
            name,
            lambda *_a, _name=name, **_k: (_ for _ in ()).throw(
                AssertionError(f"legacy mutation called: {_name}")
            ),
        )

    class FailingService:
        def ingest_reservation(self, _command):
            raise ConnectionError("synthetic PG unavailable")

    result = booking_bridge._process_one_postgres_under_global_writer(
        record_path,
        mappings,
        service=FailingService(),
        authority_epoch=1,
    )
    stored = json.loads(record_path.read_text())
    assert result == "retry_required"
    assert stored["reason"] == "POSTGRES_AUTHORITY_FAIL_CLOSED_NO_LEGACY_FALLBACK"
    assert stored["legacy_fallback_executed"] is False
    assert stored["external_writes"] == 0


def test_postgres_item_invokes_only_normalized_product_service(monkeypatch, tmp_path: Path):
    record_path = tmp_path / "item.json"
    record_path.write_text(json.dumps({"status": "PENDING_NOTION_READ", "source_message_hash": "d" * 64}))
    mappings = {"listing-1": _mapping()}
    monkeypatch.setattr(
        booking_bridge,
        "fresh_source",
        lambda _hash: (_event("d" * 64), "HMSUCCESS1", "2026-09-09T00:00:00Z"),
    )
    monkeypatch.setattr(
        booking_bridge,
        "persist_authoritative_result",
        lambda path, value: path.write_text(json.dumps(value)),
    )

    class Service:
        seen = None

        def ingest_reservation(self, command):
            self.seen = command
            return SimpleNamespace(
                reservation_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                source_version=1,
                cleaning_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                schedule_revision_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            )

    service = Service()
    result = booking_bridge._process_one_postgres_under_global_writer(
        record_path,
        mappings,
        service=service,
        authority_epoch=4,
    )
    stored = json.loads(record_path.read_text())
    assert result == "completed"
    assert service.seen.external_reservation_id == "HMSUCCESS1"
    assert service.seen.metadata.source_event_id == "d" * 64
    assert stored["authority_route"] == "POSTGRES"
    assert stored["external_writes"] == 0
    assert stored["legacy_fallback_executed"] is False


def _selected_record(path: Path, source_hash: str, status: str = "PENDING_NOTION_READ") -> None:
    path.write_text(json.dumps({"status": status, "source_message_hash": source_hash}))


def _selection(name: str, source_hash: str, code: str = "HMFIRST001"):
    return booking_bridge.FirstPostgresCommandSelection(
        queue_item_name=name,
        source_message_hash=source_hash,
        reservation_code=code,
        listing_id="listing-1",
    )


def _fresh_selection(
    source_hash: str,
    code: str = "HMFRESH001",
    listing_id: str = "listing-1",
):
    return booking_bridge.FirstPostgresCommandSelection.from_fresh_gmail_event(
        source_message_hash=source_hash,
        reservation_code=code,
        listing_id=listing_id,
    )


def test_first_postgres_command_executes_only_selected_item(monkeypatch, tmp_path: Path):
    from contextlib import contextmanager

    queue = tmp_path / "queue"
    queue.mkdir()
    selected_hash = "e" * 64
    other_hash = "f" * 64
    _selected_record(queue / "selected.json", selected_hash)
    _selected_record(queue / "other.json", other_hash)
    monkeypatch.setattr(booking_bridge, "QUEUE_DIR", queue)
    monkeypatch.setattr(
        booking_bridge,
        "fresh_source",
        lambda source_hash: (_event(source_hash), "HMFIRST001" if source_hash == selected_hash else "HMOTHER001", "2026-09-12T00:00:00Z"),
    )
    monkeypatch.setattr(
        booking_bridge,
        "persist_authoritative_result",
        lambda path, value: path.write_text(json.dumps(value)),
    )

    @contextmanager
    def scope(*_args, **_kwargs):
        yield

    monkeypatch.setattr(booking_bridge, "mutation_scope", scope)

    class Service:
        calls = []

        def ingest_reservation(self, command, *, require_absent=False):
            assert require_absent is True
            self.calls.append(command.metadata.source_event_id)
            return SimpleNamespace(
                reservation_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                source_version=1,
                cleaning_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                schedule_revision_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            )

    service = Service()
    result = booking_bridge.execute_first_postgres_command(
        _selection("selected.json", selected_hash),
        {"listing-1": _mapping()},
        service=service,
        authority_epoch=11,
    )
    assert result == "completed"
    assert service.calls == [selected_hash]
    assert json.loads((queue / "other.json").read_text())["status"] == "PENDING_NOTION_READ"


def test_fresh_source_first_postgres_command_revalidates_and_executes_exactly_once_without_queue(
    monkeypatch,
    tmp_path: Path,
):
    from contextlib import contextmanager

    queue = tmp_path / "queue-must-remain-absent"
    source_hash = "8" * 64
    source_reads = []
    lease_calls = []
    service_calls = []
    monkeypatch.setattr(booking_bridge, "QUEUE_DIR", queue)

    def fresh_source(actual_hash):
        source_reads.append(actual_hash)
        return _event(source_hash), "HMFRESH001", "2026-09-14T00:00:00Z"

    @contextmanager
    def scope(writer, **kwargs):
        lease_calls.append((writer, kwargs))
        yield

    monkeypatch.setattr(booking_bridge, "fresh_source", fresh_source)
    monkeypatch.setattr(booking_bridge, "mutation_scope", scope)
    monkeypatch.setattr(
        booking_bridge,
        "persist_authoritative_result",
        lambda *_a, **_k: pytest.fail("fresh-source execution must not create or rewrite queue state"),
    )

    class Service:
        def ingest_reservation(self, command, *, require_absent=False):
            assert require_absent is True
            service_calls.append(command)
            return SimpleNamespace(
                reservation_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                source_version=1,
                cleaning_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                schedule_revision_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            )

    result = booking_bridge.execute_first_postgres_command(
        _fresh_selection(source_hash),
        {"listing-1": _mapping()},
        service=Service(),
        authority_epoch=12,
    )

    assert result == "completed"
    assert source_reads == [source_hash, source_hash]
    assert len(service_calls) == 1
    assert service_calls[0].metadata.source_event_id == source_hash
    assert service_calls[0].external_reservation_id == "HMFRESH001"
    assert lease_calls == [
        (
            "W01",
            {
                "unit_id": f"first-booking-pg:{source_hash}",
                "operation_class": "GMAIL_INGEST_FIRST_POSTGRES_BUSINESS_ITEM",
                "target": f"fresh-gmail-source:{source_hash}",
            },
        )
    ]
    assert not queue.exists()


@pytest.mark.parametrize(
    ("event_change", "returned_code", "mapping_present", "error_code"),
    [
        ({"event_type": "BOOKING_UPDATED"}, "HMFRESH001", True, "FIRST_PG_EVENT_TYPE_NOT_BOOKING_CONFIRMED"),
        ({"event_type": "BOOKING_CANCELLED"}, "HMFRESH001", True, "FIRST_PG_EVENT_TYPE_NOT_BOOKING_CONFIRMED"),
        ({"event_type": "UNKNOWN_EVENT"}, "HMFRESH001", True, "FIRST_PG_EVENT_TYPE_NOT_BOOKING_CONFIRMED"),
        ({"source_message_hash": "9" * 64}, "HMFRESH001", True, "FIRST_PG_FRESH_SOURCE_HASH_MISMATCH"),
        ({}, "HMCHANGED", True, "FIRST_PG_RESERVATION_CODE_MISMATCH"),
        ({"listing_id": "listing-2"}, "HMFRESH001", True, "FIRST_PG_LISTING_ID_MISMATCH"),
        ({}, "HMFRESH001", False, "FIRST_PG_LISTING_MAPPING_MISSING"),
    ],
)
def test_fresh_source_first_postgres_command_fails_closed_before_lease_for_changed_evidence(
    monkeypatch,
    tmp_path: Path,
    event_change: dict,
    returned_code: str,
    mapping_present: bool,
    error_code: str,
):
    from contextlib import contextmanager

    source_hash = "7" * 64
    event = {**_event(source_hash), **event_change}
    monkeypatch.setattr(booking_bridge, "QUEUE_DIR", tmp_path / "queue-must-remain-absent")
    monkeypatch.setattr(
        booking_bridge,
        "fresh_source",
        lambda _actual_hash: (event, returned_code, "2026-09-14T00:00:00Z"),
    )

    @contextmanager
    def forbidden_scope(*_args, **_kwargs):
        pytest.fail("W01 must not be acquired for changed or incomplete source evidence")
        yield

    monkeypatch.setattr(booking_bridge, "mutation_scope", forbidden_scope)
    mappings = {"listing-1": _mapping()} if mapping_present else {}

    with pytest.raises(booking_bridge.FirstPostgresCommandSelectionError, match=error_code):
        booking_bridge.execute_first_postgres_command(
            _fresh_selection(source_hash),
            mappings,
            service=object(),
            authority_epoch=12,
        )


def test_fresh_source_first_postgres_command_revalidates_inside_w01_before_business_call(
    monkeypatch,
    tmp_path: Path,
):
    from contextlib import contextmanager

    source_hash = "6" * 64
    source_reads = 0
    lease_entered = []
    monkeypatch.setattr(booking_bridge, "QUEUE_DIR", tmp_path / "queue-must-remain-absent")

    def fresh_source(_actual_hash):
        nonlocal source_reads
        source_reads += 1
        event = _event(source_hash)
        if source_reads == 2:
            event["event_type"] = "BOOKING_CANCELLED"
        return event, "HMFRESH001", "2026-09-14T00:00:00Z"

    @contextmanager
    def scope(*_args, **_kwargs):
        lease_entered.append(True)
        yield

    class ForbiddenService:
        def ingest_reservation(self, *_args, **_kwargs):
            pytest.fail("business service must not run after in-lease source revalidation fails")

    monkeypatch.setattr(booking_bridge, "fresh_source", fresh_source)
    monkeypatch.setattr(booking_bridge, "mutation_scope", scope)
    with pytest.raises(
        booking_bridge.FirstPostgresCommandSelectionError,
        match="FIRST_PG_EVENT_TYPE_NOT_BOOKING_CONFIRMED",
    ):
        booking_bridge.execute_first_postgres_command(
            _fresh_selection(source_hash),
            {"listing-1": _mapping()},
            service=ForbiddenService(),
            authority_epoch=12,
        )

    assert source_reads == 2
    assert lease_entered == [True]


def test_first_postgres_selection_rejects_unsealed_source_modes():
    with pytest.raises(
        booking_bridge.FirstPostgresCommandSelectionError,
        match="FIRST_PG_SOURCE_EVIDENCE_KIND_INVALID",
    ):
        booking_bridge.FirstPostgresCommandSelection(
            queue_item_name=None,
            source_message_hash="5" * 64,
            reservation_code="HMFRESH001",
            listing_id="listing-1",
            source_evidence_kind="ARBITRARY_PAYLOAD",
        )

    with pytest.raises(
        booking_bridge.FirstPostgresCommandSelectionError,
        match="FIRST_PG_FRESH_SOURCE_QUEUE_ITEM_FORBIDDEN",
    ):
        booking_bridge.FirstPostgresCommandSelection(
            queue_item_name="selected.json",
            source_message_hash="5" * 64,
            reservation_code="HMFRESH001",
            listing_id="listing-1",
            source_evidence_kind=booking_bridge.FIRST_PG_SOURCE_FRESH_GMAIL_EVENT,
        )


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda selection: booking_bridge.FirstPostgresCommandSelection(selection.queue_item_name, "0" * 64, selection.reservation_code, selection.listing_id), "FIRST_PG_SOURCE_MESSAGE_HASH_MISMATCH"),
        (lambda selection: booking_bridge.FirstPostgresCommandSelection(selection.queue_item_name, selection.source_message_hash, "HMSTALE", selection.listing_id), "FIRST_PG_RESERVATION_CODE_MISMATCH"),
        (lambda selection: booking_bridge.FirstPostgresCommandSelection(selection.queue_item_name, selection.source_message_hash, selection.reservation_code, "other-listing"), "FIRST_PG_LISTING_ID_MISMATCH"),
    ],
)
def test_first_postgres_command_rejects_stale_identity_without_business_call(monkeypatch, tmp_path: Path, mutator, code: str):
    queue = tmp_path / "queue"
    queue.mkdir()
    source_hash = "a" * 64
    _selected_record(queue / "selected.json", source_hash)
    monkeypatch.setattr(booking_bridge, "QUEUE_DIR", queue)
    monkeypatch.setattr(booking_bridge, "fresh_source", lambda _hash: (_event(source_hash), "HMFIRST001", "2026-09-12T00:00:00Z"))

    class ForbiddenService:
        def ingest_reservation(self, _command, **_kwargs):
            pytest.fail("business service must not run for stale selection")

    with pytest.raises(booking_bridge.FirstPostgresCommandSelectionError, match=code):
        booking_bridge.execute_first_postgres_command(
            mutator(_selection("selected.json", source_hash)),
            {"listing-1": _mapping()},
            service=ForbiddenService(),
            authority_epoch=11,
        )


def test_first_postgres_command_rejects_missing_and_consumed_items(monkeypatch, tmp_path: Path):
    queue = tmp_path / "queue"
    queue.mkdir()
    source_hash = "b" * 64
    monkeypatch.setattr(booking_bridge, "QUEUE_DIR", queue)
    monkeypatch.setattr(booking_bridge, "fresh_source", lambda _hash: (_event(source_hash), "HMFIRST001", "2026-09-12T00:00:00Z"))
    selection = _selection("selected.json", source_hash)
    with pytest.raises(booking_bridge.FirstPostgresCommandSelectionError, match="FIRST_PG_QUEUE_ITEM_MISSING"):
        booking_bridge.execute_first_postgres_command(selection, {"listing-1": _mapping()}, service=object(), authority_epoch=11)

    _selected_record(queue / "selected.json", source_hash, status="COMPLETE")
    with pytest.raises(booking_bridge.FirstPostgresCommandSelectionError, match="FIRST_PG_QUEUE_ITEM_ALREADY_CONSUMED"):
        booking_bridge.execute_first_postgres_command(selection, {"listing-1": _mapping()}, service=object(), authority_epoch=11)


def test_first_postgres_selection_rejects_arbitrary_path():
    with pytest.raises(booking_bridge.FirstPostgresCommandSelectionError, match="FIRST_PG_QUEUE_ITEM_NAME_INVALID"):
        booking_bridge.FirstPostgresCommandSelection(
            queue_item_name="../other.json",
            source_message_hash="c" * 64,
            reservation_code="HMFIRST001",
            listing_id="listing-1",
        )

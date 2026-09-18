import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from telegram_approval import cleaner_property_access as access
from telegram_approval.cleaner_config import CLEANER_TOKEN_PATH_CONFIG
from telegram_approval.cleaner_property_access_notifications import (
    CleanerPropertyAccessDeliveryState,
    CleanerPropertyAccessNotificationPump,
    CleanerPropertyAccessNotificationScanner,
    build_cleaner_property_access_notification_pump,
    enumerate_cleaner_property_access_notifications,
    render_cleaner_property_access_approval,
)
from telegram_approval.ops_config import OPS_ALLOWLIST_CONFIG, OPS_TOKEN_PATH_CONFIG
from telegram_approval.telegram_transport import TelegramBotContext


NOW = datetime(2026, 8, 28, 5, 0, tzinfo=timezone.utc)
MAPPINGS = {
    "property-a": "JJ",
    "property-b": "Browny House",
    "property-c": "Property C",
    "property-d": "Property D",
    "property-e": "Property E",
    "property-f": "Property F",
}


def _select(value):
    return {"type": "select", "select": {"name": value} if value is not None else None}


def _relation(*values):
    return {"type": "relation", "relation": [{"id": value} for value in values]}


def _rich(value):
    return {"type": "rich_text", "rich_text": [{"plain_text": value}]}


def _unique(prefix, number):
    return {"type": "unique_id", "unique_id": {"prefix": prefix, "number": number}}


def _date(value):
    return {"type": "date", "date": None if value is None else {"start": value}}


def row(
    *,
    page_id="access-a",
    access_number=1,
    party_page_id="party-cleaner",
    property_page_id="property-a",
    status="APPROVED",
    sync="SYNCED",
    environment="PRODUCTION",
    approved_at=NOW,
):
    return {
        "id": page_id,
        "properties": {
            "Access ID": _unique("CPA", access_number),
            "인력": _relation(party_page_id),
            "집": _relation(property_page_id),
            "Idempotency Key": _rich(
                access.property_access_idempotency_key(party_page_id, property_page_id)
            ),
            "신청 상태": _select(status),
            "Runtime Sync Status": _select(sync),
            "데이터 환경": _select(environment),
            "승인일": _date(approved_at.isoformat() if approved_at is not None else None),
            "Exact Address": _rich("SECRET_STREET_ADDRESS"),
            "Door Code": _rich("SECRET_DOOR_CODE"),
            "Guest PII": _rich("SECRET_GUEST_PII"),
            "Reservation ID": _rich("SECRET_RESERVATION_ID"),
        },
    }


def roster(*, chat_id=202, status="ACTIVE", party_page_id="party-cleaner"):
    return {
        "schema_version": 1,
        "cleaners": [{
            "identity_id": "cleaner-a",
            "label": "Cleaner A",
            "role": "CLEANER",
            "status": status,
            "telegram_user_id": chat_id,
            "telegram_chat_id": chat_id,
            "party_page_id": party_page_id,
            "properties": ["JJ"],
            "priority_by_property": {"JJ": 999},
        }],
    }


class FakeSource:
    def __init__(self):
        self.get_calls = []
        self.pages = {
            "party-cleaner": {
                "id": "party-cleaner",
                "properties": {"Private": _rich("SECRET_CLEANER_PRIVATE")},
            },
            "property-a": {
                "id": "property-a",
                "properties": {
                    "Exact Address": _rich("SECRET_STREET_ADDRESS"),
                    "Door Code": _rich("SECRET_DOOR_CODE"),
                },
            },
            "property-b": {"id": "property-b", "properties": {}},
            "property-c": {"id": "property-c", "properties": {}},
            "property-d": {"id": "property-d", "properties": {}},
            "property-e": {"id": "property-e", "properties": {}},
            "property-f": {"id": "property-f", "properties": {}},
        }

    def get_page(self, page_id):
        self.get_calls.append(page_id)
        return self.pages[page_id]


class FakeLedger:
    def __init__(self, rows=None, *, discovered=None, fresh_overrides=None):
        self.rows = list(rows or [row()])
        self.discovered = list(self.rows if discovered is None else discovered)
        self.fresh_overrides = dict(fresh_overrides or {})
        self.events = []
        self.mutation_attempts = []

    @staticmethod
    def _relation_id(value, name):
        return value["properties"][name]["relation"][0]["id"]

    @staticmethod
    def _key(value):
        return value["properties"]["Idempotency Key"]["rich_text"][0]["plain_text"]

    def query_approved_synced(self):
        self.events.append(("query_approved_synced",))
        return list(self.discovered)

    def get_access(self, page_id):
        self.events.append(("get_access", page_id))
        if page_id in self.fresh_overrides:
            return self.fresh_overrides[page_id]
        return next(value for value in self.rows if value["id"] == page_id)

    def query_by_identity(self, party_page_id, property_page_id):
        self.events.append(("query_by_identity", party_page_id, property_page_id))
        return [
            value for value in self.rows
            if self._relation_id(value, "인력") == party_page_id
            and self._relation_id(value, "집") == property_page_id
        ]

    def query_by_idempotency(self, key):
        self.events.append(("query_by_idempotency", key))
        return [value for value in self.rows if self._key(value) == key]

    def approve(self, *_args, **_kwargs):
        self.mutation_attempts.append("approve")
        raise AssertionError("notification pump must not approve")

    def reject(self, *_args, **_kwargs):
        self.mutation_attempts.append("reject")
        raise AssertionError("notification pump must not reject")

    def set_sync_status(self, *_args, **_kwargs):
        self.mutation_attempts.append("set_sync_status")
        raise AssertionError("notification pump must not sync")


class FakeTransport:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.next_message_id = 10

    def request(self, bot, method, **values):
        self.calls.append((bot.token, method, values))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        result = {"message_id": self.next_message_id}
        self.next_message_id += 1
        return result


def state_path(tmp_path):
    return tmp_path / "cleaner" / "property-access-approval-notifications.json"


def pump(
    tmp_path,
    *,
    ledger=None,
    transport=None,
    roster_loader=roster,
    mappings=MAPPINGS,
    max_candidates=25,
    max_deliveries=5,
):
    ledger = ledger or FakeLedger()
    return CleanerPropertyAccessNotificationPump(
        bot=TelegramBotContext("synthetic-cleaner-token"),
        transport=transport or FakeTransport(),
        ledger=ledger,
        source=FakeSource(),
        delivery_state=CleanerPropertyAccessDeliveryState(state_path(tmp_path)),
        roster_loader=roster_loader,
        mappings=mappings,
        now=lambda: NOW,
        max_candidates_per_cycle=max_candidates,
        max_deliveries_per_cycle=max_deliveries,
    )


def test_preexisting_approved_synced_record_is_recovered_without_reapproval(tmp_path):
    ledger = FakeLedger([row(access_number=1)])
    transport = FakeTransport()
    subject = pump(tmp_path, ledger=ledger, transport=transport)

    result = subject.run_once()

    assert result["sent"] == 1
    assert result["attempted"] == 1
    assert ledger.mutation_attempts == []
    assert [method for _token, method, _values in transport.calls] == ["sendMessage"]
    assert transport.calls[0][2]["chat_id"] == 202
    assert "JJ" in transport.calls[0][2]["text"]
    assert not any(secret in transport.calls[0][2]["text"] for secret in [
        "SECRET_STREET_ADDRESS", "SECRET_DOOR_CODE", "SECRET_GUEST_PII",
        "SECRET_RESERVATION_ID", "access-a", "CPA-1", "999",
    ])


def test_completed_delivery_is_deduped_on_subsequent_cycle(tmp_path):
    transport = FakeTransport()
    subject = pump(tmp_path, transport=transport)
    assert subject.run_once()["sent"] == 1
    second = subject.run_once()
    assert second["deduped"] == 1
    assert len(transport.calls) == 1


def test_ambiguous_send_requires_reconciliation_and_is_not_blind_retried(tmp_path):
    transport = FakeTransport([RuntimeError("synthetic Telegram failure"), {"message_id": 77}])
    subject = pump(tmp_path, transport=transport)
    first = subject.run_once()
    assert first["sent"] == 0
    assert first["failures"][0]["error_type"] == "RuntimeError"
    saved = json.loads(state_path(tmp_path).read_text())["deliveries"]["access-a:CPA-1"]
    assert saved["attempts"] == 1
    assert saved["delivery_status"] == "UNCERTAIN"
    assert saved["last_error_type"] == "RuntimeError"
    assert saved["complete"] is False

    second = subject.run_once()
    assert second["sent"] == 0
    assert second["failures"][0]["error_type"] == "DeliveryReconciliationRequired"
    saved = json.loads(state_path(tmp_path).read_text())["deliveries"]["access-a:CPA-1"]
    assert saved["attempts"] == 1
    assert saved["telegram_message_id"] is None
    assert saved["complete"] is False
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "status,sync",
    [
        ("REQUESTED", "NOT_REQUIRED"),
        ("REJECTED", "NOT_REQUIRED"),
        ("REVOKED", "NOT_REQUIRED"),
        ("APPROVED", "PENDING"),
        ("APPROVED", "FAILED"),
    ],
)
def test_non_actionable_business_states_do_not_send(tmp_path, status, sync):
    candidate = row(status=status, sync=sync)
    ledger = FakeLedger([candidate], discovered=[candidate])
    transport = FakeTransport()
    result = pump(tmp_path, ledger=ledger, transport=transport).run_once()
    assert result["sent"] == 0
    assert transport.calls == []
    assert ledger.mutation_attempts == []


def test_nonproduction_record_never_sends(tmp_path):
    candidate = row(environment="TEST")
    transport = FakeTransport()
    result = pump(
        tmp_path,
        ledger=FakeLedger([candidate], discovered=[candidate]),
        transport=transport,
    ).run_once()
    assert result["sent"] == 0
    assert transport.calls == []
    assert result["failures"][0]["error_type"] == "PropertyAccessConflict"


def test_missing_approval_date_fails_safely_without_send(tmp_path):
    candidate = row(approved_at=None)
    transport = FakeTransport()
    result = pump(
        tmp_path,
        ledger=FakeLedger([candidate]),
        transport=transport,
    ).run_once()
    assert result["sent"] == 0
    assert transport.calls == []
    assert result["failures"][0]["error_type"] == "PropertyAccessError"


def test_missing_cleaner_identity_fails_safely_without_send(tmp_path):
    transport = FakeTransport()
    result = pump(
        tmp_path,
        transport=transport,
        roster_loader=lambda: {"schema_version": 1, "cleaners": []},
    ).run_once()
    assert result["sent"] == 0
    assert transport.calls == []
    assert result["failures"][0]["error_type"] == "PropertyAccessError"


def test_stale_candidate_is_fresh_read_before_send_and_revocation_suppresses_delivery(tmp_path):
    discovered = row(status="APPROVED", sync="SYNCED")
    fresh = row(status="REVOKED", sync="NOT_REQUIRED")
    transport = FakeTransport()
    ledger = FakeLedger([fresh], discovered=[discovered], fresh_overrides={"access-a": fresh})

    result = pump(tmp_path, ledger=ledger, transport=transport).run_once()

    assert result["non_actionable"] == 1
    assert transport.calls == []
    assert ("get_access", "access-a") in ledger.events


def test_fresh_read_access_id_mismatch_fails_closed(tmp_path):
    discovered = row(access_number=1)
    fresh = row(access_number=2)
    transport = FakeTransport()
    ledger = FakeLedger([fresh], discovered=[discovered], fresh_overrides={"access-a": fresh})
    result = pump(tmp_path, ledger=ledger, transport=transport).run_once()
    assert result["sent"] == 0
    assert result["failures"][0]["error_type"] == "PropertyAccessConflict"
    assert transport.calls == []


def test_renderer_contains_only_safe_property_label():
    text = render_cleaner_property_access_approval("JJ")
    assert "숙소 승인이 완료되었습니다." in text
    assert "숙소: JJ" in text
    assert "Cleaner A" not in text
    assert "SECRET" not in text
    assert "CPA-1" not in text


def test_success_state_is_atomic_private_and_keyed_by_page_identity_plus_access_id(tmp_path):
    subject = pump(tmp_path)
    assert subject.run_once()["sent"] == 1
    path = state_path(tmp_path)
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    value = json.loads(path.read_text())
    assert set(value["deliveries"]) == {"access-a:CPA-1"}
    record = value["deliveries"]["access-a:CPA-1"]
    assert record["access_page_id"] == "access-a"
    assert record["access_id"] == "CPA-1"
    assert record["delivery_status"] == "DELIVERED"
    assert record["complete"] is True
    serialized = path.read_text()
    assert "JJ" not in serialized
    assert "Cleaner A" not in serialized
    assert "SECRET" not in serialized


def test_read_only_enumeration_is_deterministic_complete_and_does_not_send_or_mutate(tmp_path):
    late = row(
        page_id="access-z", access_number=3, property_page_id="property-c",
        approved_at=NOW + timedelta(minutes=2),
    )
    early_b = row(
        page_id="access-b", access_number=2, property_page_id="property-b",
        approved_at=NOW - timedelta(minutes=2),
    )
    early_a = row(
        page_id="access-a", access_number=1, property_page_id="property-a",
        approved_at=NOW - timedelta(minutes=2),
    )
    ledger = FakeLedger([late, early_b, early_a], discovered=[late, early_b, early_a])
    scanner = CleanerPropertyAccessNotificationScanner(
        ledger=ledger,
        source=FakeSource(),
        delivery_state=CleanerPropertyAccessDeliveryState(state_path(tmp_path)),
        roster_loader=roster,
        mappings=MAPPINGS,
    )
    result = scanner.enumerate_actionable()
    assert [item["access_id"] for item in result["actionable"]] == ["CPA-1", "CPA-2", "CPA-3"]
    assert result["discovered"] == 3
    assert result["completed"] == []
    assert result["failures"] == []
    assert ledger.mutation_attempts == []
    assert not state_path(tmp_path).exists()


def test_public_preflight_enumerator_requires_no_telegram_credentials(tmp_path):
    ledger = FakeLedger([row()])
    result = enumerate_cleaner_property_access_notifications(
        ledger=ledger,
        source=FakeSource(),
        delivery_state_path=state_path(tmp_path),
        roster_loader=roster,
        mappings=MAPPINGS,
    )
    assert [item["access_id"] for item in result["actionable"]] == ["CPA-1"]
    assert not state_path(tmp_path).exists()


def test_bounded_cycle_limits_delivery_work_but_keeps_candidate_set_inspectable(tmp_path):
    property_ids = [
        "property-a", "property-b", "property-c",
        "property-d", "property-e", "property-f",
    ]
    values = [
        row(
            page_id=f"access-{index}",
            access_number=index,
            property_page_id=property_ids[index - 1],
            approved_at=NOW + timedelta(minutes=index),
        )
        for index in range(1, 7)
    ]
    ledger = FakeLedger(values)
    transport = FakeTransport()
    subject = pump(
        tmp_path,
        ledger=ledger,
        transport=transport,
        max_candidates=4,
        max_deliveries=2,
    )
    result = subject.run_once()
    assert result["discovered"] == 6
    assert result["examined"] == 4
    assert result["attempted"] == 2
    assert result["sent"] == 2
    assert result["deferred"] == 4
    assert len(transport.calls) == 2


def test_factory_uses_cleaner_credential_and_never_reads_ops_configuration(tmp_path):
    cleaner_token = tmp_path / "cleaner.token"
    cleaner_token.write_text("synthetic-cleaner-token\n")
    transport = FakeTransport()
    subject = build_cleaner_property_access_notification_pump(
        environment={
            CLEANER_TOKEN_PATH_CONFIG: str(cleaner_token),
            OPS_TOKEN_PATH_CONFIG: str(tmp_path / "must-not-read-ops.token"),
            OPS_ALLOWLIST_CONFIG: "not-an-integer-and-must-not-be-read",
        },
        transport=transport,
        ledger=FakeLedger([row()]),
        source=FakeSource(),
        delivery_state_path=state_path(tmp_path),
        roster_loader=roster,
        mappings=MAPPINGS,
        now=lambda: NOW,
    )
    assert subject.run_once()["sent"] == 1
    assert {token for token, _method, _values in transport.calls} == {"synthetic-cleaner-token"}
    assert not (tmp_path / "must-not-read-ops.token").exists()


def test_notion_candidate_discovery_is_read_only_and_strictly_filtered(tmp_path):
    token = tmp_path / "notion.token"
    token.write_text("synthetic")
    requests = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def urlopen(request, **_kwargs):
        requests.append(request)
        return Response(json.dumps({
            "results": [], "has_more": False, "next_cursor": None
        }).encode())

    ledger = access.NotionPropertyAccessLedger(token_path=token, urlopen=urlopen)
    assert ledger.query_approved_synced() == []
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    body = json.loads(request.data)
    assert body["filter"] == {"and": [
        {"property": "신청 상태", "select": {"equals": "APPROVED"}},
        {"property": "Runtime Sync Status", "select": {"equals": "SYNCED"}},
        {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
        {"property": "승인일", "date": {"is_not_empty": True}},
    ]}


def test_delivery_state_validates_malformed_state_and_never_sends(tmp_path):
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version":1,"deliveries":{"bad":{"complete":true}}}')
    transport = FakeTransport()
    result = pump(tmp_path, transport=transport).run_once()
    assert result["sent"] == 0
    assert result["failures"]
    assert transport.calls == []

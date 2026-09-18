import json
import os
from datetime import datetime, timezone

import pytest

from telegram_approval import cleaner_property_access as access
from telegram_approval.cleaner_config import CLEANER_TOKEN_PATH_CONFIG
from telegram_approval.ops_config import OPS_ALLOWLIST_CONFIG, OPS_TOKEN_PATH_CONFIG
from telegram_approval.ops_property_access_notifications import (
    OpsPropertyAccessDeliveryState,
    OpsPropertyAccessNotificationPump,
    build_ops_property_access_notification_pump,
    render_ops_property_access_notification,
)
from telegram_approval.telegram_transport import TelegramBotContext


NOW = datetime(2026, 8, 27, 7, 30, tzinfo=timezone.utc)
MAPPINGS = {"property-a": "JJ House"}


def _select(value):
    return {"type": "select", "select": {"name": value} if value is not None else None}


def _relation(*values):
    return {"type": "relation", "relation": [{"id": value} for value in values]}


def _rich(value):
    return {"type": "rich_text", "rich_text": [{"plain_text": value}]}


def row(*, status="REQUESTED", party_ids=("party-cleaner",), property_ids=("property-a",), page_id="access-a"):
    party_for_key = party_ids[0] if party_ids else "party-cleaner"
    property_for_key = property_ids[0] if property_ids else "property-a"
    return {
        "id": page_id,
        "properties": {
            "인력": _relation(*party_ids),
            "집": _relation(*property_ids),
            "Idempotency Key": _rich(
                access.property_access_idempotency_key(party_for_key, property_for_key)
            ),
            "신청 상태": _select(status),
            "Runtime Sync Status": _select("NOT_REQUIRED"),
            "데이터 환경": _select("PRODUCTION"),
            "Exact Address": _rich("SECRET_STREET_ADDRESS"),
            "Door Code": _rich("SECRET_DOOR_CODE"),
            "Guest PII": _rich("SECRET_GUEST_PII"),
        },
    }


class FakeLedger:
    def __init__(self, current, *, discovered=None):
        self.current = current
        self.discovered = [current] if discovered is None else list(discovered)
        self.requested_queries = 0
        self.get_calls = []

    @staticmethod
    def _relation_id(value, name):
        return value["properties"][name]["relation"][0]["id"]

    @staticmethod
    def _key(value):
        return value["properties"]["Idempotency Key"]["rich_text"][0]["plain_text"]

    def query_requested(self):
        self.requested_queries += 1
        return list(self.discovered)

    def get_access(self, page_id):
        self.get_calls.append(page_id)
        return self.current

    def query_by_identity(self, party_page_id, property_page_id):
        try:
            matches = (
                self._relation_id(self.current, "인력") == party_page_id
                and self._relation_id(self.current, "집") == property_page_id
            )
        except (IndexError, KeyError):
            return []
        return [self.current] if matches else []

    def query_by_idempotency(self, key):
        return [self.current] if self._key(self.current) == key else []


class FakeSource:
    def __init__(self):
        self.calls = []
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
        }

    def get_page(self, page_id):
        self.calls.append(page_id)
        return self.pages[page_id]


def roster():
    return {
        "schema_version": 1,
        "cleaners": [{
            "identity_id": "cleaner-a",
            "label": "Cleaner A",
            "role": "CLEANER",
            "status": "ACTIVE",
            "telegram_user_id": 202,
            "telegram_chat_id": 202,
            "party_page_id": "party-cleaner",
            "properties": [],
            "priority_by_property": {},
        }],
    }


class FakeTransport:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.next_message_id = 1

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


def pump(tmp_path, *, current=None, discovered=None, transport=None, chat_ids=frozenset({101})):
    current = current or row()
    return OpsPropertyAccessNotificationPump(
        bot=TelegramBotContext("synthetic-ops-token"),
        chat_ids=chat_ids,
        transport=transport or FakeTransport(),
        ledger=FakeLedger(current, discovered=discovered),
        source=FakeSource(),
        delivery_state=OpsPropertyAccessDeliveryState(tmp_path / "ops" / "property-access.json"),
        roster_loader=roster,
        mappings=MAPPINGS,
        now=lambda: NOW,
    )


def test_preexisting_requested_row_is_recovered_and_sent_once(tmp_path):
    transport = FakeTransport()
    subject = pump(tmp_path, transport=transport)
    result = subject.run_once()
    assert result == {
        "discovered": 1,
        "sent": 1,
        "deduped": 0,
        "non_actionable": 0,
        "failures": [],
    }
    assert [method for _token, method, _values in transport.calls] == ["sendMessage"]
    assert transport.calls[0][0] == "synthetic-ops-token"
    state = json.loads((tmp_path / "ops" / "property-access.json").read_text())
    delivery = state["deliveries"]["access-a"]
    assert delivery["access_page_id"] == "access-a"
    assert delivery["telegram_message_id"] == 1
    assert delivery["sent_at"] == NOW.isoformat()
    assert delivery["complete"] is True


def test_repeated_poll_dedupes_successfully_recorded_access(tmp_path):
    transport = FakeTransport()
    subject = pump(tmp_path, transport=transport)
    assert subject.run_once()["sent"] == 1
    second = subject.run_once()
    assert second["deduped"] == 1
    assert len(transport.calls) == 1


def test_ambiguous_send_is_unresolved_and_not_blind_retried(tmp_path):
    transport = FakeTransport([RuntimeError("synthetic send failure"), {"message_id": 7}])
    subject = pump(tmp_path, transport=transport)
    first = subject.run_once()
    assert first["sent"] == 0
    assert first["failures"][0]["error_type"] == "RuntimeError"
    second = subject.run_once()
    assert second["sent"] == 0
    assert len(transport.calls) == 1
    state = json.loads((tmp_path / "ops" / "property-access.json").read_text())
    delivery = state["deliveries"]["access-a"]
    assert delivery["unresolved_chat_ids"] == [101]
    assert delivery["chat_deliveries"] == {}
    assert delivery["complete"] is False


def test_partial_multichat_ambiguous_delivery_does_not_retry_missing_chat(tmp_path):
    transport = FakeTransport([
        {"message_id": 11},
        RuntimeError("second chat unavailable"),
        {"message_id": 22},
    ])
    subject = pump(tmp_path, transport=transport, chat_ids=frozenset({101, 102}))
    assert subject.run_once()["sent"] == 0
    assert subject.run_once()["sent"] == 0
    assert [call[2]["chat_id"] for call in transport.calls] == [101, 102]
    state = json.loads((tmp_path / "ops" / "property-access.json").read_text())
    delivery = state["deliveries"]["access-a"]
    assert set(delivery["chat_deliveries"]) == {"101"}
    assert delivery["unresolved_chat_ids"] == [102]
    assert delivery["complete"] is False


@pytest.mark.parametrize("status", ["APPROVED", "REJECTED", "REVOKED"])
def test_non_actionable_statuses_never_send(tmp_path, status):
    transport = FakeTransport()
    subject = pump(tmp_path, current=row(status=status), transport=transport)
    result = subject.run_once()
    assert result["non_actionable"] == 1
    assert transport.calls == []


def test_stale_requested_discovery_fresh_reads_approved_and_does_not_send(tmp_path):
    transport = FakeTransport()
    subject = pump(
        tmp_path,
        current=row(status="APPROVED"),
        discovered=[row(status="REQUESTED")],
        transport=transport,
    )
    result = subject.run_once()
    assert result["non_actionable"] == 1
    assert transport.calls == []


def test_fresh_read_identity_mismatch_fails_closed_with_safe_diagnostic(tmp_path):
    transport = FakeTransport()
    subject = pump(
        tmp_path,
        current=row(page_id="access-b"),
        discovered=[row(page_id="access-a")],
        transport=transport,
    )
    result = subject.run_once()
    assert result["sent"] == 0
    assert result["failures"] == [
        {"access_page_id": "access-a", "error_type": "PropertyAccessConflict"}
    ]
    assert transport.calls == []


@pytest.mark.parametrize("party_ids", [(), ("party-cleaner", "party-other")])
def test_malformed_cleaner_relation_fails_closed(tmp_path, party_ids):
    transport = FakeTransport()
    subject = pump(tmp_path, current=row(party_ids=party_ids), transport=transport)
    result = subject.run_once()
    assert result["sent"] == 0
    assert result["failures"]
    assert transport.calls == []


@pytest.mark.parametrize("property_ids", [(), ("property-a", "property-b")])
def test_malformed_property_relation_fails_closed(tmp_path, property_ids):
    transport = FakeTransport()
    subject = pump(tmp_path, current=row(property_ids=property_ids), transport=transport)
    result = subject.run_once()
    assert result["sent"] == 0
    assert result["failures"]
    assert transport.calls == []


def test_notification_contains_only_safe_labels_and_reference_callbacks(tmp_path):
    text, markup = render_ops_property_access_notification("access-a", "Cleaner A", "JJ House")
    combined = text + markup
    assert "Cleaner A" in combined and "JJ House" in combined
    assert "SECRET_STREET_ADDRESS" not in combined
    assert "SECRET_DOOR_CODE" not in combined
    assert "SECRET_GUEST_PII" not in combined
    data = json.loads(markup)["inline_keyboard"][0]
    assert {button["callback_data"] for button in data} == {
        "cpaops1:access-a:approve",
        "cpaops1:access-a:reject",
    }


def test_delivery_state_is_atomic_private_and_contains_no_labels_or_secrets(tmp_path):
    transport = FakeTransport()
    subject = pump(tmp_path, transport=transport)
    assert subject.run_once()["sent"] == 1
    state_path = tmp_path / "ops" / "property-access.json"
    assert os.stat(state_path).st_mode & 0o777 == 0o600
    assert os.stat(state_path.parent).st_mode & 0o777 == 0o700
    assert not state_path.with_suffix(state_path.suffix + ".tmp").exists()
    serialized = state_path.read_text()
    assert "Cleaner A" not in serialized
    assert "JJ House" not in serialized
    assert "SECRET" not in serialized


def test_malformed_delivery_state_fails_closed_without_send(tmp_path):
    state_path = tmp_path / "ops" / "property-access.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"schema_version":1,"deliveries":{"access-a":{"bad":true}}}')
    transport = FakeTransport()
    subject = OpsPropertyAccessNotificationPump(
        bot=TelegramBotContext("synthetic-ops-token"),
        chat_ids=frozenset({101}),
        transport=transport,
        ledger=FakeLedger(row()),
        source=FakeSource(),
        delivery_state=OpsPropertyAccessDeliveryState(state_path),
        roster_loader=roster,
        mappings=MAPPINGS,
        now=lambda: NOW,
    )
    result = subject.run_once()
    assert result["failures"]
    assert transport.calls == []


def test_factory_uses_only_ops_owned_credentials_and_allowlist(tmp_path):
    ops_token = tmp_path / "ops.token"
    ops_token.write_text("synthetic-ops-token\n")
    transport = FakeTransport()
    subject = build_ops_property_access_notification_pump(
        environment={
            OPS_TOKEN_PATH_CONFIG: str(ops_token),
            OPS_ALLOWLIST_CONFIG: "101",
            CLEANER_TOKEN_PATH_CONFIG: str(tmp_path / "must-not-read-cleaner.token"),
        },
        transport=transport,
        ledger=FakeLedger(row()),
        source=FakeSource(),
        delivery_state_path=tmp_path / "ops" / "property-access.json",
        roster_loader=roster,
        mappings=MAPPINGS,
        now=lambda: NOW,
    )
    assert subject.run_once()["sent"] == 1
    assert {call[0] for call in transport.calls} == {"synthetic-ops-token"}
    assert not (tmp_path / "must-not-read-cleaner.token").exists()


def test_notion_requested_discovery_is_read_only_and_filtered(tmp_path):
    import io

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
    assert ledger.query_requested() == []
    assert len(requests) == 1
    request = requests[0]
    body = json.loads(request.data)
    assert request.method == "POST"
    assert request.full_url.endswith(f"/v1/data_sources/{access.PROPERTY_ACCESS_SOURCE}/query")
    assert body["filter"] == {"and": [
        {"property": "신청 상태", "select": {"equals": "REQUESTED"}},
        {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
    ]}

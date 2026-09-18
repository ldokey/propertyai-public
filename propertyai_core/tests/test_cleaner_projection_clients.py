from __future__ import annotations

import json
import plistlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from propertyai_core.runtime import cleaner_projection_clients as clients
from propertyai_core.runtime.cleaner_pg_outbox_service import (
    CleanerPostgresOutboxRuntimeError,
    build_worker,
)
from propertyai_core.stage_b.identity import notion_identity


RESERVATION_ID = UUID("11111111-1111-4111-8111-111111111111")
CLEANING_ID = UUID("22222222-2222-4222-8222-222222222222")


class _Response:
    def __init__(self, value): self.value = value
    def execute(self): return self.value


class _Events:
    def __init__(self, owner): self.owner = owner
    def list(self, **kwargs):
        self.owner.calls.append(("events.list", kwargs))
        return _Response({"items": list(self.owner.items.values())})
    def get(self, **kwargs):
        self.owner.calls.append(("events.get", kwargs))
        item = self.owner.items.get(kwargs["eventId"])
        if item is None:
            error = RuntimeError("missing")
            error.status_code = 404
            raise error
        return _Response(item)
    def insert(self, **kwargs):
        self.owner.calls.append(("events.insert", kwargs))
        body = dict(kwargs["body"])
        body.update({"id": "gcal-1", "iCalUID": "uid-gcal-1", "etag": "etag-1"})
        self.owner.items["gcal-1"] = body
        return _Response(body)
    def update(self, **kwargs):
        self.owner.calls.append(("events.update", kwargs))
        body = dict(kwargs["body"])
        body.update({"id": kwargs["eventId"], "iCalUID": "uid-gcal-1", "etag": "etag-2"})
        self.owner.items[kwargs["eventId"]] = body
        return _Response(body)
    def delete(self, **kwargs):
        self.owner.calls.append(("events.delete", kwargs))
        self.owner.items.pop(kwargs["eventId"], None)
        return _Response({})


class _CalendarList:
    def __init__(self, owner): self.owner = owner
    def list(self, **kwargs):
        self.owner.calls.append(("calendarList.list", kwargs))
        return _Response({"items": [
            {"id": "provider", "summary": "https://www.airbnb.com/calendar/ical/example"},
            {"id": "cleaning-calendar", "summary": clients.CALENDAR_DISPLAY_TARGET},
        ]})


class _CalendarService:
    def __init__(self):
        self.calls = []
        self.items = {}
        self._events = _Events(self)
        self._list = _CalendarList(self)
    def events(self): return self._events
    def calendarList(self): return self._list


class _TelegramTransport:
    def __init__(self): self.calls = []
    def request(self, bot, method, **values):
        self.calls.append((bot.token, method, values))
        return {"message_id": 77}


def _rich(value: str):
    return {"type": "rich_text", "rich_text": [{"plain_text": value}]}


def _title(value: str):
    return {"type": "title", "title": [{"plain_text": value}]}


def _select(value: str):
    return {"type": "select", "select": {"name": value}}


def _date(value: str):
    return {"type": "date", "date": {"start": value}}


def _response_page_from_properties(page_id: str, properties):
    rendered = {}
    for name, prop in properties.items():
        if "rich_text" in prop:
            rendered[name] = _rich(prop["rich_text"][0]["text"]["content"] if prop["rich_text"] else "")
        elif "title" in prop:
            rendered[name] = _title(prop["title"][0]["text"]["content"] if prop["title"] else "")
        elif "select" in prop:
            rendered[name] = _select(prop["select"]["name"])
        elif "date" in prop:
            rendered[name] = {"type": "date", "date": prop["date"]}
        elif "relation" in prop:
            rendered[name] = {"type": "relation", "relation": prop["relation"]}
        else:
            rendered[name] = prop
    return {"id": page_id, "last_edited_time": "2026-09-12T00:00:00Z", "properties": rendered}


def test_notion_reservation_create_uses_existing_projection_fields_and_pg_identity(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("test-token\n")
    client = clients.NotionCurrentStateClient(token_path=token)
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        assert method == "POST" and path == "/v1/pages"
        return _response_page_from_properties("reservation-page", body["properties"])

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(clients, "assert_current_production_writer", lambda: None)
    desired = {
        "reservation_id": str(RESERVATION_ID),
        "reservation_code": "HMABC123",
        "reservation_status": "CONFIRMED",
        "check_in_at": "2026-09-20T06:00:00+00:00",
        "check_out_at": "2026-09-22T02:00:00+00:00",
    }
    snapshot = client.create(
        "RESERVATION",
        {"reservation_id": str(RESERVATION_ID), "reservation_code": "HMABC123"},
        desired,
    )
    props = calls[0][2]["properties"]
    assert props["Idempotency Key"]["rich_text"][0]["text"]["content"] == f"PG:RESERVATION:{RESERVATION_ID}"
    assert props["Source Project Code"]["rich_text"][0]["text"]["content"] == "PROPERTYAI_POSTGRES"
    assert props["상태"]["select"]["name"] == "확정"
    assert snapshot.state == desired


def test_notion_cleaning_find_requires_unique_reservation_and_exact_schedule(monkeypatch):
    client = clients.NotionCurrentStateClient()
    monkeypatch.setattr(
        client,
        "_query_rich_text",
        lambda source, prop, value: (
            [] if prop == "Idempotency Key" else [{"id": "reservation-page"}]
        ),
    )
    exact = {
        "id": "cleaning-page",
        "properties": {
            "시작 예정": _date("2026-09-22T11:00:00+09:00"),
            "Idempotency Key": _rich("legacy-key"),
            "점검명": _title("CLEANING-HMABC123"),
            "상태": _select("담당자 배정"),
            "배정 수락 상태": _select("미제안"),
            "완료 목표": _date("2026-09-22T13:00:00+09:00"),
        },
    }
    monkeypatch.setattr(client, "_query_relation", lambda *args: [exact])
    rows = client.find(
        "CLEANING",
        {
            "cleaning_id": str(CLEANING_ID),
            "reservation_code": "HMABC123",
            "service_window_start_at": "2026-09-22T11:00:00+09:00",
        },
    )
    assert [row.external_id for row in rows] == ["cleaning-page"]


def test_calendar_client_targets_only_cal_cleaning_display_calendar(monkeypatch):
    service = _CalendarService()
    client = clients.GoogleCleaningCalendarClient(service=service)
    monkeypatch.setattr(clients, "assert_current_production_writer", lambda: None)
    desired = {
        "cleaning_id": str(CLEANING_ID),
        "cleaning_code": "CLEANING-HMABC123",
        "cleaning_status": "PLANNED",
        "service_window_start_at": "2026-09-22T11:00:00+09:00",
        "service_deadline_at": "2026-09-22T13:00:00+09:00",
    }
    snapshot = client.create({"cleaning_id": str(CLEANING_ID)}, desired)
    insert = next(call for call in service.calls if call[0] == "events.insert")
    assert insert[1]["calendarId"] == "cleaning-calendar"
    assert insert[1]["body"]["extendedProperties"]["private"]["propertyaiResourceCode"] == "CAL.CLEANING"
    assert all(call[1].get("calendarId") != "provider" for call in service.calls if call[0].startswith("events."))
    assert snapshot.state == {
        **desired,
        "service_window_start_at": "2026-09-22T02:00:00+00:00",
        "service_deadline_at": "2026-09-22T04:00:00+00:00",
    }


def test_telegram_party_recipient_is_exact_pg_party_to_provider_identity(monkeypatch, tmp_path):
    party_page = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    party_id = notion_identity("party", party_page)
    monkeypatch.setattr(
        clients,
        "load_roster",
        lambda: {"cleaners": [{
            "status": "ACTIVE",
            "party_page_id": party_page,
            "telegram_user_id": 101,
            "telegram_chat_id": 202,
        }]},
    )
    client = clients.TelegramSealedDeliveryClient(
        environment={}, request_dir=tmp_path / "requests", action_secret_path=tmp_path / "action-secret"
    )
    resolved = client.resolve_recipient({
        "recipient": {"kind": "PARTY", "identity": str(party_id)}
    })
    assert resolved == "telegram:PARTY:user:101:chat:202"


def test_telegram_assignment_offer_materializes_existing_pg_callback_contract_and_receipt(monkeypatch, tmp_path):
    party_page = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    party_id = notion_identity("party", party_page)
    cleaner_token = tmp_path / "cleaner-token"
    cleaner_token.write_text("cleaner-test-token\n")
    secret = tmp_path / "action-secret"
    secret.write_text("test-action-secret\n")
    monkeypatch.setattr(
        clients,
        "load_roster",
        lambda: {"cleaners": [{
            "status": "ACTIVE",
            "party_page_id": party_page,
            "telegram_user_id": 101,
            "telegram_chat_id": 202,
        }]},
    )
    monkeypatch.setattr(clients, "assert_current_production_writer", lambda: None)
    transport = _TelegramTransport()
    client = clients.TelegramSealedDeliveryClient(
        environment={"PROPERTYAI_CLEANER_TELEGRAM_TOKEN_PATH": str(cleaner_token)},
        transport=transport,
        request_dir=tmp_path / "requests",
        action_secret_path=secret,
    )
    effect = {
        "effect_kind": "CLEANING_ASSIGNMENT_OFFER_OPENED",
        "recipient": {"kind": "PARTY", "identity": str(party_id)},
        "body": {
            "campaign_id": "44444444-4444-4444-8444-444444444444",
            "offer_candidate_id": "55555555-5555-4555-8555-555555555555",
            "cleaning_id": str(CLEANING_ID),
            "acceptance_cutoff_at": "2026-09-12T15:00:00+09:00",
        },
    }
    message_id = client.send(
        effect,
        recipient_identity="telegram:PARTY:user:101:chat:202",
        delivery_identity="a" * 64,
    )
    assert message_id == "77"
    assert transport.calls[0][1] == "sendMessage"
    assert transport.calls[0][2]["chat_id"] == 202
    keyboard = json.loads(transport.calls[0][2]["reply_markup"])
    assert {button["text"] for button in keyboard["inline_keyboard"][0]} == {"✅ 수락", "❌ 거절"}
    records = list((tmp_path / "requests").glob("w07-*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["authority_route"] == "POSTGRES"
    assert record["pg_cleaner_party_id"] == str(party_id)
    assert record["pg_campaign_id"] == effect["body"]["campaign_id"]
    assert record["telegram_message_id"] == "77"


def test_w07_source_runtime_fails_closed_outside_post_cutover_topology(tmp_path):
    dsn = tmp_path / "dsn"
    dsn.write_text("postgresql://example.invalid/test\n")
    with pytest.raises(CleanerPostgresOutboxRuntimeError, match="POST_CUTOVER_PG_TOPOLOGY_REQUIRED"):
        build_worker(environment={
            "PROPERTYAI_CLEANER_AUTHORITY": "POSTGRES",
            "PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY": "PRE_CUTOVER",
            "PROPERTYAI_CLEANER_POSTGRES_APP_DSN_PATH": str(dsn),
            "PROPERTYAI_CLEANER_POSTGRES_APP_SESSION_USER": "propertyai_cleaner_app",
            "PROPERTYAI_CLEANER_POSTGRES_DATABASE": "propertyai",
        })


def test_w07_launchd_source_is_sealed_for_controller_materialization():
    path = Path(__file__).resolve().parents[1] / "runtime" / "com.propertyai.cleaner-pg-outbox.plist"
    doc = plistlib.loads(path.read_bytes())
    assert doc["Label"] == "com.propertyai.cleaner-pg-outbox"
    assert doc["ProgramArguments"][-1] == "propertyai_core.runtime.cleaner_pg_outbox_service"
    env = doc["EnvironmentVariables"]
    assert env["PROPERTYAI_CLEANER_AUTHORITY"] == "POSTGRES"
    assert env["PROPERTYAI_CLEANER_RUNTIME_TOPOLOGY"] == "REPLACE_AT_CUTOVER"
    assert env["PROPERTYAI_CLEANER_POSTGRES_WORKER_CREDENTIAL_REF"] == "cleaner-prod/worker.pgpass"
    assert env[clients.NOTION_TOKEN_PATH_ENV].startswith("/REPLACE_AT_CUTOVER/")
    assert env[clients.GOOGLE_TOKEN_PATH_ENV].startswith("/REPLACE_AT_CUTOVER/")
    assert env["PROPERTYAI_CLEANER_POST_PONR_RECONCILIATION_OPERATION_ID"] == (
        "CHAT.PROJ.HQ:TK43:DL98:POST_PONR_PROJECTION_CLOSURE:V1"
    )
    assert "PROPERTYAI_CLEANER_POSTGRES_APP_DSN_PATH" not in env
    assert env["PROPERTYAI_GLOBAL_WRITER_DCS_PATH"].startswith("/REPLACE_AT_CUTOVER/")
    assert env["PROPERTYAI_GLOBAL_WRITER_RUNTIME_IDENTITY_PATH"].startswith(
        "/REPLACE_AT_CUTOVER/"
    )
    assert env["PROPERTYAI_GLOBAL_WRITER_AUTHORIZED_IDENTITY_PATH"].startswith(
        "/REPLACE_AT_CUTOVER/"
    )


def test_projection_secret_paths_are_external_absolute_regular_owner_only_files(tmp_path):
    secret_dir = tmp_path / "notion"
    secret_dir.mkdir(mode=0o700)
    token = secret_dir / "token"
    token.write_text("test-token\n")
    token.chmod(0o600)
    client = clients.NotionCurrentStateClient(
        environment={clients.NOTION_TOKEN_PATH_ENV: str(token)}
    )
    assert client._token() == "test-token"


def test_projection_secret_path_rejects_symlink_and_missing_binding(tmp_path):
    with pytest.raises(clients.ProjectionClientConfigurationError, match="_REQUIRED"):
        clients.NotionCurrentStateClient(environment={})._token()
    secret_dir = tmp_path / "google"
    secret_dir.mkdir(mode=0o700)
    token = secret_dir / "token.json"
    token.write_text("{}")
    token.chmod(0o600)
    link = secret_dir / "link.json"
    link.symlink_to(token)
    with pytest.raises(clients.ProjectionClientConfigurationError, match="SYMLINK_FORBIDDEN"):
        clients._projection_secret_path(
            explicit=link,
            environment={},
            environment_key=clients.GOOGLE_TOKEN_PATH_ENV,
        )



def test_telegram_reassignment_request_seals_ops_decision_action_for_pg_handoff(monkeypatch, tmp_path):
    ops_token = tmp_path / "ops-token"
    ops_token.write_text("ops-test-token\n")
    secret = tmp_path / "action-secret"
    secret.write_text("test-action-secret\n")
    monkeypatch.setattr(clients, "assert_current_production_writer", lambda: None)
    transport = _TelegramTransport()
    client = clients.TelegramSealedDeliveryClient(
        environment={
            "PROPERTYAI_OPS_ADMIN_TELEGRAM_TOKEN_PATH": str(ops_token),
            "PROPERTYAI_OPS_BRIEFING_ALLOWED_CHAT_IDS": "9090",
        },
        transport=transport,
        request_dir=tmp_path / "requests",
        action_secret_path=secret,
    )
    effect = {
        "effect_kind": "CLEANER_REASSIGNMENT_REQUESTED",
        "recipient": {"kind": "OPS", "identity": "PROPERTYAI_OPS"},
        "body": {
            "reassignment_request_id": "77777777-7777-4777-8777-777777777777",
            "cleaning_id": str(CLEANING_ID),
            "cleaner_party_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        },
    }
    resolved = client.resolve_recipient(effect)
    assert resolved == "telegram:OPS:chat:9090"
    message_id = client.send(
        effect,
        recipient_identity=resolved,
        delivery_identity="b" * 64,
    )
    assert message_id == "77"
    assert transport.calls[0][1] == "sendMessage"
    assert transport.calls[0][2]["chat_id"] == 9090
    keyboard = json.loads(transport.calls[0][2]["reply_markup"])
    labels = {button["text"] for button in keyboard["inline_keyboard"][0]}
    assert labels == {"원래 Cleaner 재배정", "대체 계속"}
    records = list((tmp_path / "requests").glob("w07-r-*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["action_type"] == "CLEANER_REASSIGNMENT_DECISION"
    assert record["authority_route"] == "POSTGRES"
    assert record["pg_reassignment_request_id"] == effect["body"]["reassignment_request_id"]
    assert record["decision_committed"] is False
    assert record["notification_status"] == "DELIVERED"
    assert record["notification_deliveries"]["9090"]["telegram_message_id"] == 77

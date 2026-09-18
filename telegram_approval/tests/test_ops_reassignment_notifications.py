import json
from contextlib import nullcontext
from datetime import datetime, timezone

from telegram_approval.cleaner_reassignment import ACTION_TYPE, NOTIFICATION_RETRY_REQUIRED
from telegram_approval.ops_reassignment_notifications import (
    ATTEMPTING,
    DELIVERED,
    UNCERTAIN,
    build_ops_reassignment_notification_pump,
)
from telegram_approval.ops_config import OPS_ALLOWLIST_CONFIG, OPS_TOKEN_PATH_CONFIG


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request(self, bot, method, **values):
        self.calls.append((bot.token, method, values))
        return {"message_id": 700 + len(self.calls)}


def decision(action_id="decision-1", notification_status=None):
    value = {
        "schema_version": 1,
        "action_id": action_id,
        "action_type": ACTION_TYPE,
        "status": "PENDING",
        "consumed": False,
        "decision_committed": False,
        "request_action_id": "request-1",
        "cleaning_page_id": "cleaning-1",
        "cleaner_party_page_id": "party-1",
        "history_page_id": "history-1",
        "accepted_assignment_action_id": "assignment-1",
        "assignment_version": "assignment-1",
        "acceptance_idempotency_key": "idem-1",
        "end_key": "end-1",
        "original_ended_at": "2026-09-03T15:00:00+00:00",
        "cleaner_telegram_user_id": 11,
        "cleaner_telegram_chat_id": 11,
        "created_at": "2026-09-03T15:01:00+00:00",
        "expires_at": "2026-09-04T15:01:00+00:00",
        "business_mutations": 0,
    }
    if notification_status is not None:
        value["notification_status"] = notification_status
    return value


def subject(tmp_path, value, *, transport=None):
    token = tmp_path / "ops.token"
    token.write_text("synthetic-ops-token")
    secret = tmp_path / "secret"
    secret.write_text("secret")
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    (request_dir / f"{value['action_id']}.json").write_text(json.dumps(value))
    transport = transport or FakeTransport()
    assertions = []
    pump = build_ops_reassignment_notification_pump(
        environment={OPS_TOKEN_PATH_CONFIG: str(token), OPS_ALLOWLIST_CONFIG: "101"},
        transport=transport,
        request_dir=request_dir,
        secret_path=secret,
        mutation_scope_factory=lambda action_id: nullcontext(assertions.append(action_id)),
        pre_external_assert=lambda: assertions.append("assert"),
        now=lambda: datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc),
    )
    return pump, request_dir, transport, assertions


def test_w02_pump_delivers_pending_decision_with_ops_identity(tmp_path):
    pump, request_dir, transport, assertions = subject(tmp_path, decision())
    result = pump.run_once()
    saved = json.loads((request_dir / "decision-1.json").read_text())
    assert result["sent"] == 1
    assert saved["notification_status"] == DELIVERED
    assert saved["notification_deliveries"]["101"]["status"] == DELIVERED
    assert saved["notification_deliveries"]["101"]["telegram_message_id"] == 701
    assert transport.calls[0][0] == "synthetic-ops-token"
    assert transport.calls[0][1] == "sendMessage"
    assert "원래 Cleaner 재배정" in transport.calls[0][2]["reply_markup"]
    assert assertions.count("assert") == 2
    assert "decision-1" in assertions


def test_retry_required_from_w03_legacy_attempt_is_recoverable_by_w02(tmp_path):
    pump, request_dir, transport, _ = subject(
        tmp_path, decision(notification_status=NOTIFICATION_RETRY_REQUIRED)
    )
    result = pump.run_once()
    saved = json.loads((request_dir / "decision-1.json").read_text())
    assert result["sent"] == 1
    assert saved["notification_status"] == DELIVERED
    assert len(transport.calls) == 1


def test_previous_attempting_state_fails_closed_without_duplicate_send(tmp_path):
    value = decision(notification_status=ATTEMPTING)
    pump, request_dir, transport, _ = subject(tmp_path, value)
    result = pump.run_once()
    saved = json.loads((request_dir / "decision-1.json").read_text())
    assert result["uncertain"] == 1
    assert saved["notification_status"] == UNCERTAIN
    assert transport.calls == []


def test_delivered_decision_is_idempotently_skipped(tmp_path):
    pump, request_dir, transport, _ = subject(tmp_path, decision(notification_status=DELIVERED))
    result = pump.run_once()
    assert result["sent"] == 0
    assert json.loads((request_dir / "decision-1.json").read_text())["notification_status"] == DELIVERED
    assert transport.calls == []

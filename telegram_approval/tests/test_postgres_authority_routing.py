from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from uuid import UUID

import pytest

from propertyai_core.config.cleaner_authority import CleanerAuthorityConfig
from telegram_approval import cleaner_bot_runtime
from telegram_approval.cleaner_pg_ingress import (
    CleanerPostgresReassignmentDecisionPump,
    CleanerPostgresTelegramError,
    handle_postgres_cleaner_callback,
    handle_postgres_ops_reassignment_callback,
)


CLEANER_PARTY = "11111111-1111-4111-8111-111111111111"
CAMPAIGN = "22222222-2222-4222-8222-222222222222"
CANDIDATE = "33333333-3333-4333-8333-333333333333"
ASSIGNMENT = "44444444-4444-4444-8444-444444444444"


def _authority():
    return CleanerAuthorityConfig(
        authority="POSTGRES", pg_ingress_enabled=True, authority_epoch=11
    )


def _secret(tmp_path: Path) -> Path:
    path = tmp_path / "secret"
    path.write_text("c2-test-secret")
    path.chmod(0o600)
    return path


def _callback(action_id: str, operation: str, secret: Path, *, prefix: str = "a") -> str:
    key = secret.read_text().strip().encode()
    sig = hmac.new(key, f"{action_id}:{operation}".encode(), hashlib.sha256).hexdigest()[:16]
    return f"{prefix}:{action_id}:{operation}:{sig}"


def _update(data: str):
    return {
        "update_id": 1,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": 1001},
            "message": {"chat": {"id": 2001}},
            "data": data,
        },
    }


def test_postgres_assignment_callback_calls_product_service_and_not_transport(tmp_path: Path):
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    secret = _secret(tmp_path)
    action_id = "assign1"
    record = {
        "action_id": action_id,
        "action_type": "CLEANING_ASSIGNMENT",
        "candidate_user_id": 1001,
        "candidate_chat_id": 2001,
        "pg_cleaner_party_id": CLEANER_PARTY,
        "pg_campaign_id": CAMPAIGN,
        "pg_offer_candidate_id": CANDIDATE,
        "consumed": False,
    }
    (request_dir / f"{action_id}.json").write_text(json.dumps(record))

    class Service:
        seen = None

        def accept_assignment(self, command):
            self.seen = command
            return UUID(ASSIGNMENT)

    service = Service()
    result = handle_postgres_cleaner_callback(
        _update(_callback(action_id, "approve", secret)),
        request_dir=request_dir,
        secret_path=secret,
        authority=_authority(),
        service=service,
    )
    stored = json.loads((request_dir / f"{action_id}.json").read_text())
    assert result == f"postgres_assignment_accepted:{ASSIGNMENT}"
    assert str(service.seen.campaign_id) == CAMPAIGN
    assert str(service.seen.offer_candidate_id) == CANDIDATE
    assert str(service.seen.metadata.actor_party_id) == CLEANER_PARTY
    assert service.seen.metadata.authority_epoch == 11
    assert stored["authority_route"] == "POSTGRES"
    assert stored["consumed"] is True


def test_postgres_assignment_reject_uses_pg_decline_surface(tmp_path: Path):
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    secret = _secret(tmp_path)
    action_id = "assign2"
    record = {
        "action_id": action_id,
        "action_type": "CLEANING_ASSIGNMENT",
        "candidate_user_id": 1001,
        "candidate_chat_id": 2001,
        "pg_cleaner_party_id": CLEANER_PARTY,
        "pg_campaign_id": CAMPAIGN,
        "pg_offer_candidate_id": CANDIDATE,
        "consumed": False,
    }
    (request_dir / f"{action_id}.json").write_text(json.dumps(record))

    class Service:
        called = False

        def decline_assignment(self, command):
            self.called = True
            assert str(command.cleaner_party_id) == CLEANER_PARTY
            return UUID("55555555-5555-4555-8555-555555555555")

    service = Service()
    result = handle_postgres_cleaner_callback(
        _update(_callback(action_id, "reject", secret)),
        request_dir=request_dir,
        secret_path=secret,
        authority=_authority(),
        service=service,
    )
    assert service.called is True
    assert result.startswith("postgres_assignment_declined:")


def test_postgres_unavailable_confirmation_calls_pg_service(tmp_path: Path):
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    secret = _secret(tmp_path)
    action_id = "unavailable1"
    record = {
        "action_id": action_id,
        "action_type": "CLEANER_UNAVAILABLE",
        "telegram_user_id": 1001,
        "telegram_chat_id": 2001,
        "pg_cleaner_party_id": CLEANER_PARTY,
        "pg_assignment_id": ASSIGNMENT,
        "pg_availability_classification": "EARLY_UNAVAILABLE",
        "replacement_urgency": "NORMAL",
        "consumed": False,
    }
    (request_dir / f"{action_id}.json").write_text(json.dumps(record))

    class Service:
        seen = None

        def mark_unavailable(self, command):
            self.seen = command
            return UUID("66666666-6666-4666-8666-666666666666")

    service = Service()
    result = handle_postgres_cleaner_callback(
        _update(_callback(action_id, "confirm", secret, prefix="u")),
        request_dir=request_dir,
        secret_path=secret,
        authority=_authority(),
        service=service,
    )
    assert service.seen.availability_classification == "EARLY_UNAVAILABLE"
    assert str(service.seen.assignment_id) == ASSIGNMENT
    assert result.startswith("postgres_unavailable_confirmed:")


def test_postgres_callback_missing_pg_identity_fails_closed(tmp_path: Path):
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    secret = _secret(tmp_path)
    action_id = "broken"
    record = {
        "action_id": action_id,
        "action_type": "CLEANING_ASSIGNMENT",
        "candidate_user_id": 1001,
        "candidate_chat_id": 2001,
        "pg_cleaner_party_id": CLEANER_PARTY,
        "consumed": False,
    }
    (request_dir / f"{action_id}.json").write_text(json.dumps(record))

    class Service:
        def accept_assignment(self, _command):
            raise AssertionError("service must not be called with incomplete PG authority")

    with pytest.raises(CleanerPostgresTelegramError, match="pg_campaign_id_MISSING"):
        handle_postgres_cleaner_callback(
            _update(_callback(action_id, "approve", secret)),
            request_dir=request_dir,
            secret_path=secret,
            authority=_authority(),
            service=Service(),
        )


def test_build_poller_postgres_requires_pg_service_before_runtime_start():
    with pytest.raises(RuntimeError, match="requires the Product PG application service"):
        cleaner_bot_runtime.build_poller(
            environment={},
            authority_config=_authority(),
            postgres_service=None,
        )


REASSIGNMENT_REQUEST = "77777777-7777-4777-8777-777777777777"


def test_ops_pg_reassignment_callback_records_decision_only_then_w03_executes_pg_command(tmp_path: Path, monkeypatch):
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    secret = _secret(tmp_path)
    action_id = "w07-r-decision"
    record = {
        "schema_version": 1,
        "action_id": action_id,
        "action_type": "CLEANER_REASSIGNMENT_DECISION",
        "status": "PENDING",
        "consumed": False,
        "decision_committed": False,
        "authority_route": "POSTGRES",
        "pg_reassignment_request_id": REASSIGNMENT_REQUEST,
    }
    path = request_dir / f"{action_id}.json"
    path.write_text(json.dumps(record))
    monkeypatch.setattr(
        "telegram_approval.cleaner_pg_ingress.assert_current_production_writer",
        lambda: None,
    )

    # W02 callback only commits the authenticated human decision to local state.
    result = handle_postgres_ops_reassignment_callback(
        _update(_callback(action_id, "reassign_original", secret, prefix="r")),
        request_dir=request_dir,
        secret_path=secret,
    )
    stored = json.loads(path.read_text())
    assert result == "postgres_reassignment_decision_recorded"
    assert stored["decision_committed"] is True
    assert stored["pg_decision_operation"] == "reassign_original"
    assert stored["pg_command_status"] == "PENDING"
    assert stored["consumed"] is False

    class Service:
        calls = []
        def reassign_original(self, command):
            self.calls.append(("reassign", command))
            return UUID(ASSIGNMENT)
        def continue_replacement(self, command):
            self.calls.append(("continue", command))

    service = Service()
    pump = CleanerPostgresReassignmentDecisionPump(
        request_dir=request_dir,
        authority=_authority(),
        service=service,
    )
    outcome = pump.run_once()
    stored = json.loads(path.read_text())
    assert outcome == {"executed": 1, "skipped": 0}
    assert service.calls[0][0] == "reassign"
    command = service.calls[0][1]
    assert str(command.reassignment_request_id) == REASSIGNMENT_REQUEST
    assert command.metadata.authority_epoch == 11
    assert command.metadata.source_channel_code == "TELEGRAM"
    assert command.metadata.source_stream_key == "telegram-ops-chat:2001"
    assert command.metadata.actor_party_id is None
    assert stored["pg_command_status"] == "EXECUTED"
    assert stored["consumed"] is True


def test_ops_pg_reassignment_decision_is_idempotent_and_conflicting_second_decision_fails_closed(tmp_path: Path, monkeypatch):
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    secret = _secret(tmp_path)
    action_id = "w07-r-idempotent"
    path = request_dir / f"{action_id}.json"
    path.write_text(json.dumps({
        "action_id": action_id,
        "action_type": "CLEANER_REASSIGNMENT_DECISION",
        "authority_route": "POSTGRES",
        "decision_committed": False,
        "consumed": False,
        "pg_reassignment_request_id": REASSIGNMENT_REQUEST,
    }))
    monkeypatch.setattr(
        "telegram_approval.cleaner_pg_ingress.assert_current_production_writer",
        lambda: None,
    )
    update = _update(_callback(action_id, "continue_replacement", secret, prefix="r"))
    assert handle_postgres_ops_reassignment_callback(
        update, request_dir=request_dir, secret_path=secret
    ) == "postgres_reassignment_decision_recorded"
    assert handle_postgres_ops_reassignment_callback(
        update, request_dir=request_dir, secret_path=secret
    ) == "postgres_reassignment_decision_already_recorded"
    with pytest.raises(CleanerPostgresTelegramError, match="DECISION_CONFLICT"):
        handle_postgres_ops_reassignment_callback(
            _update(_callback(action_id, "reassign_original", secret, prefix="r")),
            request_dir=request_dir,
            secret_path=secret,
        )


def test_w03_pg_reassignment_pump_executes_continue_replacement_once(tmp_path: Path, monkeypatch):
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    path = request_dir / "decision.json"
    path.write_text(json.dumps({
        "action_id": "decision",
        "action_type": "CLEANER_REASSIGNMENT_DECISION",
        "authority_route": "POSTGRES",
        "decision_committed": True,
        "pg_decision_operation": "continue_replacement",
        "pg_command_status": "PENDING",
        "decision_actor_chat_id": 2001,
        "decision_at": "2026-09-12T05:00:00+00:00",
        "pg_reassignment_request_id": REASSIGNMENT_REQUEST,
    }))
    monkeypatch.setattr(
        "telegram_approval.cleaner_pg_ingress.assert_current_production_writer",
        lambda: None,
    )
    class Service:
        calls = 0
        def continue_replacement(self, command):
            self.calls += 1
            assert str(command.reassignment_request_id) == REASSIGNMENT_REQUEST
        def reassign_original(self, command):
            raise AssertionError("wrong decision surface")
    service = Service()
    pump = CleanerPostgresReassignmentDecisionPump(
        request_dir=request_dir, authority=_authority(), service=service
    )
    assert pump.run_once()["executed"] == 1
    assert pump.run_once()["executed"] == 0
    assert service.calls == 1

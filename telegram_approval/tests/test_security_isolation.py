import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from telegram_approval import cleaner_bot_service, ops_bot_service
from telegram_approval import cleaning_assignment
from telegram_approval.cleaner_bot_service import CleanerTelegramRouter
from telegram_approval.cleaner_config import (
    CLEANER_TOKEN_PATH_CONFIG,
    CleanerCredentialProvider,
    CleanerRuntimePaths,
)
from telegram_approval.ops_bot_service import OpsTelegramRouter
from telegram_approval.ops_config import (
    OPS_ALLOWLIST_CONFIG,
    OPS_TOKEN_PATH_CONFIG,
    OpsAllowlistProvider,
    OpsCredentialProvider,
    OpsRuntimePaths,
)
from telegram_approval.telegram_transport import (
    TelegramBotContext,
    TelegramHttpTransport,
)


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request(self, bot, method, **values):
        self.calls.append((bot, method, values))
        return {"message_id": len(self.calls)}


class PollingTransport(FakeTransport):
    def __init__(self, updates):
        super().__init__()
        self.updates = updates

    def request(self, bot, method, **values):
        self.calls.append((bot, method, values))
        return self.updates if method == "getUpdates" else {"message_id": len(self.calls)}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def write_token(path: Path, value: str) -> None:
    path.write_text(value)


def test_distinct_credential_providers_use_only_frozen_config_paths(tmp_path):
    ops_path = tmp_path / "ops-token"
    cleaner_path = tmp_path / "cleaner-token"
    write_token(ops_path, "synthetic-ops-token")
    write_token(cleaner_path, "synthetic-cleaner-token")
    environment = {
        OPS_TOKEN_PATH_CONFIG: str(ops_path),
        CLEANER_TOKEN_PATH_CONFIG: str(cleaner_path),
    }

    ops = OpsCredentialProvider(environment=environment)
    cleaner = CleanerCredentialProvider(environment=environment)

    assert type(ops) is not type(cleaner)
    assert ops.credential_path() != cleaner.credential_path()
    assert ops.bot_context().token == "synthetic-ops-token"
    assert cleaner.bot_context().token == "synthetic-cleaner-token"


def test_ops_router_has_no_cleaner_registry_credential_or_session_dependency(tmp_path):
    token_path = tmp_path / "ops-token"
    write_token(token_path, "synthetic-ops-token")
    environment = {
        OPS_TOKEN_PATH_CONFIG: str(token_path),
        OPS_ALLOWLIST_CONFIG: "101",
    }
    transport = FakeTransport()
    router = OpsTelegramRouter(
        credential_provider=OpsCredentialProvider(environment=environment),
        allowlist_provider=OpsAllowlistProvider(environment=environment),
        transport=transport,
    )

    assert not hasattr(ops_bot_service, "CleanerCredentialProvider")
    assert not hasattr(ops_bot_service, "assignment_targets")
    assert router.send_message(101, "ops") == {"message_id": 1}
    with pytest.raises(PermissionError):
        router.send_message(202, "not allowed")
    assert router.route({"message": {"chat": {"id": 101}, "text": "/ops_briefing"}}) == (
        "ops_briefing_out_of_scope"
    )


def test_cleaner_router_has_no_ops_allowlist_credential_or_state_dependency(tmp_path):
    token_path = tmp_path / "cleaner-token"
    write_token(token_path, "synthetic-cleaner-token")
    transport = FakeTransport()
    router = CleanerTelegramRouter(
        credential_provider=CleanerCredentialProvider(token_path=token_path),
        transport=transport,
        handlers=(lambda update: "cleaner-action" if update.get("message") else None,),
    )

    assert not hasattr(cleaner_bot_service, "OpsCredentialProvider")
    assert not hasattr(cleaner_bot_service, "OpsAllowlistProvider")
    assert router.send_message(202, "cleaner") == {"message_id": 1}
    assert router.route({"message": {"chat": {"id": 202}}}) == "cleaner-action"


def test_transport_uses_only_explicit_supplied_bot_context():
    requests = []

    def urlopen(request, **_kwargs):
        requests.append(request)
        return Response(json.dumps({"ok": True, "result": {"message_id": 1}}).encode())

    transport = TelegramHttpTransport(urlopen=urlopen)
    transport.request(TelegramBotContext("synthetic-context-token"), "sendMessage", chat_id=1)

    assert len(requests) == 1
    assert requests[0].full_url.endswith("/botsynthetic-context-token/sendMessage")
    assert not hasattr(transport, "role")
    assert not hasattr(transport, "credential_provider")


def test_ops_and_cleaner_outbound_use_their_supplied_contexts(tmp_path):
    ops_path = tmp_path / "ops-token"
    cleaner_path = tmp_path / "cleaner-token"
    write_token(ops_path, "synthetic-ops-token")
    write_token(cleaner_path, "synthetic-cleaner-token")
    transport = FakeTransport()
    ops_environment = {
        OPS_TOKEN_PATH_CONFIG: str(ops_path),
        OPS_ALLOWLIST_CONFIG: "101",
    }
    ops = OpsTelegramRouter(
        credential_provider=OpsCredentialProvider(environment=ops_environment),
        allowlist_provider=OpsAllowlistProvider(environment=ops_environment),
        transport=transport,
    )
    cleaner = CleanerTelegramRouter(
        credential_provider=CleanerCredentialProvider(token_path=cleaner_path),
        transport=transport,
    )

    ops.send_message(101, "ops")
    cleaner.send_message(202, "cleaner")

    assert [call[0].token for call in transport.calls] == [
        "synthetic-ops-token",
        "synthetic-cleaner-token",
    ]


def test_mixed_outbound_consumer_selects_context_by_recipient_role(tmp_path, monkeypatch):
    ops_path = tmp_path / "ops-token"
    cleaner_path = tmp_path / "cleaner-token"
    operator_path = tmp_path / "operator.json"
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    write_token(ops_path, "synthetic-ops-token")
    write_token(cleaner_path, "synthetic-cleaner-token")
    operator_path.write_text(json.dumps({"telegram_user_id": 101, "telegram_chat_id": 101}))
    monkeypatch.setenv(OPS_TOKEN_PATH_CONFIG, str(ops_path))
    monkeypatch.setenv(CLEANER_TOKEN_PATH_CONFIG, str(cleaner_path))
    monkeypatch.setenv(OPS_ALLOWLIST_CONFIG, "101")
    calls = []

    def fake_api(token, method, **values):
        calls.append((token, method, values))
        return {"message_id": len(calls)}

    with patch.object(cleaning_assignment, "TOKEN_PATH", None), patch.object(
        cleaning_assignment, "OPERATOR_PATH", operator_path
    ), patch.object(cleaning_assignment, "REQUEST_DIR", request_dir), patch.object(
        cleaning_assignment, "api", side_effect=fake_api
    ), patch.object(cleaning_assignment, "secret", return_value=b"synthetic-secret"), patch.object(
        cleaning_assignment, "signature", return_value="synthetic-signature"
    ):
        cleaning_assignment.send_assignment(
            cleaning_page_id="cleaning",
            cleaning_name="cleaning",
            property_nickname="property",
            address="synthetic address",
            start_at="2026-08-25T11:00:00+09:00",
            end_at="2026-08-25T15:00:00+09:00",
            cleaning_fee_krw=1,
            expected_last_edited_time="synthetic-version",
            candidates=[{"telegram_user_id": 202, "telegram_chat_id": 202}],
            observers=[{"telegram_chat_id": 101}],
            test_mode=True,
        )

    assert [call[0] for call in calls] == [
        "synthetic-cleaner-token",
        "synthetic-ops-token",
    ]


def test_poller_callback_and_session_paths_are_identity_specific():
    ops = OpsRuntimePaths()
    cleaner = CleanerRuntimePaths()

    assert ops.state_path != cleaner.state_path
    assert ops.callback_session_dir != cleaner.callback_session_dir
    assert ops.request_dir != cleaner.request_dir
    assert cleaner.request_dir.is_relative_to(cleaner.callback_session_dir.parent)
    assert cleaner.issue_session_dir.is_relative_to(cleaner.callback_session_dir.parent)
    assert cleaner.completion_session_dir.is_relative_to(cleaner.callback_session_dir.parent)


def test_poller_offsets_are_persisted_per_identity(tmp_path):
    ops_token = tmp_path / "ops-token"
    cleaner_token = tmp_path / "cleaner-token"
    write_token(ops_token, "synthetic-ops-token")
    write_token(cleaner_token, "synthetic-cleaner-token")
    environment = {
        OPS_TOKEN_PATH_CONFIG: str(ops_token),
        OPS_ALLOWLIST_CONFIG: "101",
    }
    transport = PollingTransport(
        [{"update_id": 7, "message": {"chat": {"id": 101}, "text": "/ops_briefing"}}]
    )
    ops = OpsTelegramRouter(
        credential_provider=OpsCredentialProvider(environment=environment),
        allowlist_provider=OpsAllowlistProvider(environment=environment),
        transport=transport,
    )
    cleaner = CleanerTelegramRouter(
        credential_provider=CleanerCredentialProvider(token_path=cleaner_token),
        transport=transport,
        handlers=(lambda _update: "cleaner-action",),
    )
    ops_state = tmp_path / "ops" / "state.json"
    cleaner_state = tmp_path / "cleaner" / "state.json"

    assert ops.poll_once(state_path=ops_state)["offset"] == 8
    assert cleaner.poll_once(state_path=cleaner_state)["offset"] == 8

    assert ops_state != cleaner_state
    assert json.loads(ops_state.read_text())["offset"] == 8
    assert json.loads(cleaner_state.read_text())["offset"] == 8
    assert [call[0].token for call in transport.calls] == [
        "synthetic-ops-token",
        "synthetic-cleaner-token",
    ]

import hashlib
import hmac
import json
import plistlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from telegram_approval.cleaner_bot_runtime import (
    build_poller as build_cleaner_poller,
    run_cleaner_runtime,
)
from telegram_approval.cleaner_config import (
    CLEANER_TOKEN_PATH_CONFIG,
    CleanerRuntimePaths,
)
from telegram_approval.ops_bot_runtime import (
    build_poller as build_ops_poller,
    run_ops_runtime,
)
from telegram_approval.ops_config import (
    OPS_ALLOWLIST_CONFIG,
    OPS_TOKEN_PATH_CONFIG,
    OpsRuntimePaths,
)


ROOT = Path(__file__).resolve().parents[2]


class FakeTransport:
    def __init__(self, updates=()):
        self.update_batches = list(updates)
        self.calls = []

    def request(self, bot, method, **values):
        self.calls.append((bot.token, method, values))
        if method == "getUpdates":
            return self.update_batches.pop(0) if self.update_batches else []
        if method == "sendMessage":
            return {"message_id": len(self.calls)}
        if method == "answerCallbackQuery":
            return True
        raise AssertionError(f"unexpected synthetic Telegram method: {method}")


def _ops_paths(tmp_path):
    root = tmp_path / "ops"
    return OpsRuntimePaths(
        state_path=root / "state.json",
        callback_session_dir=root / "callback-sessions",
        request_dir=root / "requests",
    )


def _cleaner_paths(tmp_path):
    root = tmp_path / "cleaner"
    return CleanerRuntimePaths(
        state_path=root / "state.json",
        callback_session_dir=root / "callback-sessions",
        request_dir=root / "requests",
        door_code_request_dir=root / "door-code-requests",
        issue_session_dir=root / "issue-uploads",
        completion_session_dir=root / "completion-uploads",
    )


def _write_token(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n")


def _callback_update(action_id, signature, *, user_id, chat_id, update_id=1):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": user_id},
            "message": {"chat": {"id": chat_id, "type": "private"}},
            "data": f"a:{action_id}:approve:{signature}",
        },
    }


def _signature(secret, action_id):
    return hmac.new(
        secret.encode(), f"{action_id}:approve".encode(), hashlib.sha256
    ).hexdigest()[:16]


def test_runtime_builders_read_only_their_role_configuration(tmp_path):
    ops_token = tmp_path / "ops.token"
    cleaner_token = tmp_path / "cleaner.token"
    _write_token(ops_token, "synthetic-ops-token")
    _write_token(cleaner_token, "synthetic-cleaner-token")

    ops_environment = {
        OPS_TOKEN_PATH_CONFIG: str(ops_token),
        OPS_ALLOWLIST_CONFIG: "101",
        CLEANER_TOKEN_PATH_CONFIG: str(tmp_path / "must-not-read-cleaner.token"),
    }
    cleaner_environment = {
        CLEANER_TOKEN_PATH_CONFIG: str(cleaner_token),
        OPS_TOKEN_PATH_CONFIG: str(tmp_path / "must-not-read-ops.token"),
        OPS_ALLOWLIST_CONFIG: "not-an-integer-and-must-not-be-read",
    }
    ops_transport = FakeTransport([[]])
    cleaner_transport = FakeTransport([[]])
    ops_paths = _ops_paths(tmp_path)
    cleaner_paths = _cleaner_paths(tmp_path)

    ops_poller = build_ops_poller(
        environment=ops_environment,
        transport=ops_transport,
        paths=ops_paths,
        action_secret_path=tmp_path / "unused-ops-action-secret",
    )
    cleaner_poller = build_cleaner_poller(
        environment=cleaner_environment,
        transport=cleaner_transport,
        paths=cleaner_paths,
        action_secret_path=tmp_path / "unused-cleaner-action-secret",
    )

    assert ops_poller.state_path == ops_paths.state_path
    assert cleaner_poller.state_path == cleaner_paths.state_path
    ops_poller.poll_once()
    cleaner_poller.poll_once()
    assert {call[0] for call in ops_transport.calls} == {"synthetic-ops-token"}
    assert {call[0] for call in cleaner_transport.calls} == {"synthetic-cleaner-token"}
    assert not ops_paths.state_path.exists()
    assert not cleaner_paths.state_path.exists()


def test_ops_callback_consumes_ops_request_only(tmp_path):
    ops_token = tmp_path / "ops.token"
    _write_token(ops_token, "synthetic-ops-token")
    secret_path = tmp_path / "action-secret"
    secret_path.write_text("synthetic-action-secret\n")
    paths = _ops_paths(tmp_path)
    paths.request_dir.mkdir(parents=True)
    action_id = "ops-action"
    record_path = paths.request_dir / f"{action_id}.json"
    record_path.write_text(json.dumps({
        "schema_version": 1,
        "action_id": action_id,
        "status": "PENDING",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "consumed": False,
        "test_mode": True,
        "external_writes_on_approval": 0,
    }))
    transport = FakeTransport([[
        _callback_update(
            action_id,
            _signature("synthetic-action-secret", action_id),
            user_id=101,
            chat_id=101,
        )
    ]])
    poller = build_ops_poller(
        environment={
            OPS_TOKEN_PATH_CONFIG: str(ops_token),
            OPS_ALLOWLIST_CONFIG: "101",
        },
        transport=transport,
        paths=paths,
        action_secret_path=secret_path,
    )

    result = poller.poll_once()
    assert result["actions"] == ["approved"]
    assert json.loads(record_path.read_text())["consumed"] is True


def test_cleaner_callback_consumes_cleaner_request_only(tmp_path):
    cleaner_token = tmp_path / "cleaner.token"
    _write_token(cleaner_token, "synthetic-cleaner-token")
    secret_path = tmp_path / "action-secret"
    secret_path.write_text("synthetic-action-secret\n")
    paths = _cleaner_paths(tmp_path)
    paths.request_dir.mkdir(parents=True)
    action_id = "cleaner-action"
    record_path = paths.request_dir / f"{action_id}.json"
    record_path.write_text(json.dumps({
        "schema_version": 1,
        "action_id": action_id,
        "action_type": "CLEANING_START",
        "candidate_user_id": 202,
        "candidate_chat_id": 202,
        "status": "PENDING",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "consumed": False,
        "test_mode": True,
        "external_writes_on_approval": 0,
    }))
    transport = FakeTransport([[
        _callback_update(
            action_id,
            _signature("synthetic-action-secret", action_id),
            user_id=202,
            chat_id=202,
        )
    ]])
    poller = build_cleaner_poller(
        environment={CLEANER_TOKEN_PATH_CONFIG: str(cleaner_token)},
        transport=transport,
        paths=paths,
        action_secret_path=secret_path,
    )

    result = poller.poll_once()
    assert result["actions"] == ["approved"]
    assert json.loads(record_path.read_text())["consumed"] is True


def test_cross_role_callback_action_types_fail_closed_before_secret_read(tmp_path):
    ops_token = tmp_path / "ops.token"
    cleaner_token = tmp_path / "cleaner.token"
    _write_token(ops_token, "synthetic-ops-token")
    _write_token(cleaner_token, "synthetic-cleaner-token")
    missing_secret = tmp_path / "must-not-read-action-secret"
    ops_paths = _ops_paths(tmp_path)
    cleaner_paths = _cleaner_paths(tmp_path)
    ops_paths.request_dir.mkdir(parents=True)
    cleaner_paths.request_dir.mkdir(parents=True)

    # Cleaner-owned action copied into OPS state must still be rejected.
    (ops_paths.request_dir / "wrong-cleaner.json").write_text(json.dumps({
        "action_id": "wrong-cleaner",
        "action_type": "CLEANING_ASSIGNMENT",
        "candidate_user_id": 101,
        "candidate_chat_id": 101,
    }))
    ops_transport = FakeTransport([[
        _callback_update("wrong-cleaner", "bad-signature", user_id=101, chat_id=101)
    ]])
    ops_poller = build_ops_poller(
        environment={
            OPS_TOKEN_PATH_CONFIG: str(ops_token),
            OPS_ALLOWLIST_CONFIG: "101",
        },
        transport=ops_transport,
        paths=ops_paths,
        action_secret_path=missing_secret,
    )
    assert ops_poller.poll_once()["actions"] == ["ignored"]

    # OPS-owned action copied into Cleaner state must also be rejected.
    (cleaner_paths.request_dir / "wrong-ops.json").write_text(json.dumps({
        "action_id": "wrong-ops",
        "action_type": "CANCEL_RESERVATION_WORKFLOW",
    }))
    cleaner_transport = FakeTransport([[
        _callback_update("wrong-ops", "bad-signature", user_id=202, chat_id=202)
    ]])
    cleaner_poller = build_cleaner_poller(
        environment={CLEANER_TOKEN_PATH_CONFIG: str(cleaner_token)},
        transport=cleaner_transport,
        paths=cleaner_paths,
        action_secret_path=missing_secret,
    )
    assert cleaner_poller.poll_once()["actions"] == ["ignored"]
    assert not missing_secret.exists()


def test_cross_role_request_directory_is_never_consulted(tmp_path):
    ops_token = tmp_path / "ops.token"
    cleaner_token = tmp_path / "cleaner.token"
    _write_token(ops_token, "synthetic-ops-token")
    _write_token(cleaner_token, "synthetic-cleaner-token")
    ops_paths = _ops_paths(tmp_path)
    cleaner_paths = _cleaner_paths(tmp_path)
    cleaner_paths.request_dir.mkdir(parents=True)
    cleaner_record = cleaner_paths.request_dir / "cleaner-only.json"
    cleaner_record.write_text(json.dumps({"action_id": "cleaner-only", "consumed": False}))

    ops_transport = FakeTransport([[
        _callback_update("cleaner-only", "irrelevant", user_id=101, chat_id=101)
    ]])
    ops_poller = build_ops_poller(
        environment={
            OPS_TOKEN_PATH_CONFIG: str(ops_token),
            OPS_ALLOWLIST_CONFIG: "101",
        },
        transport=ops_transport,
        paths=ops_paths,
        action_secret_path=tmp_path / "unused",
    )
    assert ops_poller.poll_once()["actions"] == ["ignored"]
    assert json.loads(cleaner_record.read_text())["consumed"] is False

    ops_paths.request_dir.mkdir(parents=True, exist_ok=True)
    ops_record = ops_paths.request_dir / "ops-only.json"
    ops_record.write_text(json.dumps({"action_id": "ops-only", "consumed": False}))
    cleaner_transport = FakeTransport([[
        _callback_update("ops-only", "irrelevant", user_id=202, chat_id=202, update_id=2)
    ]])
    cleaner_poller = build_cleaner_poller(
        environment={CLEANER_TOKEN_PATH_CONFIG: str(cleaner_token)},
        transport=cleaner_transport,
        paths=cleaner_paths,
        action_secret_path=tmp_path / "unused",
    )
    assert cleaner_poller.poll_once()["actions"] == ["ignored"]
    assert json.loads(ops_record.read_text())["consumed"] is False


def test_ops_and_cleaner_offsets_never_cross_role_state(tmp_path):
    ops_token = tmp_path / "ops.token"
    cleaner_token = tmp_path / "cleaner.token"
    _write_token(ops_token, "synthetic-ops-token")
    _write_token(cleaner_token, "synthetic-cleaner-token")
    ops_paths = _ops_paths(tmp_path)
    cleaner_paths = _cleaner_paths(tmp_path)
    ops_transport = FakeTransport([[{"update_id": 10, "message": {"chat": {"id": 101}, "text": "/unknown"}}]])
    cleaner_transport = FakeTransport([[{"update_id": 30, "message": {"chat": {"id": 202}, "text": "/unknown"}}]])

    ops_poller = build_ops_poller(
        environment={OPS_TOKEN_PATH_CONFIG: str(ops_token), OPS_ALLOWLIST_CONFIG: "101"},
        transport=ops_transport,
        paths=ops_paths,
        action_secret_path=tmp_path / "unused-ops-secret",
    )
    cleaner_poller = build_cleaner_poller(
        environment={CLEANER_TOKEN_PATH_CONFIG: str(cleaner_token)},
        transport=cleaner_transport,
        paths=cleaner_paths,
        action_secret_path=tmp_path / "unused-cleaner-secret",
    )

    assert ops_poller.poll_once()["offset"] == 11
    assert cleaner_poller.poll_once()["offset"] == 31
    assert json.loads(ops_paths.state_path.read_text())["offset"] == 11
    assert json.loads(cleaner_paths.state_path.read_text())["offset"] == 31
    assert ops_transport.calls[0][2]["offset"] == 0
    assert cleaner_transport.calls[0][2]["offset"] == 0


def test_legacy_mixed_runtime_is_retired_and_launch_source_is_disabled():
    from telegram_approval import bot_service

    with pytest.raises(SystemExit, match="Legacy mixed Telegram poller is disabled"):
        bot_service.main()

    legacy = plistlib.loads(
        (ROOT / "telegram_approval" / "com.propertyai.telegram-approval.plist").read_bytes()
    )
    assert legacy["Disabled"] is True
    assert legacy["RunAtLoad"] is False
    assert legacy["KeepAlive"] is False


def test_role_specific_launch_sources_are_non_secret_and_config_explicit():
    artifacts = {
        "ops": ROOT / "telegram_approval" / "com.propertyai.telegram-ops.plist",
        "cleaner": ROOT / "telegram_approval" / "com.propertyai.telegram-cleaner.plist",
        "operations": ROOT / "telegram_approval" / "com.propertyai.cleaning-operations.plist",
        "completion": ROOT / "telegram_approval" / "com.propertyai.cleaning-completion.plist",
        "gmail": ROOT / "gmail_ingest" / "com.propertyai.gmail-readonly.plist",
    }
    texts = {name: path.read_text() for name, path in artifacts.items()}
    for path in artifacts.values():
        plistlib.loads(path.read_bytes())

    assert "telegram_approval.ops_bot_runtime" in texts["ops"]
    assert OPS_TOKEN_PATH_CONFIG in texts["ops"]
    assert OPS_ALLOWLIST_CONFIG in texts["ops"]
    assert "telegram_approval.cleaner_bot_runtime" in texts["cleaner"]
    assert CLEANER_TOKEN_PATH_CONFIG in texts["cleaner"]
    assert CLEANER_TOKEN_PATH_CONFIG in texts["operations"]
    assert OPS_TOKEN_PATH_CONFIG in texts["operations"]
    assert OPS_ALLOWLIST_CONFIG in texts["operations"]
    assert CLEANER_TOKEN_PATH_CONFIG in texts["completion"]
    assert CLEANER_TOKEN_PATH_CONFIG in texts["gmail"]
    assert OPS_TOKEN_PATH_CONFIG in texts["gmail"]

    joined = "\n".join(texts.values())
    assert "REPLACE_AT_CUTOVER" in joined
    assert not re.search(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", joined)
    assert not re.search(r"<string>\d{6,}</string>", joined)


def test_ops_runtime_isolates_pump_exception_and_keeps_single_getupdates_consumer(tmp_path):
    ops_token = tmp_path / "ops.token"
    _write_token(ops_token, "synthetic-ops-token")
    transport = FakeTransport([[], []])
    poller = build_ops_poller(
        environment={OPS_TOKEN_PATH_CONFIG: str(ops_token), OPS_ALLOWLIST_CONFIG: "101"},
        transport=transport,
        paths=_ops_paths(tmp_path),
        action_secret_path=tmp_path / "unused",
    )

    class BrokenPump:
        def __init__(self):
            self.calls = 0

        def run_once(self):
            self.calls += 1
            raise RuntimeError("synthetic pump failure")

    pump = BrokenPump()
    run_ops_runtime(
        poller=poller, notification_pump=pump, max_cycles=2,
        minimum_cycle_seconds=0, sleep=lambda _seconds: None,
    )
    methods = [method for _token, method, _values in transport.calls]
    assert methods.count("getUpdates") == 2
    assert methods == ["getUpdates", "getUpdates"]
    assert pump.calls == 2


def test_cleaner_runtime_isolates_pump_exception_and_keeps_single_getupdates_consumer(tmp_path):
    cleaner_token = tmp_path / "cleaner.token"
    _write_token(cleaner_token, "synthetic-cleaner-token")
    transport = FakeTransport([[], []])
    poller = build_cleaner_poller(
        environment={CLEANER_TOKEN_PATH_CONFIG: str(cleaner_token)},
        transport=transport,
        paths=_cleaner_paths(tmp_path),
        action_secret_path=tmp_path / "unused",
    )

    class BrokenPump:
        def __init__(self):
            self.calls = 0

        def run_once(self):
            self.calls += 1
            raise RuntimeError("synthetic pump failure")

    pump = BrokenPump()
    run_cleaner_runtime(
        poller=poller, notification_pump=pump, max_cycles=2,
        minimum_cycle_seconds=0, sleep=lambda _seconds: None,
    )
    methods = [method for _token, method, _values in transport.calls]
    assert methods.count("getUpdates") == 2
    assert methods == ["getUpdates", "getUpdates"]
    assert pump.calls == 2

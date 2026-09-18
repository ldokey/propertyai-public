import json
from pathlib import Path

import pytest

from telegram_approval.telegram_polling import (
    MAX_CONCURRENT_POLLERS_PER_BOT,
    PollerAlreadyRunningError,
    SinglePollerGuard,
    TelegramPoller,
    build_cleaner_cutover_manifest,
    monotonic_offset_seed,
)
from telegram_approval.telegram_transport import TelegramBotContext


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, bot, method, **values):
        self.calls.append((bot.token, method, values))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_long_poll_fault_isolation_and_offset_progression(tmp_path):
    transport = FakeTransport([
        [{"update_id": 4}, {"update_id": 5}],
        [],
    ])

    def handler(update):
        if update["update_id"] == 4:
            raise ValueError("synthetic malformed business update")
        return "ok"

    state_path = tmp_path / "role" / "state.json"
    poller = TelegramPoller(
        bot=TelegramBotContext("synthetic-token"),
        transport=transport,
        state_path=state_path,
        handler=handler,
    )

    result = poller.poll_once()
    assert result == {
        "updates": 2,
        "processed": 2,
        "actions": ["ignored", "ok"],
        "errors": [{"update_id": 4, "error_type": "ValueError"}],
        "offset": 6,
    }
    assert json.loads(state_path.read_text())["offset"] == 6
    assert transport.calls[0][2]["timeout"] == 25

    poller.poll_once()
    assert transport.calls[1][2]["offset"] == 6


def test_malformed_update_does_not_kill_batch(tmp_path):
    transport = FakeTransport([[{"bad": "update"}, {"update_id": 9}]])
    poller = TelegramPoller(
        bot=TelegramBotContext("synthetic-token"),
        transport=transport,
        state_path=tmp_path / "state.json",
        handler=lambda _update: "ok",
    )

    result = poller.poll_once()
    assert result["actions"] == ["ignored", "ok"]
    assert result["errors"] == [{"update_id": None, "error_type": "MalformedUpdate"}]
    assert result["offset"] == 10


def test_backoff_is_deterministic_and_resets_after_success(tmp_path):
    transport = FakeTransport([
        RuntimeError("poll-1"),
        RuntimeError("poll-2"),
        [],
        RuntimeError("poll-after-success"),
    ])
    sleeps = []
    poller = TelegramPoller(
        bot=TelegramBotContext("synthetic-token"),
        transport=transport,
        state_path=tmp_path / "state.json",
        handler=lambda _update: "ok",
    )

    poller.run_forever(
        backoff_seconds=(1, 2, 4),
        sleep=sleeps.append,
        max_cycles=4,
    )

    assert sleeps == [1, 2, 1]
    assert all(call[2]["timeout"] == 25 for call in transport.calls)


def test_single_poller_guard_rejects_second_owner(tmp_path):
    assert MAX_CONCURRENT_POLLERS_PER_BOT == 1
    state_path = tmp_path / "ops" / "state.json"
    with SinglePollerGuard(state_path):
        with pytest.raises(PollerAlreadyRunningError):
            with SinglePollerGuard(state_path):
                pass


def test_cutover_offset_seed_preserves_exact_next_update_boundary():
    assert monotonic_offset_seed(quiesced_legacy_offset=41, cleaner_last_offset=0) == 41
    assert monotonic_offset_seed(quiesced_legacy_offset=41, cleaner_last_offset=39) == 41
    assert monotonic_offset_seed(quiesced_legacy_offset=41, cleaner_last_offset=44) == 44
    with pytest.raises(ValueError):
        monotonic_offset_seed(quiesced_legacy_offset=-1, cleaner_last_offset=0)


def test_cutover_manifest_uses_only_quiesced_active_inventory():
    manifest = build_cleaner_cutover_manifest(
        quiesced_legacy_offset=50,
        cleaner_last_offset=52,
        active_request_ids=("request-a", "request-b"),
        active_session_ids=("session-a",),
    )
    assert manifest == {
        "schema_version": 1,
        "cleaner_offset_seed": 52,
        "active_request_ids": ["request-a", "request-b"],
        "active_session_ids": ["session-a"],
    }
    with pytest.raises(ValueError, match="duplicates"):
        build_cleaner_cutover_manifest(
            quiesced_legacy_offset=1, active_request_ids=("same", "same")
        )

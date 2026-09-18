"""Role-neutral long-polling runtime with caller-owned bot state."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, ContextManager, Iterable

from telegram_approval.telegram_transport import TelegramBotContext, TelegramTransport


DEFAULT_LONG_POLL_TIMEOUT_SECONDS = 25
DEFAULT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)
MAX_CONCURRENT_POLLERS_PER_BOT = 1


class PollerAlreadyRunningError(RuntimeError):
    """Raised when a second process tries to own the same bot state."""


class SinglePollerGuard:
    """Minimal process guard keyed by the role-specific state path."""

    def __init__(self, state_path: Path) -> None:
        self._lock_path = state_path.with_suffix(state_path.suffix + ".poller.lock")
        self._handle = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def __enter__(self) -> "SinglePollerGuard":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._handle = self._lock_path.open("a+")
        os.chmod(self._lock_path, 0o600)
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._handle.close()
            self._handle = None
            raise PollerAlreadyRunningError(
                f"a Telegram poller already owns {self._lock_path}"
            ) from error
        return self

    def __exit__(self, *_args: object) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def monotonic_offset_seed(
    *, quiesced_legacy_offset: int, cleaner_last_offset: int = 0
) -> int:
    """Return the cutover next-update boundary without allowing rollback."""

    if quiesced_legacy_offset < 0 or cleaner_last_offset < 0:
        raise ValueError("Telegram offsets must be non-negative")
    return max(quiesced_legacy_offset, cleaner_last_offset)


def build_cleaner_cutover_manifest(
    *,
    quiesced_legacy_offset: int,
    cleaner_last_offset: int = 0,
    active_request_ids: Iterable[str] = (),
    active_session_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Build a pure future-cutover manifest from the quiesced inventory.

    The caller supplies the exact inventory captured at QUIESCE time.  This
    helper deliberately performs no filesystem reads or writes, and it never
    revives terminal history that is absent from that inventory.
    """

    def normalized(values: Iterable[str], label: str) -> list[str]:
        items = list(values)
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError(f"{label} must contain non-empty string ids")
        if len(items) != len(set(items)):
            raise ValueError(f"{label} must not contain duplicates")
        return items

    return {
        "schema_version": 1,
        "cleaner_offset_seed": monotonic_offset_seed(
            quiesced_legacy_offset=quiesced_legacy_offset,
            cleaner_last_offset=cleaner_last_offset,
        ),
        "active_request_ids": normalized(active_request_ids, "active_request_ids"),
        "active_session_ids": normalized(active_session_ids, "active_session_ids"),
    }


class TelegramPoller:
    def __init__(
        self,
        *,
        bot: TelegramBotContext,
        transport: TelegramTransport,
        state_path: Path,
        handler: Callable[[dict[str, Any]], str],
        mutation_scope_factory: Callable[[dict[str, Any]], ContextManager[Any]] | None = None,
        pre_state_mutation_assert: Callable[[], Any] | None = None,
    ) -> None:
        self._bot = bot
        self._transport = transport
        self._state_path = state_path
        self._handler = handler
        self._mutation_scope_factory = mutation_scope_factory
        self._pre_state_mutation_assert = pre_state_mutation_assert

    @property
    def state_path(self) -> Path:
        return self._state_path

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {"schema_version": 1, "offset": 0}
        state = json.loads(self._state_path.read_text())
        offset = state.get("offset", 0)
        if not isinstance(offset, int) or offset < 0:
            raise RuntimeError("Telegram poller state has an invalid offset")
        return state

    def _store_state(self, state: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._state_path)

    def poll_once(
        self, *, long_poll_timeout: int = DEFAULT_LONG_POLL_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        if long_poll_timeout < 0:
            raise ValueError("long_poll_timeout must be non-negative")
        state = self._load_state()
        updates = self._transport.request(
            self._bot,
            "getUpdates",
            offset=state.get("offset", 0),
            timeout=long_poll_timeout,
            allowed_updates=json.dumps(["message", "callback_query"]),
        )
        if not isinstance(updates, list):
            raise RuntimeError("Telegram getUpdates result must be a list")

        actions: list[str] = []
        errors: list[dict[str, Any]] = []
        processed = 0
        for update in updates:
            if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                actions.append("ignored")
                errors.append({"update_id": None, "error_type": "MalformedUpdate"})
                continue

            update_id = update["update_id"]

            def process_valid_update() -> None:
                nonlocal processed
                try:
                    actions.append(self._handler(update))
                except Exception as error:  # isolate one update from the long-running poller
                    actions.append("ignored")
                    errors.append(
                        {"update_id": update_id, "error_type": type(error).__name__}
                    )
                finally:
                    # Offset persistence is replay-semantic durable mutation. If a
                    # GLOBAL_PRODUCTION scope is configured it occurs only after a
                    # fresh fence assertion. An acquisition failure never consumes
                    # the update or advances the offset.
                    if self._pre_state_mutation_assert is not None:
                        self._pre_state_mutation_assert()
                    state["offset"] = max(state.get("offset", 0), update_id + 1)
                    self._store_state(state)
                    processed += 1

            if self._mutation_scope_factory is None:
                process_valid_update()
            else:
                with self._mutation_scope_factory(update):
                    process_valid_update()

        return {
            "updates": len(updates),
            "processed": processed,
            "actions": actions,
            "errors": errors,
            "offset": state.get("offset", 0),
        }

    def run_forever(
        self,
        *,
        long_poll_timeout: int = DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
        backoff_seconds: Iterable[float] = DEFAULT_BACKOFF_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        max_cycles: int | None = None,
    ) -> None:
        backoff = tuple(backoff_seconds)
        if not backoff or any(delay < 0 for delay in backoff):
            raise ValueError("backoff_seconds must contain non-negative values")
        if max_cycles is not None and max_cycles < 0:
            raise ValueError("max_cycles must be non-negative")

        failure_count = 0
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            try:
                self.poll_once(long_poll_timeout=long_poll_timeout)
                failure_count = 0
            except Exception:
                delay = backoff[min(failure_count, len(backoff) - 1)]
                failure_count += 1
                sleep(delay)

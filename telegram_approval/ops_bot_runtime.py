#!/usr/bin/env python3
"""Long-running OPS Telegram runtime for the Stage-2 role-specific topology."""

from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Callable, ContextManager, Iterable, Mapping

from telegram_approval.ops_bot_service import OpsTelegramRouter
from propertyai_core.global_writer import (
    assert_current_production_writer,
    mutation_scope,
    publish_startup_runtime_identity,
)
from telegram_approval.cleaner_config import CleanerRuntimePaths
from telegram_approval.ops_config import (
    OpsAllowlistProvider,
    OpsCredentialProvider,
    OpsRuntimePaths,
)
from telegram_approval.telegram_polling import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
    SinglePollerGuard,
    TelegramPoller,
)
from telegram_approval.telegram_transport import TelegramHttpTransport, TelegramTransport


ROOT = Path(__file__).resolve().parents[1]
ACTION_SECRET_PATH = ROOT / "secrets" / "telegram" / "action-secret"


def build_poller(
    *,
    environment: Mapping[str, str] | None = None,
    transport: TelegramTransport | None = None,
    paths: OpsRuntimePaths | None = None,
    action_secret_path: Path | None = None,
    mutation_scope_factory: Callable[[dict], ContextManager] | None = None,
) -> TelegramPoller:
    """Compose OPS identity, allowlist, callback state, and polling state only."""

    environment = os.environ if environment is None else environment
    paths = paths or OpsRuntimePaths()
    transport = transport or TelegramHttpTransport()
    credential_provider = OpsCredentialProvider(environment=environment)
    allowlist_provider = OpsAllowlistProvider(environment=environment)

    # Fail closed before entering the long-running loop.
    bot = credential_provider.bot_context()
    allowlist_provider.allowed_chat_ids()

    if mutation_scope_factory is None:
        mutation_scope_factory = lambda update: mutation_scope(
            "W02",
            unit_id=f"telegram-update:{update['update_id']}",
            operation_class="OPS_TELEGRAM_UPDATE",
            target=f"telegram-update:{update['update_id']}",
        )

    def request_api(token: str, method: str, **values):
        if token != bot.token:
            raise RuntimeError("OPS callback attempted a different bot identity")
        assert_current_production_writer()
        return transport.request(bot, method, **values)

    from telegram_approval.bot_service import OPS_CALLBACK_ACTION_TYPES, callback
    from telegram_approval.cleaner_property_access import handle_ops_property_access_callback
    from telegram_approval.cleaner_reassignment import handle_ops_reassignment_callback

    def callback_handler(update):
        property_access_result = handle_ops_property_access_callback(
            update, bot.token, request_api=request_api
        )
        if property_access_result:
            return property_access_result
        from telegram_approval.cleaner_pg_ingress import (
            handle_postgres_ops_reassignment_callback,
        )

        pg_reassignment_result = handle_postgres_ops_reassignment_callback(
            update,
            request_dir=CleanerRuntimePaths().request_dir,
            secret_path=action_secret_path or ACTION_SECRET_PATH,
        )
        if pg_reassignment_result:
            request_api(
                bot.token,
                "sendMessage",
                chat_id=update["callback_query"]["message"]["chat"]["id"],
                text="결정이 기록되었습니다. PostgreSQL Cleaner writer가 처리합니다.",
            )
            return pg_reassignment_result
        reassignment_result = handle_ops_reassignment_callback(
            update, bot.token, request_api=request_api,
            request_dir=CleanerRuntimePaths().request_dir,
            secret_path=action_secret_path or ACTION_SECRET_PATH,
        )
        if reassignment_result:
            return reassignment_result
        return callback(
            update,
            bot.token,
            request_dir=paths.request_dir,
            action_secret_path=action_secret_path or ACTION_SECRET_PATH,
            allowed_action_types=OPS_CALLBACK_ACTION_TYPES,
            preauthorized_identity=True,
            request_api=request_api,
        )

    router = OpsTelegramRouter(
        credential_provider=credential_provider,
        allowlist_provider=allowlist_provider,
        transport=transport,
        callback_handler=callback_handler,
    )
    return TelegramPoller(
        bot=bot,
        transport=transport,
        state_path=paths.state_path,
        handler=router.route,
        mutation_scope_factory=mutation_scope_factory,
        pre_state_mutation_assert=assert_current_production_writer,
    )


class OpsNotificationBundle:
    """Run independent OPS-side delivery pumps without cross-suppressing them."""

    def __init__(self, *pumps) -> None:
        self._pumps = pumps

    def run_once(self) -> dict:
        result = {}
        for index, pump in enumerate(self._pumps):
            try:
                result[str(index)] = pump.run_once()
            except Exception as exc:
                result[str(index)] = {"error_type": type(exc).__name__}
        return result


DEFAULT_OPS_MINIMUM_CYCLE_SECONDS = 5.0


def run_ops_runtime(
    *,
    poller: TelegramPoller,
    notification_pump,
    long_poll_timeout: int = DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
    backoff_seconds: Iterable[float] = DEFAULT_BACKOFF_SECONDS,
    minimum_cycle_seconds: float = DEFAULT_OPS_MINIMUM_CYCLE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    max_cycles: int | None = None,
) -> None:
    """Run one Telegram poller plus an isolated OPS-side notification pump.

    The pump performs Notion reads and Telegram ``sendMessage`` calls only; it
    never owns a second ``getUpdates`` consumer.  A minimum cycle duration
    prevents a busy loop if Telegram long-polling returns immediately.
    """

    backoff = tuple(backoff_seconds)
    if not backoff or any(delay < 0 for delay in backoff):
        raise ValueError("backoff_seconds must contain non-negative values")
    if minimum_cycle_seconds < 0:
        raise ValueError("minimum_cycle_seconds must be non-negative")
    if max_cycles is not None and max_cycles < 0:
        raise ValueError("max_cycles must be non-negative")

    failure_count = 0
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        started = monotonic()
        try:
            poller.poll_once(long_poll_timeout=long_poll_timeout)
            failure_count = 0
        except Exception:
            delay = backoff[min(failure_count, len(backoff) - 1)]
            failure_count += 1
            sleep(delay)

        try:
            notification_pump.run_once()
        except Exception:
            # The notification path is supplemental OPS-side work.  Its failure
            # must never terminate or replace the sole Telegram polling loop.
            pass

        remaining = minimum_cycle_seconds - (monotonic() - started)
        if remaining > 0:
            sleep(remaining)


def main() -> None:
    # Observational startup proof only: no lease, DCS write, or business effect.
    publish_startup_runtime_identity("W02")
    from telegram_approval.ops_property_access_notifications import (
        build_ops_property_access_notification_pump,
    )
    from telegram_approval.ops_reassignment_notifications import (
        build_ops_reassignment_notification_pump,
    )

    paths = OpsRuntimePaths()
    poller = build_poller(paths=paths)
    property_access_pump = build_ops_property_access_notification_pump(
        delivery_state_path=paths.state_path.parent / "property-access-notifications.json",
        mutation_scope_factory=lambda access_page_id, chat_id: mutation_scope(
            "W02",
            unit_id=f"property-access-delivery:{access_page_id}:{chat_id}",
            operation_class="OPS_TELEGRAM_DELIVERY",
            target=f"property-access:{access_page_id}:chat:{chat_id}",
        ),
        pre_external_assert=assert_current_production_writer,
    )
    reassignment_pump = build_ops_reassignment_notification_pump(
        request_dir=CleanerRuntimePaths().request_dir,
        secret_path=ACTION_SECRET_PATH,
        mutation_scope_factory=lambda action_id: mutation_scope(
            "W02",
            unit_id=f"reassignment-decision-delivery:{action_id}",
            operation_class="OPS_TELEGRAM_DELIVERY",
            target=f"reassignment-decision:{action_id}",
        ),
        pre_external_assert=assert_current_production_writer,
    )
    notification_pump = OpsNotificationBundle(property_access_pump, reassignment_pump)
    with SinglePollerGuard(paths.state_path):
        run_ops_runtime(poller=poller, notification_pump=notification_pump)


if __name__ == "__main__":
    main()

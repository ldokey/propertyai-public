#!/usr/bin/env python3
"""Long-running Cleaner Telegram runtime for the Stage-2 role-specific topology."""

from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Callable, ContextManager, Iterable, Mapping

from telegram_approval.cleaner_bot_service import CleanerTelegramRouter
from propertyai_core.config.cleaner_authority import CleanerAuthorityConfig
from propertyai_core.runtime.cleaner_pg_composition import build_cleaner_postgres_application
from propertyai_core.global_writer import (
    assert_current_production_writer,
    mutation_scope,
    publish_startup_runtime_identity,
)
from telegram_approval.cleaner_config import CleanerCredentialProvider, CleanerRuntimePaths
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
    paths: CleanerRuntimePaths | None = None,
    action_secret_path: Path | None = None,
    mutation_scope_factory: Callable[[dict], ContextManager] | None = None,
    authority_config: CleanerAuthorityConfig | None = None,
    postgres_service=None,
) -> TelegramPoller:
    """Compose Cleaner credential, registry-aware inbound handlers, and state."""

    environment = os.environ if environment is None else environment
    paths = paths or CleanerRuntimePaths()
    transport = transport or TelegramHttpTransport()
    credential_provider = CleanerCredentialProvider(environment=environment)
    authority = authority_config or CleanerAuthorityConfig.from_environment(environment)
    if authority.uses_postgres and postgres_service is None:
        raise RuntimeError("POSTGRES Cleaner authority requires the Product PG application service")

    # Fail closed before entering the long-running loop.
    bot = credential_provider.bot_context()

    if mutation_scope_factory is None:
        mutation_scope_factory = lambda update: mutation_scope(
            "W03",
            unit_id=f"telegram-update:{update['update_id']}",
            operation_class="CLEANER_TELEGRAM_UPDATE",
            target=f"telegram-update:{update['update_id']}",
        )

    def request_api(token: str, method: str, **values):
        if token != bot.token:
            raise RuntimeError("Cleaner callback attempted a different bot identity")
        assert_current_production_writer()
        return transport.request(bot, method, **values)

    def cleaner_handler(update):
        if authority.uses_postgres:
            from telegram_approval.cleaner_pg_ingress import handle_postgres_cleaner_callback

            return handle_postgres_cleaner_callback(
                update,
                request_dir=paths.request_dir,
                secret_path=action_secret_path or ACTION_SECRET_PATH,
                authority=authority,
                service=postgres_service,
            )
        if not authority.uses_legacy:
            raise RuntimeError("Cleaner authority route is not executable")
        from telegram_approval.bot_service import handle_cleaner

        return handle_cleaner(
            update,
            bot.token,
            request_dir=paths.request_dir,
            action_secret_path=action_secret_path or ACTION_SECRET_PATH,
            request_api=request_api,
        )

    router = CleanerTelegramRouter(
        credential_provider=credential_provider,
        transport=transport,
        handlers=(cleaner_handler,),
    )
    return TelegramPoller(
        bot=bot,
        transport=transport,
        state_path=paths.state_path,
        handler=router.route,
        mutation_scope_factory=mutation_scope_factory,
        pre_state_mutation_assert=assert_current_production_writer,
    )


class CleanerRuntimeNotificationBundle:
    """Run supplemental Cleaner pumps without changing the W03 poller identity."""

    def __init__(self, *pumps) -> None:
        self._pumps = tuple(pump for pump in pumps if pump is not None)

    def run_once(self) -> dict:
        result = {}
        for index, pump in enumerate(self._pumps):
            result[str(index)] = pump.run_once()
        return result


DEFAULT_CLEANER_MINIMUM_CYCLE_SECONDS = 5.0


def run_cleaner_runtime(
    *,
    poller: TelegramPoller,
    notification_pump,
    long_poll_timeout: int = DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
    backoff_seconds: Iterable[float] = DEFAULT_BACKOFF_SECONDS,
    minimum_cycle_seconds: float = DEFAULT_CLEANER_MINIMUM_CYCLE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    max_cycles: int | None = None,
) -> None:
    """Run the sole Cleaner poller plus isolated Property Access outbound delivery."""

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
            # Property Access outbound delivery is supplemental Cleaner work.
            # Its failure must never terminate or replace the inbound Telegram loop.
            pass

        remaining = minimum_cycle_seconds - (monotonic() - started)
        if remaining > 0:
            sleep(remaining)


def main() -> None:
    # Observational startup proof only: no lease, DCS write, or business effect.
    publish_startup_runtime_identity("W03")
    from telegram_approval.cleaner_property_access_notifications import (
        build_cleaner_property_access_notification_bundle,
    )

    paths = CleanerRuntimePaths()
    authority = CleanerAuthorityConfig.from_environment()
    bundle = None
    postgres_service = None
    if authority.uses_postgres:
        bundle = build_cleaner_postgres_application(authority)
        bundle.open()
        postgres_service = bundle.service
    try:
        poller = build_poller(
            paths=paths,
            authority_config=authority,
            postgres_service=postgres_service,
        )
        property_access_pump = build_cleaner_property_access_notification_bundle(
            approval_delivery_state_path=(
                paths.state_path.parent / "property-access-approval-notifications.json"
            ),
            rejection_delivery_state_path=(
                paths.state_path.parent / "property-access-rejection-notifications.json"
            ),
            mutation_scope_factory=lambda access_page_id, access_id: mutation_scope(
                "W03",
                unit_id=f"property-access-delivery:{access_page_id}:{access_id}",
                operation_class="CLEANER_TELEGRAM_DELIVERY",
                target=f"property-access:{access_page_id}:{access_id}",
            ),
            pre_external_assert=assert_current_production_writer,
        )
        pg_reassignment_pump = None
        if authority.uses_postgres:
            from telegram_approval.cleaner_pg_ingress import (
                CleanerPostgresReassignmentDecisionPump,
            )

            pg_reassignment_pump = CleanerPostgresReassignmentDecisionPump(
                request_dir=paths.request_dir,
                authority=authority,
                service=postgres_service,
                mutation_scope_factory=lambda action_id: mutation_scope(
                    "W03",
                    unit_id=f"pg-reassignment-decision:{action_id}",
                    operation_class="CLEANER_PG_REASSIGNMENT_DECISION",
                    target=f"cleaner-reassignment:{action_id}",
                ),
            )
        notification_pump = CleanerRuntimeNotificationBundle(
            property_access_pump, pg_reassignment_pump
        )
        with SinglePollerGuard(paths.state_path):
            run_cleaner_runtime(poller=poller, notification_pump=notification_pump)
    finally:
        if bundle is not None:
            bundle.close()


if __name__ == "__main__":
    main()

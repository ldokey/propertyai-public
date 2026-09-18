"""Explicit run-once entrypoint; importing this module never starts a scheduler."""
from __future__ import annotations

from datetime import datetime
from typing import Callable
from uuid import UUID

import psycopg

from propertyai_core.adapters.postgres.recurring_rent import PostgresRecurringBilling
from propertyai_core.application.recurring_rent import RecurringRentWorker, ScanCursor, WorkerConfig


def run_recurring_billing_once(
    *, connect: Callable[[], psycopg.Connection], organization_id: UUID,
    clock: Callable[[], datetime], config: WorkerConfig = WorkerConfig(),
    dry_run: bool = False, run_id: UUID | None = None, after: ScanCursor | None = None,
    runtime: str = "ISOLATED_TEST",
) -> dict:
    """W3-B/I3 contract v1: explicit factory + clock, JSON-serializable result.

    No DSN/environment discovery, scheduler installation, loop, background thread,
    global writer acquisition, service start, or Production connection is provided.
    """
    adapter = PostgresRecurringBilling(connect, organization_id=organization_id, runtime=runtime)
    return RecurringRentWorker(adapter, clock, config).run_once(
        dry_run=dry_run, run_id=run_id, after=after,
    )

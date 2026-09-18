from __future__ import annotations

from datetime import datetime
from typing import Optional

from propertyai_core.adapters.sqlite.store import SQLiteStore
from propertyai_core.global_writer import assert_current_production_writer, mutation_scope
from propertyai_core.ports.effects import EffectAdapter


class SystemTestWorker:
    def __init__(
        self,
        store: SQLiteStore,
        adapter: EffectAdapter,
        *,
        worker_id: str,
        max_attempts: int = 3,
        retry_delay_seconds: int = 5,
    ):
        self.store = store
        self.adapter = adapter
        self.worker_id = worker_id
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds

    def run_once(self, *, now: Optional[datetime] = None) -> bool:
        unit_clock = now.isoformat() if now is not None else "current"
        with mutation_scope(
            "W07",
            unit_id=f"{self.worker_id}:claim:{unit_clock}",
            operation_class="CORE_OUTBOX_EFFECT",
            target="propertyai_core.outbox_effect",
        ):
            # claim_next_effect() is itself a durable Production mutation.
            claim = self.store.claim_next_effect(self.worker_id, now=now)
            if claim is None:
                return False

            try:
                # No SQLite transaction is held across the external effect boundary.
                assert_current_production_writer()
                outcome = self.adapter.execute(claim)
            except Exception as error:
                # The adapter may have applied the effect before its acknowledgement
                # failed. Persist an explicit reconciliation boundary; never blind-retry.
                assert_current_production_writer()
                self.store.mark_effect_reconciliation(
                    claim,
                    error_code=f"EXTERNAL_EFFECT_UNCERTAIN:{type(error).__name__}",
                )
                return True

            # Lost fencing after an external call forbids result persistence too.
            assert_current_production_writer()
            if outcome.succeeded:
                self.store.complete_effect(claim, outcome.result)
            else:
                self.store.fail_effect(
                    claim,
                    error_code=outcome.error_code or "UNKNOWN_TEST_EFFECT_ERROR",
                    retryable=outcome.retryable,
                    max_attempts=self.max_attempts,
                    retry_delay_seconds=self.retry_delay_seconds,
                    now=now,
                )
            return True

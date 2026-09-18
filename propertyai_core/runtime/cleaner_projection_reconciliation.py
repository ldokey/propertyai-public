"""Exact fail-closed reconciliation for the TK-43 first-command projections.

The normal W07 worker deliberately never claims ``PENDING_RECONCILIATION`` rows.
This bounded successor reads both external destinations first, rejects ambiguity,
and only then either idempotently delivers missing current state or records factual
success through the existing fenced PostgreSQL reconciliation function.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

from propertyai_core.adapters.postgres.repository import PostgresOutboxWorkerRepository
from propertyai_core.adapters.postgres.worker_pool import PostgresWorkerPool
from propertyai_core.global_writer import assert_current_production_writer, mutation_scope
from propertyai_core.ports.cleaner_repository import OutboxClaim, OutboxReconciliationState
from propertyai_core.runtime.cleaner_projection_adapters import (
    CALENDAR_RESOURCE_CODE,
    CalendarCurrentStateProjectionAdapter,
    ExternalResourceSnapshot,
    NotionCurrentStateProjectionAdapter,
    _calendar_cleaning_desired,
    _notion_cleaning_desired,
    _notion_reservation_desired,
    _readback_exact,
)
from propertyai_core.runtime.cleaner_projection_clients import (
    GoogleCleaningCalendarClient,
    NotionCurrentStateClient,
)


OPERATION_ID = "CHAT.PROJ.HQ:TK43:DL98:POST_PONR_PROJECTION_CLOSURE:V1"
COMMAND_ID = UUID("9a33e4f1-04a1-4618-80d9-e47546cf2b6c")
RESERVATION_ID = UUID("d8817ac7-9d65-469b-8200-80efe29041b1")
CLEANING_ID = UUID("e00e0713-da99-43a2-89a5-346347f5a674")
DOMAIN_EVENT_ID = 1
EXPECTED_ROWS = {
    UUID("d4b4db22-6ecc-4232-b3a5-ca033334b2aa"): "CALENDAR_PROJECTION",
    UUID("d7d6196e-a7bf-4d83-8a34-6ea942a92da5"): "NOTION_PROJECTION",
}

ALREADY_EXTERNALLY_APPLIED = "ALREADY_EXTERNALLY_APPLIED"
NOT_EXTERNALLY_APPLIED = "NOT_EXTERNALLY_APPLIED"
AMBIGUOUS = "AMBIGUOUS"


class ProjectionReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectionReconciliationPlan:
    outbox_id: str
    destination_type: str
    classification: str
    external_resource_ids: tuple[str, ...]
    reason: str


class ExactProjectionReconciliation:
    def __init__(
        self,
        repository: PostgresOutboxWorkerRepository,
        *,
        notion_client: NotionCurrentStateClient,
        calendar_client: GoogleCleaningCalendarClient,
    ) -> None:
        self.repository = repository
        self.notion_client = notion_client
        self.calendar_client = calendar_client
        self.notion_adapter = NotionCurrentStateProjectionAdapter(repository, notion_client)
        self.calendar_adapter = CalendarCurrentStateProjectionAdapter(repository, calendar_client)

    @staticmethod
    def _validate_row(row: OutboxReconciliationState, destination: str) -> None:
        expected_key = (
            f"CLEANER_OUTBOX:{COMMAND_ID}:RESERVATION_INGESTED:{destination}"
        )
        if (
            row.outbox_id not in EXPECTED_ROWS
            or EXPECTED_ROWS[row.outbox_id] != destination
            or row.outbox_status not in {"PENDING_RECONCILIATION", "SUCCEEDED"}
            or row.domain_event_id != DOMAIN_EVENT_ID
            or row.event_type != "RESERVATION_INGESTED"
            or row.aggregate_type != "RESERVATION"
            or row.aggregate_id != RESERVATION_ID
            or row.idempotency_key != expected_key
            or row.attempt_count != 1
            or row.max_attempts != 5
            or row.lease_fence != 1
            or (
                row.outbox_status == "PENDING_RECONCILIATION"
                and row.external_effect_id is not None
            )
            or (
                row.outbox_status == "SUCCEEDED"
                and not row.external_effect_id
            )
            or str(row.payload.get("reservation_id")) != str(RESERVATION_ID)
            or str(row.payload.get("cleaning_id")) != str(CLEANING_ID)
        ):
            raise ProjectionReconciliationError("EXACT_OUTBOX_ROW_CONTRACT_MISMATCH")

    def _row(self, outbox_id: UUID, destination: str) -> OutboxReconciliationState:
        row = self.repository.read_outbox_reconciliation(outbox_id)
        if row is None:
            raise ProjectionReconciliationError("EXACT_OUTBOX_ROW_MISSING")
        self._validate_row(row, destination)
        return row

    @staticmethod
    def _one_or_none(
        snapshots: Sequence[ExternalResourceSnapshot], *, reason: str
    ) -> ExternalResourceSnapshot | None:
        if len(snapshots) > 1:
            raise ProjectionReconciliationError(reason)
        return snapshots[0] if snapshots else None

    def _notion_snapshot(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        resource_kind: str,
        identity: Mapping[str, Any],
    ) -> ExternalResourceSnapshot | None:
        binding = self.repository.resource_binding(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            destination_type="NOTION",
            resource_code=resource_kind,
        )
        if binding is not None:
            snapshot = self.notion_client.read(
                resource_kind, str(binding["external_resource_id"])
            )
            if snapshot is None:
                raise ProjectionReconciliationError("NOTION_BOUND_RESOURCE_MISSING")
            return snapshot
        return self._one_or_none(
            tuple(self.notion_client.find(resource_kind, identity)),
            reason=f"NOTION_{resource_kind}_IDENTITY_AMBIGUOUS",
        )

    def _plan_notion(self, row: OutboxReconciliationState) -> ProjectionReconciliationPlan:
        reservation = self.repository.reservation_projection_state(RESERVATION_ID)
        cleaning = self.repository.cleaning_projection_state(CLEANING_ID)
        reservation_desired = _notion_reservation_desired(reservation)
        cleaning_desired = _notion_cleaning_desired(cleaning)
        reservation_snapshot = self._notion_snapshot(
            aggregate_type="RESERVATION",
            aggregate_id=RESERVATION_ID,
            resource_kind="RESERVATION",
            identity={
                "reservation_id": str(RESERVATION_ID),
                "reservation_code": str(reservation["reservation_code"]),
            },
        )
        cleaning_snapshot = self._notion_snapshot(
            aggregate_type="CLEANING",
            aggregate_id=CLEANING_ID,
            resource_kind="CLEANING",
            identity={
                "cleaning_id": str(CLEANING_ID),
                "reservation_code": cleaning.get("reservation_code"),
                "service_window_start_at": cleaning_desired.get("service_window_start_at"),
            },
        )
        exact = _readback_exact(reservation_snapshot, reservation_desired) and _readback_exact(
            cleaning_snapshot, cleaning_desired
        )
        external_ids = tuple(
            snapshot.external_id
            for snapshot in (reservation_snapshot, cleaning_snapshot)
            if snapshot is not None
        )
        return ProjectionReconciliationPlan(
            str(row.outbox_id),
            row.destination_type,
            ALREADY_EXTERNALLY_APPLIED if exact else NOT_EXTERNALLY_APPLIED,
            external_ids,
            "BOTH_NOTION_RESOURCES_EXACT" if exact else "NOTION_CURRENT_STATE_INCOMPLETE_OR_DRIFTED",
        )

    def _plan_calendar(self, row: OutboxReconciliationState) -> ProjectionReconciliationPlan:
        cleaning = self.repository.cleaning_projection_state(CLEANING_ID)
        desired = _calendar_cleaning_desired(cleaning)
        identity = {
            "resource_code": CALENDAR_RESOURCE_CODE,
            "cleaning_id": str(CLEANING_ID),
            "service_window_start_at": desired.get("service_window_start_at"),
        }
        binding = self.repository.resource_binding(
            aggregate_type="CLEANING",
            aggregate_id=CLEANING_ID,
            destination_type="CALENDAR",
            resource_code=CALENDAR_RESOURCE_CODE,
        )
        if binding is not None:
            snapshot = self.calendar_client.read(str(binding["external_resource_id"]))
            if snapshot is None:
                matches = tuple(self.calendar_client.find(identity))
                snapshot = self._one_or_none(
                    matches, reason="CALENDAR_RESOURCE_IDENTITY_AMBIGUOUS"
                )
        else:
            snapshot = self._one_or_none(
                tuple(self.calendar_client.find(identity)),
                reason="CALENDAR_RESOURCE_IDENTITY_AMBIGUOUS",
            )
        exact = _readback_exact(snapshot, desired)
        return ProjectionReconciliationPlan(
            str(row.outbox_id),
            row.destination_type,
            ALREADY_EXTERNALLY_APPLIED if exact else NOT_EXTERNALLY_APPLIED,
            () if snapshot is None else (snapshot.external_id,),
            "CALENDAR_RESOURCE_EXACT" if exact else "CALENDAR_CURRENT_STATE_ABSENT_OR_DRIFTED",
        )

    def plan(self) -> tuple[ProjectionReconciliationPlan, ...]:
        plans = []
        for outbox_id, destination in sorted(
            EXPECTED_ROWS.items(), key=lambda item: item[1]
        ):
            row = self._row(outbox_id, destination)
            try:
                plan = (
                    self._plan_calendar(row)
                    if destination == "CALENDAR_PROJECTION"
                    else self._plan_notion(row)
                )
            except ProjectionReconciliationError as error:
                plan = ProjectionReconciliationPlan(
                    str(outbox_id), destination, AMBIGUOUS, (), str(error)
                )
            plans.append(plan)
        return tuple(plans)

    @staticmethod
    def _claim(row: OutboxReconciliationState) -> OutboxClaim:
        return OutboxClaim(
            outbox_id=row.outbox_id,
            event_type=row.event_type,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            destination_type=row.destination_type,
            destination_ref=row.destination_ref,
            payload=row.payload,
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            lease_owner="FACTUAL_RECONCILIATION",
            lease_until=datetime.now(timezone.utc),
            lease_fence=row.lease_fence,
            external_effect_id=row.external_effect_id,
        )

    def apply(self, *, operation_id: str) -> tuple[ProjectionReconciliationPlan, ...]:
        if operation_id != OPERATION_ID:
            raise ProjectionReconciliationError("OPERATION_ID_MISMATCH")
        initial = self.plan()
        if any(plan.classification == AMBIGUOUS for plan in initial):
            raise ProjectionReconciliationError("EXTERNAL_RECONCILIATION_AMBIGUOUS")
        initial_rows = {
            outbox_id: self._row(outbox_id, destination)
            for outbox_id, destination in EXPECTED_ROWS.items()
        }
        if any(
            row.outbox_status == "SUCCEEDED"
            and next(plan for plan in initial if plan.outbox_id == str(outbox_id)).classification
            != ALREADY_EXTERNALLY_APPLIED
            for outbox_id, row in initial_rows.items()
        ):
            raise ProjectionReconciliationError("SUCCEEDED_OUTBOX_EXTERNAL_READBACK_DRIFT")
        if all(row.outbox_status == "SUCCEEDED" for row in initial_rows.values()):
            return initial
        with mutation_scope(
            "W07",
            unit_id=operation_id,
            operation_class="CLEANER_POST_PONR_PROJECTION_RECONCILIATION_EXACT_TWO",
            target=",".join(str(value) for value in sorted(EXPECTED_ROWS, key=str)),
        ):
            # Re-observe both destinations while the exact writer lease is current.
            current = self.plan()
            if any(plan.classification == AMBIGUOUS for plan in current):
                raise ProjectionReconciliationError("EXTERNAL_RECONCILIATION_AMBIGUOUS")
            for plan in current:
                outbox_id = UUID(plan.outbox_id)
                row = self._row(outbox_id, plan.destination_type)
                if row.outbox_status == "SUCCEEDED":
                    if plan.classification != ALREADY_EXTERNALLY_APPLIED:
                        raise ProjectionReconciliationError(
                            "SUCCEEDED_OUTBOX_EXTERNAL_READBACK_DRIFT"
                        )
                    continue
                assert_current_production_writer()
                receipt = (
                    self.calendar_adapter.deliver(self._claim(row))
                    if plan.destination_type == "CALENDAR_PROJECTION"
                    else self.notion_adapter.deliver(self._claim(row))
                )
                assert_current_production_writer()
                resolution_code = (
                    "FACTUALLY_RECONCILED_ALREADY_APPLIED"
                    if plan.classification == ALREADY_EXTERNALLY_APPLIED
                    else "FACTUALLY_RECONCILED_AFTER_ABSENCE_OR_DRIFT_PROOF"
                )
                applied = self.repository.resolve_outbox_reconciliation(
                    outbox_id,
                    expected_lease_fence=row.lease_fence,
                    resolution="SUCCEEDED",
                    external_effect_id=receipt.external_effect_id,
                    error_code=resolution_code,
                    retry_delay_seconds=0,
                )
                if not applied:
                    raise ProjectionReconciliationError("OUTBOX_RECONCILIATION_CAS_REJECTED")
                readback = self.repository.read_outbox_reconciliation(outbox_id)
                if (
                    readback is None
                    or readback.outbox_status != "SUCCEEDED"
                    or readback.external_effect_id != receipt.external_effect_id
                    or readback.attempt_count != 1
                    or readback.lease_fence != 1
                ):
                    raise ProjectionReconciliationError("OUTBOX_RECONCILIATION_READBACK_MISMATCH")
            return current


def _run(*, apply: bool, operation_id: str) -> dict[str, Any]:
    environment = os.environ
    pool = PostgresWorkerPool(environment=environment)
    pool.open()
    try:
        repository = PostgresOutboxWorkerRepository(pool)
        service = ExactProjectionReconciliation(
            repository,
            notion_client=NotionCurrentStateClient(environment=environment),
            calendar_client=GoogleCleaningCalendarClient(environment=environment),
        )
        plans = service.apply(operation_id=operation_id) if apply else service.plan()
        return {
            "operation_id": operation_id,
            "mode": "APPLY" if apply else "PLAN",
            "plans": [asdict(plan) for plan in plans],
        }
    finally:
        pool.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.operation_id != OPERATION_ID:
        raise SystemExit("OPERATION_ID_MISMATCH")
    print(json.dumps(_run(apply=args.apply, operation_id=args.operation_id), sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = [
    "ALREADY_EXTERNALLY_APPLIED",
    "AMBIGUOUS",
    "NOT_EXTERNALLY_APPLIED",
    "ExactProjectionReconciliation",
    "OPERATION_ID",
    "ProjectionReconciliationError",
    "ProjectionReconciliationPlan",
]

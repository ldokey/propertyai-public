from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID


@dataclass(frozen=True)
class CanonicalReservationSnapshot:
    """Normalized reservation reference supplied by the Global Reservation Master.

    ``source_version`` is deliberately absent. PostgreSQL owns the contiguous
    Cleaner-local reference revision and the repository derives it transactionally.
    """

    reservation_id: UUID
    reservation_code: str
    property_id: UUID
    rental_unit_id: UUID | None
    source_channel: str | None
    external_reservation_id: str | None
    reservation_status: str
    check_in_at: datetime | None
    check_out_at: datetime


@dataclass(frozen=True)
class ReservationIngestResult:
    reservation_id: UUID
    source_version: int
    changed: bool
    created: bool


@dataclass(frozen=True)
class CommandReceiptInput:
    command_id: UUID
    authority_scope_code: str
    command_type: str
    idempotency_key: str
    request_payload: Mapping[str, Any]
    source_channel_code: str
    authority_epoch: int
    decided_at: datetime
    principal_type: str = "SYSTEM"
    source_stream_key: str | None = None
    source_event_id: str | None = None
    actor_party_id: UUID | None = None
    actor_external_identity_id: UUID | None = None
    source_observed_at: datetime | None = None


@dataclass(frozen=True)
class CommandReceiptResult:
    command_id: UUID
    reused: bool


@dataclass(frozen=True)
class OutboxMessage:
    outbox_id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    destination_type: str
    available_at: datetime
    idempotency_key: str
    payload: Mapping[str, Any]
    max_attempts: int
    domain_event_id: int | None = None
    destination_ref: str | None = None


@dataclass(frozen=True)
class OutboxEnqueueResult:
    outbox_id: UUID
    reused: bool


@dataclass(frozen=True)
class OutboxClaim:
    outbox_id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    destination_type: str
    destination_ref: str | None
    payload: Mapping[str, Any]
    attempt_count: int
    max_attempts: int
    lease_owner: str
    lease_until: datetime
    lease_fence: int
    external_effect_id: str | None = None
    last_error_code: str | None = None


@dataclass(frozen=True)
class OutboxReconciliationState:
    outbox_id: UUID
    domain_event_id: int | None
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    destination_type: str
    destination_ref: str | None
    outbox_status: str
    idempotency_key: str
    payload: Mapping[str, Any]
    attempt_count: int
    max_attempts: int
    lease_fence: int
    external_effect_id: str | None
    last_error_code: str | None


class CleanerTransactionPort(Protocol):
    def verify_authority_epoch(self, scope_code: str, expected_epoch: int) -> int: ...

    def find_command_receipt(
        self, authority_scope_code: str, command_type: str, idempotency_key: str
    ) -> UUID | None: ...

    def find_reservation_by_source_code(
        self, source_channel: str, reservation_code: str
    ) -> Mapping[str, Any] | None: ...

    def register_command_receipt(self, receipt: CommandReceiptInput) -> CommandReceiptResult: ...

    def ingest_canonical_reservation(
        self, snapshot: CanonicalReservationSnapshot
    ) -> ReservationIngestResult: ...

    def enqueue_outbox(self, message: OutboxMessage) -> OutboxEnqueueResult: ...


class CleanerRepositoryPort(Protocol):
    def ingest_canonical_reservation(
        self, snapshot: CanonicalReservationSnapshot
    ) -> ReservationIngestResult: ...


class OutboxWorkerPort(Protocol):
    def claim_outbox(self, worker_id: str, *, limit: int, lease_seconds: int) -> Sequence[OutboxClaim]: ...

    def complete_outbox(
        self, outbox_id: UUID, *, worker_id: str, lease_fence: int, external_effect_id: str | None
    ) -> bool: ...

    def fail_outbox(
        self,
        outbox_id: UUID,
        *,
        worker_id: str,
        lease_fence: int,
        error_code: str,
        retry_delay_seconds: int,
    ) -> bool: ...

    def mark_outbox_pending_reconciliation(
        self,
        outbox_id: UUID,
        *,
        worker_id: str,
        lease_fence: int,
        error_code: str,
        external_effect_id: str | None,
    ) -> bool: ...

    def read_outbox_reconciliation(
        self, outbox_id: UUID
    ) -> OutboxReconciliationState | None: ...

    def resolve_outbox_reconciliation(
        self,
        outbox_id: UUID,
        *,
        expected_lease_fence: int,
        resolution: str,
        external_effect_id: str | None,
        error_code: str | None,
        retry_delay_seconds: int,
    ) -> bool: ...


    def begin_outbox_delivery(
        self, claim: OutboxClaim, *, worker_id: str, error_code: str
    ) -> OutboxReconciliationState | None: ...

    def resolve_outbox_reconciliation_exact(
        self, expected: OutboxReconciliationState, *, resolution: str,
        external_effect_id: str | None, error_code: str | None,
        retry_delay_seconds: int = 0, confirmed_no_effect: bool = False,
    ) -> OutboxReconciliationState | None: ...

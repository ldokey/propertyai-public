"""Sealed one-item PostgreSQL integration_outbox worker for Cleaner Production effects."""

from __future__ import annotations

from propertyai_core.runtime.outbox_reconciliation import confirmed_no_effect_retry_allowed

from dataclasses import dataclass, replace
from typing import Protocol

from propertyai_core.global_writer import assert_current_production_writer, mutation_scope
from propertyai_core.ports.cleaner_repository import OutboxClaim, OutboxWorkerPort


ALLOWED_DESTINATIONS = frozenset({"NOTION_PROJECTION", "CALENDAR_PROJECTION", "TELEGRAM_PROJECTION"})
ALLOWED_EVENT_DESTINATIONS = frozenset({
    ("RESERVATION_INGESTED", "NOTION_PROJECTION"),
    ("RESERVATION_INGESTED", "CALENDAR_PROJECTION"),
    ("RESERVATION_CANCELLED", "NOTION_PROJECTION"),
    ("RESERVATION_CANCELLED", "CALENDAR_PROJECTION"),
    ("CLEANING_ASSIGNMENT_OFFER_OPENED", "TELEGRAM_PROJECTION"),
    ("CLEANING_ASSIGNMENT_ACCEPTED", "NOTION_PROJECTION"),
    ("CLEANING_ASSIGNMENT_ACCEPTED", "TELEGRAM_PROJECTION"),
    ("CLEANING_ASSIGNMENT_DECLINED", "TELEGRAM_PROJECTION"),
    ("CLEANER_UNAVAILABLE_CONFIRMED", "NOTION_PROJECTION"),
    ("CLEANER_UNAVAILABLE_CONFIRMED", "TELEGRAM_PROJECTION"),
    ("CLEANER_REASSIGNMENT_REQUESTED", "TELEGRAM_PROJECTION"),
    ("ORIGINAL_CLEANER_REASSIGNED", "NOTION_PROJECTION"),
    ("ORIGINAL_CLEANER_REASSIGNED", "TELEGRAM_PROJECTION"),
    ("CLEANER_REPLACEMENT_CONTINUES", "TELEGRAM_PROJECTION"),
    ("CLEANING_COMPLETED", "NOTION_PROJECTION"),
    ("CLEANING_COMPLETED", "TELEGRAM_PROJECTION"),
})


class CleanerOutboxDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryReceipt:
    external_effect_id: str | None
    confirmed_no_effect: bool = False


class CleanerProjectionAdapter(Protocol):
    def deliver(self, claim: OutboxClaim) -> DeliveryReceipt: ...


@dataclass(frozen=True)
class OutboxRunResult:
    status: str
    outbox_id: str | None = None
    destination_type: str | None = None
    event_type: str | None = None
    diagnostic_code: str | None = None
    external_effect_id: str | None = None


class CleanerPostgresOutboxWorker:
    """Claim and deliver at most one exact outbox item per W07 global-writer lease."""

    def __init__(
        self,
        repository: OutboxWorkerPort,
        adapter: CleanerProjectionAdapter,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        retry_delay_seconds: int = 30,
    ) -> None:
        if not worker_id or worker_id != worker_id.strip():
            raise ValueError("worker_id must be one exact non-blank identifier")
        if lease_seconds <= 0 or retry_delay_seconds < 0:
            raise ValueError("invalid outbox lease/retry timing")
        self.repository = repository
        self.adapter = adapter
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds

    @staticmethod
    def _validate_claim(claim: OutboxClaim) -> None:
        if claim.destination_type not in ALLOWED_DESTINATIONS:
            raise CleanerOutboxDeliveryError("OUTBOX_DESTINATION_NOT_ALLOWLISTED")
        if (claim.event_type, claim.destination_type) not in ALLOWED_EVENT_DESTINATIONS:
            raise CleanerOutboxDeliveryError("OUTBOX_EVENT_DESTINATION_NOT_ALLOWLISTED")
        if claim.attempt_count <= 0 or claim.max_attempts <= 0 or claim.attempt_count > claim.max_attempts:
            raise CleanerOutboxDeliveryError("OUTBOX_ATTEMPT_STATE_INVALID")
        if claim.lease_fence <= 0:
            raise CleanerOutboxDeliveryError("OUTBOX_LEASE_FENCE_INVALID")

    def run_once(self) -> OutboxRunResult:
        with mutation_scope(
            "W07", unit_id=f"pg-outbox:{self.worker_id}",
            operation_class="CLEANER_POSTGRES_OUTBOX_DELIVERY_ONE",
            target="propertyai.integration_outbox",
        ):
            assert_current_production_writer()
            claims = tuple(self.repository.claim_outbox(self.worker_id, limit=1, lease_seconds=self.lease_seconds))
            if not claims:
                return OutboxRunResult(status="IDLE")
            if len(claims) != 1:
                raise CleanerOutboxDeliveryError("OUTBOX_CLAIM_CARDINALITY_INVALID")
            claim = claims[0]
            result_identity = dict(outbox_id=str(claim.outbox_id), destination_type=claim.destination_type,
                                   event_type=claim.event_type)
            try:
                self._validate_claim(claim)
                if claim.lease_owner != self.worker_id:
                    raise CleanerOutboxDeliveryError("OUTBOX_CLAIM_OWNER_MISMATCH")
            except CleanerOutboxDeliveryError as error:
                # No adapter has been entered. The existing pre-delivery failure
                # primitive remains fenced; a retry still needs a no-effect permit.
                assert_current_production_writer()
                failed = self.repository.fail_outbox(
                    claim.outbox_id, worker_id=self.worker_id, lease_fence=claim.lease_fence,
                    error_code=type(error).__name__[:80], retry_delay_seconds=self.retry_delay_seconds)
                if not failed:
                    raise CleanerOutboxDeliveryError("OUTBOX_FAIL_FENCE_REJECTED") from error
                return OutboxRunResult(status="RETRY_SCHEDULED", **result_identity)

            # A reclaimed RUNNING row may have been produced by the old worker,
            # which did not persist uncertainty before sending. Attempt>1 is not
            # proof of no effect. Only an exact next-attempt/fence permit from the
            # evidence service can authorize another delivery.
            permitted = claim.attempt_count == 1 or confirmed_no_effect_retry_allowed(claim)
            code = "DELIVERY_INTENT_UNRESOLVED" if permitted else "RECLAIMED_OUTCOME_REQUIRES_EVIDENCE"
            assert_current_production_writer()
            pending = self.repository.begin_outbox_delivery(claim, worker_id=self.worker_id, error_code=code)
            if pending is None:
                raise CleanerOutboxDeliveryError("OUTBOX_DELIVERY_INTENT_FENCE_REJECTED")
            if not permitted:
                return OutboxRunResult(status="PENDING_RECONCILIATION", diagnostic_code=code, **result_identity)

            # The durable intent is already non-reclaimable. SIGKILL, interpreter
            # loss and lost acknowledgements therefore cannot open a blind resend
            # window. Uncertainty remains pending even when this except cannot run.
            external_effect_id = None
            resolution_code = None
            no_effect = False
            try:
                assert_current_production_writer()
                receipt = self.adapter.deliver(claim)
                # Existing destination adapters return ProjectionDeliveryReceipt,
                # not this module's convenience dataclass. Validate the port's
                # fields, and require an explicit no-effect receipt for accepted
                # Calendar no-ops; missing acknowledgements are never success.
                external_effect_id = getattr(receipt, "external_effect_id", None)
                no_effect = getattr(receipt, "confirmed_no_effect", False) is True
                if no_effect:
                    if external_effect_id is not None:
                        raise CleanerOutboxDeliveryError("OUTBOX_CONFLICTING_PROVIDER_RECEIPT")
                elif (not isinstance(external_effect_id, str) or not external_effect_id
                      or external_effect_id != external_effect_id.strip()):
                    raise CleanerOutboxDeliveryError("OUTBOX_PROVIDER_RECEIPT_REQUIRED")
                assert_current_production_writer()
                resolution_code = f"DELIVERY_{'NO_EFFECT' if no_effect else 'EFFECT'}_CONFIRMED:{claim.attempt_count}:{claim.lease_fence}"
                completed = self.repository.resolve_outbox_reconciliation_exact(
                    pending, resolution="SUCCEEDED", external_effect_id=external_effect_id,
                    error_code=resolution_code,
                    retry_delay_seconds=0, confirmed_no_effect=no_effect)
                if completed is None:
                    raise CleanerOutboxDeliveryError("OUTBOX_COMPLETE_FENCE_REJECTED_AFTER_DELIVERY")
                return OutboxRunResult(status="COMPLETED", **result_identity)
            except Exception as error:
                # A commit acknowledgement can be lost after PostgreSQL has
                # durably completed the exact resolution. A read-only full-row
                # match may prove completion; it never authorizes another send.
                if resolution_code is not None:
                    try:
                        observed = self.repository.read_outbox_reconciliation(claim.outbox_id)
                    except Exception:
                        observed = None
                    expected = replace(pending, outbox_status="SUCCEEDED", last_error_code=resolution_code,
                                       external_effect_id=external_effect_id if external_effect_id is not None else pending.external_effect_id)
                    if observed == expected:
                        return OutboxRunResult(status="COMPLETED", diagnostic_code="COMPLETED_BY_DURABLE_READBACK",
                                               external_effect_id=external_effect_id, **result_identity)
                # Do not mutate using lost global authority or replay a send. The
                # pre-effect durable intent is the crash-safe diagnostic owner.
                return OutboxRunResult(status="PENDING_RECONCILIATION",
                                       diagnostic_code=f"EXTERNAL_EFFECT_UNCERTAIN:{type(error).__name__}"[:160],
                                       external_effect_id=external_effect_id if isinstance(external_effect_id, str) else None,
                                       **result_identity)

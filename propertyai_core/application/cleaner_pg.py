"""Cleaner PostgreSQL application transaction service for the bounded M1 ingress.

Business workflow decisions stay here. PostgreSQL repository methods are persistence
primitives and V2.2.1 remains the invariant/concurrency fence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Mapping
from uuid import UUID, uuid4

from propertyai_core.adapters.postgres.repository import PostgresCleanerRepository
from propertyai_core.ports.cleaner_repository import (
    CanonicalReservationSnapshot,
    CommandReceiptInput,
    OutboxMessage,
)


CLEANER_AUTHORITY_SCOPE = "CLEANER_SCHEDULING"

# DL-78: every actual Cleaner domain event has one explicit accepted destination set.
# This is producer/application policy; W07 may deliver it but may never invent it.
EVENT_DESTINATIONS: dict[str, tuple[str, ...]] = {
    "RESERVATION_INGESTED": ("NOTION_PROJECTION", "CALENDAR_PROJECTION"),
    "RESERVATION_CANCELLED": ("NOTION_PROJECTION", "CALENDAR_PROJECTION"),
    "CLEANING_ASSIGNMENT_OFFER_OPENED": ("TELEGRAM_PROJECTION",),
    "CLEANING_ASSIGNMENT_ACCEPTED": ("NOTION_PROJECTION", "TELEGRAM_PROJECTION"),
    "CLEANING_ASSIGNMENT_DECLINED": ("TELEGRAM_PROJECTION",),
    "CLEANER_UNAVAILABLE_CONFIRMED": ("NOTION_PROJECTION", "TELEGRAM_PROJECTION"),
    "CLEANER_REASSIGNMENT_REQUESTED": ("TELEGRAM_PROJECTION",),
    "ORIGINAL_CLEANER_REASSIGNED": ("NOTION_PROJECTION", "TELEGRAM_PROJECTION"),
    "CLEANER_REPLACEMENT_CONTINUES": ("TELEGRAM_PROJECTION",),
    "CLEANING_COMPLETED": ("NOTION_PROJECTION", "TELEGRAM_PROJECTION"),
}


class FirstAuthoritativeReservationAlreadyPresent(RuntimeError):
    """The selected future first PG command is no longer absent in authoritative state."""


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _uuid_text(value: UUID | None) -> str | None:
    return None if value is None else str(value)


@dataclass(frozen=True)
class CommandMetadata:
    idempotency_key: str
    source_channel_code: str
    source_stream_key: str
    source_event_id: str
    authority_epoch: int
    decided_at: datetime
    source_observed_at: datetime | None = None
    actor_party_id: UUID | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("idempotency_key", self.idempotency_key),
            ("source_channel_code", self.source_channel_code),
            ("source_stream_key", self.source_stream_key),
            ("source_event_id", self.source_event_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must be nonblank")
        if self.authority_epoch < 0:
            raise ValueError("authority_epoch must be nonnegative")
        _aware(self.decided_at, "decided_at")
        if self.source_observed_at is not None:
            _aware(self.source_observed_at, "source_observed_at")


@dataclass(frozen=True)
class ReservationIngressCommand:
    metadata: CommandMetadata
    source_channel: str
    external_reservation_id: str
    reservation_code: str
    property_id: UUID
    rental_unit_id: UUID | None
    reservation_status: str
    check_in_at: datetime | None
    check_out_at: datetime
    cleaning_code: str
    service_window_start_at: datetime
    service_deadline_at: datetime
    required_work_minutes: int

    def __post_init__(self) -> None:
        if not self.source_channel.strip() or not self.external_reservation_id.strip():
            raise ValueError("external reservation business key must be nonblank")
        if not self.reservation_code.strip() or not self.cleaning_code.strip():
            raise ValueError("reservation_code and cleaning_code must be nonblank")
        if self.reservation_status not in {"CONFIRMED", "CANCELLED"}:
            raise ValueError("reservation_status must be CONFIRMED or CANCELLED")
        if self.check_in_at is not None:
            _aware(self.check_in_at, "check_in_at")
        _aware(self.check_out_at, "check_out_at")
        _aware(self.service_window_start_at, "service_window_start_at")
        _aware(self.service_deadline_at, "service_deadline_at")
        if self.service_deadline_at <= self.service_window_start_at:
            raise ValueError("cleaning service window is invalid")
        if self.required_work_minutes <= 0:
            raise ValueError("required_work_minutes must be positive")


@dataclass(frozen=True)
class OpenAssignmentOfferCommand:
    metadata: CommandMetadata
    cleaning_id: UUID
    cleaner_party_id: UUID
    base_fee_krw: int
    replacement_urgency: str = "NORMAL"
    urgent_premium_krw: int = 0
    urgent_premium_policy_version: str | None = None
    tier_no: int = 1
    acceptance_cutoff_at: datetime | None = None


@dataclass(frozen=True)
class AcceptAssignmentCommand:
    metadata: CommandMetadata
    campaign_id: UUID
    offer_candidate_id: UUID
    cleaner_party_id: UUID


@dataclass(frozen=True)
class DeclineAssignmentCommand:
    metadata: CommandMetadata
    campaign_id: UUID
    offer_candidate_id: UUID
    cleaner_party_id: UUID


@dataclass(frozen=True)
class MarkUnavailableCommand:
    metadata: CommandMetadata
    assignment_id: UUID
    availability_classification: str
    replacement_urgency: str
    reason_code: str | None = None
    reason_text: str | None = None


@dataclass(frozen=True)
class RequestReassignmentCommand:
    metadata: CommandMetadata
    unavailability_id: UUID


@dataclass(frozen=True)
class ReassignOriginalCommand:
    metadata: CommandMetadata
    reassignment_request_id: UUID


@dataclass(frozen=True)
class ContinueReplacementCommand:
    metadata: CommandMetadata
    reassignment_request_id: UUID


@dataclass(frozen=True)
class CompleteCleaningCommand:
    metadata: CommandMetadata
    assignment_id: UUID


@dataclass(frozen=True)
class ReservationWorkflowResult:
    reservation_id: UUID
    source_version: int
    cleaning_id: UUID | None
    schedule_revision_id: UUID | None
    changed: bool
    created: bool


@dataclass(frozen=True)
class AssignmentOfferResult:
    campaign_id: UUID
    offer_candidate_id: UUID
    cleaning_id: UUID


class PostgresCleanerApplicationService:
    def __init__(
        self,
        repository: PostgresCleanerRepository,
        *,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self.repository = repository
        self._uuid_factory = uuid_factory

    def _receipt(self, tx, metadata: CommandMetadata, command_type: str, payload: Mapping[str, object]):
        candidate_id = self._uuid_factory()
        result = tx.register_command_receipt(
            CommandReceiptInput(
                command_id=candidate_id,
                authority_scope_code=CLEANER_AUTHORITY_SCOPE,
                command_type=command_type,
                idempotency_key=metadata.idempotency_key,
                request_payload=payload,
                source_channel_code=metadata.source_channel_code,
                source_stream_key=metadata.source_stream_key,
                source_event_id=metadata.source_event_id,
                principal_type="PARTY" if metadata.actor_party_id is not None else "SYSTEM",
                actor_party_id=metadata.actor_party_id,
                authority_epoch=metadata.authority_epoch,
                source_observed_at=metadata.source_observed_at,
                decided_at=metadata.decided_at,
            )
        )
        receipt = {
            "command_id": result.command_id,
            "decided_at": metadata.decided_at,
            "actor_party_id": metadata.actor_party_id,
        }
        return result, receipt

    def _emit(
        self,
        tx,
        *,
        receipt: Mapping[str, object],
        aggregate_type: str,
        aggregate_id: UUID,
        aggregate_version: int | None,
        event_type: str,
        payload: Mapping[str, object],
        destinations: tuple[str, ...],
        telegram_effect: Mapping[str, object] | None = None,
    ) -> int:
        expected_destinations = EVENT_DESTINATIONS.get(event_type)
        if expected_destinations is None:
            raise ValueError(f"CLEANER_EVENT_TYPE_NOT_ACCEPTED:{event_type}")
        if destinations != expected_destinations:
            raise ValueError(f"CLEANER_EVENT_DESTINATIONS_NOT_ACCEPTED:{event_type}")
        occurred_at = receipt["decided_at"]
        assert isinstance(occurred_at, datetime)
        domain_event_id = tx.append_domain_event_once(
            command_id=receipt["command_id"],
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            event_type=event_type,
            actor_party_id=receipt["actor_party_id"],
            payload=payload,
            occurred_at=occurred_at,
        )
        for destination in destinations:
            destination_payload = dict(payload)
            if destination == "TELEGRAM_PROJECTION":
                if telegram_effect is None or telegram_effect.get("effect_kind") != event_type:
                    raise ValueError("TELEGRAM_EFFECT_NOT_SEALED_BY_APPLICATION")
                sealed_effect = dict(telegram_effect)
                sealed_effect.setdefault("body", dict(payload))
                destination_payload["telegram_effect"] = sealed_effect
            tx.enqueue_outbox(
                OutboxMessage(
                    outbox_id=self._uuid_factory(),
                    domain_event_id=domain_event_id,
                    event_type=event_type,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    destination_type=destination,
                    destination_ref=None,
                    available_at=occurred_at,
                    idempotency_key=(
                        f"CLEANER_OUTBOX:{receipt['command_id']}:{event_type}:{destination}"
                    ),
                    payload=destination_payload,
                    max_attempts=5,
                )
            )
        return domain_event_id

    @staticmethod
    def _reservation_payload(command: ReservationIngressCommand) -> dict[str, object]:
        return {
            "source_channel": command.source_channel,
            "external_reservation_id": command.external_reservation_id,
            "reservation_code": command.reservation_code,
            "property_id": str(command.property_id),
            "rental_unit_id": _uuid_text(command.rental_unit_id),
            "reservation_status": command.reservation_status,
            "check_in_at": None if command.check_in_at is None else command.check_in_at.isoformat(),
            "check_out_at": command.check_out_at.isoformat(),
            "cleaning_code": command.cleaning_code,
            "service_window_start_at": command.service_window_start_at.isoformat(),
            "service_deadline_at": command.service_deadline_at.isoformat(),
            "required_work_minutes": command.required_work_minutes,
        }

    def ingest_reservation(
        self,
        command: ReservationIngressCommand,
        *,
        require_absent: bool = False,
    ) -> ReservationWorkflowResult:
        """Ingest one reservation; optionally require exact first-command absence.

        `require_absent=True` is sealed for the future first authoritative cutover command.
        Both command-receipt and Airbnb reservation absence are checked inside the same
        PostgreSQL transaction before any authoritative domain mutation can commit.
        """
        payload = self._reservation_payload(command)
        with self.repository.transaction() as tx:
            if require_absent:
                # The authority-epoch read establishes the accepted transaction lock.
                # Both exact absence facts must then be observed before the receipt
                # INSERT becomes the first authoritative mutation in this transaction.
                tx.verify_authority_epoch(CLEANER_AUTHORITY_SCOPE, command.metadata.authority_epoch)
                existing_receipt_id = tx.find_command_receipt(
                    CLEANER_AUTHORITY_SCOPE,
                    "RESERVATION_INGRESS",
                    command.metadata.idempotency_key,
                )
                if existing_receipt_id is not None:
                    raise FirstAuthoritativeReservationAlreadyPresent(
                        "FIRST_PG_COMMAND_RECEIPT_ALREADY_PRESENT"
                    )
                existing_business_key = tx.find_reservation_by_source_code(
                    command.source_channel, command.reservation_code
                )
                if existing_business_key is not None:
                    raise FirstAuthoritativeReservationAlreadyPresent(
                        "FIRST_PG_RESERVATION_ALREADY_PRESENT"
                    )

            receipt_result, receipt = self._receipt(
                tx, command.metadata, "RESERVATION_INGRESS", payload
            )
            if receipt_result.reused and require_absent:
                # A concurrent insert may win after both read-only absence checks.
                # Its unique receipt fence is authoritative: this transaction must
                # stop before any reservation/business mutation.
                raise FirstAuthoritativeReservationAlreadyPresent(
                    "FIRST_PG_COMMAND_RECEIPT_ALREADY_PRESENT"
                )
            if receipt_result.reused:
                event_type = (
                    "RESERVATION_CANCELLED"
                    if command.reservation_status == "CANCELLED"
                    else "RESERVATION_INGESTED"
                )
                prior = tx.domain_event_for_command(receipt["command_id"], event_type)
                if prior is None:
                    raise RuntimeError("reused reservation command has no committed domain event")
                prior_payload = prior["payload"]
                return ReservationWorkflowResult(
                    reservation_id=UUID(prior_payload["reservation_id"]),
                    source_version=int(prior_payload["source_version"]),
                    cleaning_id=(
                        None if prior_payload.get("cleaning_id") is None
                        else UUID(prior_payload["cleaning_id"])
                    ),
                    schedule_revision_id=(
                        None if prior_payload.get("schedule_revision_id") is None
                        else UUID(prior_payload["schedule_revision_id"])
                    ),
                    changed=False,
                    created=False,
                )
            existing = tx.find_external_reservation(
                command.source_channel, command.external_reservation_id
            )
            if existing is not None and require_absent:
                raise FirstAuthoritativeReservationAlreadyPresent(
                    "FIRST_PG_RESERVATION_ALREADY_PRESENT"
                )
            reservation_id = (
                existing["reservation_id"] if existing is not None else self._uuid_factory()
            )
            reservation = tx.ingest_external_reservation(
                CanonicalReservationSnapshot(
                    reservation_id=reservation_id,
                    reservation_code=command.reservation_code,
                    property_id=command.property_id,
                    rental_unit_id=command.rental_unit_id,
                    source_channel=command.source_channel,
                    external_reservation_id=command.external_reservation_id,
                    reservation_status=command.reservation_status,
                    check_in_at=command.check_in_at,
                    check_out_at=command.check_out_at,
                )
            )

            cleaning_id: UUID | None = None
            schedule_revision_id: UUID | None = None
            reconciliation_id: UUID | None = None
            cancelled_campaign_id: UUID | None = None
            if command.reservation_status == "CONFIRMED":
                cleaning = tx.ensure_checkout_cleaning(
                    cleaning_id=self._uuid_factory(),
                    cleaning_code=command.cleaning_code,
                    reservation_id=reservation.reservation_id,
                    property_id=command.property_id,
                    rental_unit_id=command.rental_unit_id,
                    schedule_revision_id=self._uuid_factory(),
                    service_window_start_at=command.service_window_start_at,
                    service_deadline_at=command.service_deadline_at,
                    required_work_minutes=command.required_work_minutes,
                    source_checkout_at=command.check_out_at,
                    source_reservation_version=reservation.source_version,
                    source_command_id=receipt["command_id"],
                )
                cleaning_id = cleaning["cleaning_id"]
                schedule_revision_id = cleaning["schedule_revision_id"]
                if cleaning["revision_changed"]:
                    assignment = tx.hard_booked_assignment(cleaning_id)
                    if (
                        assignment is not None
                        and assignment["schedule_revision_id"] != schedule_revision_id
                    ):
                        reconciliation = tx.ensure_schedule_reconciliation(
                            schedule_reconciliation_id=self._uuid_factory(),
                            cleaning_id=cleaning_id,
                            hard_booked_assignment_id=assignment["assignment_id"],
                            base_assignment_revision_id=assignment["schedule_revision_id"],
                            target_schedule_revision_id=schedule_revision_id,
                            reason_code="RESERVATION_SOURCE_UPDATED",
                            source_ref=f"command:{receipt['command_id']}",
                        )
                        reconciliation_id = reconciliation["schedule_reconciliation_id"]
            else:
                cleaning = tx.checkout_cleaning_for_reservation(reservation.reservation_id)
                if cleaning is not None:
                    cancelled_campaign_id = tx.cancel_open_offer_campaign(
                        cleaning["cleaning_id"],
                        cancelled_at=receipt["decided_at"],
                        reason="RESERVATION_CANCELLED",
                    )
                    assignment = tx.hard_booked_assignment(cleaning["cleaning_id"])
                    if assignment is not None:
                        tx.terminate_assignment(
                            assignment["assignment_id"],
                            status="CANCELLED",
                            ended_at=receipt["decided_at"],
                            reason="RESERVATION_CANCELLED",
                        )
                    tx.set_cleaning_status(cleaning["cleaning_id"], "CANCELLED")
                    cleaning_id = cleaning["cleaning_id"]
                    schedule_revision_id = cleaning["current_schedule_revision_id"]

            event_type = (
                "RESERVATION_CANCELLED"
                if command.reservation_status == "CANCELLED"
                else "RESERVATION_INGESTED"
            )
            event_payload = {
                **payload,
                "reservation_id": str(reservation.reservation_id),
                "source_version": reservation.source_version,
                "cleaning_id": _uuid_text(cleaning_id),
                "schedule_revision_id": _uuid_text(schedule_revision_id),
                "schedule_reconciliation_id": _uuid_text(reconciliation_id),
                "cancelled_campaign_id": _uuid_text(cancelled_campaign_id),
            }
            self._emit(
                tx,
                receipt=receipt,
                aggregate_type="RESERVATION",
                aggregate_id=reservation.reservation_id,
                aggregate_version=reservation.source_version,
                event_type=event_type,
                payload=event_payload,
                destinations=("NOTION_PROJECTION", "CALENDAR_PROJECTION"),
            )
            return ReservationWorkflowResult(
                reservation_id=reservation.reservation_id,
                source_version=reservation.source_version,
                cleaning_id=cleaning_id,
                schedule_revision_id=schedule_revision_id,
                changed=reservation.changed,
                created=reservation.created,
            )

    def open_assignment_offer(self, command: OpenAssignmentOfferCommand) -> AssignmentOfferResult:
        cutoff = command.acceptance_cutoff_at or (
            command.metadata.decided_at.replace(microsecond=0)
        )
        if cutoff <= command.metadata.decided_at:
            # Existing Telegram offers are time-bounded; the application caller must
            # provide a future cutoff rather than repository code inventing policy.
            raise ValueError("acceptance_cutoff_at must be in the future")
        if command.tier_no <= 0:
            raise ValueError("tier_no must be positive")
        if command.base_fee_krw < 0 or command.urgent_premium_krw < 0:
            raise ValueError("assignment fee cannot be negative")
        if command.replacement_urgency not in {"NORMAL", "URGENT"}:
            raise ValueError("invalid replacement urgency")
        if command.urgent_premium_krw and not command.urgent_premium_policy_version:
            raise ValueError("urgent premium requires policy version")
        payload = {
            "cleaning_id": str(command.cleaning_id),
            "cleaner_party_id": str(command.cleaner_party_id),
            "base_fee_krw": command.base_fee_krw,
            "replacement_urgency": command.replacement_urgency,
            "urgent_premium_krw": command.urgent_premium_krw,
            "urgent_premium_policy_version": command.urgent_premium_policy_version,
            "tier_no": command.tier_no,
            "acceptance_cutoff_at": cutoff.isoformat(),
        }
        with self.repository.transaction() as tx:
            receipt_result, receipt = self._receipt(
                tx, command.metadata, "OPEN_ASSIGNMENT_OFFER", payload
            )
            if receipt_result.reused:
                prior = tx.domain_event_for_command(
                    receipt["command_id"], "CLEANING_ASSIGNMENT_OFFER_OPENED"
                )
                if prior is None:
                    raise RuntimeError("reused offer command has no committed domain event")
                prior_payload = prior["payload"]
                return AssignmentOfferResult(
                    campaign_id=UUID(prior_payload["campaign_id"]),
                    offer_candidate_id=UUID(prior_payload["offer_candidate_id"]),
                    cleaning_id=UUID(prior_payload["cleaning_id"]),
                )
            current = tx.current_cleaning_schedule(command.cleaning_id)
            if current["cleaning_status"] not in {"PLANNED", "OFFERING"}:
                raise ValueError("cleaning is not offerable")
            if current["required_work_minutes"] is None:
                raise ValueError("current cleaning revision lacks work minutes")
            campaign_id = self._uuid_factory()
            candidate_id = self._uuid_factory()
            campaign_no = tx.next_campaign_number(command.cleaning_id)
            total = command.base_fee_krw + command.urgent_premium_krw
            tx.insert_offer_campaign(
                {
                    "campaign_id": campaign_id,
                    "cleaning_id": command.cleaning_id,
                    "schedule_revision_id": current["current_schedule_revision_id"],
                    "campaign_no": campaign_no,
                    "max_tier": max(1, command.tier_no),
                    "tier_expand_after_minutes": None,
                    "acceptance_cutoff_at": cutoff,
                    "base_fee_krw": command.base_fee_krw,
                    "replacement_urgency": command.replacement_urgency,
                    "urgent_premium_krw": command.urgent_premium_krw,
                    "total_agreed_fee_krw": total,
                    "urgent_premium_policy_version": command.urgent_premium_policy_version,
                    "opened_at": receipt["decided_at"],
                }
            )
            proposed_start = current["service_window_start_at"]
            # Assignment exact duration is the required work minutes; keep it inside
            # the approved service window rather than using the full deadline span.
            proposed_end = proposed_start + timedelta(minutes=int(current["required_work_minutes"]))
            tx.insert_offer_candidate(
                {
                    "offer_candidate_id": candidate_id,
                    "campaign_id": campaign_id,
                    "cleaner_party_id": command.cleaner_party_id,
                    "tier_no": command.tier_no,
                    "proposed_start_at": proposed_start,
                    "proposed_end_at": proposed_end,
                    "evaluated_at": receipt["decided_at"],
                }
            )
            tx.set_cleaning_status(command.cleaning_id, "OFFERING")
            out_payload = {
                **payload,
                "campaign_id": str(campaign_id),
                "offer_candidate_id": str(candidate_id),
                "schedule_revision_id": str(current["current_schedule_revision_id"]),
                "proposed_start_at": proposed_start.isoformat(),
                "proposed_end_at": proposed_end.isoformat(),
            }
            self._emit(
                tx,
                receipt=receipt,
                aggregate_type="CLEANING",
                aggregate_id=command.cleaning_id,
                aggregate_version=int(current["revision_no"]),
                event_type="CLEANING_ASSIGNMENT_OFFER_OPENED",
                payload=out_payload,
                destinations=("TELEGRAM_PROJECTION",),
                telegram_effect={
                    "effect_kind": "CLEANING_ASSIGNMENT_OFFER_OPENED",
                    "recipient": {"kind": "PARTY", "identity": str(command.cleaner_party_id)},
                },
            )
            return AssignmentOfferResult(campaign_id, candidate_id, command.cleaning_id)

    def accept_assignment(self, command: AcceptAssignmentCommand) -> UUID:
        payload = {
            "campaign_id": str(command.campaign_id),
            "offer_candidate_id": str(command.offer_candidate_id),
            "cleaner_party_id": str(command.cleaner_party_id),
        }
        with self.repository.transaction() as tx:
            receipt_result, receipt = self._receipt(
                tx, command.metadata, "ACCEPT_ASSIGNMENT", payload
            )
            if receipt_result.reused:
                prior = tx.domain_event_for_command(
                    receipt["command_id"], "CLEANING_ASSIGNMENT_ACCEPTED"
                )
                if prior is None:
                    raise RuntimeError("reused assignment command has no committed domain event")
                return UUID(prior["payload"]["assignment_id"])
            context = tx.offer_acceptance_context(
                command.campaign_id, command.offer_candidate_id, command.cleaner_party_id
            )
            if context["campaign_status"] != "OPEN" or context["candidate_status"] != "ELIGIBLE":
                raise ValueError("assignment offer is not open and eligible")
            existing = tx.hard_booked_assignment(context["cleaning_id"])
            if existing is not None:
                if (
                    existing["campaign_id"] == command.campaign_id
                    and existing["offer_candidate_id"] == command.offer_candidate_id
                    and existing["cleaner_party_id"] == command.cleaner_party_id
                ):
                    return existing["assignment_id"]
                raise ValueError("cleaning already has a different hard-booked assignment")
            assignment_id = self._uuid_factory()
            tx.insert_assignment(
                {
                    "assignment_id": assignment_id,
                    "cleaning_id": context["cleaning_id"],
                    "assignment_no": tx.next_assignment_number(context["cleaning_id"]),
                    "schedule_revision_id": context["schedule_revision_id"],
                    "campaign_id": command.campaign_id,
                    "offer_candidate_id": command.offer_candidate_id,
                    "accepted_proposal_version": int(context["proposal_version"]),
                    "cleaner_party_id": command.cleaner_party_id,
                    "assignment_source": "OFFER_ACCEPTED",
                    "booked_at": receipt["decided_at"],
                    "scheduled_start_at": context["proposed_start_at"],
                    "scheduled_end_at": context["proposed_end_at"],
                    "work_minutes_snapshot": int(context["required_work_minutes"]),
                    "travel_buffer_before_minutes": int(context["proposed_buffer_before_minutes"]),
                    "travel_buffer_after_minutes": int(context["proposed_buffer_after_minutes"]),
                    "buffer_basis": context["buffer_basis"],
                    "buffer_policy_ref": context["buffer_policy_ref"],
                    "base_fee_krw": int(context["base_fee_krw"]),
                    "replacement_urgency": context["replacement_urgency"],
                    "urgent_premium_krw": int(context["urgent_premium_krw"]),
                    "total_agreed_fee_krw": int(context["total_agreed_fee_krw"]),
                    "urgent_premium_policy_version": context["urgent_premium_policy_version"],
                }
            )
            tx.accept_offer_candidate(command.offer_candidate_id, receipt["decided_at"])
            tx.close_offer_campaign(command.campaign_id, receipt["decided_at"], "ACCEPTED")
            tx.set_cleaning_status(context["cleaning_id"], "ASSIGNED")
            out_payload = {**payload, "assignment_id": str(assignment_id), "cleaning_id": str(context["cleaning_id"])}
            self._emit(
                tx,
                receipt=receipt,
                aggregate_type="CLEANING_ASSIGNMENT",
                aggregate_id=assignment_id,
                aggregate_version=1,
                event_type="CLEANING_ASSIGNMENT_ACCEPTED",
                payload=out_payload,
                destinations=("NOTION_PROJECTION", "TELEGRAM_PROJECTION"),
                telegram_effect={
                    "effect_kind": "CLEANING_ASSIGNMENT_ACCEPTED",
                    "recipient": {"kind": "PARTY", "identity": str(command.cleaner_party_id)},
                },
            )
            return assignment_id

    def decline_assignment(self, command: DeclineAssignmentCommand) -> UUID:
        payload = {
            "campaign_id": str(command.campaign_id),
            "offer_candidate_id": str(command.offer_candidate_id),
            "cleaner_party_id": str(command.cleaner_party_id),
        }
        with self.repository.transaction() as tx:
            receipt_result, receipt = self._receipt(
                tx, command.metadata, "DECLINE_ASSIGNMENT", payload
            )
            if receipt_result.reused:
                prior = tx.domain_event_for_command(
                    receipt["command_id"], "CLEANING_ASSIGNMENT_DECLINED"
                )
                if prior is None:
                    raise RuntimeError("reused decline command has no committed domain event")
                return UUID(prior["payload"]["cleaning_id"])
            context = tx.offer_acceptance_context(
                command.campaign_id, command.offer_candidate_id, command.cleaner_party_id
            )
            if context["campaign_status"] != "OPEN" or context["candidate_status"] != "ELIGIBLE":
                raise ValueError("assignment offer is not open and eligible")
            tx.decline_offer_candidate(command.offer_candidate_id, receipt["decided_at"])
            self._emit(
                tx,
                receipt=receipt,
                aggregate_type="CLEANING",
                aggregate_id=context["cleaning_id"],
                aggregate_version=None,
                event_type="CLEANING_ASSIGNMENT_DECLINED",
                payload={**payload, "cleaning_id": str(context["cleaning_id"])},
                destinations=("TELEGRAM_PROJECTION",),
                telegram_effect={
                    "effect_kind": "CLEANING_ASSIGNMENT_DECLINED",
                    "recipient": {"kind": "PARTY", "identity": str(command.cleaner_party_id)},
                },
            )
            return context["cleaning_id"]

    def mark_unavailable(self, command: MarkUnavailableCommand) -> UUID:
        if command.availability_classification not in {"EARLY_UNAVAILABLE", "SAME_DAY_UNAVAILABLE"}:
            raise ValueError("invalid availability classification")
        if command.replacement_urgency not in {"NORMAL", "URGENT"}:
            raise ValueError("invalid replacement urgency")
        payload = {
            "assignment_id": str(command.assignment_id),
            "availability_classification": command.availability_classification,
            "replacement_urgency": command.replacement_urgency,
            "reason_code": command.reason_code,
            "reason_text": command.reason_text,
        }
        with self.repository.transaction() as tx:
            receipt_result, receipt = self._receipt(
                tx, command.metadata, "MARK_UNAVAILABLE", payload
            )
            if receipt_result.reused:
                prior = tx.domain_event_for_command(
                    receipt["command_id"], "CLEANER_UNAVAILABLE_CONFIRMED"
                )
                if prior is None:
                    raise RuntimeError("reused unavailable command has no committed domain event")
                return UUID(prior["payload"]["unavailability_id"])
            assignment = tx.assignment_by_id(command.assignment_id)
            existing = tx.unavailability_for_assignment(command.assignment_id)
            if existing is not None:
                return existing["unavailability_id"]
            if assignment["assignment_status"] != "HARD_BOOKED":
                raise ValueError("assignment is not hard-booked")
            unavailability_id = self._uuid_factory()
            tx.terminate_assignment(
                command.assignment_id,
                status="RELEASED",
                ended_at=receipt["decided_at"],
                reason=command.availability_classification,
            )
            tx.insert_unavailability(
                {
                    "unavailability_id": unavailability_id,
                    "cleaning_id": assignment["cleaning_id"],
                    "schedule_revision_id": assignment["schedule_revision_id"],
                    "original_assignment_id": command.assignment_id,
                    "cleaner_party_id": assignment["cleaner_party_id"],
                    "availability_classification": command.availability_classification,
                    "replacement_urgency": command.replacement_urgency,
                    "reason_code": command.reason_code,
                    "reason_text": command.reason_text,
                    "occurred_at": receipt["decided_at"],
                }
            )
            tx.set_cleaning_status(assignment["cleaning_id"], "OFFERING")
            out_payload = {
                **payload,
                "unavailability_id": str(unavailability_id),
                "cleaning_id": str(assignment["cleaning_id"]),
                "cleaner_party_id": str(assignment["cleaner_party_id"]),
            }
            self._emit(
                tx,
                receipt=receipt,
                aggregate_type="CLEANER_UNAVAILABILITY",
                aggregate_id=unavailability_id,
                aggregate_version=1,
                event_type="CLEANER_UNAVAILABLE_CONFIRMED",
                payload=out_payload,
                destinations=("NOTION_PROJECTION", "TELEGRAM_PROJECTION"),
                telegram_effect={
                    "effect_kind": "CLEANER_UNAVAILABLE_CONFIRMED",
                    "recipient": {"kind": "PARTY", "identity": str(assignment["cleaner_party_id"])},
                },
            )
            return unavailability_id

    def request_reassignment(self, command: RequestReassignmentCommand) -> UUID:
        payload = {"unavailability_id": str(command.unavailability_id)}
        with self.repository.transaction() as tx:
            receipt_result, receipt = self._receipt(
                tx, command.metadata, "REQUEST_REASSIGNMENT", payload
            )
            if receipt_result.reused:
                prior = tx.domain_event_for_command(
                    receipt["command_id"], "CLEANER_REASSIGNMENT_REQUESTED"
                )
                if prior is None:
                    raise RuntimeError("reused reassignment request has no committed domain event")
                return UUID(prior["payload"]["reassignment_request_id"])
            case = tx.unavailability_by_id(command.unavailability_id)
            existing = tx.requested_reassignment(case["cleaning_id"])
            if existing is not None:
                return existing["reassignment_request_id"]
            request_id = self._uuid_factory()
            tx.insert_reassignment_request(
                {
                    "reassignment_request_id": request_id,
                    "unavailability_id": case["unavailability_id"],
                    "cleaning_id": case["cleaning_id"],
                    "cleaner_party_id": case["cleaner_party_id"],
                    "original_assignment_id": case["original_assignment_id"],
                    "requested_schedule_revision_id": case["schedule_revision_id"],
                    "request_no": tx.next_reassignment_request_number(
                        case["cleaning_id"]
                    ),
                    "requested_at": receipt["decided_at"],
                }
            )
            self._emit(
                tx,
                receipt=receipt,
                aggregate_type="CLEANER_REASSIGNMENT",
                aggregate_id=request_id,
                aggregate_version=1,
                event_type="CLEANER_REASSIGNMENT_REQUESTED",
                payload={
                    **payload,
                    "reassignment_request_id": str(request_id),
                    "cleaning_id": str(case["cleaning_id"]),
                    "cleaner_party_id": str(case["cleaner_party_id"]),
                },
                destinations=("TELEGRAM_PROJECTION",),
                telegram_effect={
                    "effect_kind": "CLEANER_REASSIGNMENT_REQUESTED",
                    "recipient": {"kind": "OPS", "identity": "PROPERTYAI_OPS"},
                },
            )
            return request_id

    def reassign_original(self, command: ReassignOriginalCommand) -> UUID:
        payload = {"reassignment_request_id": str(command.reassignment_request_id)}
        with self.repository.transaction() as tx:
            receipt_result, receipt = self._receipt(
                tx, command.metadata, "REASSIGN_ORIGINAL", payload
            )
            if receipt_result.reused:
                prior = tx.domain_event_for_command(
                    receipt["command_id"], "ORIGINAL_CLEANER_REASSIGNED"
                )
                if prior is None:
                    raise RuntimeError("reused original reassignment has no committed domain event")
                return UUID(prior["payload"]["assignment_id"])
            request = tx.reassignment_request_by_id(command.reassignment_request_id)
            if request["request_status"] != "REQUESTED":
                existing = tx.hard_booked_assignment(request["cleaning_id"])
                if request["request_status"] == "REASSIGNED_ORIGINAL" and existing is not None:
                    return existing["assignment_id"]
                raise ValueError("reassignment request is not open")
            original = tx.assignment_by_id(request["original_assignment_id"])
            if tx.hard_booked_assignment(request["cleaning_id"]) is not None:
                raise ValueError("cleaning already has a hard-booked assignment")
            assignment_id = self._uuid_factory()
            assignment_no = tx.next_assignment_number(request["cleaning_id"])
            tx.insert_assignment(
                {
                    "assignment_id": assignment_id,
                    "cleaning_id": request["cleaning_id"],
                    "assignment_no": assignment_no,
                    "schedule_revision_id": request["requested_schedule_revision_id"],
                    "cleaner_party_id": request["cleaner_party_id"],
                    "assignment_source": "ORIGINAL_REASSIGNED",
                    "booked_at": receipt["decided_at"],
                    "scheduled_start_at": original["scheduled_start_at"],
                    "scheduled_end_at": original["scheduled_end_at"],
                    "work_minutes_snapshot": int(original["work_minutes_snapshot"]),
                    "travel_buffer_before_minutes": int(original["travel_buffer_before_minutes"]),
                    "travel_buffer_after_minutes": int(original["travel_buffer_after_minutes"]),
                    "buffer_basis": original["buffer_basis"],
                    "buffer_policy_ref": original["buffer_policy_ref"],
                    "base_fee_krw": int(original["base_fee_krw"]),
                    "replacement_urgency": original["replacement_urgency"],
                    "urgent_premium_krw": int(original["urgent_premium_krw"]),
                    "total_agreed_fee_krw": int(original["total_agreed_fee_krw"]),
                    "urgent_premium_policy_version": original["urgent_premium_policy_version"],
                }
            )
            tx.decide_reassignment_request(
                command.reassignment_request_id,
                status="REASSIGNED_ORIGINAL",
                decided_at=receipt["decided_at"],
                decision_code="ORIGINAL_CLEANER_REASSIGNED",
            )
            tx.set_cleaning_status(request["cleaning_id"], "ASSIGNED")
            self._emit(
                tx,
                receipt=receipt,
                aggregate_type="CLEANING_ASSIGNMENT",
                aggregate_id=assignment_id,
                aggregate_version=assignment_no,
                event_type="ORIGINAL_CLEANER_REASSIGNED",
                payload={
                    **payload,
                    "assignment_id": str(assignment_id),
                    "cleaning_id": str(request["cleaning_id"]),
                    "cleaner_party_id": str(request["cleaner_party_id"]),
                },
                destinations=("NOTION_PROJECTION", "TELEGRAM_PROJECTION"),
                telegram_effect={
                    "effect_kind": "ORIGINAL_CLEANER_REASSIGNED",
                    "recipient": {"kind": "PARTY", "identity": str(request["cleaner_party_id"])},
                },
            )
            return assignment_id

    def continue_replacement(self, command: ContinueReplacementCommand) -> None:
        payload = {"reassignment_request_id": str(command.reassignment_request_id)}
        with self.repository.transaction() as tx:
            receipt_result, receipt = self._receipt(
                tx, command.metadata, "CONTINUE_REPLACEMENT", payload
            )
            if receipt_result.reused:
                prior = tx.domain_event_for_command(
                    receipt["command_id"], "CLEANER_REPLACEMENT_CONTINUES"
                )
                if prior is None:
                    raise RuntimeError("reused replacement decision has no committed domain event")
                return
            request = tx.reassignment_request_by_id(command.reassignment_request_id)
            if request["request_status"] == "CONTINUE_REPLACEMENT":
                return
            tx.decide_reassignment_request(
                command.reassignment_request_id,
                status="CONTINUE_REPLACEMENT",
                decided_at=receipt["decided_at"],
                decision_code="REPLACEMENT_PATH_CONTINUES",
            )
            self._emit(
                tx,
                receipt=receipt,
                aggregate_type="CLEANER_REASSIGNMENT",
                aggregate_id=command.reassignment_request_id,
                aggregate_version=1,
                event_type="CLEANER_REPLACEMENT_CONTINUES",
                payload={
                    **payload,
                    "cleaning_id": str(request["cleaning_id"]),
                    "cleaner_party_id": str(request["cleaner_party_id"]),
                },
                destinations=("TELEGRAM_PROJECTION",),
                telegram_effect={
                    "effect_kind": "CLEANER_REPLACEMENT_CONTINUES",
                    "recipient": {"kind": "PARTY", "identity": str(request["cleaner_party_id"])},
                },
            )

    def complete_cleaning(self, command: CompleteCleaningCommand) -> UUID:
        payload = {"assignment_id": str(command.assignment_id)}
        with self.repository.transaction() as tx:
            receipt_result, receipt = self._receipt(
                tx, command.metadata, "COMPLETE_CLEANING", payload
            )
            if receipt_result.reused:
                prior = tx.domain_event_for_command(
                    receipt["command_id"], "CLEANING_COMPLETED"
                )
                if prior is None:
                    raise RuntimeError("reused completion command has no committed domain event")
                return UUID(prior["payload"]["cleaning_id"])
            assignment = tx.assignment_by_id(command.assignment_id)
            if assignment["assignment_status"] == "COMPLETED":
                return assignment["cleaning_id"]
            if assignment["assignment_status"] != "HARD_BOOKED":
                raise ValueError("assignment cannot be completed")
            tx.terminate_assignment(
                command.assignment_id,
                status="COMPLETED",
                ended_at=receipt["decided_at"],
                reason="CLEANING_COMPLETED",
            )
            tx.set_cleaning_status(assignment["cleaning_id"], "COMPLETED")
            self._emit(
                tx,
                receipt=receipt,
                aggregate_type="CLEANING",
                aggregate_id=assignment["cleaning_id"],
                aggregate_version=None,
                event_type="CLEANING_COMPLETED",
                payload={
                    **payload,
                    "cleaning_id": str(assignment["cleaning_id"]),
                    "cleaner_party_id": str(assignment["cleaner_party_id"]),
                },
                destinations=("NOTION_PROJECTION", "TELEGRAM_PROJECTION"),
                telegram_effect={
                    "effect_kind": "CLEANING_COMPLETED",
                    "recipient": {"kind": "PARTY", "identity": str(assignment["cleaner_party_id"])},
                },
            )
            return assignment["cleaning_id"]


__all__ = [
    "AcceptAssignmentCommand",
    "AssignmentOfferResult",
    "CLEANER_AUTHORITY_SCOPE",
    "EVENT_DESTINATIONS",
    "CommandMetadata",
    "CompleteCleaningCommand",
    "ContinueReplacementCommand",
    "DeclineAssignmentCommand",
    "MarkUnavailableCommand",
    "OpenAssignmentOfferCommand",
    "PostgresCleanerApplicationService",
    "ReassignOriginalCommand",
    "RequestReassignmentCommand",
    "ReservationIngressCommand",
    "ReservationWorkflowResult",
]

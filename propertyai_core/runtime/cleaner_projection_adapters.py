"""Destination-pure Cleaner projections for PostgreSQL integration_outbox.

DL-78 keeps business meaning in Domain/Application.  These adapters only reconcile
one already-selected destination against current PostgreSQL state and the existing
integration_resource_binding ledger.  They deliberately expose client protocols so
external API tests remain fake/disposable and never need live credentials.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence
from uuid import UUID

from propertyai_core.ports.cleaner_repository import OutboxClaim


NOTION_DESTINATION = "NOTION_PROJECTION"
CALENDAR_DESTINATION = "CALENDAR_PROJECTION"
TELEGRAM_DESTINATION = "TELEGRAM_PROJECTION"
CALENDAR_RESOURCE_CODE = "CAL.CLEANING"

_NOTION_EVENTS = frozenset({
    "RESERVATION_INGESTED",
    "RESERVATION_CANCELLED",
    "CLEANING_ASSIGNMENT_ACCEPTED",
    "CLEANER_UNAVAILABLE_CONFIRMED",
    "ORIGINAL_CLEANER_REASSIGNED",
    "CLEANING_COMPLETED",
})
_CALENDAR_EVENTS = frozenset({"RESERVATION_INGESTED", "RESERVATION_CANCELLED"})
_TELEGRAM_EVENTS = frozenset({
    "CLEANING_ASSIGNMENT_OFFER_OPENED",
    "CLEANING_ASSIGNMENT_ACCEPTED",
    "CLEANING_ASSIGNMENT_DECLINED",
    "CLEANER_UNAVAILABLE_CONFIRMED",
    "CLEANER_REASSIGNMENT_REQUESTED",
    "ORIGINAL_CLEANER_REASSIGNED",
    "CLEANER_REPLACEMENT_CONTINUES",
    "CLEANING_COMPLETED",
})


class CleanerProjectionError(RuntimeError):
    """Fail-closed source/runtime contract violation before an accepted effect."""


class ProjectionPendingReconciliation(RuntimeError):
    """An external effect or identity is ambiguous and must not be blindly retried."""


@dataclass(frozen=True)
class ProjectionDeliveryReceipt:
    external_effect_id: str | None
    confirmed_no_effect: bool = False


@dataclass(frozen=True)
class ExternalResourceSnapshot:
    external_id: str
    state: Mapping[str, Any]
    external_uid: str | None = None
    external_version: str | None = None


class ProjectionStatePort(Protocol):
    def reservation_projection_state(self, reservation_id: UUID) -> Mapping[str, Any]: ...
    def cleaning_projection_state(self, cleaning_id: UUID) -> Mapping[str, Any]: ...
    def resource_binding(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        destination_type: str,
        resource_code: str,
    ) -> Mapping[str, Any] | None: ...
    def bind_resource(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        destination_type: str,
        resource_code: str,
        external_resource_id: str,
        external_uid: str | None = None,
        sync_status: str = "SYNCED",
        aggregate_version: int | None = None,
        external_version: str | None = None,
    ) -> Mapping[str, Any]: ...

    def rebind_resource(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        destination_type: str,
        resource_code: str,
        expected_external_resource_id: str,
        new_external_resource_id: str,
        external_uid: str | None = None,
        sync_status: str = "SYNCED",
        aggregate_version: int | None = None,
        external_version: str | None = None,
    ) -> Mapping[str, Any]: ...


class NotionProjectionClient(Protocol):
    def find(self, resource_kind: str, identity: Mapping[str, Any]) -> Sequence[ExternalResourceSnapshot]: ...
    def read(self, resource_kind: str, external_id: str) -> ExternalResourceSnapshot | None: ...
    def create(
        self,
        resource_kind: str,
        identity: Mapping[str, Any],
        desired: Mapping[str, Any],
    ) -> ExternalResourceSnapshot: ...
    def update(
        self,
        resource_kind: str,
        external_id: str,
        desired: Mapping[str, Any],
    ) -> ExternalResourceSnapshot: ...


class CalendarProjectionClient(Protocol):
    def find(self, identity: Mapping[str, Any]) -> Sequence[ExternalResourceSnapshot]: ...
    def read(self, external_id: str) -> ExternalResourceSnapshot | None: ...
    def create(
        self, identity: Mapping[str, Any], desired: Mapping[str, Any]
    ) -> ExternalResourceSnapshot: ...
    def update(self, external_id: str, desired: Mapping[str, Any]) -> ExternalResourceSnapshot: ...
    def retire(self, external_id: str) -> None: ...


class TelegramDeliveryClient(Protocol):
    def resolve_recipient(self, effect: Mapping[str, Any]) -> str: ...
    def send(
        self,
        effect: Mapping[str, Any],
        *,
        recipient_identity: str,
        delivery_identity: str,
    ) -> str: ...


def _uuid(value: object, label: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise CleanerProjectionError(f"{label}_INVALID") from error


def _version(state: Mapping[str, Any]) -> int | None:
    raw = state.get("source_version", state.get("revision_no"))
    if raw is None:
        return None
    value = int(raw)
    return value if value > 0 else None


def _scalar(value: object) -> object:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _notion_reservation_desired(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "reservation_id": str(state["reservation_id"]),
        "reservation_code": str(state["reservation_code"]),
        "reservation_status": str(state["reservation_status"]),
        "check_in_at": _scalar(state.get("check_in_at")),
        "check_out_at": _scalar(state.get("check_out_at")),
    }


def _notion_cleaning_desired(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cleaning_id": str(state["cleaning_id"]),
        "cleaning_code": str(state["cleaning_code"]),
        "cleaning_status": str(state["cleaning_status"]),
        "service_window_start_at": _scalar(state.get("service_window_start_at")),
        "service_deadline_at": _scalar(state.get("service_deadline_at")),
    }


def _calendar_cleaning_desired(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "cleaning_id": str(state["cleaning_id"]),
        "cleaning_code": str(state["cleaning_code"]),
        "cleaning_status": str(state["cleaning_status"]),
        "service_window_start_at": _scalar(state.get("service_window_start_at")),
        "service_deadline_at": _scalar(state.get("service_deadline_at")),
    }


def _readback_exact(snapshot: ExternalResourceSnapshot | None, desired: Mapping[str, Any]) -> bool:
    return snapshot is not None and dict(snapshot.state) == dict(desired)


class NotionCurrentStateProjectionAdapter:
    """Reconcile Reservation/Cleaning current state to Notion only."""

    def __init__(self, state: ProjectionStatePort, client: NotionProjectionClient) -> None:
        self.state = state
        self.client = client

    def _reconcile(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        resource_kind: str,
        desired: Mapping[str, Any],
        identity: Mapping[str, Any],
        aggregate_version: int | None,
    ) -> str:
        binding = self.state.resource_binding(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            destination_type="NOTION",
            resource_code=resource_kind,
        )
        bound_external_id = (
            None if binding is None else str(binding["external_resource_id"])
        )
        snapshot: ExternalResourceSnapshot | None = None
        if bound_external_id is not None:
            snapshot = self.client.read(resource_kind, bound_external_id)
        if snapshot is None:
            matches = tuple(self.client.find(resource_kind, identity))
            if len(matches) > 1:
                raise ProjectionPendingReconciliation("NOTION_RESOURCE_IDENTITY_AMBIGUOUS")
            snapshot = matches[0] if matches else None
            if bound_external_id is not None and snapshot is None:
                # A missing resource behind an existing binding is not authority to create
                # a replacement blindly. Keep the exact identity for reconciliation.
                raise ProjectionPendingReconciliation("NOTION_BOUND_RESOURCE_MISSING")

        if snapshot is None:
            try:
                snapshot = self.client.create(resource_kind, identity, desired)
            except Exception as error:
                matches = tuple(self.client.find(resource_kind, identity))
                if len(matches) == 1 and _readback_exact(matches[0], desired):
                    snapshot = matches[0]
                else:
                    raise ProjectionPendingReconciliation(
                        "NOTION_CREATE_OUTCOME_AMBIGUOUS"
                    ) from error
        elif not _readback_exact(snapshot, desired):
            try:
                self.client.update(resource_kind, snapshot.external_id, desired)
            except Exception as error:
                readback = self.client.read(resource_kind, snapshot.external_id)
                if not _readback_exact(readback, desired):
                    raise ProjectionPendingReconciliation(
                        "NOTION_UPDATE_OUTCOME_AMBIGUOUS"
                    ) from error
            readback = self.client.read(resource_kind, snapshot.external_id)
            if not _readback_exact(readback, desired):
                raise ProjectionPendingReconciliation("NOTION_UPDATE_READBACK_MISMATCH")
            snapshot = readback

        if snapshot is None or not _readback_exact(snapshot, desired):
            raise ProjectionPendingReconciliation("NOTION_FINAL_READBACK_MISMATCH")
        binding_values = dict(
            aggregate_type=aggregate_type, aggregate_id=aggregate_id,
            destination_type="NOTION", resource_code=resource_kind,
            external_uid=snapshot.external_uid, aggregate_version=aggregate_version,
            external_version=snapshot.external_version,
        )
        if bound_external_id is not None and bound_external_id != snapshot.external_id:
            self.state.rebind_resource(
                **binding_values,
                expected_external_resource_id=bound_external_id,
                new_external_resource_id=snapshot.external_id,
            )
        else:
            self.state.bind_resource(
                **binding_values, external_resource_id=snapshot.external_id
            )
        return snapshot.external_id

    def deliver(self, claim: OutboxClaim) -> ProjectionDeliveryReceipt:
        if claim.destination_type != NOTION_DESTINATION or claim.event_type not in _NOTION_EVENTS:
            raise CleanerProjectionError("NOTION_EVENT_DESTINATION_NOT_ACCEPTED")
        external_ids: list[str] = []
        if claim.event_type in {"RESERVATION_INGESTED", "RESERVATION_CANCELLED"}:
            reservation_id = claim.aggregate_id
            reservation = self.state.reservation_projection_state(reservation_id)
            reservation_desired = _notion_reservation_desired(reservation)
            external_ids.append(self._reconcile(
                aggregate_type="RESERVATION",
                aggregate_id=reservation_id,
                resource_kind="RESERVATION",
                desired=reservation_desired,
                identity={
                    "resource_kind": "RESERVATION",
                    "reservation_id": str(reservation_id),
                    "reservation_code": str(reservation["reservation_code"]),
                },
                aggregate_version=int(reservation["source_version"]),
            ))
        cleaning_raw = claim.payload.get("cleaning_id")
        if cleaning_raw is not None:
            cleaning_id = _uuid(cleaning_raw, "CLEANING_ID")
            cleaning = self.state.cleaning_projection_state(cleaning_id)
            cleaning_desired = _notion_cleaning_desired(cleaning)
            external_ids.append(self._reconcile(
                aggregate_type="CLEANING",
                aggregate_id=cleaning_id,
                resource_kind="CLEANING",
                desired=cleaning_desired,
                identity={
                    "resource_kind": "CLEANING",
                    "cleaning_id": str(cleaning_id),
                    "reservation_code": cleaning.get("reservation_code"),
                    "service_window_start_at": cleaning_desired.get("service_window_start_at"),
                },
                aggregate_version=(
                    None if cleaning.get("revision_no") is None else int(cleaning["revision_no"])
                ),
            ))
        if not external_ids:
            raise CleanerProjectionError("NOTION_ACCEPTED_EVENT_HAS_NO_PROJECTABLE_RESOURCE")
        return ProjectionDeliveryReceipt("notion:" + ",".join(external_ids))


class CalendarCurrentStateProjectionAdapter:
    """Reconcile only the authoritative PG Cleaning schedule to CAL.CLEANING."""

    def __init__(self, state: ProjectionStatePort, client: CalendarProjectionClient) -> None:
        self.state = state
        self.client = client

    def deliver(self, claim: OutboxClaim) -> ProjectionDeliveryReceipt:
        if claim.destination_type != CALENDAR_DESTINATION or claim.event_type not in _CALENDAR_EVENTS:
            raise CleanerProjectionError("CALENDAR_EVENT_DESTINATION_NOT_ACCEPTED")
        cleaning_raw = claim.payload.get("cleaning_id")
        if cleaning_raw is None:
            # A cancelled Reservation may factually have had no Cleaning aggregate.
            if claim.event_type == "RESERVATION_CANCELLED":
                return ProjectionDeliveryReceipt(None, confirmed_no_effect=True)
            raise CleanerProjectionError("CALENDAR_CLEANING_ID_REQUIRED")
        cleaning_id = _uuid(cleaning_raw, "CLEANING_ID")
        current = self.state.cleaning_projection_state(cleaning_id)
        desired = _calendar_cleaning_desired(current)
        identity = {
            "resource_code": CALENDAR_RESOURCE_CODE,
            "cleaning_id": str(cleaning_id),
            "service_window_start_at": desired.get("service_window_start_at"),
        }
        binding = self.state.resource_binding(
            aggregate_type="CLEANING",
            aggregate_id=cleaning_id,
            destination_type="CALENDAR",
            resource_code=CALENDAR_RESOURCE_CODE,
        )

        if current.get("cleaning_status") == "CANCELLED":
            if binding is None:
                matches = tuple(self.client.find(identity))
                if matches:
                    # Unbound destination objects are orphans even if one candidate is found.
                    raise ProjectionPendingReconciliation("CALENDAR_ORPHAN_REQUIRES_RECONCILIATION")
                return ProjectionDeliveryReceipt(None, confirmed_no_effect=True)
            external_id = str(binding["external_resource_id"])
            external_current = self.client.read(external_id)
            if external_current is None:
                return ProjectionDeliveryReceipt(external_id)
            if claim.event_type != "RESERVATION_CANCELLED":
                raise ProjectionPendingReconciliation("CALENDAR_RETIREMENT_SEMANTIC_REQUIRED")
            try:
                self.client.retire(external_id)
            except Exception as error:
                if self.client.read(external_id) is not None:
                    raise ProjectionPendingReconciliation(
                        "CALENDAR_RETIRE_OUTCOME_AMBIGUOUS"
                    ) from error
            if self.client.read(external_id) is not None:
                raise ProjectionPendingReconciliation("CALENDAR_RETIRE_READBACK_MISMATCH")
            self.state.bind_resource(
                aggregate_type="CLEANING",
                aggregate_id=cleaning_id,
                destination_type="CALENDAR",
                resource_code=CALENDAR_RESOURCE_CODE,
                external_resource_id=external_id,
                external_uid=binding.get("external_uid"),
                sync_status="RETIRED",
                aggregate_version=(None if current.get("revision_no") is None else int(current["revision_no"])),
                external_version=binding.get("external_version"),
            )
            return ProjectionDeliveryReceipt(external_id)

        snapshot: ExternalResourceSnapshot | None = None
        bound_external_id = None if binding is None else str(binding["external_resource_id"])
        if bound_external_id is not None:
            snapshot = self.client.read(bound_external_id)
        if snapshot is None:
            matches = tuple(self.client.find(identity))
            if len(matches) > 1:
                raise ProjectionPendingReconciliation("CALENDAR_RESOURCE_IDENTITY_AMBIGUOUS")
            snapshot = matches[0] if matches else None
        if snapshot is None:
            try:
                snapshot = self.client.create(identity, desired)
            except Exception as error:
                matches = tuple(self.client.find(identity))
                if len(matches) == 1 and _readback_exact(matches[0], desired):
                    snapshot = matches[0]
                else:
                    raise ProjectionPendingReconciliation(
                        "CALENDAR_CREATE_OUTCOME_AMBIGUOUS"
                    ) from error
        elif not _readback_exact(snapshot, desired):
            try:
                self.client.update(snapshot.external_id, desired)
            except Exception as error:
                readback = self.client.read(snapshot.external_id)
                if not _readback_exact(readback, desired):
                    raise ProjectionPendingReconciliation(
                        "CALENDAR_UPDATE_OUTCOME_AMBIGUOUS"
                    ) from error
            readback = self.client.read(snapshot.external_id)
            if not _readback_exact(readback, desired):
                raise ProjectionPendingReconciliation("CALENDAR_UPDATE_READBACK_MISMATCH")
            snapshot = readback
        if snapshot is None or not _readback_exact(snapshot, desired):
            raise ProjectionPendingReconciliation("CALENDAR_FINAL_READBACK_MISMATCH")
        binding_values = dict(
            aggregate_type="CLEANING", aggregate_id=cleaning_id,
            destination_type="CALENDAR", resource_code=CALENDAR_RESOURCE_CODE,
            external_uid=snapshot.external_uid,
            aggregate_version=(None if current.get("revision_no") is None else int(current["revision_no"])),
            external_version=snapshot.external_version,
        )
        if bound_external_id is not None and bound_external_id != snapshot.external_id:
            self.state.rebind_resource(
                **binding_values,
                expected_external_resource_id=bound_external_id,
                new_external_resource_id=snapshot.external_id,
            )
        else:
            self.state.bind_resource(
                **binding_values, external_resource_id=snapshot.external_id
            )
        return ProjectionDeliveryReceipt(snapshot.external_id)


class TelegramSealedEffectAdapter:
    """Deliver only an Application-sealed Telegram effect; never derive policy."""

    def __init__(self, client: TelegramDeliveryClient) -> None:
        self.client = client

    @staticmethod
    def delivery_identity(
        claim: OutboxClaim,
        effect: Mapping[str, Any],
        recipient_identity: str,
    ) -> str:
        if not recipient_identity.strip():
            raise CleanerProjectionError("TELEGRAM_RESOLVED_RECIPIENT_INVALID")
        material = "\0".join(
            (
                str(claim.outbox_id),
                str(effect["effect_kind"]),
                recipient_identity,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def deliver(self, claim: OutboxClaim) -> ProjectionDeliveryReceipt:
        if claim.destination_type != TELEGRAM_DESTINATION or claim.event_type not in _TELEGRAM_EVENTS:
            raise CleanerProjectionError("TELEGRAM_EVENT_DESTINATION_NOT_ACCEPTED")
        effect = claim.payload.get("telegram_effect")
        if not isinstance(effect, Mapping) or effect.get("effect_kind") != claim.event_type:
            raise CleanerProjectionError("TELEGRAM_EFFECT_NOT_SEALED_BY_APPLICATION")
        recipient = effect.get("recipient")
        if not isinstance(recipient, Mapping):
            raise CleanerProjectionError("TELEGRAM_RECIPIENT_NOT_SEALED")
        kind = recipient.get("kind")
        identity = recipient.get("identity")
        if kind not in {"PARTY", "OPS"} or not isinstance(identity, str) or not identity.strip():
            raise CleanerProjectionError("TELEGRAM_RECIPIENT_IDENTITY_INVALID")
        recipient_identity = self.client.resolve_recipient(effect)
        identity_hash = self.delivery_identity(claim, effect, recipient_identity)
        if claim.external_effect_id:
            return ProjectionDeliveryReceipt(claim.external_effect_id)
        if claim.attempt_count > 1:
            # A prior worker may have sent before crashing. Telegram has no accepted
            # provider-side exact absence proof here, so never blind-resend.
            raise ProjectionPendingReconciliation("TELEGRAM_PRIOR_ATTEMPT_REQUIRES_READBACK")
        message_id = self.client.send(
            effect,
            recipient_identity=recipient_identity,
            delivery_identity=identity_hash,
        )
        if not isinstance(message_id, str) or not message_id.strip():
            raise ProjectionPendingReconciliation("TELEGRAM_DELIVERY_RECEIPT_MISSING")
        return ProjectionDeliveryReceipt(message_id)


class DestinationProjectionRouter:
    """Exact destination dispatch; one adapter can never call another destination."""

    def __init__(
        self,
        *,
        notion: NotionCurrentStateProjectionAdapter,
        calendar: CalendarCurrentStateProjectionAdapter,
        telegram: TelegramSealedEffectAdapter,
    ) -> None:
        self._adapters = {
            NOTION_DESTINATION: notion,
            CALENDAR_DESTINATION: calendar,
            TELEGRAM_DESTINATION: telegram,
        }

    def deliver(self, claim: OutboxClaim) -> ProjectionDeliveryReceipt:
        adapter = self._adapters.get(claim.destination_type)
        if adapter is None:
            raise CleanerProjectionError("DESTINATION_NOT_ALLOWLISTED")
        return adapter.deliver(claim)


__all__ = [
    "CALENDAR_DESTINATION",
    "CALENDAR_RESOURCE_CODE",
    "CalendarCurrentStateProjectionAdapter",
    "CleanerProjectionError",
    "DestinationProjectionRouter",
    "ExternalResourceSnapshot",
    "NOTION_DESTINATION",
    "NotionCurrentStateProjectionAdapter",
    "ProjectionDeliveryReceipt",
    "ProjectionPendingReconciliation",
    "TELEGRAM_DESTINATION",
    "TelegramSealedEffectAdapter",
]

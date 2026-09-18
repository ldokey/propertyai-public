from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any, Iterator, Mapping
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from propertyai_core.adapters.postgres.errors import (
    IdempotencyConflictError,
    PostgresRepositoryError,
    ReservationIdentityConflictError,
    map_postgres_error,
)
from propertyai_core.adapters.postgres.pool import PostgresStageAPool
from propertyai_core.ports.cleaner_repository import (
    CanonicalReservationSnapshot,
    CommandReceiptInput,
    CommandReceiptResult,
    OutboxClaim,
    OutboxReconciliationState,
    OutboxEnqueueResult,
    OutboxMessage,
    ReservationIngestResult,
)


_RESERVATION_SELECT = """
SELECT reservation_id, reservation_code, property_id, rental_unit_id,
       source_channel, external_reservation_id, reservation_status,
       check_in_at, check_out_at, source_version
  FROM propertyai.reservation
 WHERE reservation_id = %s
 FOR UPDATE
"""


class PostgresCleanerTransaction:
    def __init__(self, connection: psycopg.Connection):
        self._connection = connection

    def verify_authority_epoch(self, scope_code: str, expected_epoch: int) -> int:
        try:
            row = self._connection.execute(
                "SELECT propertyai.lock_and_verify_authority_epoch(%s, %s) AS epoch",
                (scope_code, expected_epoch),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("authority epoch function returned no row")
        return int(row["epoch"])

    def find_command_receipt(
        self, authority_scope_code: str, command_type: str, idempotency_key: str
    ) -> UUID | None:
        """Read the exact command-receipt fence without mutating it."""
        if not all(value.strip() for value in (authority_scope_code, command_type, idempotency_key)):
            raise ValueError("command receipt identity must be nonblank")
        try:
            # This is an absence observation under the authority-epoch fence. Do
            # not request a row lock: the least-privileged app role has SELECT but
            # intentionally no UPDATE privilege here. The unique receipt insert
            # remains the authoritative concurrent-winner fence.
            row = self._connection.execute(
                """
                SELECT command_id
                  FROM propertyai.command_receipt
                 WHERE authority_scope_code = %s
                   AND command_type = %s
                   AND idempotency_key = %s
                """,
                (authority_scope_code, command_type, idempotency_key),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        return None if row is None else row["command_id"]

    def find_reservation_by_source_code(
        self, source_channel: str, reservation_code: str
    ) -> Mapping[str, Any] | None:
        """Read the exact source/reservation business key before first-command mutation."""
        if not source_channel.strip() or not reservation_code.strip():
            raise ValueError("reservation source/code key must be nonblank")
        try:
            return self._connection.execute(
                """
                SELECT reservation_id, reservation_code, property_id, rental_unit_id,
                       source_channel, external_reservation_id, reservation_status,
                       check_in_at, check_out_at, source_version
                  FROM propertyai.reservation
                 WHERE source_channel = %s
                   AND reservation_code = %s
                 FOR SHARE
                """,
                (source_channel, reservation_code),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def find_external_reservation(
        self, source_channel: str, external_reservation_id: str
    ) -> Mapping[str, Any] | None:
        if not source_channel.strip() or not external_reservation_id.strip():
            raise ValueError("external reservation source key must be nonblank")
        try:
            return self._connection.execute(
                """
                SELECT reservation_id, reservation_code, property_id, rental_unit_id,
                       source_channel, external_reservation_id, reservation_status,
                       check_in_at, check_out_at, source_version
                  FROM propertyai.reservation
                 WHERE source_channel = %s
                   AND external_reservation_id = %s
                 FOR UPDATE
                """,
                (source_channel, external_reservation_id),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def ingest_external_reservation(
        self, snapshot: CanonicalReservationSnapshot
    ) -> ReservationIngestResult:
        """Converge one external business key onto exactly one durable aggregate.

        The application layer owns UUIDv4 allocation. This repository only
        serializes the external source-key race and resolves the persisted winner.
        """
        if snapshot.source_channel is None or snapshot.external_reservation_id is None:
            raise ValueError("external reservation ingress requires a source key")
        if snapshot.reservation_status not in {"CONFIRMED", "CANCELLED"}:
            raise ValueError("reservation_status must be CONFIRMED or CANCELLED")

        existing = self.find_external_reservation(
            snapshot.source_channel, snapshot.external_reservation_id
        )
        if existing is None:
            try:
                inserted = self._connection.execute(
                    """
                    INSERT INTO propertyai.reservation(
                        reservation_id, reservation_code, property_id, rental_unit_id,
                        source_channel, external_reservation_id, reservation_status,
                        check_in_at, check_out_at, source_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
                    ON CONFLICT DO NOTHING
                    RETURNING reservation_id, source_version
                    """,
                    (
                        snapshot.reservation_id,
                        snapshot.reservation_code,
                        snapshot.property_id,
                        snapshot.rental_unit_id,
                        snapshot.source_channel,
                        snapshot.external_reservation_id,
                        snapshot.reservation_status,
                        snapshot.check_in_at,
                        snapshot.check_out_at,
                    ),
                ).fetchone()
            except psycopg.Error as error:
                raise map_postgres_error(error) from error
            if inserted is not None:
                return ReservationIngestResult(
                    reservation_id=inserted["reservation_id"],
                    source_version=int(inserted["source_version"]),
                    changed=True,
                    created=True,
                )
            existing = self.find_external_reservation(
                snapshot.source_channel, snapshot.external_reservation_id
            )
        if existing is None:
            raise PostgresRepositoryError("external reservation conflict did not resolve to a row")

        immutable_expected = (
            snapshot.reservation_code,
            snapshot.property_id,
            snapshot.rental_unit_id,
            snapshot.source_channel,
            snapshot.external_reservation_id,
        )
        immutable_actual = (
            existing["reservation_code"],
            existing["property_id"],
            existing["rental_unit_id"],
            existing["source_channel"],
            existing["external_reservation_id"],
        )
        if immutable_actual != immutable_expected:
            raise ReservationIdentityConflictError(
                "external reservation source key resolved to different immutable identity"
            )
        payload_expected = (
            snapshot.reservation_status,
            snapshot.check_in_at,
            snapshot.check_out_at,
        )
        payload_actual = (
            existing["reservation_status"],
            existing["check_in_at"],
            existing["check_out_at"],
        )
        current_version = int(existing["source_version"])
        if payload_actual == payload_expected:
            return ReservationIngestResult(
                reservation_id=existing["reservation_id"],
                source_version=current_version,
                changed=False,
                created=False,
            )
        try:
            updated = self._connection.execute(
                """
                UPDATE propertyai.reservation
                   SET reservation_status = %s,
                       check_in_at = %s,
                       check_out_at = %s,
                       source_version = %s,
                       updated_at = clock_timestamp()
                 WHERE reservation_id = %s
                 RETURNING reservation_id, source_version
                """,
                (
                    snapshot.reservation_status,
                    snapshot.check_in_at,
                    snapshot.check_out_at,
                    current_version + 1,
                    existing["reservation_id"],
                ),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if updated is None:
            raise PostgresRepositoryError("external reservation update returned no row")
        return ReservationIngestResult(
            reservation_id=updated["reservation_id"],
            source_version=int(updated["source_version"]),
            changed=True,
            created=False,
        )

    def register_command_receipt(self, receipt: CommandReceiptInput) -> CommandReceiptResult:
        self.verify_authority_epoch(receipt.authority_scope_code, receipt.authority_epoch)
        if (receipt.source_stream_key is None) != (receipt.source_event_id is None):
            raise ValueError("source_stream_key and source_event_id must be supplied together")
        try:
            inserted = self._connection.execute(
                """
                INSERT INTO propertyai.command_receipt(
                    command_id, authority_scope_code, command_type, idempotency_key,
                    request_payload, source_channel_code, source_stream_key, source_event_id,
                    principal_type, actor_party_id, actor_external_identity_id,
                    authority_epoch, source_observed_at, decided_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (authority_scope_code, command_type, idempotency_key) DO NOTHING
                RETURNING command_id
                """,
                (
                    receipt.command_id,
                    receipt.authority_scope_code,
                    receipt.command_type,
                    receipt.idempotency_key,
                    Jsonb(dict(receipt.request_payload)),
                    receipt.source_channel_code,
                    receipt.source_stream_key,
                    receipt.source_event_id,
                    receipt.principal_type,
                    receipt.actor_party_id,
                    receipt.actor_external_identity_id,
                    receipt.authority_epoch,
                    receipt.source_observed_at,
                    receipt.decided_at,
                ),
            ).fetchone()
            if inserted is not None:
                return CommandReceiptResult(command_id=inserted["command_id"], reused=False)
            existing = self._connection.execute(
                """
                SELECT command_id, request_payload, source_channel_code, source_stream_key,
                       source_event_id, principal_type, actor_party_id,
                       actor_external_identity_id, authority_epoch, source_observed_at
                  FROM propertyai.command_receipt
                 WHERE authority_scope_code = %s
                   AND command_type = %s
                   AND idempotency_key = %s
                """,
                (receipt.authority_scope_code, receipt.command_type, receipt.idempotency_key),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if existing is None:
            raise PostgresRepositoryError("idempotent command receipt disappeared")
        expected = {
            "request_payload": dict(receipt.request_payload),
            "source_channel_code": receipt.source_channel_code,
            "source_stream_key": receipt.source_stream_key,
            "source_event_id": receipt.source_event_id,
            "principal_type": receipt.principal_type,
            "actor_party_id": receipt.actor_party_id,
            "actor_external_identity_id": receipt.actor_external_identity_id,
            "authority_epoch": receipt.authority_epoch,
            "source_observed_at": receipt.source_observed_at,
        }
        actual = {key: existing[key] for key in expected}
        if actual != expected:
            raise IdempotencyConflictError(
                "same command idempotency key was reused with different canonical request semantics"
            )
        return CommandReceiptResult(command_id=existing["command_id"], reused=True)

    @staticmethod
    def _identity(snapshot: CanonicalReservationSnapshot) -> tuple[Any, ...]:
        return (
            snapshot.reservation_code,
            snapshot.property_id,
            snapshot.rental_unit_id,
            snapshot.source_channel,
            snapshot.external_reservation_id,
        )

    @staticmethod
    def _stored_identity(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            row["reservation_code"],
            row["property_id"],
            row["rental_unit_id"],
            row["source_channel"],
            row["external_reservation_id"],
        )

    @staticmethod
    def _canonical_payload(snapshot: CanonicalReservationSnapshot) -> tuple[Any, ...]:
        return snapshot.reservation_status, snapshot.check_in_at, snapshot.check_out_at

    @staticmethod
    def _stored_payload(row: Mapping[str, Any]) -> tuple[Any, ...]:
        return row["reservation_status"], row["check_in_at"], row["check_out_at"]

    def ingest_canonical_reservation(
        self, snapshot: CanonicalReservationSnapshot
    ) -> ReservationIngestResult:
        if snapshot.reservation_status not in {"CONFIRMED", "CANCELLED"}:
            raise ValueError("reservation_status must be CONFIRMED or CANCELLED")
        if (snapshot.source_channel is None) != (snapshot.external_reservation_id is None):
            raise ValueError("source_channel and external_reservation_id must be supplied together")
        try:
            row = self._connection.execute(_RESERVATION_SELECT, (snapshot.reservation_id,)).fetchone()
            if row is None:
                inserted = self._connection.execute(
                    """
                    INSERT INTO propertyai.reservation(
                        reservation_id, reservation_code, property_id, rental_unit_id,
                        source_channel, external_reservation_id, reservation_status,
                        check_in_at, check_out_at, source_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
                    ON CONFLICT (reservation_id) DO NOTHING
                    RETURNING reservation_id, source_version
                    """,
                    (
                        snapshot.reservation_id,
                        snapshot.reservation_code,
                        snapshot.property_id,
                        snapshot.rental_unit_id,
                        snapshot.source_channel,
                        snapshot.external_reservation_id,
                        snapshot.reservation_status,
                        snapshot.check_in_at,
                        snapshot.check_out_at,
                    ),
                ).fetchone()
                if inserted is not None:
                    return ReservationIngestResult(
                        reservation_id=inserted["reservation_id"],
                        source_version=int(inserted["source_version"]),
                        changed=True,
                        created=True,
                    )
                row = self._connection.execute(_RESERVATION_SELECT, (snapshot.reservation_id,)).fetchone()
            if row is None:
                raise PostgresRepositoryError("reservation conflict did not resolve to a row")
            if self._stored_identity(row) != self._identity(snapshot):
                raise ReservationIdentityConflictError(
                    "canonical reservation identity differs from the stored Cleaner reference"
                )
            current_version = int(row["source_version"])
            if self._stored_payload(row) == self._canonical_payload(snapshot):
                return ReservationIngestResult(
                    reservation_id=row["reservation_id"],
                    source_version=current_version,
                    changed=False,
                    created=False,
                )
            updated = self._connection.execute(
                """
                UPDATE propertyai.reservation
                   SET reservation_status = %s,
                       check_in_at = %s,
                       check_out_at = %s,
                       source_version = %s,
                       updated_at = clock_timestamp()
                 WHERE reservation_id = %s
                 RETURNING reservation_id, source_version
                """,
                (
                    snapshot.reservation_status,
                    snapshot.check_in_at,
                    snapshot.check_out_at,
                    current_version + 1,
                    snapshot.reservation_id,
                ),
            ).fetchone()
        except (IdempotencyConflictError, ReservationIdentityConflictError, PostgresRepositoryError):
            raise
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if updated is None:
            raise PostgresRepositoryError("reservation update returned no row")
        return ReservationIngestResult(
            reservation_id=updated["reservation_id"],
            source_version=int(updated["source_version"]),
            changed=True,
            created=False,
        )

    def enqueue_outbox(self, message: OutboxMessage) -> OutboxEnqueueResult:
        if message.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        try:
            inserted = self._connection.execute(
                """
                INSERT INTO propertyai.integration_outbox(
                    outbox_id, domain_event_id, event_type, aggregate_type, aggregate_id,
                    destination_type, destination_ref, available_at, idempotency_key,
                    payload, max_attempts
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING outbox_id
                """,
                (
                    message.outbox_id,
                    message.domain_event_id,
                    message.event_type,
                    message.aggregate_type,
                    message.aggregate_id,
                    message.destination_type,
                    message.destination_ref,
                    message.available_at,
                    message.idempotency_key,
                    Jsonb(dict(message.payload)),
                    message.max_attempts,
                ),
            ).fetchone()
            if inserted is not None:
                return OutboxEnqueueResult(outbox_id=inserted["outbox_id"], reused=False)
            existing = self._connection.execute(
                """
                SELECT outbox_id, domain_event_id, event_type, aggregate_type, aggregate_id,
                       destination_type, destination_ref, available_at, payload, max_attempts
                  FROM propertyai.integration_outbox
                 WHERE idempotency_key = %s
                """,
                (message.idempotency_key,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if existing is None:
            raise PostgresRepositoryError("idempotent outbox row disappeared")
        expected = {
            "domain_event_id": message.domain_event_id,
            "event_type": message.event_type,
            "aggregate_type": message.aggregate_type,
            "aggregate_id": message.aggregate_id,
            "destination_type": message.destination_type,
            "destination_ref": message.destination_ref,
            "payload": dict(message.payload),
            "max_attempts": message.max_attempts,
        }
        actual = {key: existing[key] for key in expected}
        if actual != expected:
            raise IdempotencyConflictError(
                "same outbox idempotency key was reused with different event semantics"
            )
        return OutboxEnqueueResult(outbox_id=existing["outbox_id"], reused=True)


    def ensure_checkout_cleaning(
        self,
        *,
        cleaning_id: UUID,
        cleaning_code: str,
        reservation_id: UUID,
        property_id: UUID,
        rental_unit_id: UUID | None,
        schedule_revision_id: UUID,
        service_window_start_at: datetime,
        service_deadline_at: datetime,
        required_work_minutes: int,
        source_checkout_at: datetime,
        source_reservation_version: int,
        source_command_id: UUID,
    ) -> Mapping[str, Any]:
        if required_work_minutes <= 0:
            raise ValueError("required_work_minutes must be positive")
        try:
            inserted = self._connection.execute(
                """
                INSERT INTO propertyai.cleaning_job(
                    cleaning_id, cleaning_code, reservation_id, property_id, rental_unit_id,
                    schedule_source_type, cleaning_status, current_schedule_revision_id
                ) VALUES (%s, %s, %s, %s, %s, 'RESERVATION_CHECKOUT', 'PLANNED', NULL)
                ON CONFLICT DO NOTHING
                RETURNING cleaning_id
                """,
                (cleaning_id, cleaning_code, reservation_id, property_id, rental_unit_id),
            ).fetchone()
            row = self._connection.execute(
                """
                SELECT cleaning_id, cleaning_code, reservation_id, property_id, rental_unit_id,
                       schedule_source_type, cleaning_status, current_schedule_revision_id
                  FROM propertyai.cleaning_job
                 WHERE reservation_id = %s
                   AND schedule_source_type = 'RESERVATION_CHECKOUT'
                   AND cleaning_status <> 'CANCELLED'
                 FOR UPDATE
                """,
                (reservation_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("checkout cleaning conflict did not resolve")
        expected_identity = (
            cleaning_code,
            reservation_id,
            property_id,
            rental_unit_id,
            "RESERVATION_CHECKOUT",
        )
        actual_identity = (
            row["cleaning_code"],
            row["reservation_id"],
            row["property_id"],
            row["rental_unit_id"],
            row["schedule_source_type"],
        )
        if actual_identity != expected_identity:
            raise PostgresRepositoryError("checkout cleaning immutable identity conflict")

        current_revision_id = row["current_schedule_revision_id"]
        if current_revision_id is not None:
            try:
                revision = self._connection.execute(
                    """
                    SELECT schedule_revision_id, service_window_start_at, service_deadline_at,
                           required_work_minutes, source_checkout_at, source_reservation_version
                      FROM propertyai.cleaning_schedule_revision
                     WHERE cleaning_id=%s AND schedule_revision_id=%s
                    """,
                    (row["cleaning_id"], current_revision_id),
                ).fetchone()
            except psycopg.Error as error:
                raise map_postgres_error(error) from error
            if revision is None:
                raise PostgresRepositoryError("current cleaning schedule revision disappeared")
            current_source_version = revision["source_reservation_version"]
            if current_source_version is None:
                raise PostgresRepositoryError("checkout cleaning revision lacks reservation source version")
            current_source_version = int(current_source_version)
            if source_reservation_version < current_source_version:
                raise PostgresRepositoryError("STALE_RESERVATION_SOURCE_VERSION")
            requested = (
                service_window_start_at,
                service_deadline_at,
                required_work_minutes,
                source_checkout_at,
                source_reservation_version,
            )
            actual = (
                revision["service_window_start_at"],
                revision["service_deadline_at"],
                revision["required_work_minutes"],
                revision["source_checkout_at"],
                current_source_version,
            )
            if source_reservation_version == current_source_version:
                if actual != requested:
                    raise PostgresRepositoryError(
                        "same reservation source version has different cleaning schedule semantics"
                    )
                return {
                    "cleaning_id": row["cleaning_id"],
                    "schedule_revision_id": current_revision_id,
                    "created": inserted is not None,
                    "revision_changed": False,
                }

        try:
            revision_row = self._connection.execute(
                """
                SELECT propertyai.append_cleaning_schedule_revision(
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                ) AS schedule_revision_id
                """,
                (
                    schedule_revision_id,
                    row["cleaning_id"],
                    current_revision_id,
                    service_window_start_at,
                    service_deadline_at,
                    required_work_minutes,
                    source_checkout_at,
                    source_reservation_version,
                    "RESERVATION_SOURCE_UPDATED" if current_revision_id else "RESERVATION_CHECKOUT_CREATED",
                    source_command_id,
                ),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if revision_row is None:
            raise PostgresRepositoryError("schedule revision append returned no row")
        return {
            "cleaning_id": row["cleaning_id"],
            "schedule_revision_id": revision_row["schedule_revision_id"],
            "created": inserted is not None,
            "revision_changed": True,
        }

    def checkout_cleaning_for_reservation(
        self, reservation_id: UUID
    ) -> Mapping[str, Any] | None:
        try:
            return self._connection.execute(
                """
                SELECT cleaning_id, cleaning_status, current_schedule_revision_id
                  FROM propertyai.cleaning_job
                 WHERE reservation_id=%s
                   AND schedule_source_type='RESERVATION_CHECKOUT'
                   AND cleaning_status <> 'CANCELLED'
                 FOR UPDATE
                """,
                (reservation_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def current_cleaning_schedule(self, cleaning_id: UUID) -> Mapping[str, Any]:
        try:
            row = self._connection.execute(
                """
                SELECT c.cleaning_id, c.cleaning_status, c.current_schedule_revision_id,
                       r.revision_no, r.service_window_start_at, r.service_deadline_at,
                       r.required_work_minutes, r.source_checkout_at, r.source_reservation_version
                  FROM propertyai.cleaning_job c
                  JOIN propertyai.cleaning_schedule_revision r
                    ON r.cleaning_id=c.cleaning_id
                   AND r.schedule_revision_id=c.current_schedule_revision_id
                 WHERE c.cleaning_id=%s
                 FOR UPDATE OF c
                """,
                (cleaning_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("cleaning current schedule unavailable")
        return row

    def set_cleaning_status(self, cleaning_id: UUID, status: str) -> None:
        if status not in {"PLANNED", "OFFERING", "ASSIGNED", "IN_PROGRESS", "COMPLETED", "CANCELLED"}:
            raise ValueError("invalid cleaning status")
        try:
            row = self._connection.execute(
                """
                UPDATE propertyai.cleaning_job
                   SET cleaning_status=%s, updated_at=clock_timestamp()
                 WHERE cleaning_id=%s
                 RETURNING cleaning_id
                """,
                (status, cleaning_id),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("cleaning status target missing")

    def pending_schedule_reconciliation(
        self, cleaning_id: UUID
    ) -> Mapping[str, Any] | None:
        try:
            return self._connection.execute(
                """
                SELECT schedule_reconciliation_id, cleaning_id, hard_booked_assignment_id,
                       base_assignment_revision_id, target_schedule_revision_id,
                       case_version, reconciliation_status, reason_code, source_ref
                  FROM propertyai.cleaning_schedule_reconciliation
                 WHERE cleaning_id=%s AND reconciliation_status='PENDING'
                 FOR UPDATE
                """,
                (cleaning_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def ensure_schedule_reconciliation(
        self,
        *,
        schedule_reconciliation_id: UUID,
        cleaning_id: UUID,
        hard_booked_assignment_id: UUID,
        base_assignment_revision_id: UUID,
        target_schedule_revision_id: UUID,
        reason_code: str,
        source_ref: str,
    ) -> Mapping[str, Any]:
        existing = self.pending_schedule_reconciliation(cleaning_id)
        if existing is None:
            try:
                row = self._connection.execute(
                    """
                    INSERT INTO propertyai.cleaning_schedule_reconciliation(
                        schedule_reconciliation_id, cleaning_id, hard_booked_assignment_id,
                        base_assignment_revision_id, target_schedule_revision_id,
                        case_version, reconciliation_status, reason_code, source_ref
                    ) VALUES (%s,%s,%s,%s,%s,1,'PENDING',%s,%s)
                    RETURNING schedule_reconciliation_id, target_schedule_revision_id, case_version
                    """,
                    (
                        schedule_reconciliation_id,
                        cleaning_id,
                        hard_booked_assignment_id,
                        base_assignment_revision_id,
                        target_schedule_revision_id,
                        reason_code,
                        source_ref,
                    ),
                ).fetchone()
            except psycopg.Error as error:
                raise map_postgres_error(error) from error
            if row is None:
                raise PostgresRepositoryError("schedule reconciliation insert returned no row")
            return row

        expected_binding = (
            hard_booked_assignment_id,
            base_assignment_revision_id,
            reason_code,
        )
        actual_binding = (
            existing["hard_booked_assignment_id"],
            existing["base_assignment_revision_id"],
            existing["reason_code"],
        )
        if actual_binding != expected_binding:
            raise PostgresRepositoryError(
                "pending schedule reconciliation belongs to different immutable assignment binding"
            )
        if existing["target_schedule_revision_id"] == target_schedule_revision_id:
            return existing
        try:
            row = self._connection.execute(
                """
                UPDATE propertyai.cleaning_schedule_reconciliation
                   SET target_schedule_revision_id=%s,
                       case_version=case_version+1,
                       updated_at=clock_timestamp()
                 WHERE schedule_reconciliation_id=%s
                   AND reconciliation_status='PENDING'
                 RETURNING schedule_reconciliation_id, target_schedule_revision_id, case_version
                """,
                (target_schedule_revision_id, existing["schedule_reconciliation_id"]),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("schedule reconciliation target update lost pending row")
        return row

    def cancel_open_offer_campaign(
        self, cleaning_id: UUID, *, cancelled_at: datetime, reason: str
    ) -> UUID | None:
        try:
            campaign = self._connection.execute(
                """
                SELECT campaign_id
                  FROM propertyai.cleaning_offer_campaign
                 WHERE cleaning_id=%s AND campaign_status='OPEN'
                 FOR UPDATE
                """,
                (cleaning_id,),
            ).fetchone()
            if campaign is None:
                return None
            self._connection.execute(
                """
                UPDATE propertyai.cleaning_offer_candidate
                   SET candidate_status='DECLINED',
                       declined_at=%s,
                       updated_at=clock_timestamp()
                 WHERE campaign_id=%s AND candidate_status='ELIGIBLE'
                """,
                (cancelled_at, campaign["campaign_id"]),
            )
            closed = self._connection.execute(
                """
                UPDATE propertyai.cleaning_offer_campaign
                   SET campaign_status='CANCELLED',
                       closed_at=%s,
                       closed_reason_code=%s,
                       updated_at=clock_timestamp()
                 WHERE campaign_id=%s AND campaign_status='OPEN'
                 RETURNING campaign_id
                """,
                (cancelled_at, reason, campaign["campaign_id"]),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if closed is None:
            raise PostgresRepositoryError("open campaign disappeared during cancellation")
        return closed["campaign_id"]

    def next_campaign_number(self, cleaning_id: UUID) -> int:
        self.current_cleaning_schedule(cleaning_id)
        try:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(campaign_no),0)+1 AS next_no FROM propertyai.cleaning_offer_campaign WHERE cleaning_id=%s",
                (cleaning_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        return int(row["next_no"])

    def insert_offer_campaign(self, values: Mapping[str, Any]) -> None:
        try:
            self._connection.execute(
                """
                INSERT INTO propertyai.cleaning_offer_campaign(
                    campaign_id, cleaning_id, schedule_revision_id, campaign_no,
                    campaign_status, open_tier_floor, max_tier, tier_expand_after_minutes,
                    acceptance_cutoff_at, base_fee_krw, replacement_urgency,
                    urgent_premium_krw, total_agreed_fee_krw,
                    urgent_premium_policy_version, opened_at
                ) VALUES (%s,%s,%s,%s,'OPEN',1,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    values["campaign_id"], values["cleaning_id"], values["schedule_revision_id"],
                    values["campaign_no"], values["max_tier"], values.get("tier_expand_after_minutes"),
                    values["acceptance_cutoff_at"], values["base_fee_krw"],
                    values["replacement_urgency"], values["urgent_premium_krw"],
                    values["total_agreed_fee_krw"], values.get("urgent_premium_policy_version"),
                    values["opened_at"],
                ),
            )
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def insert_offer_candidate(self, values: Mapping[str, Any]) -> None:
        try:
            self._connection.execute(
                """
                INSERT INTO propertyai.cleaning_offer_candidate(
                    offer_candidate_id, campaign_id, cleaner_party_id, tier_no,
                    candidate_status, proposal_version, proposed_start_at, proposed_end_at,
                    proposed_buffer_before_minutes, proposed_buffer_after_minutes,
                    buffer_basis, buffer_policy_ref, evaluated_at
                ) VALUES (%s,%s,%s,%s,'ELIGIBLE',1,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    values["offer_candidate_id"], values["campaign_id"], values["cleaner_party_id"],
                    values["tier_no"], values["proposed_start_at"], values["proposed_end_at"],
                    values.get("proposed_buffer_before_minutes", 0),
                    values.get("proposed_buffer_after_minutes", 0),
                    values.get("buffer_basis", "NOT_APPLIED"), values.get("buffer_policy_ref"),
                    values["evaluated_at"],
                ),
            )
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def offer_acceptance_context(
        self, campaign_id: UUID, offer_candidate_id: UUID, cleaner_party_id: UUID
    ) -> Mapping[str, Any]:
        try:
            row = self._connection.execute(
                """
                SELECT c.campaign_id, c.cleaning_id, c.schedule_revision_id, c.campaign_status,
                       c.base_fee_krw, c.replacement_urgency, c.urgent_premium_krw,
                       c.total_agreed_fee_krw, c.urgent_premium_policy_version,
                       o.offer_candidate_id, o.cleaner_party_id, o.candidate_status,
                       o.proposal_version, o.proposed_start_at, o.proposed_end_at,
                       o.proposed_buffer_before_minutes, o.proposed_buffer_after_minutes,
                       o.buffer_basis, o.buffer_policy_ref,
                       r.required_work_minutes
                  FROM propertyai.cleaning_offer_campaign c
                  JOIN propertyai.cleaning_offer_candidate o ON o.campaign_id=c.campaign_id
                  JOIN propertyai.cleaning_schedule_revision r
                    ON r.cleaning_id=c.cleaning_id AND r.schedule_revision_id=c.schedule_revision_id
                 WHERE c.campaign_id=%s AND o.offer_candidate_id=%s AND o.cleaner_party_id=%s
                 FOR UPDATE OF c, o
                """,
                (campaign_id, offer_candidate_id, cleaner_party_id),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("offer acceptance context missing")
        return row

    def next_assignment_number(self, cleaning_id: UUID) -> int:
        self.current_cleaning_schedule(cleaning_id)
        try:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(assignment_no),0)+1 AS next_no FROM propertyai.cleaning_assignment WHERE cleaning_id=%s",
                (cleaning_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        return int(row["next_no"])

    def insert_assignment(self, values: Mapping[str, Any]) -> None:
        try:
            self._connection.execute(
                """
                INSERT INTO propertyai.cleaning_assignment(
                    assignment_id, cleaning_id, assignment_no, schedule_revision_id,
                    campaign_id, offer_candidate_id, accepted_proposal_version,
                    cleaner_party_id, assignment_source, assignment_status, booked_at,
                    scheduled_start_at, scheduled_end_at, work_minutes_snapshot,
                    travel_buffer_before_minutes, travel_buffer_after_minutes,
                    buffer_basis, buffer_policy_ref, busy_window,
                    base_fee_krw, replacement_urgency, urgent_premium_krw,
                    total_agreed_fee_krw, urgent_premium_policy_version
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'HARD_BOOKED',%s,%s,%s,%s,%s,%s,%s,%s,
                          tstzrange(%s,%s,'[)'),%s,%s,%s,%s,%s)
                """,
                (
                    values["assignment_id"], values["cleaning_id"], values["assignment_no"],
                    values["schedule_revision_id"], values.get("campaign_id"),
                    values.get("offer_candidate_id"), values.get("accepted_proposal_version"),
                    values["cleaner_party_id"], values["assignment_source"], values["booked_at"],
                    values["scheduled_start_at"], values["scheduled_end_at"],
                    values["work_minutes_snapshot"], values.get("travel_buffer_before_minutes", 0),
                    values.get("travel_buffer_after_minutes", 0),
                    values.get("buffer_basis", "NOT_APPLIED"), values.get("buffer_policy_ref"),
                    values["scheduled_start_at"], values["scheduled_end_at"],
                    values["base_fee_krw"], values["replacement_urgency"],
                    values["urgent_premium_krw"], values["total_agreed_fee_krw"],
                    values.get("urgent_premium_policy_version"),
                ),
            )
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def decline_offer_candidate(self, offer_candidate_id: UUID, declined_at: datetime) -> None:
        try:
            row = self._connection.execute(
                """
                UPDATE propertyai.cleaning_offer_candidate
                   SET candidate_status='DECLINED', declined_at=%s, updated_at=clock_timestamp()
                 WHERE offer_candidate_id=%s AND candidate_status='ELIGIBLE'
                 RETURNING offer_candidate_id
                """,
                (declined_at, offer_candidate_id),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("offer candidate is no longer eligible")

    def accept_offer_candidate(self, offer_candidate_id: UUID, accepted_at: datetime) -> None:
        try:
            row = self._connection.execute(
                """
                UPDATE propertyai.cleaning_offer_candidate
                   SET candidate_status='ACCEPTED', accepted_at=%s, updated_at=clock_timestamp()
                 WHERE offer_candidate_id=%s AND candidate_status='ELIGIBLE'
                 RETURNING offer_candidate_id
                """,
                (accepted_at, offer_candidate_id),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("offer candidate is no longer eligible")

    def close_offer_campaign(self, campaign_id: UUID, closed_at: datetime, reason: str) -> None:
        try:
            row = self._connection.execute(
                """
                UPDATE propertyai.cleaning_offer_campaign
                   SET campaign_status='CLOSED', closed_at=%s, closed_reason_code=%s,
                       updated_at=clock_timestamp()
                 WHERE campaign_id=%s AND campaign_status='OPEN'
                 RETURNING campaign_id
                """,
                (closed_at, reason, campaign_id),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("offer campaign is no longer open")

    def hard_booked_assignment(self, cleaning_id: UUID) -> Mapping[str, Any] | None:
        try:
            return self._connection.execute(
                """
                SELECT * FROM propertyai.cleaning_assignment
                 WHERE cleaning_id=%s AND assignment_status='HARD_BOOKED'
                 FOR UPDATE
                """,
                (cleaning_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def assignment_by_id(self, assignment_id: UUID) -> Mapping[str, Any]:
        try:
            row = self._connection.execute(
                "SELECT * FROM propertyai.cleaning_assignment WHERE assignment_id=%s FOR UPDATE",
                (assignment_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("assignment missing")
        return row

    def terminate_assignment(
        self, assignment_id: UUID, *, status: str, ended_at: datetime, reason: str
    ) -> None:
        if status not in {"RELEASED", "COMPLETED", "CANCELLED"}:
            raise ValueError("invalid terminal assignment status")
        try:
            row = self._connection.execute(
                """
                UPDATE propertyai.cleaning_assignment
                   SET assignment_status=%s, ended_at=%s, end_reason_code=%s,
                       updated_at=clock_timestamp()
                 WHERE assignment_id=%s AND assignment_status='HARD_BOOKED'
                 RETURNING assignment_id
                """,
                (status, ended_at, reason, assignment_id),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("assignment is not hard-booked")

    def unavailability_by_id(self, unavailability_id: UUID) -> Mapping[str, Any]:
        try:
            row = self._connection.execute(
                "SELECT * FROM propertyai.cleaner_unavailability_case WHERE unavailability_id=%s FOR UPDATE",
                (unavailability_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("unavailability case missing")
        return row

    def unavailability_for_assignment(self, assignment_id: UUID) -> Mapping[str, Any] | None:
        try:
            return self._connection.execute(
                """
                SELECT * FROM propertyai.cleaner_unavailability_case
                 WHERE original_assignment_id=%s
                 FOR UPDATE
                """,
                (assignment_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def insert_unavailability(self, values: Mapping[str, Any]) -> None:
        try:
            self._connection.execute(
                """
                INSERT INTO propertyai.cleaner_unavailability_case(
                    unavailability_id, cleaning_id, schedule_revision_id,
                    original_assignment_id, cleaner_party_id, case_status,
                    availability_classification, replacement_urgency,
                    reason_code, reason_text, occurred_at
                ) VALUES (%s,%s,%s,%s,%s,'CONFIRMED',%s,%s,%s,%s,%s)
                """,
                (
                    values["unavailability_id"], values["cleaning_id"],
                    values["schedule_revision_id"], values["original_assignment_id"],
                    values["cleaner_party_id"], values["availability_classification"],
                    values["replacement_urgency"], values.get("reason_code"),
                    values.get("reason_text"), values["occurred_at"],
                ),
            )
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def next_reassignment_request_number(self, cleaning_id: UUID) -> int:
        try:
            row = self._connection.execute(
                """
                SELECT COALESCE(MAX(request_no),0)+1 AS next_no
                  FROM propertyai.cleaner_reassignment_request
                 WHERE cleaning_id=%s
                """,
                (cleaning_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        return int(row["next_no"])

    def requested_reassignment(self, cleaning_id: UUID) -> Mapping[str, Any] | None:
        try:
            return self._connection.execute(
                """
                SELECT * FROM propertyai.cleaner_reassignment_request
                 WHERE cleaning_id=%s AND request_status='REQUESTED'
                 FOR UPDATE
                """,
                (cleaning_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def insert_reassignment_request(self, values: Mapping[str, Any]) -> None:
        try:
            self._connection.execute(
                """
                INSERT INTO propertyai.cleaner_reassignment_request(
                    reassignment_request_id, unavailability_id, cleaning_id,
                    cleaner_party_id, original_assignment_id, requested_schedule_revision_id,
                    request_no, request_status, requested_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,'REQUESTED',%s)
                """,
                (
                    values["reassignment_request_id"], values["unavailability_id"],
                    values["cleaning_id"], values["cleaner_party_id"],
                    values["original_assignment_id"], values["requested_schedule_revision_id"],
                    values["request_no"], values["requested_at"],
                ),
            )
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def reassignment_request_by_id(self, request_id: UUID) -> Mapping[str, Any]:
        try:
            row = self._connection.execute(
                "SELECT * FROM propertyai.cleaner_reassignment_request WHERE reassignment_request_id=%s FOR UPDATE",
                (request_id,),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("reassignment request missing")
        return row

    def decide_reassignment_request(
        self, request_id: UUID, *, status: str, decided_at: datetime, decision_code: str
    ) -> None:
        if status not in {"REASSIGNED_ORIGINAL", "CONTINUE_REPLACEMENT", "SUPERSEDED", "CANCELLED"}:
            raise ValueError("invalid reassignment decision")
        try:
            row = self._connection.execute(
                """
                UPDATE propertyai.cleaner_reassignment_request
                   SET request_status=%s, decided_at=%s, decision_code=%s,
                       updated_at=clock_timestamp()
                 WHERE reassignment_request_id=%s AND request_status='REQUESTED'
                 RETURNING reassignment_request_id
                """,
                (status, decided_at, decision_code, request_id),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("reassignment request is no longer requested")

    def domain_event_for_command(
        self, command_id: UUID, event_type: str
    ) -> Mapping[str, Any] | None:
        try:
            rows = self._connection.execute(
                """
                SELECT domain_event_id, aggregate_type, aggregate_id, aggregate_version,
                       event_type, actor_party_id, payload, occurred_at
                  FROM propertyai.domain_event
                 WHERE command_id=%s AND event_type=%s
                 ORDER BY domain_event_id
                """,
                (command_id, event_type),
            ).fetchall()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if len(rows) > 1:
            raise PostgresRepositoryError("command produced duplicate domain events")
        return rows[0] if rows else None

    def append_domain_event_once(
        self,
        *,
        command_id: UUID,
        aggregate_type: str,
        aggregate_id: UUID,
        aggregate_version: int | None,
        event_type: str,
        actor_party_id: UUID | None,
        payload: Mapping[str, Any],
        occurred_at: datetime,
    ) -> int:
        try:
            rows = self._connection.execute(
                """
                SELECT domain_event_id, aggregate_type, aggregate_id, aggregate_version,
                       event_type, actor_party_id, payload, occurred_at
                  FROM propertyai.domain_event
                 WHERE command_id=%s AND event_type=%s AND aggregate_id=%s
                 ORDER BY domain_event_id
                """,
                (command_id, event_type, aggregate_id),
            ).fetchall()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if len(rows) > 1:
            raise PostgresRepositoryError("command produced duplicate domain events")
        if rows:
            row = rows[0]
            expected = (
                aggregate_type, aggregate_id, aggregate_version, event_type,
                actor_party_id, dict(payload), occurred_at,
            )
            actual = (
                row["aggregate_type"], row["aggregate_id"], row["aggregate_version"],
                row["event_type"], row["actor_party_id"], row["payload"], row["occurred_at"],
            )
            if actual != expected:
                raise IdempotencyConflictError("domain event replay semantics differ")
            return int(row["domain_event_id"])
        try:
            row = self._connection.execute(
                """
                INSERT INTO propertyai.domain_event(
                    aggregate_type, aggregate_id, aggregate_version, event_type,
                    command_id, actor_party_id, payload, occurred_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING domain_event_id
                """,
                (
                    aggregate_type, aggregate_id, aggregate_version, event_type,
                    command_id, actor_party_id, Jsonb(dict(payload)), occurred_at,
                ),
            ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("domain event insert returned no row")
        return int(row["domain_event_id"])


class PostgresCleanerRepository:
    def __init__(self, pool: PostgresStageAPool):
        self._pool = pool

    @contextmanager
    def transaction(self) -> Iterator[PostgresCleanerTransaction]:
        try:
            with self._pool._connection() as connection:
                with connection.transaction():
                    yield PostgresCleanerTransaction(connection)
        except PostgresRepositoryError:
            raise
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def ingest_canonical_reservation(
        self, snapshot: CanonicalReservationSnapshot
    ) -> ReservationIngestResult:
        with self.transaction() as transaction:
            return transaction.ingest_canonical_reservation(snapshot)


class PostgresOutboxWorkerRepository:
    def __init__(self, pool: PostgresStageAPool):
        self._pool = pool

    def claim_outbox(self, worker_id: str, *, limit: int = 10, lease_seconds: int = 30) -> list[OutboxClaim]:
        try:
            with self._pool._connection() as connection:
                with connection.transaction():
                    rows = connection.execute(
                        "SELECT * FROM propertyai.claim_integration_outbox(%s, %s, %s)",
                        (worker_id, limit, lease_seconds),
                    ).fetchall()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        return [
            OutboxClaim(
                outbox_id=row["outbox_id"],
                event_type=row["event_type"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                destination_type=row["destination_type"],
                destination_ref=row["destination_ref"],
                payload=row["payload"],
                attempt_count=int(row["attempt_count"]),
                max_attempts=int(row["max_attempts"]),
                lease_owner=row["lease_owner"],
                lease_until=row["lease_until"],
                lease_fence=int(row["lease_fence"]),
                external_effect_id=row["external_effect_id"],
                last_error_code=row["last_error_code"],
            )
            for row in rows
        ]

    def reservation_projection_state(self, reservation_id: UUID) -> Mapping[str, Any]:
        try:
            with self._pool._connection() as connection:
                row = connection.execute(
                    """
                    SELECT reservation_id, reservation_code, property_id, rental_unit_id,
                           source_channel, external_reservation_id, reservation_status,
                           check_in_at, check_out_at, source_version
                      FROM propertyai.reservation
                     WHERE reservation_id=%s
                    """,
                    (reservation_id,),
                ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("reservation projection source missing")
        return row

    def cleaning_projection_state(self, cleaning_id: UUID) -> Mapping[str, Any]:
        try:
            with self._pool._connection() as connection:
                row = connection.execute(
                    """
                    SELECT c.cleaning_id, c.cleaning_code, c.reservation_id, c.property_id,
                           c.rental_unit_id, c.schedule_source_type, c.cleaning_status,
                           c.current_schedule_revision_id, r.revision_no,
                           r.service_window_start_at, r.service_deadline_at,
                           r.required_work_minutes, r.source_checkout_at,
                           r.source_reservation_version, v.reservation_code
                      FROM propertyai.cleaning_job c
                      LEFT JOIN propertyai.cleaning_schedule_revision r
                        ON r.cleaning_id=c.cleaning_id
                       AND r.schedule_revision_id=c.current_schedule_revision_id
                      LEFT JOIN propertyai.reservation v ON v.reservation_id=c.reservation_id
                     WHERE c.cleaning_id=%s
                    """,
                    (cleaning_id,),
                ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("cleaning projection source missing")
        return row

    def resource_binding(
        self,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        destination_type: str,
        resource_code: str,
    ) -> Mapping[str, Any] | None:
        try:
            with self._pool._connection() as connection:
                return connection.execute(
                    """
                    SELECT binding_id, aggregate_type, aggregate_id, destination_type,
                           resource_code, external_resource_id, external_uid, sync_status,
                           last_applied_aggregate_version, external_version, last_synced_at
                      FROM propertyai.integration_resource_binding
                     WHERE aggregate_type=%s AND aggregate_id=%s
                       AND destination_type=%s AND resource_code=%s
                    """,
                    (aggregate_type, aggregate_id, destination_type, resource_code),
                ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

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
    ) -> Mapping[str, Any]:
        if not external_resource_id.strip():
            raise ValueError("external_resource_id must be nonblank")
        try:
            with self._pool._connection() as connection:
                with connection.transaction():
                    existing = connection.execute(
                        """
                        SELECT binding_id, external_resource_id
                          FROM propertyai.integration_resource_binding
                         WHERE aggregate_type=%s AND aggregate_id=%s
                           AND destination_type=%s AND resource_code=%s
                         FOR UPDATE
                        """,
                        (aggregate_type, aggregate_id, destination_type, resource_code),
                    ).fetchone()
                    if existing is None:
                        connection.execute(
                            """
                            INSERT INTO propertyai.integration_resource_binding(
                                binding_id, aggregate_type, aggregate_id, destination_type,
                                resource_code, external_resource_id, external_uid, sync_status,
                                last_applied_aggregate_version, external_version, last_synced_at
                            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp())
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                uuid4(), aggregate_type, aggregate_id, destination_type,
                                resource_code, external_resource_id, external_uid, sync_status,
                                aggregate_version, external_version,
                            ),
                        )
                        existing = connection.execute(
                            """
                            SELECT binding_id, external_resource_id
                              FROM propertyai.integration_resource_binding
                             WHERE aggregate_type=%s AND aggregate_id=%s
                               AND destination_type=%s AND resource_code=%s
                             FOR UPDATE
                            """,
                            (aggregate_type, aggregate_id, destination_type, resource_code),
                        ).fetchone()
                    if existing is None or existing["external_resource_id"] != external_resource_id:
                        raise PostgresRepositoryError("RESOURCE_BINDING_IDENTITY_CONFLICT")
                    row = connection.execute(
                        """
                        UPDATE propertyai.integration_resource_binding
                           SET external_uid=%s, sync_status=%s,
                               last_applied_aggregate_version=%s, external_version=%s,
                               last_synced_at=clock_timestamp(), updated_at=clock_timestamp()
                         WHERE binding_id=%s
                         RETURNING binding_id, aggregate_type, aggregate_id, destination_type,
                                   resource_code, external_resource_id, external_uid, sync_status,
                                   last_applied_aggregate_version, external_version, last_synced_at
                        """,
                        (
                            external_uid, sync_status, aggregate_version, external_version,
                            existing["binding_id"],
                        ),
                    ).fetchone()
        except PostgresRepositoryError:
            raise
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("resource binding readback missing")
        return row

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
    ) -> Mapping[str, Any]:
        if not expected_external_resource_id.strip() or not new_external_resource_id.strip():
            raise ValueError("resource binding identities must be nonblank")
        try:
            with self._pool._connection() as connection:
                with connection.transaction():
                    row = connection.execute(
                        """
                        UPDATE propertyai.integration_resource_binding
                           SET external_resource_id=%s, external_uid=%s, sync_status=%s,
                               last_applied_aggregate_version=%s, external_version=%s,
                               last_synced_at=clock_timestamp(), updated_at=clock_timestamp()
                         WHERE aggregate_type=%s AND aggregate_id=%s
                           AND destination_type=%s AND resource_code=%s
                           AND external_resource_id=%s
                         RETURNING binding_id, aggregate_type, aggregate_id, destination_type,
                                   resource_code, external_resource_id, external_uid, sync_status,
                                   last_applied_aggregate_version, external_version, last_synced_at
                        """,
                        (
                            new_external_resource_id, external_uid, sync_status, aggregate_version,
                            external_version, aggregate_type, aggregate_id, destination_type,
                            resource_code, expected_external_resource_id,
                        ),
                    ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        if row is None:
            raise PostgresRepositoryError("RESOURCE_BINDING_REBIND_CAS_REJECTED")
        return row

    def complete_outbox(
        self,
        outbox_id: UUID,
        *,
        worker_id: str,
        lease_fence: int,
        external_effect_id: str | None = None,
    ) -> bool:
        return self._run_fenced_function(
            "complete_integration_outbox",
            (outbox_id, worker_id, lease_fence, external_effect_id),
        )

    def fail_outbox(
        self,
        outbox_id: UUID,
        *,
        worker_id: str,
        lease_fence: int,
        error_code: str,
        retry_delay_seconds: int,
    ) -> bool:
        return self._run_fenced_function(
            "fail_integration_outbox",
            (outbox_id, worker_id, lease_fence, error_code, retry_delay_seconds),
        )

    def mark_outbox_pending_reconciliation(
        self,
        outbox_id: UUID,
        *,
        worker_id: str,
        lease_fence: int,
        error_code: str,
        external_effect_id: str | None = None,
    ) -> bool:
        return self._run_fenced_function(
            "mark_outbox_pending_reconciliation",
            (outbox_id, worker_id, lease_fence, error_code, external_effect_id),
        )

    def read_outbox_reconciliation(
        self, outbox_id: UUID
    ) -> OutboxReconciliationState | None:
        try:
            with self._pool._connection() as connection:
                row = connection.execute(
                    """
                    SELECT outbox_id, domain_event_id, event_type, aggregate_type,
                           aggregate_id, destination_type, destination_ref, outbox_status,
                           idempotency_key, payload, attempt_count, max_attempts,
                           lease_fence, external_effect_id, last_error_code
                      FROM propertyai.integration_outbox
                     WHERE outbox_id=%s
                    """,
                    (outbox_id,),
                ).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        return None if row is None else OutboxReconciliationState(**row)

    def resolve_outbox_reconciliation(
        self,
        outbox_id: UUID,
        *,
        expected_lease_fence: int,
        resolution: str,
        external_effect_id: str | None,
        error_code: str | None,
        retry_delay_seconds: int = 0,
    ) -> bool:
        return self._run_fenced_function(
            "resolve_outbox_reconciliation",
            (
                outbox_id,
                expected_lease_fence,
                resolution,
                external_effect_id,
                error_code,
                retry_delay_seconds,
            ),
        )

    @staticmethod
    def _read_outbox_state(connection: psycopg.Connection, outbox_id: UUID) -> OutboxReconciliationState | None:
        row = connection.execute(
            """SELECT outbox_id, domain_event_id, event_type, aggregate_type,
                      aggregate_id, destination_type, destination_ref, outbox_status,
                      idempotency_key, payload, attempt_count, max_attempts,
                      lease_fence, external_effect_id, last_error_code
                 FROM propertyai.integration_outbox WHERE outbox_id=%s""", (outbox_id,),
        ).fetchone()
        return None if row is None else OutboxReconciliationState(**row)

    def begin_outbox_delivery(
        self, claim: OutboxClaim, *, worker_id: str, error_code: str
    ) -> OutboxReconciliationState | None:
        """Commit a non-reclaimable intent BEFORE crossing the provider boundary.

        Reuses the accepted RUNNING -> PENDING_RECONCILIATION owner/fence CAS.
        No new state, grant, migration or direct outbox UPDATE is introduced.
        """
        if claim.lease_owner != worker_id:
            return None
        try:
            with self._pool._connection() as connection:
                with connection.transaction():
                    before = self._read_outbox_state(connection, claim.outbox_id)
                    fields = ("outbox_id", "event_type", "aggregate_type", "aggregate_id",
                              "destination_type", "destination_ref", "payload", "attempt_count",
                              "max_attempts", "lease_fence", "external_effect_id", "last_error_code")
                    if before is None or before.outbox_status != "RUNNING" or any(
                            getattr(before, name) != getattr(claim, name) for name in fields):
                        return None
                    applied = connection.execute(
                        "SELECT propertyai.mark_outbox_pending_reconciliation(%s,%s,%s,%s,%s) AS applied",
                        (claim.outbox_id, worker_id, claim.lease_fence, error_code, before.external_effect_id),
                    ).fetchone()
                    if not applied or not applied["applied"]:
                        return None
                    after = self._read_outbox_state(connection, claim.outbox_id)
                    if after != replace(before, outbox_status="PENDING_RECONCILIATION", last_error_code=error_code):
                        raise PostgresRepositoryError("OUTBOX_DELIVERY_INTENT_READBACK_MISMATCH")
                    return after
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def resolve_outbox_reconciliation_exact(
        self, expected: OutboxReconciliationState, *, resolution: str,
        external_effect_id: str | None, error_code: str | None,
        retry_delay_seconds: int = 0, confirmed_no_effect: bool = False,
    ) -> OutboxReconciliationState | None:
        """Full-row expectation + existing atomic pending-state/fence CAS + readback.

        Async role has no direct outbox UPDATE privilege, so FOR UPDATE is not
        available here. Accepted primitives cannot change an attempt/identity
        while remaining pending at the same fence. Leaving pending either makes
        the function reject its status predicate or requires a new claim/fence
        before re-entering pending. Thus the full read cannot suffer same-fence
        pending ABA through the authorized runtime capability surface.
        """
        if expected.outbox_status != "PENDING_RECONCILIATION" or expected.attempt_count <= 0 or expected.lease_fence <= 0:
            return None
        if resolution not in {"SUCCEEDED", "FAILED_RETRYABLE", "DEAD_LETTER"}:
            raise ValueError("invalid reconciliation resolution")
        if type(confirmed_no_effect) is not bool or (confirmed_no_effect and external_effect_id is not None):
            raise ValueError("conflicting no-effect receipt")
        if resolution == "SUCCEEDED" and not confirmed_no_effect and (
                not isinstance(external_effect_id, str) or not external_effect_id
                or external_effect_id != external_effect_id.strip()):
            raise ValueError("successful reconciliation requires an exact provider receipt")
        try:
            with self._pool._connection() as connection:
                with connection.transaction():
                    if self._read_outbox_state(connection, expected.outbox_id) != expected:
                        return None
                    applied = connection.execute(
                        "SELECT propertyai.resolve_outbox_reconciliation(%s,%s,%s,%s,%s,%s) AS applied",
                        (expected.outbox_id, expected.lease_fence, resolution, external_effect_id,
                         error_code, retry_delay_seconds),
                    ).fetchone()
                    if not applied or not applied["applied"]:
                        return None
                    after = self._read_outbox_state(connection, expected.outbox_id)
                    wanted = replace(expected, outbox_status=resolution, last_error_code=error_code,
                                     external_effect_id=external_effect_id if external_effect_id is not None else expected.external_effect_id)
                    if after != wanted:
                        raise PostgresRepositoryError("OUTBOX_RECONCILIATION_EXACT_READBACK_MISMATCH")
                    return after
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    def _run_fenced_function(self, function_name: str, parameters: tuple[Any, ...]) -> bool:
        placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in parameters)
        query = sql.SQL("SELECT propertyai.{}({}) AS applied").format(
            sql.Identifier(function_name), placeholders
        )
        try:
            with self._pool._connection() as connection:
                with connection.transaction():
                    row = connection.execute(query, parameters).fetchone()
        except psycopg.Error as error:
            raise map_postgres_error(error) from error
        return bool(row and row["applied"])

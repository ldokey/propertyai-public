"""Airbnb W01 provider adapter for Cleaner PostgreSQL business ingress."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from propertyai_core.application.cleaner_pg import (
    CommandMetadata,
    ReservationIngressCommand,
)
from propertyai_core.stage_b.identity import notion_identity


AIRBNB_SOURCE_CHANNEL = "AIRBNB"
KST_OFFSET = "+09:00"


class AirbnbPostgresIngressError(RuntimeError):
    pass


def _local_datetime(day: str, clock: str) -> datetime:
    try:
        value = datetime.fromisoformat(f"{day}T{clock}:00{KST_OFFSET}")
    except (TypeError, ValueError) as error:
        raise AirbnbPostgresIngressError("AIRBNB_SCHEDULE_DATETIME_INVALID") from error
    if value.tzinfo is None or value.utcoffset() is None:
        raise AirbnbPostgresIngressError("AIRBNB_SCHEDULE_DATETIME_NAIVE")
    return value


def _required(mapping: Mapping[str, object], name: str) -> str:
    value = mapping.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AirbnbPostgresIngressError(f"AIRBNB_MAPPING_{name.upper()}_MISSING")
    return value.strip()


def normalize_airbnb_reservation_command(
    *,
    event: Mapping[str, object],
    reservation_code: str,
    mapping: Mapping[str, object],
    authority_epoch: int,
    decided_at: datetime,
) -> ReservationIngressCommand:
    if not isinstance(reservation_code, str) or not reservation_code.strip():
        raise AirbnbPostgresIngressError("AIRBNB_RESERVATION_CODE_MISSING")
    exact_code = reservation_code.strip()
    event_type = event.get("event_type")
    if event_type not in {"BOOKING_CONFIRMED", "BOOKING_UPDATED", "BOOKING_CANCELLED"}:
        raise AirbnbPostgresIngressError("AIRBNB_EVENT_TYPE_UNSUPPORTED")
    source_hash = event.get("source_message_hash")
    if not isinstance(source_hash, str) or not source_hash.strip():
        raise AirbnbPostgresIngressError("AIRBNB_SOURCE_EVENT_ID_MISSING")

    check_in_day = event.get("check_in")
    check_out_day = event.get("check_out")
    if not isinstance(check_in_day, str) or not isinstance(check_out_day, str):
        raise AirbnbPostgresIngressError("AIRBNB_RESERVATION_DATES_MISSING")
    check_in = _local_datetime(check_in_day, _required(mapping, "check_in_time"))
    check_out = _local_datetime(check_out_day, _required(mapping, "check_out_time"))
    cleaning_start = check_out
    cleaning_end = _local_datetime(check_out_day, _required(mapping, "cleaning_end_time"))
    required_minutes = int((cleaning_end - cleaning_start).total_seconds() // 60)
    if required_minutes <= 0:
        raise AirbnbPostgresIngressError("AIRBNB_CLEANING_WINDOW_INVALID")

    property_page_id = _required(mapping, "property_page_id")
    rental_unit_page_id = _required(mapping, "rental_unit_page_id")
    return ReservationIngressCommand(
        metadata=CommandMetadata(
            idempotency_key=f"AIRBNB_RESERVATION_EVENT:{source_hash}",
            source_channel_code=AIRBNB_SOURCE_CHANNEL,
            source_stream_key=exact_code,
            source_event_id=source_hash,
            authority_epoch=authority_epoch,
            decided_at=decided_at,
        ),
        source_channel=AIRBNB_SOURCE_CHANNEL,
        external_reservation_id=exact_code,
        reservation_code=exact_code,
        property_id=notion_identity("property", property_page_id),
        rental_unit_id=notion_identity("rental-unit", rental_unit_page_id),
        reservation_status=("CANCELLED" if event_type == "BOOKING_CANCELLED" else "CONFIRMED"),
        check_in_at=check_in,
        check_out_at=check_out,
        cleaning_code=f"CLEANING-AIRBNB-{exact_code}",
        service_window_start_at=cleaning_start,
        service_deadline_at=cleaning_end,
        required_work_minutes=required_minutes,
    )


__all__ = [
    "AIRBNB_SOURCE_CHANNEL",
    "AirbnbPostgresIngressError",
    "normalize_airbnb_reservation_command",
]

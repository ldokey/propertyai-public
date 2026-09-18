"""Frozen P1 longstay facts: contractual coverage and explicit actual occupancy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID


class LongstayError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Interval:
    start: date
    end_exclusive: date | None

    def __post_init__(self) -> None:
        if self.end_exclusive is not None and self.end_exclusive <= self.start:
            raise LongstayError("VALIDATION_ERROR")

    def overlaps(self, other: "Interval") -> bool:
        return (self.end_exclusive is None or other.start < self.end_exclusive) and (
            other.end_exclusive is None or self.start < other.end_exclusive
        )


@dataclass(frozen=True)
class ActualOccupancy:
    occupancy_id: UUID
    organization_id: UUID
    contract_id: UUID
    resident_id: UUID
    rental_unit_id: UUID
    review_status: str
    actual_start: date | None
    actual_end_exclusive: date | None

    def verified_interval(self) -> Interval | None:
        if self.review_status != "VERIFIED":
            return None
        if self.actual_start is None:
            raise LongstayError("VALIDATION_ERROR")
        return Interval(self.actual_start, self.actual_end_exclusive)


def validate_actual_occupancies(rows: list[ActualOccupancy], linked_residents: dict[UUID, set[UUID]]) -> None:
    """Only explicitly linked different residents in the same contract may co-reside."""
    for row in rows:
        if row.resident_id not in linked_residents.get(row.contract_id, set()):
            raise LongstayError("VALIDATION_ERROR")
        row.verified_interval()
    for index, left in enumerate(rows):
        a = left.verified_interval()
        if a is None:
            continue
        for right in rows[index + 1:]:
            b = right.verified_interval()
            if b is None or left.organization_id != right.organization_id or left.rental_unit_id != right.rental_unit_id:
                continue
            if not a.overlaps(b):
                continue
            if left.resident_id == right.resident_id or left.contract_id != right.contract_id:
                raise LongstayError("PERIOD_CONFLICT")


def confirm_start(row: ActualOccupancy, actual_start: date) -> ActualOccupancy:
    if row.review_status != "NEEDS_REVIEW":
        raise LongstayError("VERSION_CONFLICT")
    updated = ActualOccupancy(**{**row.__dict__, "actual_start": actual_start, "review_status": "VERIFIED"})
    updated.verified_interval()
    return updated


def confirm_end(row: ActualOccupancy, actual_end_exclusive: date) -> ActualOccupancy:
    if row.verified_interval() is None or row.actual_end_exclusive is not None:
        raise LongstayError("VERSION_CONFLICT")
    updated = ActualOccupancy(**{**row.__dict__, "actual_end_exclusive": actual_end_exclusive})
    updated.verified_interval()
    return updated


def correct_dates(row: ActualOccupancy, actual_start: date, actual_end_exclusive: date | None) -> ActualOccupancy:
    if row.verified_interval() is None:
        raise LongstayError("VERSION_CONFLICT")
    updated = ActualOccupancy(**{**row.__dict__, "actual_start": actual_start, "actual_end_exclusive": actual_end_exclusive})
    updated.verified_interval()
    return updated

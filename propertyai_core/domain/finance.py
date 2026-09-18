"""Decimal-primary rent calculation with exact integer-rational boundary checks."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext
from functools import cmp_to_key
from hashlib import sha256
from math import gcd
import json
import re
from uuid import UUID

BIGINT_MAX = 9223372036854775807
POLICY_VERSION = "RENT_APPROVED_1G_2G_3G_4G_V1"
ISSUE_POLICY = "ISSUE_RENT_ELIGIBILITY_D7_V1"
MONEY_CONTEXT_PRECISION = 64


class FinanceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def money(value: str, *, positive: bool = False) -> int:
    if not isinstance(value, str) or re.fullmatch(r"(?:0|[1-9][0-9]*)", value) is None:
        raise FinanceError("VALIDATION_ERROR")
    amount = int(value)
    if amount > BIGINT_MAX or (positive and amount == 0):
        raise FinanceError("VALIDATION_ERROR")
    return amount


def money_text(value: int) -> str:
    if value < 0 or value > BIGINT_MAX:
        raise FinanceError("VALIDATION_ERROR")
    return str(value)


def first_next_month(value: date) -> date:
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


def due_on(due_month: date, due_day: int) -> date:
    if due_month.day != 1 or not 1 <= due_day <= 31:
        raise FinanceError("VALIDATION_ERROR")
    return due_month.replace(day=min(due_day, monthrange(due_month.year, due_month.month)[1]))


def generation_on(due_date: date) -> date:
    return due_date - timedelta(days=7)


@dataclass(frozen=True)
class IssueRentEligibilityV1:
    effective_issue_date: date
    frozen_due_on: date

    def evaluate(self) -> dict[str, str]:
        generation = generation_on(self.frozen_due_on)
        if self.effective_issue_date < generation:
            raise FinanceError("BILLING_NOT_YET_DUE")
        return {
            "policy_id": ISSUE_POLICY,
            "effective_issue_date": self.effective_issue_date.isoformat(),
            "generation_on": generation.isoformat(),
            "date_authority": "APPLICATION_PROPERTY_LOCAL_CALENDAR",
        }


@dataclass(frozen=True)
class ConfirmedTerm:
    term_id: UUID
    version: int
    valid_from: date
    valid_to_exclusive: date | None
    monthly_rent: int
    amount_confirmed: bool
    due_day: int
    cycle_confirmed: bool


@dataclass(frozen=True)
class Segment:
    term_id: UUID
    service_from: date
    service_to_exclusive: date
    monthly_rent: int
    month_days: int
    charge_days: int
    exact_amount: Decimal
    exact_numerator: int
    exact_denominator: int
    line_amount: int = 0

    def wire(self) -> dict:
        return {
            "term_id": str(self.term_id),
            "service_from": self.service_from.isoformat(),
            "service_to_exclusive": self.service_to_exclusive.isoformat(),
            "monthly_rent": money_text(self.monthly_rent),
            "month_days": self.month_days,
            "charge_days": self.charge_days,
            "unrounded_numerator": str(self.exact_numerator),
            "unrounded_denominator": self.exact_denominator,
            "line_amount": money_text(self.line_amount),
        }


@dataclass(frozen=True)
class RentCalculation:
    amount: int
    segments: tuple[Segment, ...]
    fingerprint: str
    snapshot: dict


def _add_ratio(left_numerator: int, left_denominator: int,
               right_numerator: int, right_denominator: int) -> tuple[int, int]:
    common = gcd(left_denominator, right_denominator)
    numerator = (
        left_numerator * (right_denominator // common)
        + right_numerator * (left_denominator // common)
    )
    denominator = (left_denominator // common) * right_denominator
    reduction = gcd(numerator, denominator)
    return numerator // reduction, denominator // reduction


def _round_half_up_exact(numerator: int, denominator: int) -> int:
    quotient, remainder = divmod(numerator, denominator)
    return quotient + (2 * remainder >= denominator)


def _remainder_order(left: Segment, right: Segment) -> int:
    left_remainder = left.exact_numerator % left.exact_denominator
    right_remainder = right.exact_numerator % right.exact_denominator
    cross_left = left_remainder * right.exact_denominator
    cross_right = right_remainder * left.exact_denominator
    if cross_left != cross_right:
        return -1 if cross_left > cross_right else 1
    left_tie = (left.service_from, left.service_to_exclusive, str(left.term_id))
    right_tie = (right.service_from, right.service_to_exclusive, str(right.term_id))
    return (left_tie > right_tie) - (left_tie < right_tie)


def calculate_rent(
    *, contract_id: UUID, contract_version: int, contract_start: date,
    contract_end_exclusive: date, period_id: UUID, period_start: date,
    period_end_exclusive: date, due_date: date, terms: list[ConfirmedTerm],
) -> RentCalculation:
    if contract_end_exclusive <= contract_start or period_end_exclusive <= period_start:
        raise FinanceError("VALIDATION_ERROR")
    start = max(contract_start, period_start)
    end = min(contract_end_exclusive, period_end_exclusive)
    pieces: list[Segment] = []
    exact_total_numerator, exact_total_denominator = 0, 1
    # The calculation has an explicit, operation-local context; process-global
    # Decimal settings cannot change monetary results.
    with localcontext(Context(prec=MONEY_CONTEXT_PRECISION, rounding=ROUND_HALF_UP)) as context:
        decimal_total = context.create_decimal(0)
        if end > start:
            cursor = start
            while cursor < end:
                applicable = [
                    term for term in terms
                    if term.valid_from <= cursor
                    and (term.valid_to_exclusive is None or cursor < term.valid_to_exclusive)
                ]
                if (
                    len(applicable) != 1
                    or not applicable[0].amount_confirmed
                    or not applicable[0].cycle_confirmed
                ):
                    raise FinanceError("CONTRACT_NOT_READY")
                term = applicable[0]
                if term.monthly_rent < 0 or term.monthly_rent > BIGINT_MAX:
                    raise FinanceError("VALIDATION_ERROR")
                boundary = min(end, first_next_month(cursor), term.valid_to_exclusive or end)
                days = (boundary - cursor).days
                month_days = monthrange(cursor.year, cursor.month)[1]
                numerator = term.monthly_rent * days
                decimal_amount = context.divide(
                    context.create_decimal(numerator),
                    context.create_decimal(month_days),
                )
                decimal_total = context.add(decimal_total, decimal_amount)
                exact_total_numerator, exact_total_denominator = _add_ratio(
                    exact_total_numerator, exact_total_denominator, numerator, month_days
                )
                pieces.append(Segment(
                    term.term_id, cursor, boundary, term.monthly_rent, month_days, days,
                    decimal_amount, numerator, month_days,
                ))
                cursor = boundary
        # Decimal is the primary monetary calculation. The exact integer ratio
        # is the authority at a rounding boundary, including a value that lies
        # just to either side of a half after a repeating Decimal expansion.
        decimal_candidate = int(decimal_total.to_integral_value(rounding=ROUND_HALF_UP))
        exact_candidate = _round_half_up_exact(
            exact_total_numerator, exact_total_denominator
        )
        if decimal_candidate != exact_candidate:
            raise FinanceError("INTERNAL_ERROR")
        total = decimal_candidate
    money_text(total)
    floors = [piece.exact_numerator // piece.exact_denominator for piece in pieces]
    residual = total - sum(floors)
    ordered = sorted(pieces, key=cmp_to_key(_remainder_order))
    increments = {id(piece) for piece in ordered[:residual]}
    final = tuple(
        replace(piece, line_amount=floors[index] + (id(piece) in increments))
        for index, piece in enumerate(pieces)
    )
    snapshot = {
        "policy_version": POLICY_VERSION,
        "contract_id": str(contract_id),
        "contract_version": contract_version,
        "period_id": str(period_id),
        "period_start": period_start.isoformat(),
        "period_end_exclusive": period_end_exclusive.isoformat(),
        "due_on": due_date.isoformat(),
        "term_versions": [
            {"id": str(term.term_id), "version": term.version}
            for term in sorted(terms, key=lambda item: str(item.term_id))
        ],
        "segments": [piece.wire() for piece in final],
        "final_amount": total,
    }
    encoded = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return RentCalculation(total, final, sha256(encoded).hexdigest(), snapshot)


def signed_money(value: str) -> int:
    """Frozen signed, nonzero KRW correction amount; never accept binary floats."""
    if not isinstance(value, str) or re.fullmatch(r"-?[1-9][0-9]*", value) is None:
        raise FinanceError("VALIDATION_ERROR")
    amount = int(value)
    if abs(amount) > BIGINT_MAX:
        raise FinanceError("VALIDATION_ERROR")
    return amount


def adjusted_obligation(current: int, delta: int) -> int:
    """Correction arithmetic on already-final KRW facts; no second rounding."""
    if type(current) is not int or type(delta) is not int or not 0 <= current <= BIGINT_MAX:
        raise FinanceError("VALIDATION_ERROR")
    if delta == 0 or abs(delta) > BIGINT_MAX or not 0 <= current + delta <= BIGINT_MAX:
        raise FinanceError("VALIDATION_ERROR")
    return current + delta


def require_finance_balances(*, obligation: int | None = None, principal: int | None = None,
                             allocated: int = 0, returned: int = 0) -> None:
    """Final-state invariants shared by all correction commands, before sealing."""
    amounts = [allocated, returned] + [v for v in (obligation, principal) if v is not None]
    if any(type(v) is not int or not 0 <= v <= BIGINT_MAX for v in amounts):
        raise FinanceError("VALIDATION_ERROR")
    if obligation is not None and allocated > obligation:
        raise FinanceError("RECEIVABLE_OVERALLOCATED")
    if principal is not None and allocated + returned > principal:
        raise FinanceError("ALLOCATION_EXCEEDS_AVAILABLE")

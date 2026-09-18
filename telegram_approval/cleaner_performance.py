#!/usr/bin/env python3
"""Bounded Phase-3 Cleaner performance, policy, urgency, and review contract.

The authoritative business transition always happens outside this module first.
This module owns exactly one downstream immutable Cleaner Performance Event
ledger abstraction plus the finite Policy-28 resolver needed by Phase 3.  It is
not a generic policy/scoring/settlement/event platform.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol
from zoneinfo import ZoneInfo

from propertyai_core.global_writer import assert_current_production_writer
from telegram_approval.cleaner_jobs import NOTION_TOKEN_PATH, NOTION_VERSION


KST = ZoneInfo("Asia/Seoul")
POLICY_28_SOURCE = "1a5d8117-abd8-411c-9eb8-d69216bfc860"
PERFORMANCE_EVENT_SOURCE_ENV = "CLEANER_PERFORMANCE_EVENT_SOURCE_ID"
PERFORMANCE_EVENT_LOCK_DIR = (
    Path(__file__).resolve().parent / "runtime" / "performance-event-locks"
)

JOB_ACCEPTED = "JOB_ACCEPTED"
JOB_COMPLETED = "JOB_COMPLETED"
EARLY_UNAVAILABLE = "EARLY_UNAVAILABLE"
SAME_DAY_UNAVAILABLE = "SAME_DAY_UNAVAILABLE"
NO_SHOW = "NO_SHOW"
URGENT_ACCEPTED = "URGENT_ACCEPTED"
URGENT_COMPLETED = "URGENT_COMPLETED"
EVENT_TYPES = frozenset(
    {
        JOB_ACCEPTED,
        JOB_COMPLETED,
        EARLY_UNAVAILABLE,
        SAME_DAY_UNAVAILABLE,
        NO_SHOW,
        URGENT_ACCEPTED,
        URGENT_COMPLETED,
    }
)
PHASE3_SCORABLE_EVENT_TYPES = frozenset(
    {EARLY_UNAVAILABLE, SAME_DAY_UNAVAILABLE, URGENT_ACCEPTED, URGENT_COMPLETED}
)

NORMAL = "NORMAL"
URGENT = "URGENT"
REPLACEMENT_URGENCIES = frozenset({NORMAL, URGENT})

NOT_APPLICABLE = "NOT_APPLICABLE"
PENDING_REVIEW = "PENDING_REVIEW"
APPROVED = "APPROVED"
OVERRIDDEN = "OVERRIDDEN"
WAIVED = "WAIVED"
FINANCIAL_REVIEW_STATES = frozenset(
    {NOT_APPLICABLE, PENDING_REVIEW, APPROVED, OVERRIDDEN, WAIVED}
)

APPLIED = "APPLIED"
RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
POLICY_RESOLUTION_REQUIRED = "POLICY_RESOLUTION_REQUIRED"
RECONCILIATION_STATES = frozenset(
    {APPLIED, RECONCILIATION_REQUIRED, POLICY_RESOLUTION_REQUIRED}
)

AUTO_TIER_MOVE = False
AUTO_ROLE_CHANGE = False
AUTO_SUSPEND = False
AUTO_BAN = False

# Finite Phase-3 fields only. Existing Policy-28 activation/scope/date fields are
# reused and deliberately not replaced with a generic policy DSL.
POLICY_PROPERTY_NAMES = {
    "version": "Performance Policy Version",
    "replacement_urgent_lead_minutes": "Replacement Urgent Lead Minutes",
    "urgent_premium_krw": "Urgent Premium KRW",
    "same_day_penalty_enabled": "Same-Day Penalty Enabled",
    "same_day_penalty_default_krw": "Same-Day Penalty Default KRW",
    EARLY_UNAVAILABLE: "Score · EARLY_UNAVAILABLE",
    SAME_DAY_UNAVAILABLE: "Score · SAME_DAY_UNAVAILABLE",
    URGENT_ACCEPTED: "Score · URGENT_ACCEPTED",
    URGENT_COMPLETED: "Score · URGENT_COMPLETED",
}

PERFORMANCE_EVENT_SCHEMA = {
    "name": "청소 인력 성과 이벤트 DB_cleaner_performance_event",
    "properties": {
        "Event ID": "title",
        "Event Key": "rich_text",
        "Event Type": {"select": sorted(EVENT_TYPES)},
        "Cleaner": "relation:collection://9da5978c-7c5b-4a1f-af7a-4af24f304b4b",
        "Cleaning": "relation:binding://PROPERTYAI_NOTION_CLEANING_SOURCE_ID",
        "Assignment": "relation:collection://3b43edac-140d-4699-8db1-00020d10652c",
        "Assignment Version": "rich_text",
        "Occurred At": "date",
        "Performance Classification": {
            "select": [EARLY_UNAVAILABLE, SAME_DAY_UNAVAILABLE]
        },
        "Replacement Urgency": {"select": [NORMAL, URGENT]},
        "Policy Version": "rich_text",
        "Default Score Delta": "number",
        "Default Financial Amount": "number",
        "Applied Score Delta": "number",
        "Applied Financial Amount": "number",
        "Override": "checkbox",
        "Override Reason": "rich_text",
        "Operator": "rich_text",
        "Applied At": "date",
        "Financial Review State": {
            "select": [NOT_APPLICABLE, PENDING_REVIEW, APPROVED, OVERRIDDEN, WAIVED]
        },
        "Reconciliation State": {
            "select": [APPLIED, RECONCILIATION_REQUIRED, POLICY_RESOLUTION_REQUIRED]
        },
        "Operator Action Version": "number",
        "Operator Action Key": "rich_text",
        "Data Environment": {"select": ["TEST", "PRODUCTION"]},
        "Created At": "created_time",
    },
}

POLICY_28_SCHEMA_PATCH = {
    POLICY_PROPERTY_NAMES["version"]: "rich_text",
    POLICY_PROPERTY_NAMES["replacement_urgent_lead_minutes"]: "number",
    POLICY_PROPERTY_NAMES["urgent_premium_krw"]: "number",
    POLICY_PROPERTY_NAMES["same_day_penalty_enabled"]: "checkbox",
    POLICY_PROPERTY_NAMES["same_day_penalty_default_krw"]: "number",
    POLICY_PROPERTY_NAMES[EARLY_UNAVAILABLE]: "number",
    POLICY_PROPERTY_NAMES[SAME_DAY_UNAVAILABLE]: "number",
    POLICY_PROPERTY_NAMES[URGENT_ACCEPTED]: "number",
    POLICY_PROPERTY_NAMES[URGENT_COMPLETED]: "number",
}

ASSIGNMENT_19_SCHEMA_PATCH = {
    "Replacement Urgency": {"select": [NORMAL, URGENT]},
    "Base Fee Snapshot": "number",
    "Urgent Premium Snapshot": "number",
    "Total Agreed Fee Snapshot": "number",
    "Urgent Premium Policy Version": "rich_text",
}


class PerformancePolicyError(RuntimeError):
    pass


class PerformanceEventError(RuntimeError):
    pass


class PerformanceReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class PerformancePolicy:
    version: str
    score_deltas: Mapping[str, int | None]
    same_day_penalty_enabled: bool
    same_day_penalty_default_krw: int | None
    urgent_premium_krw: int | None
    replacement_urgent_lead_minutes: int | None
    effective_from: datetime | None = None
    effective_until: datetime | None = None

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version.strip():
            raise PerformancePolicyError("Performance Policy Version is required")
        for event_type in PHASE3_SCORABLE_EVENT_TYPES:
            value = self.score_deltas.get(event_type)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise PerformancePolicyError(f"score delta malformed: {event_type}")
        if self.replacement_urgent_lead_minutes is not None:
            if (
                isinstance(self.replacement_urgent_lead_minutes, bool)
                or not isinstance(self.replacement_urgent_lead_minutes, int)
                or self.replacement_urgent_lead_minutes < 0
            ):
                raise PerformancePolicyError("replacement urgent lead minutes malformed")
        if self.urgent_premium_krw is not None:
            _money(self.urgent_premium_krw, "urgent premium")
        if self.same_day_penalty_default_krw is not None:
            _money(self.same_day_penalty_default_krw, "same-day penalty")
        if self.same_day_penalty_enabled and self.same_day_penalty_default_krw is None:
            raise PerformancePolicyError(
                "enabled same-day penalty requires a configured default amount"
            )


@dataclass(frozen=True)
class OfferEconomics:
    replacement_urgency: str
    base_fee_krw: int
    urgent_premium_krw: int
    total_agreed_fee_krw: int
    urgent_premium_policy_version: str | None


class PerformancePolicyStore(Protocol):
    def resolve_at(self, occurred_at: datetime) -> PerformancePolicy: ...


class PerformanceEventStore(Protocol):
    def query_by_key(self, event_key: str) -> list[dict]: ...
    def create_event(self, event: dict) -> dict: ...
    def update_event(self, event_id: str, *, changes: dict) -> dict: ...
    def query_for_cleaner(self, cleaner_party_page_id: str) -> list[dict]: ...


def _money(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer KRW amount")
    return value


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _parse_kst_datetime(value: str | datetime, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{label} malformed") from exc
    else:
        raise ValueError(f"{label} malformed")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def _cleaning_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return _parse_kst_datetime(value, "Cleaning date").date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Cleaning date malformed")
    raw = value.strip()
    try:
        if len(raw) == 10:
            return date.fromisoformat(raw)
        return _parse_kst_datetime(raw, "Cleaning date").date()
    except ValueError as exc:
        raise ValueError("Cleaning date malformed") from exc


def classify_unavailable(
    *, cleaning_date: str | date | datetime, occurred_at: datetime
) -> str:
    """Classify by KST calendar date, never by an arbitrary rolling 24/48h cutoff."""

    occurred_local = _aware(occurred_at, "Occurred At").astimezone(KST)
    target_date = _cleaning_date(cleaning_date)
    if occurred_local.date() < target_date:
        return EARLY_UNAVAILABLE
    if occurred_local.date() == target_date:
        return SAME_DAY_UNAVAILABLE
    raise ValueError("unavailable occurrence is after the Cleaning calendar date")


def resolve_replacement_urgency(
    *,
    classification: str,
    occurred_at: datetime,
    cleaning_start_at: str | datetime,
    policy: PerformancePolicy,
) -> str:
    if classification == SAME_DAY_UNAVAILABLE:
        return URGENT
    if classification != EARLY_UNAVAILABLE:
        raise ValueError("unsupported unavailable classification")
    threshold = policy.replacement_urgent_lead_minutes
    if threshold is None:
        return NORMAL
    occurred_local = _aware(occurred_at, "Occurred At").astimezone(KST)
    start_local = _parse_kst_datetime(cleaning_start_at, "Cleaning start")
    lead_minutes = (start_local - occurred_local).total_seconds() / 60.0
    if lead_minutes <= 0:
        raise ValueError("unavailable must occur before Cleaning start")
    return URGENT if lead_minutes <= threshold else NORMAL


def quote_replacement_offer(
    *, base_fee_krw: int, replacement_urgency: str, policy: PerformancePolicy | None
) -> OfferEconomics:
    base = _money(base_fee_krw, "base fee")
    if replacement_urgency == NORMAL:
        return OfferEconomics(NORMAL, base, 0, base, None)
    if replacement_urgency != URGENT:
        raise ValueError("replacement urgency malformed")
    if policy is None or policy.urgent_premium_krw is None:
        raise PerformancePolicyError(
            "urgent Offer requires a resolved configured urgent premium"
        )
    premium = _money(policy.urgent_premium_krw, "urgent premium")
    return OfferEconomics(URGENT, base, premium, base + premium, policy.version)


def canonical_offer_economics(record: Mapping[str, object]) -> OfferEconomics | None:
    """Validate frozen action economics while preserving pre-Phase3 legacy records."""

    base_raw = record.get("cleaning_fee_krw")
    new_keys = {
        "replacement_urgency",
        "urgent_premium_krw",
        "total_agreed_fee_krw",
        "urgent_premium_policy_version",
    }
    has_new = any(key in record for key in new_keys)
    if base_raw is None and not has_new:
        # Old focused receipt fixtures and genuinely old historical actions can
        # remain valid without inventing an economic snapshot that never existed.
        return None
    base = _money(base_raw, "base cleaning fee")
    urgency_raw = record.get("replacement_urgency")
    if urgency_raw is None and not has_new:
        return OfferEconomics(NORMAL, base, 0, base, None)
    if not isinstance(urgency_raw, str) or urgency_raw not in REPLACEMENT_URGENCIES:
        raise ValueError("replacement urgency malformed")
    urgency = urgency_raw
    premium_raw = record.get("urgent_premium_krw")
    total_raw = record.get("total_agreed_fee_krw")
    if urgency == NORMAL:
        premium = 0 if premium_raw is None else _money(premium_raw, "urgent premium")
        total = base if total_raw is None else _money(total_raw, "total agreed fee")
        if premium != 0 or total != base:
            raise ValueError("NORMAL assignment economics conflict")
        policy_version = record.get("urgent_premium_policy_version")
        if policy_version not in (None, ""):
            raise ValueError("NORMAL assignment cannot carry urgent premium policy")
        return OfferEconomics(NORMAL, base, 0, base, None)
    if premium_raw is None or total_raw is None:
        raise ValueError("URGENT assignment requires frozen premium and total")
    premium = _money(premium_raw, "urgent premium")
    total = _money(total_raw, "total agreed fee")
    if total != base + premium:
        raise ValueError("URGENT total agreed fee does not equal base + premium")
    policy_version = record.get("urgent_premium_policy_version")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ValueError("URGENT assignment requires premium policy version")
    return OfferEconomics(URGENT, base, premium, total, policy_version.strip())


def payout_amount_from_record(record: Mapping[str, object]) -> int:
    economics = canonical_offer_economics(record)
    if economics is None:
        base = record.get("cleaning_fee_krw")
        if base is None:
            raise ValueError("payment amount is unavailable")
        return _money(base, "base cleaning fee")
    return economics.total_agreed_fee_krw


def event_key(*, business_key: str, event_type: str) -> str:
    if event_type not in EVENT_TYPES:
        raise ValueError("unsupported performance event type")
    if not isinstance(business_key, str) or not business_key.strip():
        raise ValueError("business event identity is required")
    # Business keys are already canonical immutable identities. Keep them human
    # inspectable and bounded for Notion rich-text equality queries.
    value = f"PERF:{business_key.strip()}:{event_type}"
    if len(value) > 512:
        raise ValueError("performance Event Key exceeds bounded length")
    return value


def _policy_score(policy: PerformancePolicy, event_type: str) -> int | None:
    if event_type not in PHASE3_SCORABLE_EVENT_TYPES:
        return None
    return policy.score_deltas.get(event_type)


def build_event(
    *,
    business_key: str,
    event_type: str,
    cleaner_party_page_id: str,
    cleaning_page_id: str,
    assignment_page_id: str | None,
    assignment_version: str | None,
    occurred_at: datetime,
    policy: PerformancePolicy,
    performance_classification: str | None = None,
    replacement_urgency: str | None = None,
    data_environment: str = "PRODUCTION",
) -> dict:
    if event_type not in EVENT_TYPES:
        raise PerformanceEventError("unsupported performance Event Type")
    if data_environment not in {"TEST", "PRODUCTION"}:
        raise PerformanceEventError("Data Environment malformed")
    occurred = _aware(occurred_at, "Occurred At")
    default_score = _policy_score(policy, event_type)
    default_financial = None
    review_state = NOT_APPLICABLE
    if event_type == SAME_DAY_UNAVAILABLE and policy.same_day_penalty_enabled:
        if policy.same_day_penalty_default_krw is None:
            raise PerformancePolicyError("same-day penalty policy amount unresolved")
        default_financial = policy.same_day_penalty_default_krw
        review_state = PENDING_REVIEW
    return {
        "event_id": event_key(business_key=business_key, event_type=event_type),
        "event_key": event_key(business_key=business_key, event_type=event_type),
        "event_type": event_type,
        "cleaner_party_page_id": cleaner_party_page_id,
        "cleaning_page_id": cleaning_page_id,
        "assignment_page_id": assignment_page_id,
        "assignment_version": assignment_version,
        "occurred_at": occurred.isoformat(),
        "performance_classification": performance_classification,
        "replacement_urgency": replacement_urgency,
        "policy_version": policy.version,
        "default_score_delta": default_score,
        "default_financial_amount": default_financial,
        "applied_score_delta": default_score,
        "applied_financial_amount": None,
        "override": False,
        "override_reason": None,
        "operator": None,
        "applied_at": occurred.isoformat() if default_score is not None else None,
        "financial_review_state": review_state,
        "reconciliation_state": APPLIED,
        "operator_action_version": None,
        "operator_action_key": None,
        "data_environment": data_environment,
    }


def _minute_precision_event_timestamp(value: object) -> str:
    """Normalize Notion date readback to its persisted minute precision."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise PerformanceEventError("Occurred At identity malformed") from exc
    else:
        raise PerformanceEventError("Occurred At identity malformed")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PerformanceEventError("Occurred At identity must be timezone-aware")
    return (
        parsed.astimezone(timezone.utc)
        .replace(second=0, microsecond=0)
        .isoformat()
    )


def _immutable_event_identity(event: Mapping[str, object]) -> tuple:
    keys = (
        "event_key",
        "event_type",
        "cleaner_party_page_id",
        "cleaning_page_id",
        "assignment_page_id",
        "assignment_version",
        "occurred_at",
        "performance_classification",
        "replacement_urgency",
        "policy_version",
        "default_score_delta",
        "default_financial_amount",
        "data_environment",
    )
    return tuple(
        _minute_precision_event_timestamp(event.get(key))
        if key == "occurred_at"
        else event.get(key)
        for key in keys
    )


def append_performance_event(*, store: PerformanceEventStore, event: dict) -> dict:
    """Append at most one immutable Event for one business fact."""

    key = str(event.get("event_key") or "")
    if not key:
        raise PerformanceEventError("Event Key missing")
    with _event_lock(key):
        existing = store.query_by_key(key)
        if len(existing) > 1:
            raise PerformanceEventError("multiple rows share one Event Key")
        if existing:
            if _immutable_event_identity(existing[0]) != _immutable_event_identity(event):
                raise PerformanceEventError("Event Key belongs to a conflicting business fact")
            return copy.deepcopy(existing[0])
        try:
            created = store.create_event(copy.deepcopy(event))
        except Exception:
            recovered = store.query_by_key(key)
            if len(recovered) == 1 and _immutable_event_identity(recovered[0]) == _immutable_event_identity(event):
                return copy.deepcopy(recovered[0])
            raise
        converged = store.query_by_key(key)
        if len(converged) != 1:
            raise PerformanceEventError("Event create did not converge to one row")
        if _immutable_event_identity(converged[0]) != _immutable_event_identity(event):
            raise PerformanceEventError("created Event identity changed")
        return copy.deepcopy(converged[0])


def _require_operator_action(
    *, reason: str | None, operator: str, action_key: str, action_version: int
) -> tuple[str, str, str, int]:
    if not isinstance(reason, str) or not reason.strip():
        raise PerformanceReviewError("Override Reason is required")
    if not isinstance(operator, str) or not operator.strip():
        raise PerformanceReviewError("Operator is required")
    if not isinstance(action_key, str) or not action_key.strip():
        raise PerformanceReviewError("operator action key is required")
    if isinstance(action_version, bool) or not isinstance(action_version, int) or action_version < 1:
        raise PerformanceReviewError("operator action version must be >= 1")
    return reason.strip(), operator.strip(), action_key.strip(), action_version


def _check_operator_action_replay(event: dict, *, action_key: str, action_version: int, desired: dict) -> bool:
    existing_version = event.get("operator_action_version")
    existing_key = event.get("operator_action_key")
    if existing_version is None:
        return False
    if action_version < int(existing_version):
        raise PerformanceReviewError("stale operator action version")
    if action_version == int(existing_version):
        if existing_key == action_key and all(event.get(k) == v for k, v in desired.items()):
            return True
        raise PerformanceReviewError("conflicting operator action requires a newer version")
    return False


def override_score(
    *,
    store: PerformanceEventStore,
    event_key_value: str,
    applied_score_delta: int,
    reason: str,
    operator: str,
    action_key: str,
    action_version: int,
    applied_at: datetime,
) -> dict:
    if isinstance(applied_score_delta, bool) or not isinstance(applied_score_delta, int):
        raise PerformanceReviewError("applied score delta must be an integer")
    reason, operator, action_key, action_version = _require_operator_action(
        reason=reason, operator=operator, action_key=action_key, action_version=action_version
    )
    _aware(applied_at, "Applied At")
    with _event_lock(event_key_value):
        rows = store.query_by_key(event_key_value)
        if len(rows) != 1:
            raise PerformanceReviewError("exact Performance Event not found")
        event = rows[0]
        desired = {
            "applied_score_delta": applied_score_delta,
            "override": True,
            "override_reason": reason,
            "operator": operator,
        }
        if _check_operator_action_replay(
            event, action_key=action_key, action_version=action_version, desired=desired
        ):
            return copy.deepcopy(event)
        changes = {
            **desired,
            "applied_at": applied_at.isoformat(),
            "operator_action_key": action_key,
            "operator_action_version": action_version,
        }
        return store.update_event(str(event.get("event_id") or event_key_value), changes=changes)


def review_same_day_penalty(
    *,
    store: PerformanceEventStore,
    event_key_value: str,
    decision: str,
    operator: str,
    action_key: str,
    action_version: int,
    applied_at: datetime,
    applied_amount_krw: int | None = None,
    reason: str | None = None,
) -> dict:
    if decision not in {APPROVED, OVERRIDDEN, WAIVED}:
        if decision == "POSTED":
            raise PerformanceReviewError("negative Cleaner Finance POST is out of Phase-3 scope")
        raise PerformanceReviewError("unsupported penalty review decision")
    _aware(applied_at, "Applied At")
    if not isinstance(operator, str) or not operator.strip():
        raise PerformanceReviewError("Operator is required")
    if not isinstance(action_key, str) or not action_key.strip():
        raise PerformanceReviewError("operator action key is required")
    if isinstance(action_version, bool) or not isinstance(action_version, int) or action_version < 1:
        raise PerformanceReviewError("operator action version must be >= 1")
    with _event_lock(event_key_value):
        rows = store.query_by_key(event_key_value)
        if len(rows) != 1:
            raise PerformanceReviewError("exact Performance Event not found")
        event = rows[0]
        if event.get("event_type") != SAME_DAY_UNAVAILABLE:
            raise PerformanceReviewError("financial penalty review requires SAME_DAY_UNAVAILABLE")
        if event.get("financial_review_state") == NOT_APPLICABLE:
            raise PerformanceReviewError("same-day financial penalty is not policy-applicable")
        default_amount = event.get("default_financial_amount")
        if default_amount is None:
            raise PerformanceReviewError("default financial amount missing")
        if decision == APPROVED:
            if applied_amount_krw not in (None, default_amount):
                raise PerformanceReviewError("edited amount must use OVERRIDDEN")
            desired = {
                "financial_review_state": APPROVED,
                "applied_financial_amount": int(default_amount),
                "override": bool(event.get("override")),
                "override_reason": event.get("override_reason"),
                "operator": operator.strip(),
            }
        elif decision == OVERRIDDEN:
            reason, operator_norm, action_key_norm, action_version_norm = _require_operator_action(
                reason=reason,
                operator=operator,
                action_key=action_key,
                action_version=action_version,
            )
            operator, action_key, action_version = operator_norm, action_key_norm, action_version_norm
            if applied_amount_krw is None:
                raise PerformanceReviewError("OVERRIDDEN penalty requires an applied amount")
            amount = _money(applied_amount_krw, "applied financial amount")
            desired = {
                "financial_review_state": OVERRIDDEN,
                "applied_financial_amount": amount,
                "override": True,
                "override_reason": reason,
                "operator": operator,
            }
        else:
            reason, operator_norm, action_key_norm, action_version_norm = _require_operator_action(
                reason=reason,
                operator=operator,
                action_key=action_key,
                action_version=action_version,
            )
            operator, action_key, action_version = operator_norm, action_key_norm, action_version_norm
            desired = {
                "financial_review_state": WAIVED,
                "applied_financial_amount": 0,
                "override": True,
                "override_reason": reason,
                "operator": operator,
            }
        if _check_operator_action_replay(
            event, action_key=action_key, action_version=action_version, desired=desired
        ):
            return copy.deepcopy(event)
        return store.update_event(
            str(event.get("event_id") or event_key_value),
            changes={
                **desired,
                "applied_at": applied_at.isoformat(),
                "operator_action_key": action_key,
                "operator_action_version": action_version,
            },
        )


def performance_summary(
    *, store: PerformanceEventStore, cleaner_party_page_id: str, now: datetime
) -> dict:
    now = _aware(now, "summary now")
    cutoff = now - timedelta(days=90)
    rows = sorted(
        store.query_for_cleaner(cleaner_party_page_id),
        key=lambda item: item.get("occurred_at") or "",
        reverse=True,
    )

    def counters(items: Iterable[dict]) -> dict:
        values = {
            "completed": 0,
            "early_unavailable": 0,
            "same_day_unavailable": 0,
            "no_show": 0,
            "urgent_accepted": 0,
            "urgent_completed": 0,
        }
        score = 0
        score_known = False
        for item in items:
            event_type = item.get("event_type")
            if event_type in {JOB_COMPLETED, URGENT_COMPLETED}:
                values["completed"] += 1
            if event_type == EARLY_UNAVAILABLE:
                values["early_unavailable"] += 1
            elif event_type == SAME_DAY_UNAVAILABLE:
                values["same_day_unavailable"] += 1
            elif event_type == NO_SHOW:
                values["no_show"] += 1
            elif event_type == URGENT_ACCEPTED:
                values["urgent_accepted"] += 1
            elif event_type == URGENT_COMPLETED:
                values["urgent_completed"] += 1
            applied_score = item.get("applied_score_delta")
            if isinstance(applied_score, int) and not isinstance(applied_score, bool):
                score += applied_score
                score_known = True
        values["derived_score"] = score if score_known else None
        return values

    recent = []
    for row in rows:
        try:
            occurred = datetime.fromisoformat(str(row["occurred_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError, TypeError):
            raise PerformanceEventError("stored event occurred_at malformed")
        if occurred.tzinfo is None or occurred.utcoffset() is None:
            raise PerformanceEventError("stored event occurred_at must be timezone-aware")
        if occurred >= cutoff:
            recent.append(row)
    return {
        "recent_90_days": counters(recent),
        "lifetime": counters(rows),
        "recent_events": copy.deepcopy(rows[:10]),
    }


def format_ops_performance_summary(*, cleaner_label: str, summary: Mapping[str, object]) -> str:
    recent = summary["recent_90_days"]
    lifetime = summary["lifetime"]

    def section(label: str, values: Mapping[str, object]) -> list[str]:
        lines = [label]
        lines.extend(
            [
                f"완료 {values['completed']}",
                f"사전 수행불가 {values['early_unavailable']}",
                f"당일 수행불가 {values['same_day_unavailable']}",
                f"노쇼 {values['no_show']}",
                f"긴급 대체 수락 {values['urgent_accepted']}",
                f"긴급 대체 완료 {values['urgent_completed']}",
            ]
        )
        if values.get("derived_score") is not None:
            lines.append(f"적용 점수 합계 {values['derived_score']}")
        return lines

    lines = [f"Cleaner {cleaner_label}"]
    lines += section("최근 90일", recent)
    lines += section("전체", lifetime)
    lines.append("최근 기록")
    for item in summary.get("recent_events", []):
        lines.append(f"{item.get('occurred_at')} · {item.get('event_type')}")
    return "\n".join(lines)


class InMemoryPerformancePolicyStore:
    def __init__(self, policies: Iterable[PerformancePolicy]):
        self.policies = list(policies)

    def resolve_at(self, occurred_at: datetime) -> PerformancePolicy:
        when = _aware(occurred_at, "Occurred At")
        matches = []
        for policy in self.policies:
            if policy.effective_from is not None and when < policy.effective_from:
                continue
            if policy.effective_until is not None and when >= policy.effective_until:
                continue
            matches.append(policy)
        if len(matches) != 1:
            raise PerformancePolicyError(
                "exactly one Performance Policy must resolve for the occurrence"
            )
        return matches[0]


class InMemoryPerformanceEventStore:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.create_calls = 0
        self.update_calls = 0
        self.fail_next_create = False
        self.create_then_fail = False

    def query_by_key(self, event_key_value: str) -> list[dict]:
        item = self.events.get(event_key_value)
        return [] if item is None else [copy.deepcopy(item)]

    def create_event(self, event: dict) -> dict:
        self.create_calls += 1
        key = event["event_key"]
        if key in self.events:
            raise PerformanceEventError("duplicate Event Key")
        if self.fail_next_create:
            self.fail_next_create = False
            raise PerformanceEventError("synthetic create failure")
        item = copy.deepcopy(event)
        item.setdefault("event_id", key)
        self.events[key] = item
        if self.create_then_fail:
            self.create_then_fail = False
            raise PerformanceEventError("synthetic response loss")
        return copy.deepcopy(item)

    def update_event(self, event_id: str, *, changes: dict) -> dict:
        self.update_calls += 1
        target_key = next(
            (key for key, value in self.events.items() if value.get("event_id") == event_id),
            event_id if event_id in self.events else None,
        )
        if target_key is None:
            raise PerformanceEventError("event not found")
        self.events[target_key].update(copy.deepcopy(changes))
        return copy.deepcopy(self.events[target_key])

    def query_for_cleaner(self, cleaner_party_page_id: str) -> list[dict]:
        return [
            copy.deepcopy(value)
            for value in self.events.values()
            if value.get("cleaner_party_page_id") == cleaner_party_page_id
        ]


class NotionPerformancePolicyStore:
    """Read-only Policy-28 adapter. Missing schema/value fails closed."""

    def __init__(self, *, token_path: Path = NOTION_TOKEN_PATH, urlopen=urllib.request.urlopen):
        self._token_path = token_path
        self._urlopen = urlopen

    def _headers(self) -> dict[str, str]:
        token = self._token_path.read_text().strip()
        if not token:
            raise PerformancePolicyError("Notion token unavailable")
        return {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _query(self) -> list[dict]:
        body = {
            "filter": {
                "and": [
                    {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
                    {"property": "활성", "checkbox": {"equals": True}},
                ]
            },
            "page_size": 100,
        }
        req = urllib.request.Request(
            f"https://api.notion.com/v1/data_sources/{POLICY_28_SOURCE}/query",
            data=json.dumps(body, ensure_ascii=False).encode(),
            method="POST",
            headers=self._headers(),
        )
        try:
            with self._urlopen(req, timeout=40) as response:
                payload = json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise PerformancePolicyError("Policy-28 read failed") from exc
        rows = payload.get("results")
        if not isinstance(rows, list) or payload.get("has_more") is not False:
            raise PerformancePolicyError("Policy-28 response malformed or paginated")
        return rows

    @staticmethod
    def _text(page: dict, name: str) -> str | None:
        values = page.get("properties", {}).get(name, {}).get("rich_text")
        if not isinstance(values, list) or len(values) != 1:
            return None
        item = values[0]
        return item.get("plain_text") or (item.get("text") or {}).get("content")

    @staticmethod
    def _number(page: dict, name: str) -> int | None:
        value = page.get("properties", {}).get(name, {}).get("number")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _checkbox(page: dict, name: str) -> bool | None:
        value = page.get("properties", {}).get(name, {}).get("checkbox")
        return value if isinstance(value, bool) else None

    @staticmethod
    def _date(page: dict, name: str) -> datetime | None:
        value = (page.get("properties", {}).get(name, {}).get("date") or {}).get("start")
        if not value:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=KST)
        return parsed

    def resolve_at(self, occurred_at: datetime) -> PerformancePolicy:
        when = _aware(occurred_at, "Occurred At")
        matches = []
        for row in self._query():
            starts = self._date(row, "적용 시작일")
            ends = self._date(row, "적용 종료일")
            if starts is not None and when < starts:
                continue
            if ends is not None and when >= ends:
                continue
            matches.append(row)
        if len(matches) != 1:
            raise PerformancePolicyError(
                "exactly one active Production Policy-28 row must apply"
            )
        row = matches[0]
        version = self._text(row, POLICY_PROPERTY_NAMES["version"])
        enabled = self._checkbox(row, POLICY_PROPERTY_NAMES["same_day_penalty_enabled"])
        if version is None or enabled is None:
            raise PerformancePolicyError("Phase-3 Policy-28 schema/value is unresolved")
        return PerformancePolicy(
            version=version,
            score_deltas={
                event_type: self._number(row, POLICY_PROPERTY_NAMES[event_type])
                for event_type in PHASE3_SCORABLE_EVENT_TYPES
            },
            same_day_penalty_enabled=enabled,
            same_day_penalty_default_krw=self._number(
                row, POLICY_PROPERTY_NAMES["same_day_penalty_default_krw"]
            ),
            urgent_premium_krw=self._number(row, POLICY_PROPERTY_NAMES["urgent_premium_krw"]),
            replacement_urgent_lead_minutes=self._number(
                row, POLICY_PROPERTY_NAMES["replacement_urgent_lead_minutes"]
            ),
            effective_from=self._date(row, "적용 시작일"),
            effective_until=self._date(row, "적용 종료일"),
        )


class NotionPerformanceEventStore:
    """Single new Event-ledger adapter; source ID is supplied only after schema gate."""

    def __init__(
        self,
        *,
        source_id: str | None = None,
        token_path: Path = NOTION_TOKEN_PATH,
        urlopen=urllib.request.urlopen,
        authority_assert: Callable[[], object] = assert_current_production_writer,
    ):
        self.source_id = source_id or os.environ.get(PERFORMANCE_EVENT_SOURCE_ENV)
        if not self.source_id:
            raise PerformanceEventError(
                "Cleaner Performance Event data source is not provisioned"
            )
        self._token_path = token_path
        self._urlopen = urlopen
        self._authority_assert = authority_assert

    def _headers(self) -> dict[str, str]:
        token = self._token_path.read_text().strip()
        if not token:
            raise PerformanceEventError("Notion token unavailable")
        return {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body: dict | None = None, *, mutating=False) -> dict:
        if mutating:
            self._authority_assert()
        req = urllib.request.Request(
            "https://api.notion.com" + path,
            data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
            method=method,
            headers=self._headers(),
        )
        try:
            with self._urlopen(req, timeout=40) as response:
                payload = json.loads(response.read())
        except (OSError, ValueError, urllib.error.URLError) as exc:
            if mutating:
                self._authority_assert()
            raise PerformanceEventError("Performance Event Notion request failed") from exc
        if mutating:
            self._authority_assert()
        if not isinstance(payload, dict):
            raise PerformanceEventError("Performance Event Notion response malformed")
        return payload

    @staticmethod
    def _rt(value: str | None) -> dict:
        return {"rich_text": [] if value is None else [{"type": "text", "text": {"content": str(value)}}]}

    @staticmethod
    def _select(value: str | None) -> dict:
        return {"select": None if value is None else {"name": value}}

    @staticmethod
    def _relation(value: str | None) -> dict:
        return {"relation": [] if value is None else [{"id": value}]}

    @staticmethod
    def _number(value: int | None) -> dict:
        return {"number": value}

    @staticmethod
    def _date(value: str | None) -> dict:
        return {"date": None if value is None else {"start": value}}

    def _properties(self, event: Mapping[str, object]) -> dict:
        return {
            "Event ID": {"title": [{"type": "text", "text": {"content": str(event["event_id"])}}]},
            "Event Key": self._rt(str(event["event_key"])),
            "Event Type": self._select(str(event["event_type"])),
            "Cleaner": self._relation(str(event["cleaner_party_page_id"])),
            "Cleaning": self._relation(str(event["cleaning_page_id"])),
            "Assignment": self._relation(event.get("assignment_page_id")),
            "Assignment Version": self._rt(event.get("assignment_version")),
            "Occurred At": self._date(str(event["occurred_at"])),
            "Performance Classification": self._select(event.get("performance_classification")),
            "Replacement Urgency": self._select(event.get("replacement_urgency")),
            "Policy Version": self._rt(str(event["policy_version"])),
            "Default Score Delta": self._number(event.get("default_score_delta")),
            "Default Financial Amount": self._number(event.get("default_financial_amount")),
            "Applied Score Delta": self._number(event.get("applied_score_delta")),
            "Applied Financial Amount": self._number(event.get("applied_financial_amount")),
            "Override": {"checkbox": bool(event.get("override"))},
            "Override Reason": self._rt(event.get("override_reason")),
            "Operator": self._rt(event.get("operator")),
            "Applied At": self._date(event.get("applied_at")),
            "Financial Review State": self._select(str(event["financial_review_state"])),
            "Reconciliation State": self._select(str(event["reconciliation_state"])),
            "Operator Action Version": self._number(event.get("operator_action_version")),
            "Operator Action Key": self._rt(event.get("operator_action_key")),
            "Data Environment": self._select(str(event["data_environment"])),
        }

    @staticmethod
    def _plain(prop: dict) -> str | None:
        values = prop.get("rich_text") or prop.get("title")
        if not isinstance(values, list) or not values:
            return None
        return "".join(item.get("plain_text") or (item.get("text") or {}).get("content", "") for item in values)

    def _from_page(self, page: dict) -> dict:
        p = page.get("properties", {})
        def sel(name):
            return (p.get(name, {}).get("select") or {}).get("name")
        def num(name):
            return p.get(name, {}).get("number")
        def rel(name):
            values = p.get(name, {}).get("relation") or []
            return values[0]["id"] if len(values) == 1 else None
        def dat(name):
            return (p.get(name, {}).get("date") or {}).get("start")
        return {
            "event_id": page.get("id") or self._plain(p.get("Event ID", {})),
            "event_key": self._plain(p.get("Event Key", {})),
            "event_type": sel("Event Type"),
            "cleaner_party_page_id": rel("Cleaner"),
            "cleaning_page_id": rel("Cleaning"),
            "assignment_page_id": rel("Assignment"),
            "assignment_version": self._plain(p.get("Assignment Version", {})),
            "occurred_at": dat("Occurred At"),
            "performance_classification": sel("Performance Classification"),
            "replacement_urgency": sel("Replacement Urgency"),
            "policy_version": self._plain(p.get("Policy Version", {})),
            "default_score_delta": num("Default Score Delta"),
            "default_financial_amount": num("Default Financial Amount"),
            "applied_score_delta": num("Applied Score Delta"),
            "applied_financial_amount": num("Applied Financial Amount"),
            "override": p.get("Override", {}).get("checkbox", False),
            "override_reason": self._plain(p.get("Override Reason", {})),
            "operator": self._plain(p.get("Operator", {})),
            "applied_at": dat("Applied At"),
            "financial_review_state": sel("Financial Review State"),
            "reconciliation_state": sel("Reconciliation State"),
            "operator_action_version": num("Operator Action Version"),
            "operator_action_key": self._plain(p.get("Operator Action Key", {})),
            "data_environment": sel("Data Environment"),
        }

    def query_by_key(self, event_key_value: str) -> list[dict]:
        payload = self._request(
            "POST",
            f"/v1/data_sources/{self.source_id}/query",
            {
                "filter": {"and": [
                    {"property": "Data Environment", "select": {"equals": "PRODUCTION"}},
                    {"property": "Event Key", "rich_text": {"equals": event_key_value}},
                ]},
                "page_size": 3,
            },
        )
        if payload.get("has_more") is not False:
            raise PerformanceEventError("Event Key query unexpectedly paginated")
        return [self._from_page(row) for row in payload.get("results", [])]

    def create_event(self, event: dict) -> dict:
        page = self._request(
            "POST",
            "/v1/pages",
            {
                "parent": {"type": "data_source_id", "data_source_id": self.source_id},
                "properties": self._properties(event),
            },
            mutating=True,
        )
        result = self._from_page(page)
        result["event_id"] = page.get("id")
        return result

    def update_event(self, event_id: str, *, changes: dict) -> dict:
        allowed = {
            "applied_score_delta",
            "applied_financial_amount",
            "override",
            "override_reason",
            "operator",
            "applied_at",
            "financial_review_state",
            "operator_action_version",
            "operator_action_key",
        }
        if not set(changes).issubset(allowed):
            raise PerformanceEventError("immutable Event fields cannot be updated")
        mapping = {
            "applied_score_delta": ("Applied Score Delta", self._number),
            "applied_financial_amount": ("Applied Financial Amount", self._number),
            "override": ("Override", lambda value: {"checkbox": bool(value)}),
            "override_reason": ("Override Reason", self._rt),
            "operator": ("Operator", self._rt),
            "applied_at": ("Applied At", self._date),
            "financial_review_state": ("Financial Review State", self._select),
            "operator_action_version": ("Operator Action Version", self._number),
            "operator_action_key": ("Operator Action Key", self._rt),
        }
        properties = {mapping[key][0]: mapping[key][1](value) for key, value in changes.items()}
        page = self._request(
            "PATCH", f"/v1/pages/{event_id}", {"properties": properties}, mutating=True
        )
        return self._from_page(page)

    def query_for_cleaner(self, cleaner_party_page_id: str) -> list[dict]:
        payload = self._request(
            "POST",
            f"/v1/data_sources/{self.source_id}/query",
            {
                "filter": {"and": [
                    {"property": "Data Environment", "select": {"equals": "PRODUCTION"}},
                    {"property": "Cleaner", "relation": {"contains": cleaner_party_page_id}},
                ]},
                "sorts": [{"property": "Occurred At", "direction": "descending"}],
                "page_size": 100,
            },
        )
        if payload.get("has_more") is not False:
            raise PerformanceEventError("bounded performance summary query paginated")
        return [self._from_page(row) for row in payload.get("results", [])]


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextmanager
def _event_lock(key: str):
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    if not lock.acquire(timeout=30):
        raise PerformanceEventError("performance Event Key serialization busy")
    handle = None
    try:
        PERFORMANCE_EVENT_LOCK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        digest = __import__("hashlib").sha256(key.encode()).hexdigest()
        path = PERFORMANCE_EVENT_LOCK_DIR / f"{digest}.lock"
        handle = path.open("a+")
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PerformanceEventError("performance Event Key serialization busy")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if handle is not None:
            handle.close()
        lock.release()

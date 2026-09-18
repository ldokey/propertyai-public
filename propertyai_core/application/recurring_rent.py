"""Bounded recurring-rent orchestration; Finance remains the sole billing authority."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Protocol
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from propertyai_core.application.rent_errors import RentError
from propertyai_core.domain.finance import FinanceError, IssueRentEligibilityV1

SCHEMA_VERSION = 1
ZERO_UUID = UUID(int=0)


@dataclass(frozen=True)
class WorkerConfig:
    enabled: bool = False
    max_items: int = 100
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if (type(self.enabled) is not bool or type(self.max_items) is not int
                or not 1 <= self.max_items <= 500 or type(self.max_attempts) is not int
                or not 1 <= self.max_attempts <= 3):
            raise ValueError("INVALID_WORKER_CONFIG")


@dataclass(frozen=True)
class ScanCursor:
    organization_id: UUID
    contract_id: UUID
    period_id: UUID

    def __post_init__(self) -> None:
        if not all(isinstance(v, UUID) for v in (self.organization_id, self.contract_id, self.period_id)):
            raise ValueError("INVALID_SCAN_CURSOR")

    def wire(self) -> dict:
        return {"organization_id": str(self.organization_id), "contract_id": str(self.contract_id),
                "period_id": str(self.period_id)}


@dataclass(frozen=True)
class BillingTarget:
    organization_id: UUID
    contract_id: UUID
    period_id: UUID | None
    readiness: str | None
    timezone_name: str | None

    @property
    def cursor(self) -> ScanCursor:
        return ScanCursor(self.organization_id, self.contract_id, self.period_id or ZERO_UUID)


class RecurringBillingPort(Protocol):
    organization_id: UUID

    def candidates(self, after: ScanCursor | None, limit: int) -> list[BillingTarget]: ...
    def lookup(self, key: UUID) -> dict | None: ...
    def preview(self, target: BillingTarget, instant: datetime) -> dict: ...
    def issue(self, target: BillingTarget, key: UUID, instant: datetime) -> tuple[dict, bool]: ...


def cycle_command_key(target: BillingTarget) -> UUID:
    if not all(isinstance(v, UUID) for v in (target.organization_id, target.contract_id, target.period_id)):
        raise ValueError("INVALID_CYCLE_IDENTITY")
    return uuid5(NAMESPACE_URL, "propertyai:rent-recurring:v1:"
                 f"{target.organization_id}:{target.contract_id}:{target.period_id}")


def _instant(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("AWARE_CLOCK_REQUIRED")
    return value


def _receipt_outcome(receipt: dict) -> str:
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        raise ValueError("INVALID_COMMAND_RECEIPT")
    UUID(receipt["command_id"])
    outcome = receipt["result"]["outcome"]
    if outcome not in {"CREATED", "EXISTING", "NO_CHARGE"}:
        raise ValueError("INVALID_COMMAND_RECEIPT")
    return outcome


def _item(target: BillingTarget, disposition: str, reason: str, *, targeted: bool = False,
          recovered: bool = False) -> dict:
    return {"contract_id": str(target.contract_id),
            "period_id": str(target.period_id) if target.period_id else None,
            "disposition": disposition, "reason": reason,
            "targeting": "TARGETED" if targeted else "NOT_TARGETED", "recovered": recovered}


def _completed(target: BillingTarget, receipt: dict, *, replayed: bool,
               recovered: bool = False) -> dict:
    outcome = _receipt_outcome(receipt)
    disposition = ("SKIPPED_NO_CHARGE" if outcome == "NO_CHARGE" else
                   "ALREADY_ISSUED" if replayed else "CREATED" if outcome == "CREATED" else "ALREADY_ISSUED")
    return _item(target, disposition,
                 "DURABLE_COMPLETION_RECOVERED" if recovered else "DURABLE_COMMAND_RECEIPT",
                 targeted=True, recovered=recovered)


class RecurringRentWorker:
    """One bounded scan page. Cursor continuation is not an idempotency authority.

    Restarting at the beginning is safe; a caller must follow next_cursor to finish
    a scan larger than max_items. Every periodic sweep begins at the beginning.
    """

    def __init__(self, port: RecurringBillingPort, clock: Callable[[], datetime],
                 config: WorkerConfig = WorkerConfig()):
        self._port, self._clock, self.config = port, clock, config

    def _evaluate(self, target: BillingTarget, instant: datetime, dry_run: bool) -> dict:
        if target.organization_id != self._port.organization_id:
            return _item(target, "ERROR", "ORGANIZATION_BINDING_MISMATCH")
        if target.readiness != "READY":
            return _item(target, "SKIPPED_NOT_READY", "READINESS_NOT_POSITIVELY_CONFIRMED")
        if target.period_id is None:
            return _item(target, "SKIPPED_NOT_READY", "CONFIRMED_BILLING_PERIOD_MISSING")
        try:
            local_day = instant.astimezone(ZoneInfo(target.timezone_name)).date()
            key = cycle_command_key(target)
        except (ValueError, TypeError, KeyError):
            return _item(target, "SKIPPED_NOT_READY", "INVALID_TIMEZONE_OR_CYCLE")

        for attempt in range(self.config.max_attempts):
            # A regenerated preview includes a new ledger revision. Lookup before
            # dispatch avoids changing the payload of an already completed key.
            receipt = self._port.lookup(key)
            if receipt is not None:
                return _completed(target, receipt, replayed=True)
            preview = self._port.preview(target, instant)
            if preview.get("status") == "ALREADY_ISSUED":
                return _item(target, "ALREADY_ISSUED", "AUTHORITATIVE_PREVIEW", targeted=True)
            if preview.get("status") == "NO_CHARGE":
                return _item(target, "SKIPPED_NO_CHARGE", "AUTHORITATIVE_NO_CHARGE")
            if preview.get("status") != "READY":
                return _item(target, "SKIPPED_NOT_READY", "FINANCE_PREVIEW_NOT_READY")
            try:
                IssueRentEligibilityV1(local_day, date.fromisoformat(preview["due_on"])).evaluate()
            except FinanceError as exc:
                if exc.code == "BILLING_NOT_YET_DUE":
                    return _item(target, "SKIPPED_NOT_DUE", "BILLING_NOT_YET_DUE")
                raise
            if dry_run:
                return _item(target, "WOULD_CREATE", "ELIGIBLE_DRY_RUN", targeted=True)
            try:
                receipt, replayed = self._port.issue(target, key, instant)
                return _completed(target, receipt, replayed=replayed)
            except Exception as exc:
                # Never translate a lost acknowledgement into a confirmed failure.
                # Reconcile first, including concurrent same-key hash conflicts.
                try:
                    receipt = self._port.lookup(key)
                    if receipt is not None:
                        return _completed(target, receipt, replayed=True, recovered=True)
                except Exception:
                    return _item(target, "UNKNOWN_EFFECT", "COMMAND_RECEIPT_UNAVAILABLE", targeted=True)
                if (not isinstance(exc, RentError) or exc.state_unknown):
                    return _item(target, "UNKNOWN_EFFECT", "COMMIT_RESULT_UNKNOWN", targeted=True)
                if exc.code in {"VERSION_CONFLICT", "RETRYABLE_TRANSACTION", "IDEMPOTENCY_CONFLICT"}:
                    if attempt + 1 < self.config.max_attempts:
                        continue
                    return _item(target, "ERROR", "BOUNDED_RETRY_EXHAUSTED", targeted=True)
                if exc.code == "CONTRACT_NOT_READY":
                    return _item(target, "SKIPPED_NOT_READY", "FINANCE_RECHECK_NOT_READY")
                if exc.code == "BILLING_NOT_YET_DUE":
                    return _item(target, "SKIPPED_NOT_DUE", "FINANCE_RECHECK_NOT_DUE")
                reason = "CHARGE_CORRECTION_REQUIRED" if exc.code == "CHARGE_CORRECTION_REQUIRED" else "COMMAND_REJECTED"
                return _item(target, "ERROR", reason, targeted=True)
        raise AssertionError("UNREACHABLE")

    def run_once(self, *, dry_run: bool = False, run_id: UUID | None = None,
                 after: ScanCursor | None = None) -> dict:
        correlation = run_id if isinstance(run_id, UUID) else uuid4()
        result = {
            "schema_version": SCHEMA_VERSION, "run_id": str(correlation), "request_id": str(correlation),
            "started_at": None, "ended_at": None, "dry_run": dry_run if type(dry_run) is bool else False,
            "scheduler_enabled": self.config.enabled, "automatic_scheduler_activated": False,
            "result_class": "PROCESS_FAILED", "process_reason": None,
            "evaluated_count": 0, "targeted_count": 0, "created_count": 0,
            "skipped_count": 0, "failed_count": 0, "unknown_count": 0,
            "would_create_count": 0, "recovered_count": 0,
            "scan_complete": False, "next_cursor": None, "dispositions": [],
        }
        try:
            if (type(dry_run) is not bool or (run_id is not None and not isinstance(run_id, UUID))
                    or not isinstance(self._port.organization_id, UUID)
                    or (after is not None and (not isinstance(after, ScanCursor)
                        or after.organization_id != self._port.organization_id))):
                raise ValueError("INVALID_RUN_INPUT")
            instant = _instant(self._clock)
            result["started_at"] = instant.isoformat()
            if not self.config.enabled and not dry_run:
                result["result_class"] = "DISABLED"
                result["ended_at"] = instant.isoformat()
                return result
            targets = self._port.candidates(after, self.config.max_items + 1)
            if len(targets) > self.config.max_items + 1:
                raise ValueError("UNBOUNDED_DISCOVERY")
            page = targets[:self.config.max_items]
            result["scan_complete"] = len(targets) <= self.config.max_items
            if not result["scan_complete"]:
                result["next_cursor"] = page[-1].cursor.wire()
            for target in page:
                try:
                    disposition = self._evaluate(target, instant, dry_run)
                except Exception:
                    # No dispatch result is lost here: _evaluate contains its own
                    # dispatch/reconciliation boundary. Do not publish raw errors.
                    disposition = _item(target, "ERROR", "CONTRACT_EVALUATION_FAILED")
                result["dispositions"].append(disposition)
                name = disposition["disposition"]
                result["evaluated_count"] += 1
                result["targeted_count"] += disposition["targeting"] == "TARGETED"
                result["created_count"] += name == "CREATED"
                result["would_create_count"] += name == "WOULD_CREATE"
                result["failed_count"] += name == "ERROR"
                result["unknown_count"] += name == "UNKNOWN_EFFECT"
                result["recovered_count"] += disposition["recovered"]
                result["skipped_count"] += name.startswith("SKIPPED_") or name == "ALREADY_ISSUED"
            result["result_class"] = (
                "PARTIAL_UNKNOWN_EFFECT" if result["unknown_count"] else
                "RUN_COMPLETED_WITH_CONTRACT_FAILURES" if result["failed_count"] else
                "DRY_RUN_SUCCESS" if dry_run else
                "ZERO_TARGET_NOOP" if not result["targeted_count"] else "RUN_COMPLETED"
            )
            result["ended_at"] = _instant(self._clock).isoformat()
        except Exception:
            # Preserve any already confirmed counts if reporting/clock fails.
            result["result_class"] = "PARTIAL_UNKNOWN_EFFECT" if result["unknown_count"] else "PROCESS_FAILED"
            result["process_reason"] = "WORKER_INPUT_DISCOVERY_OR_CLOCK_FAILED"
        return result

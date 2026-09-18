"""Cleaner-owned delivery of Property Access approval/rejection notifications.

The OPS business transaction completes before Cleaner Telegram delivery.
Approval delivery discovers durable ``APPROVED`` + ``SYNCED`` state; rejection
delivery discovers durable ``REJECTED`` + ``NOT_REQUIRED`` state.  Each event
uses an independent Cleaner-owned delivery ledger.

Each Telegram attempt is durably recorded before ``sendMessage``.  ATTEMPTING
or UNCERTAIN state requires reconciliation and is not automatically retried,
preventing blind duplicate external effects after crash or ambiguous delivery.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping

from propertyai_core.global_writer import (
    ProductionWriterError,
    assert_current_production_writer,
    mutation_scope,
)
from telegram_approval.cleaner_config import CleanerCredentialProvider
from telegram_approval.cleaner_jobs import load_property_mappings
from telegram_approval.cleaner_property_access import (
    APPROVED_MESSAGE,
    REJECTED_REQUEST_MESSAGE,
    NotionPropertyAccessLedger,
    NotionPropertyAccessSource,
    PropertyAccessConflict,
    PropertyAccessError,
    _access_row,
    _fresh_access_row,
    _mapped_property_label,
    _normalize_page_id,
    _resolve_cleaner_by_party,
    _same_page_id,
    _validate_fresh_relation_page,
)
from telegram_approval.cleaner_registry import load_roster
from telegram_approval.telegram_transport import (
    TelegramBotContext,
    TelegramHttpTransport,
    TelegramTransport,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DELIVERY_STATE_PATH = (
    ROOT
    / "telegram_approval"
    / "runtime"
    / "cleaner"
    / "property-access-approval-notifications.json"
)
DEFAULT_REJECTION_DELIVERY_STATE_PATH = (
    ROOT
    / "telegram_approval"
    / "runtime"
    / "cleaner"
    / "property-access-rejection-notifications.json"
)
DEFAULT_MAX_CANDIDATES_PER_CYCLE = 25
DEFAULT_MAX_DELIVERIES_PER_CYCLE = 5
logger = logging.getLogger(__name__)


class _NonActionableAccess(RuntimeError):
    """A discovered row no longer satisfies the approval delivery contract."""


def _approval_time(page: dict) -> datetime:
    date_value = page.get("properties", {}).get("승인일", {}).get("date")
    start = date_value.get("start") if isinstance(date_value, dict) else None
    if not isinstance(start, str) or not start:
        raise PropertyAccessError("approval date unavailable")
    try:
        parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError as error:
        raise PropertyAccessError("approval date malformed") from error
    if parsed.tzinfo is None:
        raise PropertyAccessError("approval date must be timezone-aware")
    return parsed


def _access_id(page: dict) -> str:
    value = page.get("properties", {}).get("Access ID", {}).get("unique_id")
    if not isinstance(value, dict):
        raise PropertyAccessError("Access ID unavailable")
    number = value.get("number")
    prefix = value.get("prefix")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise PropertyAccessError("Access ID malformed")
    if prefix is None:
        return str(number)
    if not isinstance(prefix, str) or not prefix.strip() or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in prefix
    ):
        raise PropertyAccessError("Access ID prefix malformed")
    return f"{prefix.strip()}-{number}"


def _delivery_key(
    access_page_id: str, access_id: str, *, event_kind: str | None = None
) -> str:
    normalized_page_id = _normalize_page_id(access_page_id)
    if not isinstance(access_id, str) or not access_id or ":" in access_id:
        raise ValueError("invalid Access ID")
    if event_kind is None:
        return f"{normalized_page_id}:{access_id}"
    if event_kind != "REJECTION":
        raise ValueError("invalid Property Access notification event kind")
    return f"{normalized_page_id}:{access_id}:{event_kind}"


def render_cleaner_property_access_approval(property_label: str) -> str:
    """Render a privacy-safe Cleaner message using only the safe Property label."""

    if not isinstance(property_label, str) or not property_label.strip():
        raise PropertyAccessError("safe Property label unavailable")
    return f"{APPROVED_MESSAGE}\n\n숙소: {property_label.strip()}"


def render_cleaner_property_access_rejection(property_label: str) -> str:
    """Render a privacy-safe rejection message with no fabricated reason."""

    if not isinstance(property_label, str) or not property_label.strip():
        raise PropertyAccessError("safe Property label unavailable")
    return f"{REJECTED_REQUEST_MESSAGE}\n\n숙소: {property_label.strip()}"


class CleanerPropertyAccessDeliveryState:
    """Atomic private at-least-once delivery state owned by Cleaner runtime."""

    def __init__(
        self,
        path: Path = DEFAULT_DELIVERY_STATE_PATH,
        *,
        event_kind: str | None = None,
    ) -> None:
        if event_kind not in {None, "REJECTION"}:
            raise ValueError("invalid Property Access notification event kind")
        self.path = path
        self._event_kind = event_kind

    @staticmethod
    def _empty() -> dict:
        return {"schema_version": 1, "deliveries": {}}

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise RuntimeError("Cleaner Property Access delivery state malformed")
        deliveries = value.get("deliveries")
        if not isinstance(deliveries, dict):
            raise RuntimeError("Cleaner Property Access delivery state malformed")
        for key, record in deliveries.items():
            if not isinstance(key, str) or not isinstance(record, dict):
                raise RuntimeError("Cleaner Property Access delivery record malformed")
            page_id = record.get("access_page_id")
            access_id = record.get("access_id")
            if not isinstance(page_id, str) or not isinstance(access_id, str):
                raise RuntimeError("Cleaner Property Access delivery identity malformed")
            try:
                expected_key = _delivery_key(
                    page_id, access_id, event_kind=self._event_kind
                )
            except ValueError as error:
                raise RuntimeError("Cleaner Property Access delivery identity malformed") from error
            if key != expected_key:
                raise RuntimeError("Cleaner Property Access delivery key malformed")
            attempts = record.get("attempts")
            if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
                raise RuntimeError("Cleaner Property Access delivery attempts malformed")
            status = record.get("delivery_status")
            if status not in {"PENDING", "ATTEMPTING", "FAILED", "UNCERTAIN", "DELIVERED"}:
                raise RuntimeError("Cleaner Property Access delivery status malformed")
            if record.get("complete") not in {True, False}:
                raise RuntimeError("Cleaner Property Access completion state malformed")
            last_attempt_at = record.get("last_attempt_at")
            if last_attempt_at is not None:
                self._validate_timestamp(last_attempt_at, "last attempt")
            last_error_type = record.get("last_error_type")
            if last_error_type is not None and (
                not isinstance(last_error_type, str) or not last_error_type
            ):
                raise RuntimeError("Cleaner Property Access error type malformed")
            message_id = record.get("telegram_message_id")
            sent_at = record.get("sent_at")
            if record["complete"] is True:
                if status != "DELIVERED":
                    raise RuntimeError("Cleaner Property Access completed status malformed")
                if (
                    isinstance(message_id, bool)
                    or not isinstance(message_id, int)
                    or message_id <= 0
                ):
                    raise RuntimeError("Cleaner Property Access message id malformed")
                self._validate_timestamp(sent_at, "sent")
            elif message_id is not None or sent_at is not None:
                raise RuntimeError("Cleaner Property Access incomplete delivery malformed")
        return value

    @staticmethod
    def _validate_timestamp(value: object, label: str) -> None:
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"Cleaner Property Access {label} timestamp malformed")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise RuntimeError(
                f"Cleaner Property Access {label} timestamp malformed"
            ) from error
        if parsed.tzinfo is None:
            raise RuntimeError(
                f"Cleaner Property Access {label} timestamp must be timezone-aware"
            )

    def _store(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def is_complete(self, access_page_id: str, access_id: str) -> bool:
        key = _delivery_key(
            access_page_id, access_id, event_kind=self._event_kind
        )
        record = self._load()["deliveries"].get(key)
        return bool(record is not None and record["complete"] is True)

    def requires_reconciliation(self, access_page_id: str, access_id: str) -> bool:
        key = _delivery_key(
            access_page_id, access_id, event_kind=self._event_kind
        )
        record = self._load()["deliveries"].get(key)
        return bool(
            record is not None
            and record["complete"] is False
            and record["delivery_status"] in {"ATTEMPTING", "UNCERTAIN"}
        )

    def record_attempt(
        self, *, access_page_id: str, access_id: str, attempted_at: datetime
    ) -> None:
        if attempted_at.tzinfo is None:
            raise ValueError("attempt timestamp must be timezone-aware")
        key = _delivery_key(
            access_page_id, access_id, event_kind=self._event_kind
        )
        state = self._load()
        record = state["deliveries"].get(key)
        if record is None:
            record = {
                "access_page_id": _normalize_page_id(access_page_id),
                "access_id": access_id,
                "attempts": 0,
                "delivery_status": "PENDING",
                "last_attempt_at": None,
                "last_error_type": None,
                "telegram_message_id": None,
                "sent_at": None,
                "complete": False,
            }
            state["deliveries"][key] = record
        if record["complete"] is True:
            raise RuntimeError("cannot retry a completed Access notification")
        if record["delivery_status"] in {"ATTEMPTING", "UNCERTAIN"}:
            raise RuntimeError("Access notification requires reconciliation before retry")
        record["attempts"] += 1
        record["delivery_status"] = "ATTEMPTING"
        record["last_attempt_at"] = attempted_at.isoformat()
        record["last_error_type"] = None
        self._store(state)

    def record_failure(
        self, *, access_page_id: str, access_id: str, error_type: str
    ) -> None:
        if not isinstance(error_type, str) or not error_type:
            raise ValueError("error type must be non-empty")
        key = _delivery_key(
            access_page_id, access_id, event_kind=self._event_kind
        )
        state = self._load()
        record = state["deliveries"].get(key)
        if record is None or record["complete"] is True:
            raise RuntimeError("cannot fail an unattempted or completed Access notification")
        record["delivery_status"] = "FAILED"
        record["last_error_type"] = error_type
        self._store(state)

    def record_uncertain(
        self, *, access_page_id: str, access_id: str, error_type: str
    ) -> None:
        if not isinstance(error_type, str) or not error_type:
            raise ValueError("error type must be non-empty")
        key = _delivery_key(
            access_page_id, access_id, event_kind=self._event_kind
        )
        state = self._load()
        record = state["deliveries"].get(key)
        if record is None or record["complete"] is True:
            raise RuntimeError("cannot mark an unattempted or completed Access notification uncertain")
        record["delivery_status"] = "UNCERTAIN"
        record["last_error_type"] = error_type
        self._store(state)

    def record_success(
        self,
        *,
        access_page_id: str,
        access_id: str,
        telegram_message_id: int,
        sent_at: datetime,
    ) -> None:
        if (
            isinstance(telegram_message_id, bool)
            or not isinstance(telegram_message_id, int)
            or telegram_message_id <= 0
        ):
            raise ValueError("Telegram message id must be a positive integer")
        if sent_at.tzinfo is None:
            raise ValueError("sent timestamp must be timezone-aware")
        key = _delivery_key(
            access_page_id, access_id, event_kind=self._event_kind
        )
        state = self._load()
        record = state["deliveries"].get(key)
        if record is None or record["complete"] is True:
            raise RuntimeError("cannot complete an unattempted or completed Access notification")
        record["delivery_status"] = "DELIVERED"
        record["last_error_type"] = None
        record["telegram_message_id"] = telegram_message_id
        record["sent_at"] = sent_at.isoformat()
        record["complete"] = True
        self._store(state)


class CleanerPropertyAccessRejectionDeliveryState(CleanerPropertyAccessDeliveryState):
    """Independent rejection ledger; approval ledger keys remain byte-compatible."""

    def __init__(
        self, path: Path = DEFAULT_REJECTION_DELIVERY_STATE_PATH
    ) -> None:
        super().__init__(path, event_kind="REJECTION")


class CleanerPropertyAccessNotificationScanner:
    """Read-only candidate discovery plus Cleaner destination resolution."""

    def __init__(
        self,
        *,
        ledger,
        source,
        delivery_state: CleanerPropertyAccessDeliveryState,
        roster_loader: Callable[[], dict] = load_roster,
        mappings: dict[str, str] | None = None,
    ) -> None:
        self._ledger = ledger
        self._source = source
        self._delivery_state = delivery_state
        self._roster_loader = roster_loader
        self._mappings = mappings

    @staticmethod
    def _sort_key(candidate: object) -> tuple[str, str]:
        if not isinstance(candidate, dict):
            return ("", "")
        approval = candidate.get("properties", {}).get("승인일", {}).get("date")
        approval_start = approval.get("start") if isinstance(approval, dict) else ""
        page_id = candidate.get("id")
        return (
            approval_start if isinstance(approval_start, str) else "",
            page_id if isinstance(page_id, str) else "",
        )

    @staticmethod
    def _safe_candidate_ref(candidate: object) -> dict:
        if not isinstance(candidate, dict):
            return {"access_page_id": None, "access_id": None}
        page_id = candidate.get("id")
        try:
            normalized = _normalize_page_id(page_id) if isinstance(page_id, str) else None
        except ValueError:
            normalized = None
        try:
            access_id = _access_id(candidate)
        except Exception:
            access_id = None
        return {"access_page_id": normalized, "access_id": access_id}

    def _resolve_actionable(self, candidate: dict) -> dict:
        initial = _access_row(candidate)
        if initial["status"] != "APPROVED" or initial["sync_status"] != "SYNCED":
            raise _NonActionableAccess("Access is no longer APPROVED + SYNCED")

        initial_access_id = _access_id(candidate)
        fresh_page = self._ledger.get_access(initial["page_id"])
        fresh = _access_row(fresh_page)
        if not _same_page_id(fresh["page_id"], initial["page_id"]):
            raise PropertyAccessConflict("Access fresh-read identity mismatch")
        if fresh["status"] != "APPROVED" or fresh["sync_status"] != "SYNCED":
            raise _NonActionableAccess("Access is no longer APPROVED + SYNCED")
        fresh_access_id = _access_id(fresh_page)
        if fresh_access_id != initial_access_id:
            raise PropertyAccessConflict("Access ID changed across fresh read")
        approved_at = _approval_time(fresh_page)

        authoritative = _fresh_access_row(
            self._ledger, fresh["party_page_id"], fresh["property_page_id"]
        )
        if (
            authoritative is None
            or not _same_page_id(authoritative["page_id"], fresh["page_id"])
            or authoritative["status"] != "APPROVED"
            or authoritative["sync_status"] != "SYNCED"
        ):
            raise PropertyAccessConflict("Access fresh-read reconciliation failed")

        property_page = self._source.get_page(authoritative["property_page_id"])
        party_page = self._source.get_page(authoritative["party_page_id"])
        _validate_fresh_relation_page(
            property_page, authoritative["property_page_id"], "Property"
        )
        _validate_fresh_relation_page(
            party_page, authoritative["party_page_id"], "Cleaner Party"
        )

        mappings = self._mappings if self._mappings is not None else load_property_mappings()
        property_label = _mapped_property_label(authoritative["property_page_id"], mappings)
        if property_label is None:
            raise PropertyAccessError("canonical safe Property label unavailable or ambiguous")
        cleaner = _resolve_cleaner_by_party(
            self._roster_loader(), authoritative["party_page_id"]
        )
        return {
            "access_page_id": _normalize_page_id(authoritative["page_id"]),
            "access_id": fresh_access_id,
            "approved_at": approved_at.isoformat(),
            "property_label": property_label,
            "cleaner_chat_id": cleaner["telegram_chat_id"],
        }

    def enumerate_actionable(self) -> dict:
        """Enumerate the complete current candidate set without sending or mutation."""

        candidates = self._ledger.query_approved_synced()
        if not isinstance(candidates, list):
            raise RuntimeError("Property Access APPROVED + SYNCED discovery malformed")
        result = {
            "discovered": len(candidates),
            "actionable": [],
            "completed": [],
            "non_actionable": 0,
            "failures": [],
        }
        for candidate in sorted(candidates, key=self._sort_key):
            candidate_ref = self._safe_candidate_ref(candidate)
            try:
                current = self._resolve_actionable(candidate)
                public = {
                    "access_page_id": current["access_page_id"],
                    "access_id": current["access_id"],
                    "approved_at": current["approved_at"],
                    "property_label": current["property_label"],
                }
                if self._delivery_state.is_complete(
                    current["access_page_id"], current["access_id"]
                ):
                    result["completed"].append(public)
                else:
                    result["actionable"].append(public)
            except _NonActionableAccess:
                result["non_actionable"] += 1
            except Exception as error:
                result["failures"].append(
                    {**candidate_ref, "error_type": type(error).__name__}
                )
        return result


class CleanerPropertyAccessNotificationPump(CleanerPropertyAccessNotificationScanner):
    """Bounded Cleaner-owned sender for already-completed approval business state."""

    def __init__(
        self,
        *,
        bot: TelegramBotContext,
        transport: TelegramTransport,
        ledger,
        source,
        delivery_state: CleanerPropertyAccessDeliveryState,
        roster_loader: Callable[[], dict] = load_roster,
        mappings: dict[str, str] | None = None,
        now: Callable[[], datetime] | None = None,
        max_candidates_per_cycle: int = DEFAULT_MAX_CANDIDATES_PER_CYCLE,
        max_deliveries_per_cycle: int = DEFAULT_MAX_DELIVERIES_PER_CYCLE,
        mutation_scope_factory: Callable[[str, str], ContextManager[Any]] | None = None,
        pre_external_assert: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(
            ledger=ledger,
            source=source,
            delivery_state=delivery_state,
            roster_loader=roster_loader,
            mappings=mappings,
        )
        if max_candidates_per_cycle <= 0 or max_deliveries_per_cycle <= 0:
            raise ValueError("Cleaner notification bounds must be positive")
        if max_deliveries_per_cycle > max_candidates_per_cycle:
            raise ValueError("delivery bound cannot exceed candidate bound")
        self._bot = bot
        self._transport = transport
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._max_candidates = max_candidates_per_cycle
        self._max_deliveries = max_deliveries_per_cycle
        if mutation_scope_factory is None:
            mutation_scope_factory = lambda access_page_id, access_id: mutation_scope(
                "W03",
                unit_id=f"property-access-delivery:{access_page_id}:{access_id}",
                operation_class="CLEANER_TELEGRAM_DELIVERY",
                target=f"property-access:{access_page_id}:{access_id}",
            )
        self._mutation_scope_factory = mutation_scope_factory
        self._pre_external_assert = pre_external_assert or assert_current_production_writer

    def run_once(self) -> dict:
        candidates = self._ledger.query_approved_synced()
        if not isinstance(candidates, list):
            raise RuntimeError("Property Access APPROVED + SYNCED discovery malformed")
        ordered = sorted(candidates, key=self._sort_key)
        limited = ordered[: self._max_candidates]
        result = {
            "discovered": len(ordered),
            "examined": len(limited),
            "attempted": 0,
            "sent": 0,
            "deduped": 0,
            "non_actionable": 0,
            "deferred": max(0, len(ordered) - len(limited)),
            "failures": [],
        }
        for candidate in limited:
            if result["attempted"] >= self._max_deliveries:
                result["deferred"] += 1
                continue
            candidate_ref = self._safe_candidate_ref(candidate)
            try:
                current = self._resolve_actionable(candidate)
                access_page_id = current["access_page_id"]
                access_id = current["access_id"]
                logger.info(
                    "cleaner_property_access_notification candidate_found access_id=%s",
                    access_id,
                )
                if self._delivery_state.is_complete(access_page_id, access_id):
                    result["deduped"] += 1
                    logger.info(
                        "cleaner_property_access_notification skipped_completed access_id=%s",
                        access_id,
                    )
                    continue
                if self._delivery_state.requires_reconciliation(access_page_id, access_id):
                    result["failures"].append({
                        **candidate_ref, "error_type": "DeliveryReconciliationRequired"
                    })
                    continue
                if self._mutation_scope_factory is None:
                    raise RuntimeError("Cleaner Global Writer delivery scope is required")

                with self._mutation_scope_factory(access_page_id, access_id):
                    attempted_at = self._now()
                    if attempted_at.tzinfo is None:
                        raise RuntimeError("Cleaner notification clock returned a naive timestamp")
                    self._delivery_state.record_attempt(
                        access_page_id=access_page_id,
                        access_id=access_id,
                        attempted_at=attempted_at,
                    )
                    result["attempted"] += 1
                    logger.info(
                        "cleaner_property_access_notification delivery_attempted access_id=%s",
                        access_id,
                    )
                    if self._pre_external_assert is None:
                        raise RuntimeError("Cleaner pre-Telegram fencing assertion is required")
                    self._pre_external_assert()
                    try:
                        response = self._transport.request(
                            self._bot,
                            "sendMessage",
                            chat_id=current["cleaner_chat_id"],
                            text=render_cleaner_property_access_approval(
                                current["property_label"]
                            ),
                        )
                        if not isinstance(response, dict):
                            raise RuntimeError("Telegram sendMessage result malformed")
                        message_id = response.get("message_id")
                        if (
                            isinstance(message_id, bool)
                            or not isinstance(message_id, int)
                            or message_id <= 0
                        ):
                            raise RuntimeError("Telegram sendMessage message_id malformed")
                    except ProductionWriterError:
                        raise
                    except Exception as error:
                        self._pre_external_assert()
                        self._delivery_state.record_uncertain(
                            access_page_id=access_page_id,
                            access_id=access_id,
                            error_type=type(error).__name__,
                        )
                        logger.warning(
                            "cleaner_property_access_notification delivery_uncertain access_id=%s error_type=%s",
                            access_id,
                            type(error).__name__,
                        )
                        raise
                    sent_at = self._now()
                    if sent_at.tzinfo is None:
                        raise RuntimeError("Cleaner notification clock returned a naive timestamp")
                    self._pre_external_assert()
                    self._delivery_state.record_success(
                        access_page_id=access_page_id,
                        access_id=access_id,
                        telegram_message_id=message_id,
                        sent_at=sent_at,
                    )
                    result["sent"] += 1
                    logger.info(
                        "cleaner_property_access_notification delivery_succeeded access_id=%s",
                        access_id,
                    )
            except _NonActionableAccess:
                result["non_actionable"] += 1
            except ProductionWriterError:
                raise
            except Exception as error:
                result["failures"].append(
                    {**candidate_ref, "error_type": type(error).__name__}
                )
        return result



class CleanerPropertyAccessRejectionNotificationScanner:
    """Read-only discovery of durable REJECTED Property Access rows."""

    def __init__(
        self,
        *,
        ledger,
        source,
        delivery_state: CleanerPropertyAccessRejectionDeliveryState,
        roster_loader: Callable[[], dict] = load_roster,
        mappings: dict[str, str] | None = None,
    ) -> None:
        self._ledger = ledger
        self._source = source
        self._delivery_state = delivery_state
        self._roster_loader = roster_loader
        self._mappings = mappings

    @staticmethod
    def _sort_key(candidate: object) -> tuple[str, str]:
        if not isinstance(candidate, dict):
            return ("", "")
        page_id = candidate.get("id")
        try:
            normalized_page_id = (
                _normalize_page_id(page_id) if isinstance(page_id, str) else ""
            )
        except ValueError:
            normalized_page_id = ""
        try:
            access_id = _access_id(candidate)
        except Exception:
            access_id = ""
        return (normalized_page_id, access_id)

    @staticmethod
    def _safe_candidate_ref(candidate: object) -> dict:
        if not isinstance(candidate, dict):
            return {"access_page_id": None, "access_id": None}
        page_id = candidate.get("id")
        try:
            normalized = _normalize_page_id(page_id) if isinstance(page_id, str) else None
        except ValueError:
            normalized = None
        try:
            access_id = _access_id(candidate)
        except Exception:
            access_id = None
        return {"access_page_id": normalized, "access_id": access_id}

    def _resolve_actionable(self, candidate: dict) -> dict:
        initial = _access_row(candidate)
        if initial["status"] != "REJECTED" or initial["sync_status"] != "NOT_REQUIRED":
            raise _NonActionableAccess("Access is no longer REJECTED + NOT_REQUIRED")

        initial_access_id = _access_id(candidate)
        fresh_page = self._ledger.get_access(initial["page_id"])
        fresh = _access_row(fresh_page)
        if not _same_page_id(fresh["page_id"], initial["page_id"]):
            raise PropertyAccessConflict("Access fresh-read identity mismatch")
        if fresh["status"] != "REJECTED" or fresh["sync_status"] != "NOT_REQUIRED":
            raise _NonActionableAccess("Access is no longer REJECTED + NOT_REQUIRED")
        fresh_access_id = _access_id(fresh_page)
        if fresh_access_id != initial_access_id:
            raise PropertyAccessConflict("Access ID changed across fresh read")

        authoritative = _fresh_access_row(
            self._ledger, fresh["party_page_id"], fresh["property_page_id"]
        )
        if (
            authoritative is None
            or not _same_page_id(authoritative["page_id"], fresh["page_id"])
            or authoritative["status"] != "REJECTED"
            or authoritative["sync_status"] != "NOT_REQUIRED"
        ):
            raise PropertyAccessConflict("Access fresh-read reconciliation failed")

        property_page = self._source.get_page(authoritative["property_page_id"])
        party_page = self._source.get_page(authoritative["party_page_id"])
        _validate_fresh_relation_page(
            property_page, authoritative["property_page_id"], "Property"
        )
        _validate_fresh_relation_page(
            party_page, authoritative["party_page_id"], "Cleaner Party"
        )

        mappings = self._mappings if self._mappings is not None else load_property_mappings()
        property_label = _mapped_property_label(authoritative["property_page_id"], mappings)
        if property_label is None:
            raise PropertyAccessError("canonical safe Property label unavailable or ambiguous")
        cleaner = _resolve_cleaner_by_party(
            self._roster_loader(), authoritative["party_page_id"]
        )
        return {
            "access_page_id": _normalize_page_id(authoritative["page_id"]),
            "access_id": fresh_access_id,
            "property_label": property_label,
            "cleaner_chat_id": cleaner["telegram_chat_id"],
        }

    def enumerate_actionable(self) -> dict:
        """Return complete inspectable targets without Telegram or state mutation."""

        candidates = self._ledger.query_rejected()
        if not isinstance(candidates, list):
            raise RuntimeError("Property Access REJECTED discovery malformed")
        result = {
            "discovered": len(candidates),
            "actionable": [],
            "completed": [],
            "non_actionable": 0,
            "failures": [],
        }
        for candidate in sorted(candidates, key=self._sort_key):
            candidate_ref = self._safe_candidate_ref(candidate)
            try:
                current = self._resolve_actionable(candidate)
                public = {
                    "access_page_id": current["access_page_id"],
                    "access_id": current["access_id"],
                    "property_label": current["property_label"],
                }
                if self._delivery_state.is_complete(
                    current["access_page_id"], current["access_id"]
                ):
                    result["completed"].append(public)
                else:
                    result["actionable"].append(public)
            except _NonActionableAccess:
                result["non_actionable"] += 1
            except Exception as error:
                result["failures"].append(
                    {**candidate_ref, "error_type": type(error).__name__}
                )
        return result


class CleanerPropertyAccessRejectionNotificationPump(
    CleanerPropertyAccessRejectionNotificationScanner
):
    """Bounded Cleaner-owned sender for already-durable rejection state."""

    def __init__(
        self,
        *,
        bot: TelegramBotContext,
        transport: TelegramTransport,
        ledger,
        source,
        delivery_state: CleanerPropertyAccessRejectionDeliveryState,
        roster_loader: Callable[[], dict] = load_roster,
        mappings: dict[str, str] | None = None,
        now: Callable[[], datetime] | None = None,
        max_candidates_per_cycle: int = DEFAULT_MAX_CANDIDATES_PER_CYCLE,
        max_deliveries_per_cycle: int = DEFAULT_MAX_DELIVERIES_PER_CYCLE,
        mutation_scope_factory: Callable[[str, str], ContextManager[Any]] | None = None,
        pre_external_assert: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(
            ledger=ledger,
            source=source,
            delivery_state=delivery_state,
            roster_loader=roster_loader,
            mappings=mappings,
        )
        if max_candidates_per_cycle <= 0 or max_deliveries_per_cycle <= 0:
            raise ValueError("Cleaner rejection notification bounds must be positive")
        if max_deliveries_per_cycle > max_candidates_per_cycle:
            raise ValueError("delivery bound cannot exceed candidate bound")
        self._bot = bot
        self._transport = transport
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._max_candidates = max_candidates_per_cycle
        self._max_deliveries = max_deliveries_per_cycle
        if mutation_scope_factory is None:
            mutation_scope_factory = lambda access_page_id, access_id: mutation_scope(
                "W03",
                unit_id=f"property-access-delivery:{access_page_id}:{access_id}",
                operation_class="CLEANER_TELEGRAM_DELIVERY",
                target=f"property-access:{access_page_id}:{access_id}",
            )
        self._mutation_scope_factory = mutation_scope_factory
        self._pre_external_assert = pre_external_assert or assert_current_production_writer

    def run_once(self) -> dict:
        candidates = self._ledger.query_rejected()
        if not isinstance(candidates, list):
            raise RuntimeError("Property Access REJECTED discovery malformed")
        ordered = sorted(candidates, key=self._sort_key)
        limited = ordered[: self._max_candidates]
        result = {
            "discovered": len(ordered),
            "examined": len(limited),
            "attempted": 0,
            "sent": 0,
            "deduped": 0,
            "non_actionable": 0,
            "deferred": max(0, len(ordered) - len(limited)),
            "failures": [],
        }
        for candidate in limited:
            if result["attempted"] >= self._max_deliveries:
                result["deferred"] += 1
                continue
            candidate_ref = self._safe_candidate_ref(candidate)
            try:
                current = self._resolve_actionable(candidate)
                access_page_id = current["access_page_id"]
                access_id = current["access_id"]
                if self._delivery_state.is_complete(access_page_id, access_id):
                    result["deduped"] += 1
                    continue
                if self._delivery_state.requires_reconciliation(access_page_id, access_id):
                    result["failures"].append({
                        **candidate_ref, "error_type": "DeliveryReconciliationRequired"
                    })
                    continue
                if self._mutation_scope_factory is None:
                    raise RuntimeError("Cleaner Global Writer rejection delivery scope is required")

                with self._mutation_scope_factory(access_page_id, access_id):
                    attempted_at = self._now()
                    if attempted_at.tzinfo is None:
                        raise RuntimeError("Cleaner rejection notification clock returned a naive timestamp")
                    self._delivery_state.record_attempt(
                        access_page_id=access_page_id,
                        access_id=access_id,
                        attempted_at=attempted_at,
                    )
                    result["attempted"] += 1
                    if self._pre_external_assert is None:
                        raise RuntimeError("Cleaner pre-Telegram fencing assertion is required")
                    self._pre_external_assert()
                    try:
                        response = self._transport.request(
                            self._bot,
                            "sendMessage",
                            chat_id=current["cleaner_chat_id"],
                            text=render_cleaner_property_access_rejection(
                                current["property_label"]
                            ),
                        )
                        if not isinstance(response, dict):
                            raise RuntimeError("Telegram sendMessage result malformed")
                        message_id = response.get("message_id")
                        if (
                            isinstance(message_id, bool)
                            or not isinstance(message_id, int)
                            or message_id <= 0
                        ):
                            raise RuntimeError("Telegram sendMessage message_id malformed")
                    except ProductionWriterError:
                        raise
                    except Exception as error:
                        self._pre_external_assert()
                        self._delivery_state.record_uncertain(
                            access_page_id=access_page_id,
                            access_id=access_id,
                            error_type=type(error).__name__,
                        )
                        logger.warning(
                            "cleaner_property_access_rejection_notification delivery_uncertain access_id=%s error_type=%s",
                            access_id,
                            type(error).__name__,
                        )
                        raise
                    sent_at = self._now()
                    if sent_at.tzinfo is None:
                        raise RuntimeError("Cleaner rejection notification clock returned a naive timestamp")
                    self._pre_external_assert()
                    self._delivery_state.record_success(
                        access_page_id=access_page_id,
                        access_id=access_id,
                        telegram_message_id=message_id,
                        sent_at=sent_at,
                    )
                    result["sent"] += 1
            except _NonActionableAccess:
                result["non_actionable"] += 1
            except ProductionWriterError:
                raise
            except Exception as error:
                result["failures"].append(
                    {**candidate_ref, "error_type": type(error).__name__}
                )
        return result


def enumerate_cleaner_property_access_rejection_notifications(
    *,
    ledger=None,
    source=None,
    delivery_state_path: Path = DEFAULT_REJECTION_DELIVERY_STATE_PATH,
    roster_loader: Callable[[], dict] = load_roster,
    mappings: dict[str, str] | None = None,
) -> dict:
    """Deployment-preflight API for deterministic read-only rejection targets."""

    scanner = CleanerPropertyAccessRejectionNotificationScanner(
        ledger=ledger or NotionPropertyAccessLedger(),
        source=source or NotionPropertyAccessSource(),
        delivery_state=CleanerPropertyAccessRejectionDeliveryState(
            delivery_state_path
        ),
        roster_loader=roster_loader,
        mappings=mappings,
    )
    return scanner.enumerate_actionable()


def build_cleaner_property_access_rejection_notification_pump(
    *,
    environment: Mapping[str, str] | None = None,
    transport: TelegramTransport | None = None,
    ledger=None,
    source=None,
    delivery_state_path: Path = DEFAULT_REJECTION_DELIVERY_STATE_PATH,
    roster_loader: Callable[[], dict] = load_roster,
    mappings: dict[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
    max_candidates_per_cycle: int = DEFAULT_MAX_CANDIDATES_PER_CYCLE,
    max_deliveries_per_cycle: int = DEFAULT_MAX_DELIVERIES_PER_CYCLE,
    mutation_scope_factory: Callable[[str, str], ContextManager[Any]] | None = None,
    pre_external_assert: Callable[[], Any] | None = None,
) -> CleanerPropertyAccessRejectionNotificationPump:
    """Compose rejection delivery exclusively from Cleaner-owned credentials."""

    environment = os.environ if environment is None else environment
    transport = transport or TelegramHttpTransport()
    bot = CleanerCredentialProvider(environment=environment).bot_context()
    return CleanerPropertyAccessRejectionNotificationPump(
        bot=bot,
        transport=transport,
        ledger=ledger or NotionPropertyAccessLedger(),
        source=source or NotionPropertyAccessSource(),
        delivery_state=CleanerPropertyAccessRejectionDeliveryState(
            delivery_state_path
        ),
        roster_loader=roster_loader,
        mappings=mappings,
        now=now,
        max_candidates_per_cycle=max_candidates_per_cycle,
        max_deliveries_per_cycle=max_deliveries_per_cycle,
        mutation_scope_factory=mutation_scope_factory,
        pre_external_assert=pre_external_assert,
    )


class CleanerPropertyAccessNotificationBundle:
    """Property-Access-specific approval + rejection outbound work bundle."""

    def __init__(self, *, approval_pump, rejection_pump) -> None:
        self._approval_pump = approval_pump
        self._rejection_pump = rejection_pump

    def run_once(self) -> dict:
        result = {}
        for kind, pump in (
            ("approval", self._approval_pump),
            ("rejection", self._rejection_pump),
        ):
            try:
                result[kind] = pump.run_once()
            except ProductionWriterError:
                raise
            except Exception as error:
                logger.warning(
                    "cleaner_property_access_notification_bundle pump_failed kind=%s error_type=%s",
                    kind,
                    type(error).__name__,
                )
                result[kind] = {
                    "failures": [{"error_type": type(error).__name__}]
                }
        return result


def build_cleaner_property_access_notification_bundle(
    *,
    environment: Mapping[str, str] | None = None,
    transport: TelegramTransport | None = None,
    ledger=None,
    source=None,
    approval_delivery_state_path: Path = DEFAULT_DELIVERY_STATE_PATH,
    rejection_delivery_state_path: Path = DEFAULT_REJECTION_DELIVERY_STATE_PATH,
    roster_loader: Callable[[], dict] = load_roster,
    mappings: dict[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
    mutation_scope_factory: Callable[[str, str], ContextManager[Any]] | None = None,
    pre_external_assert: Callable[[], Any] | None = None,
) -> CleanerPropertyAccessNotificationBundle:
    """Build both bounded pumps using one Cleaner bot credential context."""

    environment = os.environ if environment is None else environment
    transport = transport or TelegramHttpTransport()
    bot = CleanerCredentialProvider(environment=environment).bot_context()
    ledger = ledger or NotionPropertyAccessLedger()
    source = source or NotionPropertyAccessSource()
    approval = CleanerPropertyAccessNotificationPump(
        bot=bot,
        transport=transport,
        ledger=ledger,
        source=source,
        delivery_state=CleanerPropertyAccessDeliveryState(
            approval_delivery_state_path
        ),
        roster_loader=roster_loader,
        mappings=mappings,
        now=now,
        mutation_scope_factory=mutation_scope_factory,
        pre_external_assert=pre_external_assert,
    )
    rejection = CleanerPropertyAccessRejectionNotificationPump(
        bot=bot,
        transport=transport,
        ledger=ledger,
        source=source,
        delivery_state=CleanerPropertyAccessRejectionDeliveryState(
            rejection_delivery_state_path
        ),
        roster_loader=roster_loader,
        mappings=mappings,
        now=now,
        mutation_scope_factory=mutation_scope_factory,
        pre_external_assert=pre_external_assert,
    )
    return CleanerPropertyAccessNotificationBundle(
        approval_pump=approval, rejection_pump=rejection
    )

def enumerate_cleaner_property_access_notifications(
    *,
    ledger=None,
    source=None,
    delivery_state_path: Path = DEFAULT_DELIVERY_STATE_PATH,
    roster_loader: Callable[[], dict] = load_roster,
    mappings: dict[str, str] | None = None,
) -> dict:
    """Deployment-preflight API: complete deterministic read-only target enumeration."""

    scanner = CleanerPropertyAccessNotificationScanner(
        ledger=ledger or NotionPropertyAccessLedger(),
        source=source or NotionPropertyAccessSource(),
        delivery_state=CleanerPropertyAccessDeliveryState(delivery_state_path),
        roster_loader=roster_loader,
        mappings=mappings,
    )
    return scanner.enumerate_actionable()


def build_cleaner_property_access_notification_pump(
    *,
    environment: Mapping[str, str] | None = None,
    transport: TelegramTransport | None = None,
    ledger=None,
    source=None,
    delivery_state_path: Path = DEFAULT_DELIVERY_STATE_PATH,
    roster_loader: Callable[[], dict] = load_roster,
    mappings: dict[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
    max_candidates_per_cycle: int = DEFAULT_MAX_CANDIDATES_PER_CYCLE,
    max_deliveries_per_cycle: int = DEFAULT_MAX_DELIVERIES_PER_CYCLE,
    mutation_scope_factory: Callable[[str, str], ContextManager[Any]] | None = None,
    pre_external_assert: Callable[[], Any] | None = None,
) -> CleanerPropertyAccessNotificationPump:
    """Compose the sender exclusively from Cleaner-owned runtime credentials."""

    environment = os.environ if environment is None else environment
    transport = transport or TelegramHttpTransport()
    bot = CleanerCredentialProvider(environment=environment).bot_context()
    return CleanerPropertyAccessNotificationPump(
        bot=bot,
        transport=transport,
        ledger=ledger or NotionPropertyAccessLedger(),
        source=source or NotionPropertyAccessSource(),
        delivery_state=CleanerPropertyAccessDeliveryState(delivery_state_path),
        roster_loader=roster_loader,
        mappings=mappings,
        now=now,
        max_candidates_per_cycle=max_candidates_per_cycle,
        max_deliveries_per_cycle=max_deliveries_per_cycle,
        mutation_scope_factory=mutation_scope_factory,
        pre_external_assert=pre_external_assert,
    )

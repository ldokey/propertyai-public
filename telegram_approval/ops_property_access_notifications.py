"""OPS-owned delivery of pending Cleaner Property Access approval notices.

Each transport attempt is durably recorded before ``sendMessage``.  A crash or
ambiguous Telegram result leaves that chat in reconciliation-required state, so
the runtime does not blindly repeat a possibly-applied external effect.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping

from propertyai_core.global_writer import (
    ProductionWriterError,
    assert_current_production_writer,
    mutation_scope,
)
from telegram_approval.cleaner_jobs import load_property_mappings
from telegram_approval.cleaner_property_access import (
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
    ops_access_callback_data,
)
from telegram_approval.cleaner_registry import load_roster
from telegram_approval.ops_config import OpsAllowlistProvider, OpsCredentialProvider
from telegram_approval.telegram_transport import (
    TelegramBotContext,
    TelegramHttpTransport,
    TelegramTransport,
)


ROOT = Path(__file__).resolve().parents[1]


class _NonActionableAccess(RuntimeError):
    """A previously discovered Access row is no longer actionable."""


DEFAULT_DELIVERY_STATE_PATH = (
    ROOT / "telegram_approval" / "runtime" / "ops" / "property-access-notifications.json"
)


class OpsPropertyAccessDeliveryState:
    """Atomic private local dedupe state owned only by the OPS runtime."""

    def __init__(self, path: Path = DEFAULT_DELIVERY_STATE_PATH) -> None:
        self.path = path

    @staticmethod
    def _empty() -> dict:
        return {"schema_version": 1, "deliveries": {}}

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise RuntimeError("OPS Property Access delivery state malformed")
        deliveries = value.get("deliveries")
        if not isinstance(deliveries, dict):
            raise RuntimeError("OPS Property Access delivery state malformed")
        for key, record in deliveries.items():
            if not isinstance(key, str) or not isinstance(record, dict):
                raise RuntimeError("OPS Property Access delivery record malformed")
            try:
                normalized = _normalize_page_id(key)
            except ValueError as error:
                raise RuntimeError("OPS Property Access delivery key malformed") from error
            if normalized != key or record.get("access_page_id") != key:
                raise RuntimeError("OPS Property Access delivery identity malformed")
            first_message_id = record.get("telegram_message_id")
            sent_at = record.get("sent_at")
            chat_deliveries = record.get("chat_deliveries")
            if not isinstance(chat_deliveries, dict):
                raise RuntimeError("OPS Property Access chat delivery state malformed")
            unresolved = record.get("unresolved_chat_ids", [])
            if (
                not isinstance(unresolved, list)
                or any(isinstance(value, bool) or not isinstance(value, int) for value in unresolved)
                or len(set(unresolved)) != len(unresolved)
            ):
                raise RuntimeError("OPS Property Access unresolved delivery state malformed")
            delivered_ids: set[int] = set()
            for chat_key, chat_record in chat_deliveries.items():
                if not isinstance(chat_key, str) or not chat_key.lstrip("-").isdigit():
                    raise RuntimeError("OPS Property Access delivery chat key malformed")
                if not isinstance(chat_record, dict):
                    raise RuntimeError("OPS Property Access chat delivery record malformed")
                delivered_ids.add(int(chat_key))
                message_id = chat_record.get("telegram_message_id")
                if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
                    raise RuntimeError("OPS Property Access chat message id malformed")
            if delivered_ids & set(unresolved):
                raise RuntimeError("OPS Property Access delivery state overlaps unresolved state")
            if chat_deliveries:
                if not isinstance(first_message_id, int) or isinstance(first_message_id, bool) or first_message_id <= 0:
                    raise RuntimeError("OPS Property Access delivery message id malformed")
                if not isinstance(sent_at, str) or not sent_at:
                    raise RuntimeError("OPS Property Access delivery timestamp malformed")
                try:
                    parsed = datetime.fromisoformat(sent_at)
                except ValueError as error:
                    raise RuntimeError("OPS Property Access delivery timestamp malformed") from error
                if parsed.tzinfo is None:
                    raise RuntimeError("OPS Property Access delivery timestamp must be timezone-aware")
            elif unresolved:
                if first_message_id is not None or sent_at is not None:
                    raise RuntimeError("OPS Property Access unresolved first delivery malformed")
            else:
                raise RuntimeError("OPS Property Access empty delivery record malformed")
            if record.get("complete") not in {True, False}:
                raise RuntimeError("OPS Property Access delivery completion state malformed")
            if record["complete"] and (unresolved or not chat_deliveries):
                raise RuntimeError("OPS Property Access completed delivery has unresolved chats")
        return value

    def _store(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def delivered_chat_ids(self, access_page_id: str) -> frozenset[int]:
        key = _normalize_page_id(access_page_id)
        record = self._load()["deliveries"].get(key)
        if record is None:
            return frozenset()
        return frozenset(int(value) for value in record["chat_deliveries"])

    def unresolved_chat_ids(self, access_page_id: str) -> frozenset[int]:
        key = _normalize_page_id(access_page_id)
        record = self._load()["deliveries"].get(key)
        if record is None:
            return frozenset()
        return frozenset(record.get("unresolved_chat_ids", []))

    def record_chat_attempt(
        self, *, access_page_id: str, chat_id: int
    ) -> None:
        key = _normalize_page_id(access_page_id)
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise ValueError("OPS chat id must be an integer")
        state = self._load()
        record = state["deliveries"].get(key)
        if record is None:
            record = {
                "access_page_id": key,
                "telegram_message_id": None,
                "sent_at": None,
                "chat_deliveries": {},
                "unresolved_chat_ids": [],
                "complete": False,
            }
            state["deliveries"][key] = record
        unresolved = record.setdefault("unresolved_chat_ids", [])
        if record["complete"]:
            raise RuntimeError("cannot retry a completed Access notification")
        if str(chat_id) in record["chat_deliveries"] or chat_id in unresolved:
            raise RuntimeError("OPS chat delivery already resolved or pending reconciliation")
        unresolved.append(chat_id)
        unresolved.sort()
        self._store(state)

    def is_complete(self, access_page_id: str) -> bool:
        key = _normalize_page_id(access_page_id)
        record = self._load()["deliveries"].get(key)
        return bool(record is not None and record["complete"] is True)

    def record_chat_delivery(
        self,
        *,
        access_page_id: str,
        chat_id: int,
        telegram_message_id: int,
        sent_at: datetime,
    ) -> None:
        key = _normalize_page_id(access_page_id)
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise ValueError("OPS chat id must be an integer")
        if (
            isinstance(telegram_message_id, bool)
            or not isinstance(telegram_message_id, int)
            or telegram_message_id <= 0
        ):
            raise ValueError("Telegram message id must be a positive integer")
        if sent_at.tzinfo is None:
            raise ValueError("delivery timestamp must be timezone-aware")
        state = self._load()
        record = state["deliveries"].get(key)
        timestamp = sent_at.isoformat()
        if record is None:
            record = {
                "access_page_id": key,
                "telegram_message_id": telegram_message_id,
                "sent_at": timestamp,
                "chat_deliveries": {},
                "unresolved_chat_ids": [],
                "complete": False,
            }
            state["deliveries"][key] = record
        if record.get("telegram_message_id") is None:
            record["telegram_message_id"] = telegram_message_id
            record["sent_at"] = timestamp
        unresolved = record.setdefault("unresolved_chat_ids", [])
        if chat_id in unresolved:
            unresolved.remove(chat_id)
        record["chat_deliveries"][str(chat_id)] = {
            "telegram_message_id": telegram_message_id,
            "sent_at": timestamp,
        }
        self._store(state)

    def mark_complete(self, access_page_id: str, expected_chat_ids: frozenset[int]) -> None:
        key = _normalize_page_id(access_page_id)
        state = self._load()
        record = state["deliveries"].get(key)
        if record is None:
            raise RuntimeError("cannot complete an undelivered Access notification")
        actual = frozenset(int(value) for value in record["chat_deliveries"])
        if actual != expected_chat_ids or record.get("unresolved_chat_ids", []):
            raise RuntimeError("cannot complete a partially delivered Access notification")
        record["complete"] = True
        self._store(state)


def render_ops_property_access_notification(
    access_page_id: str, cleaner_label: str, property_label: str
) -> tuple[str, str]:
    """Render only canonical safe labels plus reference-only callback payloads."""

    if not isinstance(cleaner_label, str) or not cleaner_label.strip():
        raise PropertyAccessError("safe Cleaner label unavailable")
    if not isinstance(property_label, str) or not property_label.strip():
        raise PropertyAccessError("safe Property label unavailable")
    text = (
        "🏠 Cleaner 숙소 신청\n\n"
        f"Cleaner:\n{cleaner_label.strip()}\n\n"
        f"숙소:\n{property_label.strip()}"
    )
    keyboard = json.dumps(
        {
            "inline_keyboard": [[
                {
                    "text": "승인",
                    "callback_data": ops_access_callback_data(access_page_id, "approve"),
                },
                {
                    "text": "거절",
                    "callback_data": ops_access_callback_data(access_page_id, "reject"),
                },
            ]]
        },
        ensure_ascii=False,
    )
    return text, keyboard


class OpsPropertyAccessNotificationPump:
    """Discover and deliver actionable REQUESTED Access rows from OPS context."""

    def __init__(
        self,
        *,
        bot: TelegramBotContext,
        chat_ids: frozenset[int],
        transport: TelegramTransport,
        ledger,
        source,
        delivery_state: OpsPropertyAccessDeliveryState,
        roster_loader: Callable[[], dict] = load_roster,
        mappings: dict[str, str] | None = None,
        now: Callable[[], datetime] | None = None,
        mutation_scope_factory: Callable[[str, int], ContextManager[Any]] | None = None,
        pre_external_assert: Callable[[], Any] | None = None,
    ) -> None:
        if not chat_ids or any(isinstance(value, bool) or not isinstance(value, int) for value in chat_ids):
            raise ValueError("OPS Property Access notifier requires a non-empty integer chat allowlist")
        self._bot = bot
        self._chat_ids = frozenset(chat_ids)
        self._transport = transport
        self._ledger = ledger
        self._source = source
        self._delivery_state = delivery_state
        self._roster_loader = roster_loader
        self._mappings = mappings
        self._now = now or (lambda: datetime.now(timezone.utc))
        if mutation_scope_factory is None:
            mutation_scope_factory = lambda access_page_id, chat_id: mutation_scope(
                "W02",
                unit_id=f"property-access-delivery:{access_page_id}:{chat_id}",
                operation_class="OPS_TELEGRAM_DELIVERY",
                target=f"property-access:{access_page_id}:chat:{chat_id}",
            )
        self._mutation_scope_factory = mutation_scope_factory
        self._pre_external_assert = pre_external_assert or assert_current_production_writer

    def _resolve_actionable(self, candidate: dict) -> tuple[dict, str, str]:
        initial = _access_row(candidate)
        if initial["status"] != "REQUESTED":
            raise _NonActionableAccess("Access is no longer REQUESTED")
        fresh = _access_row(self._ledger.get_access(initial["page_id"]))
        if not _same_page_id(fresh["page_id"], initial["page_id"]):
            raise PropertyAccessConflict("Access fresh-read identity mismatch")
        if fresh["status"] != "REQUESTED":
            raise _NonActionableAccess("Access is no longer REQUESTED")
        authoritative = _fresh_access_row(
            self._ledger, fresh["party_page_id"], fresh["property_page_id"]
        )
        if (
            authoritative is None
            or not _same_page_id(authoritative["page_id"], fresh["page_id"])
            or authoritative["status"] != "REQUESTED"
        ):
            raise PropertyAccessConflict("Access fresh-read reconciliation failed")

        property_page = self._source.get_page(authoritative["property_page_id"])
        party_page = self._source.get_page(authoritative["party_page_id"])
        _validate_fresh_relation_page(
            property_page, authoritative["property_page_id"], "Property"
        )
        _validate_fresh_relation_page(party_page, authoritative["party_page_id"], "Cleaner Party")

        mappings = self._mappings if self._mappings is not None else load_property_mappings()
        property_label = _mapped_property_label(authoritative["property_page_id"], mappings)
        if property_label is None:
            raise PropertyAccessError("canonical safe Property label unavailable or ambiguous")
        cleaner = _resolve_cleaner_by_party(
            self._roster_loader(), authoritative["party_page_id"]
        )
        cleaner_label = cleaner.get("label")
        if not isinstance(cleaner_label, str) or not cleaner_label.strip():
            raise PropertyAccessError("canonical safe Cleaner label unavailable")
        return authoritative, cleaner_label.strip(), property_label

    @staticmethod
    def _safe_candidate_ref(candidate: object) -> str | None:
        if not isinstance(candidate, dict):
            return None
        page_id = candidate.get("id")
        if not isinstance(page_id, str):
            return None
        try:
            return _normalize_page_id(page_id)
        except ValueError:
            return None

    def run_once(self) -> dict:
        candidates = self._ledger.query_requested()
        if not isinstance(candidates, list):
            raise RuntimeError("Property Access REQUESTED discovery returned malformed results")
        result = {
            "discovered": len(candidates),
            "sent": 0,
            "deduped": 0,
            "non_actionable": 0,
            "failures": [],
        }
        for candidate in candidates:
            candidate_ref = self._safe_candidate_ref(candidate)
            try:
                initial = _access_row(candidate)
            except Exception as error:
                result["failures"].append(
                    {"access_page_id": candidate_ref, "error_type": type(error).__name__}
                )
                continue
            try:
                access_page_id = initial["page_id"]
                if initial["status"] != "REQUESTED":
                    result["non_actionable"] += 1
                    continue
                if self._delivery_state.is_complete(access_page_id):
                    result["deduped"] += 1
                    continue
                current, cleaner_label, property_label = self._resolve_actionable(candidate)
                text, keyboard = render_ops_property_access_notification(
                    current["page_id"], cleaner_label, property_label
                )
                delivered = self._delivery_state.delivered_chat_ids(current["page_id"])
                unresolved = self._delivery_state.unresolved_chat_ids(current["page_id"])
                for chat_id in sorted(self._chat_ids - delivered - unresolved):
                    if self._mutation_scope_factory is None:
                        raise RuntimeError("OPS Global Writer delivery scope is required")
                    with self._mutation_scope_factory(current["page_id"], chat_id):
                        self._delivery_state.record_chat_attempt(
                            access_page_id=current["page_id"], chat_id=chat_id
                        )
                        if self._pre_external_assert is None:
                            raise RuntimeError("OPS pre-Telegram fencing assertion is required")
                        self._pre_external_assert()
                        try:
                            response = self._transport.request(
                                self._bot,
                                "sendMessage",
                                chat_id=chat_id,
                                text=text,
                                reply_markup=keyboard,
                            )
                        except ProductionWriterError:
                            raise
                        except Exception:
                            # The pre-send durable attempt remains unresolved.
                            # Do not change it into an automatically retryable failure.
                            raise
                        if not isinstance(response, dict):
                            raise RuntimeError("Telegram sendMessage result malformed")
                        message_id = response.get("message_id")
                        if (
                            isinstance(message_id, bool)
                            or not isinstance(message_id, int)
                            or message_id <= 0
                        ):
                            raise RuntimeError("Telegram sendMessage message_id malformed")
                        sent_at = self._now()
                        if sent_at.tzinfo is None:
                            raise RuntimeError("OPS notification clock returned a naive timestamp")
                        self._pre_external_assert()
                        self._delivery_state.record_chat_delivery(
                            access_page_id=current["page_id"],
                            chat_id=chat_id,
                            telegram_message_id=message_id,
                            sent_at=sent_at,
                        )
                unresolved = self._delivery_state.unresolved_chat_ids(current["page_id"])
                if unresolved:
                    result["failures"].append({
                        "access_page_id": current["page_id"],
                        "error_type": "DeliveryReconciliationRequired",
                    })
                    continue
                if self._delivery_state.delivered_chat_ids(current["page_id"]) == self._chat_ids:
                    self._delivery_state.mark_complete(current["page_id"], self._chat_ids)
                    result["sent"] += 1
            except _NonActionableAccess:
                result["non_actionable"] += 1
            except ProductionWriterError:
                raise
            except Exception as error:
                result["failures"].append(
                    {"access_page_id": candidate_ref, "error_type": type(error).__name__}
                )
        return result


def build_ops_property_access_notification_pump(
    *,
    environment: Mapping[str, str] | None = None,
    transport: TelegramTransport | None = None,
    ledger=None,
    source=None,
    delivery_state_path: Path = DEFAULT_DELIVERY_STATE_PATH,
    roster_loader: Callable[[], dict] = load_roster,
    mappings: dict[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
    mutation_scope_factory: Callable[[str, int], ContextManager[Any]] | None = None,
    pre_external_assert: Callable[[], Any] | None = None,
) -> OpsPropertyAccessNotificationPump:
    """Compose the notifier exclusively from OPS-owned runtime credentials."""

    environment = os.environ if environment is None else environment
    transport = transport or TelegramHttpTransport()
    bot = OpsCredentialProvider(environment=environment).bot_context()
    chat_ids = OpsAllowlistProvider(environment=environment).allowed_chat_ids()
    return OpsPropertyAccessNotificationPump(
        bot=bot,
        chat_ids=chat_ids,
        transport=transport,
        ledger=ledger or NotionPropertyAccessLedger(),
        source=source or NotionPropertyAccessSource(),
        delivery_state=OpsPropertyAccessDeliveryState(delivery_state_path),
        roster_loader=roster_loader,
        mappings=mappings,
        now=now,
        mutation_scope_factory=mutation_scope_factory,
        pre_external_assert=pre_external_assert,
    )

"""OPS-owned delivery for restricted Cleaner reassignment decision requests.

Cleaner W03 may create the durable decision action, but it must never need OPS
bot credentials.  This pump runs under W02 and owns only OPS Telegram delivery
for already-created ``CLEANER_REASSIGNMENT_DECISION`` actions.
"""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping

from propertyai_core.global_writer import ProductionWriterError
from telegram_approval.cleaner_config import CleanerRuntimePaths
from telegram_approval.cleaner_reassignment import (
    ACTION_TYPE,
    NOTIFICATION_RETRY_REQUIRED,
    _atomic_private,
    decision_keyboard,
)
from telegram_approval.ops_config import OpsAllowlistProvider, OpsCredentialProvider
from telegram_approval.telegram_transport import TelegramHttpTransport, TelegramTransport


PENDING = "PENDING"
ATTEMPTING = "ATTEMPTING"
DELIVERED = "DELIVERED"
UNCERTAIN = "UNCERTAIN"


class OpsReassignmentNotificationPump:
    """Deliver pending reassignment decision actions with the OPS bot only."""

    def __init__(
        self,
        *,
        request_dir: Path,
        bot,
        chat_ids: frozenset[int],
        transport: TelegramTransport,
        secret_path: Path,
        mutation_scope_factory: Callable[[str], ContextManager[Any]] | None = None,
        pre_external_assert: Callable[[], Any] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._request_dir = request_dir
        self._bot = bot
        self._chat_ids = chat_ids
        self._transport = transport
        self._secret_path = secret_path
        self._mutation_scope_factory = mutation_scope_factory or (lambda _action_id: nullcontext())
        self._pre_external_assert = pre_external_assert or (lambda: None)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _candidate_paths(self) -> list[Path]:
        return sorted(self._request_dir.glob("*.json"))

    def run_once(self) -> dict:
        result = {"sent": 0, "skipped": 0, "uncertain": 0, "failures": []}
        for path in self._candidate_paths():
            try:
                try:
                    record = json.loads(path.read_text())
                except (OSError, ValueError, json.JSONDecodeError):
                    result["skipped"] += 1
                    continue
                if record.get("action_type") != ACTION_TYPE or record.get("status") != PENDING:
                    continue
                if record.get("decision_committed") is True:
                    continue
                notification_status = record.get("notification_status")
                if notification_status == DELIVERED:
                    continue
                if notification_status == ATTEMPTING:
                    # A previous process may have sent Telegram but died before
                    # persisting the message id. Never auto-duplicate that send.
                    record["notification_status"] = UNCERTAIN
                    record["notification_error_type"] = "PreviousAttemptOutcomeUnknown"
                    _atomic_private(path, record)
                    result["uncertain"] += 1
                    continue
                if notification_status not in {None, PENDING, NOTIFICATION_RETRY_REQUIRED}:
                    result["skipped"] += 1
                    continue

                action_id = record.get("action_id")
                if not isinstance(action_id, str) or not action_id:
                    result["skipped"] += 1
                    continue
                with self._mutation_scope_factory(action_id):
                    latest = json.loads(path.read_text())
                    if latest.get("notification_status") == DELIVERED:
                        continue
                    deliveries = dict(latest.get("notification_deliveries") or {})
                    text = (
                        "원래 Cleaner가 다시 가능하다고 요청했습니다.\n"
                        "현재 대체 배정 상태를 확인한 뒤 한 가지 결정을 선택하세요."
                    )
                    keyboard = decision_keyboard(action_id, secret_path=self._secret_path)
                    for chat_id in sorted(self._chat_ids):
                        key = str(chat_id)
                        existing = deliveries.get(key) or {}
                        if existing.get("status") == DELIVERED:
                            continue
                        if existing.get("status") == ATTEMPTING:
                            latest["notification_status"] = UNCERTAIN
                            latest["notification_error_type"] = "PreviousChatDeliveryOutcomeUnknown"
                            latest["notification_deliveries"] = deliveries
                            _atomic_private(path, latest)
                            result["uncertain"] += 1
                            break
                        attempted_at = self._now()
                        if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
                            raise RuntimeError("OPS reassignment notification clock returned naive timestamp")
                        deliveries[key] = {
                            "status": ATTEMPTING,
                            "attempted_at": attempted_at.isoformat(),
                        }
                        latest["notification_status"] = ATTEMPTING
                        latest["notification_deliveries"] = deliveries
                        latest["notification_attempts"] = int(latest.get("notification_attempts", 0)) + 1
                        _atomic_private(path, latest)
                        try:
                            self._pre_external_assert()
                            sent = self._transport.request(
                                self._bot,
                                "sendMessage",
                                chat_id=chat_id,
                                text=text,
                                reply_markup=keyboard,
                            )
                            self._pre_external_assert()
                        except ProductionWriterError:
                            raise
                        except Exception as exc:
                            deliveries[key] = {
                                "status": "FAILED",
                                "attempted_at": attempted_at.isoformat(),
                                "error_type": type(exc).__name__,
                            }
                            latest["notification_status"] = NOTIFICATION_RETRY_REQUIRED
                            latest["notification_error_type"] = type(exc).__name__
                            latest["notification_deliveries"] = deliveries
                            _atomic_private(path, latest)
                            result["failures"].append({"action_id": action_id, "error_type": type(exc).__name__})
                            break
                        deliveries[key] = {
                            "status": DELIVERED,
                            "attempted_at": attempted_at.isoformat(),
                            "telegram_message_id": sent.get("message_id") if isinstance(sent, dict) else None,
                        }
                        latest["notification_deliveries"] = deliveries
                        _atomic_private(path, latest)
                    else:
                        latest["notification_status"] = DELIVERED
                        latest["notification_delivered_at"] = self._now().isoformat()
                        latest.pop("notification_error_type", None)
                        latest["notification_deliveries"] = deliveries
                        _atomic_private(path, latest)
                        result["sent"] += 1
            except ProductionWriterError:
                raise
            except Exception as exc:
                result["failures"].append({"action_id": path.stem, "error_type": type(exc).__name__})
        return result


def build_ops_reassignment_notification_pump(
    *,
    environment: Mapping[str, str] | None = None,
    transport: TelegramTransport | None = None,
    request_dir: Path | None = None,
    secret_path: Path,
    mutation_scope_factory: Callable[[str], ContextManager[Any]] | None = None,
    pre_external_assert: Callable[[], Any] | None = None,
    now: Callable[[], datetime] | None = None,
) -> OpsReassignmentNotificationPump:
    environment = os.environ if environment is None else environment
    transport = transport or TelegramHttpTransport()
    return OpsReassignmentNotificationPump(
        request_dir=request_dir or CleanerRuntimePaths().request_dir,
        bot=OpsCredentialProvider(environment=environment).bot_context(),
        chat_ids=OpsAllowlistProvider(environment=environment).allowed_chat_ids(),
        transport=transport,
        secret_path=secret_path,
        mutation_scope_factory=mutation_scope_factory,
        pre_external_assert=pre_external_assert,
        now=now,
    )

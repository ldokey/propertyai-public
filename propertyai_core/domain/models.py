from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class CommandEnvelope:
    command_id: str
    command_type: str
    actor_party_id: str
    actor_role: str
    requested_at: str
    source_channel: str
    source_message_ref: str
    idempotency_key: str
    data_environment: str
    expected_version: int
    payload: Dict[str, Any]

    def canonical_json(self) -> str:
        canonical = {
            "command_type": self.command_type,
            "actor_party_id": self.actor_party_id,
            "actor_role": self.actor_role,
            "source_channel": self.source_channel,
            "source_message_ref": self.source_message_ref,
            "data_environment": self.data_environment,
            "expected_version": self.expected_version,
            "payload": self.payload,
        }
        return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def canonical_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EffectClaim:
    effect_id: str
    command_id: str
    effect_type: str
    attempt_count: int
    worker_id: str
    lease_until: str


@dataclass(frozen=True)
class EffectOutcome:
    succeeded: bool
    retryable: bool = False
    result: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

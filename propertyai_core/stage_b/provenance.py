from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID

from .normalize import canonical_value, semantic_hash


@dataclass(frozen=True)
class ProvenanceRecord:
    source_key: str
    source_ref: str | None
    source_semantic_hash: str
    snapshot_id: UUID
    observed_at: datetime
    diagnostic_last_edited_time: datetime | None = None
    exact_evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "exact_evidence", MappingProxyType(canonical_value(self.exact_evidence))
        )


def provenance_hash(record: ProvenanceRecord) -> str:
    return semantic_hash(
        {
            "source_key": record.source_key,
            "source_ref": record.source_ref,
            "source_semantic_hash": record.source_semantic_hash,
            "snapshot_id": record.snapshot_id,
            "observed_at": record.observed_at,
            "exact_evidence": record.exact_evidence,
        }
    )


__all__ = ["ProvenanceRecord", "provenance_hash"]

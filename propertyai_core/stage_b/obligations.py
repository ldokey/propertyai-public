from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable
from uuid import UUID

from .identity import derived_identity
from .models import MigrationObligation


STATE_MUTATING = "STATE_MUTATING"
EFFECT_ONLY = "EFFECT_ONLY"
INFORMATIONAL = "INFORMATIONAL"
_CATEGORIES = {STATE_MUTATING, EFFECT_ONLY, INFORMATIONAL}


def obligation(
    source_key: str,
    category: str,
    reason: str,
    *,
    resolved: bool = False,
) -> MigrationObligation:
    if category not in _CATEGORIES:
        raise ValueError(f"unsupported obligation category: {category}")
    obligation_id = derived_identity("obligation", source_key, category, reason)
    return MigrationObligation(
        obligation_id=obligation_id,
        source_key=source_key,
        category=category,
        reason=reason,
        resolved=resolved,
        effect_only=category == EFFECT_ONLY,
    )


@dataclass
class ObligationLedger:
    _items: dict[UUID, MigrationObligation] = field(default_factory=dict)

    def add(self, item: MigrationObligation) -> None:
        existing = self._items.get(item.obligation_id)
        if existing is not None and existing != item:
            raise ValueError("obligation identity reused with different evidence")
        self._items[item.obligation_id] = item

    def extend(self, items: Iterable[MigrationObligation]) -> None:
        for item in items:
            self.add(item)

    @property
    def unresolved_state_mutating(self) -> tuple[MigrationObligation, ...]:
        return tuple(
            item
            for item in self._items.values()
            if not item.resolved and item.category == STATE_MUTATING
        )

    @property
    def pending_effect_only(self) -> tuple[MigrationObligation, ...]:
        return tuple(
            item
            for item in self._items.values()
            if not item.resolved and item.effect_only
        )

    def all(self) -> tuple[MigrationObligation, ...]:
        return tuple(sorted(self._items.values(), key=lambda item: str(item.obligation_id)))


__all__ = [
    "EFFECT_ONLY",
    "INFORMATIONAL",
    "ObligationLedger",
    "STATE_MUTATING",
    "obligation",
]

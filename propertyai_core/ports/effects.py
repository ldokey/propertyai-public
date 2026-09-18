from __future__ import annotations

from typing import Protocol

from propertyai_core.domain.models import EffectClaim, EffectOutcome


class EffectAdapter(Protocol):
    def execute(self, claim: EffectClaim) -> EffectOutcome:
        ...

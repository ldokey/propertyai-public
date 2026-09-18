from __future__ import annotations

from collections import Counter
from typing import Dict, Optional

from propertyai_core.domain.models import EffectClaim, EffectOutcome


class FailClosedEffectAdapter:
    """Deterministic in-process adapter with no external integration surface."""

    ALLOWED_EFFECTS = frozenset({"TEST_RECORD_CONFIRMATION", "TEST_BUILD_NOTIFICATION"})

    def __init__(self, retryable_failures: Optional[Dict[str, int]] = None):
        self.retryable_failures = dict(retryable_failures or {})
        self.call_counts: Counter[str] = Counter()
        self.external_call_count = 0

    def execute(self, claim: EffectClaim) -> EffectOutcome:
        if claim.effect_type not in self.ALLOWED_EFFECTS:
            return EffectOutcome(succeeded=False, retryable=False, error_code="ADAPTER_FAIL_CLOSED")
        self.call_counts[claim.effect_type] += 1
        remaining = self.retryable_failures.get(claim.effect_type, 0)
        if remaining > 0:
            self.retryable_failures[claim.effect_type] = remaining - 1
            return EffectOutcome(
                succeeded=False,
                retryable=True,
                error_code="SYNTHETIC_RETRYABLE_FAILURE",
            )
        return EffectOutcome(
            succeeded=True,
            result={"effect_type": claim.effect_type, "data_environment": "TEST"},
        )

"""Evidence-bound recovery of arbitrary Cleaner outbox delivery uncertainty.

This module never sends to a provider. A provider evidence adapter is responsible
for the meaning of its evidence (including exclusion of delayed/in-flight effects
before certifying absence). Missing objects and transport errors are NOT absence
proof. The application owns the decision; the existing database primitives own
state/fence CAS. No historical cutover operation or fixed business ID is used.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Any, Callable, Protocol
from uuid import UUID

from propertyai_core.global_writer import assert_current_production_writer, mutation_scope
from propertyai_core.ports.cleaner_repository import OutboxClaim, OutboxReconciliationState, OutboxWorkerPort


class ProviderEvidenceClass(str, Enum):
    CONFIRMED_EFFECT = "CONFIRMED_EFFECT"
    CONFIRMED_NO_EFFECT = "CONFIRMED_NO_EFFECT"
    AMBIGUOUS = "AMBIGUOUS"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"


class OutboxReconciliationError(RuntimeError):
    pass


class ProviderEvidenceUnavailable(RuntimeError):
    """An evidence adapter could not observe its provider; not proof of absence."""


@dataclass(frozen=True)
class ProviderEvidence:
    classification: ProviderEvidenceClass
    row_binding_sha256: str
    observed_at: datetime
    evidence_ref: str
    external_effect_id: str | None = None
    complete_no_effect_proof: bool = False
    in_flight_effects_excluded: bool = False


class ProviderEvidencePort(Protocol):
    def inspect(self, row: OutboxReconciliationState) -> ProviderEvidence: ...


@dataclass(frozen=True)
class OutboxReconciliationResult:
    outbox_id: UUID
    classification: ProviderEvidenceClass
    applied: bool
    durable_status: str
    diagnostic_owner: str
    evidence_sha256: str
    row_binding_sha256: str
    readback: OutboxReconciliationState


def _json_value(value: Any) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).isoformat()
    raise TypeError(f"unsupported evidence value type: {type(value).__name__}")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False, default=_json_value).encode()
    return hashlib.sha256(encoded).hexdigest()


def row_binding_sha256(row: OutboxReconciliationState) -> str:
    """Bind the complete observed row, including attempt/fence and diagnostic state."""
    return _digest(asdict(row))


def effect_binding_sha256(row: OutboxReconciliationState) -> str:
    """Provider-side identity excludes mutable delivery state, not business semantics."""
    values = asdict(row)
    for field in ("outbox_status", "attempt_count", "lease_fence", "external_effect_id", "last_error_code"):
        values.pop(field)
    return _digest(values)


_RETRY_PROOF = re.compile(r"NO_EFFECT_CONFIRMED:([1-9][0-9]*):([1-9][0-9]*):([0-9a-f]{64}):OWNER:([A-Za-z0-9._:/-]{1,120})")


def confirmed_no_effect_retry_allowed(claim: OutboxClaim) -> bool:
    """A retry permit is consumed by exactly the next attempt and fence.

    A reclaimed historical RUNNING row has no such proof. A crash before consuming
    a retry permit also cannot carry that permit across another fence increment.
    Both cases must return to evidence-based reconciliation, never blind resend.
    """
    match = _RETRY_PROOF.fullmatch(claim.last_error_code or "")
    return bool(match and claim.attempt_count == int(match[1]) + 1
                and claim.lease_fence == int(match[2]) + 1)


class GeneralOutboxReconciliation:
    def __init__(self, repository: OutboxWorkerPort, provider: ProviderEvidencePort, *,
                 clock: Callable[[], datetime] | None = None, max_evidence_age_seconds: int = 300) -> None:
        if type(max_evidence_age_seconds) is not int or max_evidence_age_seconds <= 0:
            raise ValueError("max_evidence_age_seconds must be a positive integer")
        self.repository = repository
        self.provider = provider
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_evidence_age_seconds = max_evidence_age_seconds

    def resolve(self, outbox_id: UUID, *, expected_attempt: int, expected_lease_fence: int,
                diagnostic_owner: str, allow_confirmed_no_effect_retry: bool = False) -> OutboxReconciliationResult:
        if not isinstance(outbox_id, UUID):
            raise ValueError("outbox_id must be a UUID")
        if any(type(value) is not int or value <= 0 for value in (expected_attempt, expected_lease_fence)):
            raise ValueError("expected attempt and fence must be positive integers")
        if not isinstance(diagnostic_owner, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", diagnostic_owner):
            raise ValueError("diagnostic_owner must be an exact bounded nonsecret identifier")
        if type(allow_confirmed_no_effect_retry) is not bool:
            raise ValueError("retry authorization must be explicit boolean")

        with mutation_scope("W07", unit_id=f"outbox-reconcile:{outbox_id}:{expected_attempt}:{expected_lease_fence}",
                            operation_class="CLEANER_OUTBOX_EVIDENCE_RECONCILIATION_ONE", target=str(outbox_id)):
            assert_current_production_writer()
            row = self.repository.read_outbox_reconciliation(outbox_id)
            if (row is None or row.outbox_id != outbox_id or row.outbox_status != "PENDING_RECONCILIATION"
                    or row.attempt_count != expected_attempt or row.lease_fence != expected_lease_fence
                    or not 1 <= row.attempt_count <= row.max_attempts):
                raise OutboxReconciliationError("OUTBOX_RECONCILIATION_EXPECTATION_MISMATCH")
            expected = deepcopy(row)
            binding = row_binding_sha256(expected)
            try:
                evidence = self.provider.inspect(deepcopy(expected))
            except ProviderEvidenceUnavailable:
                evidence = ProviderEvidence(ProviderEvidenceClass.PROVIDER_UNAVAILABLE, binding,
                                            self.clock(), "provider-observation-unavailable")
            if not isinstance(evidence, ProviderEvidence) or not isinstance(evidence.classification, ProviderEvidenceClass):
                raise OutboxReconciliationError("PROVIDER_EVIDENCE_MALFORMED")
            if evidence.row_binding_sha256 != binding:
                raise OutboxReconciliationError("PROVIDER_EVIDENCE_WRONG_ROW_OR_FENCE")
            now = self.clock()
            if (not isinstance(evidence.observed_at, datetime) or evidence.observed_at.tzinfo is None
                    or now.tzinfo is None or not 0 <= (now - evidence.observed_at).total_seconds() <= self.max_evidence_age_seconds
                    or not isinstance(evidence.evidence_ref, str) or not evidence.evidence_ref
                    or evidence.evidence_ref != evidence.evidence_ref.strip() or len(evidence.evidence_ref) > 512):
                raise OutboxReconciliationError("PROVIDER_EVIDENCE_STALE_OR_UNBOUND")
            if evidence.external_effect_id is not None and (
                    not isinstance(evidence.external_effect_id, str) or not evidence.external_effect_id
                    or evidence.external_effect_id != evidence.external_effect_id.strip()):
                raise OutboxReconciliationError("PROVIDER_EFFECT_ID_MALFORMED")
            evidence_digest = _digest({"binding": binding, "evidence": asdict(evidence),
                                       "diagnostic_owner": diagnostic_owner,
                                       "retry_authorized": allow_confirmed_no_effect_retry})
            resolution = None
            code = None
            if evidence.classification is ProviderEvidenceClass.CONFIRMED_EFFECT:
                if not evidence.external_effect_id:
                    raise OutboxReconciliationError("CONFIRMED_EFFECT_REQUIRES_RECEIPT")
                if expected.external_effect_id not in (None, evidence.external_effect_id):
                    raise OutboxReconciliationError("PROVIDER_EFFECT_RECEIPT_CONFLICT")
                resolution = "SUCCEEDED"
                code = f"EFFECT_CONFIRMED:{evidence_digest}:OWNER:{diagnostic_owner}"
            elif evidence.classification is ProviderEvidenceClass.CONFIRMED_NO_EFFECT:
                if (evidence.external_effect_id is not None or expected.external_effect_id is not None
                        or evidence.complete_no_effect_proof is not True or evidence.in_flight_effects_excluded is not True):
                    raise OutboxReconciliationError("NO_EFFECT_PROOF_INCOMPLETE_OR_CONFLICTING")
                if allow_confirmed_no_effect_retry:
                    resolution = "FAILED_RETRYABLE" if expected.attempt_count < expected.max_attempts else "DEAD_LETTER"
                    code = f"NO_EFFECT_CONFIRMED:{expected.attempt_count}:{expected.lease_fence}:{evidence_digest}:OWNER:{diagnostic_owner}"

            # No send, regardless of classification. Revalidate even a diagnostic-only
            # result so it cannot present a stale row as a current durable observation.
            assert_current_production_writer()
            if self.repository.read_outbox_reconciliation(outbox_id) != expected:
                raise OutboxReconciliationError("OUTBOX_RECONCILIATION_STATE_CHANGED")
            readback = expected
            if resolution is not None:
                readback = self.repository.resolve_outbox_reconciliation_exact(
                    expected, resolution=resolution, external_effect_id=evidence.external_effect_id,
                    error_code=code, retry_delay_seconds=0)
                if readback is None:
                    raise OutboxReconciliationError("OUTBOX_RECONCILIATION_CAS_REJECTED")
            return OutboxReconciliationResult(outbox_id, evidence.classification, resolution is not None,
                                              readback.outbox_status, diagnostic_owner, evidence_digest,
                                              binding, readback)

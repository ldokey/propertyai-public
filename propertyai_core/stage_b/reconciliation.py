from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence
from uuid import UUID

from .backfill import MigrationRepository
from .models import (
    CleanCycleEvidence,
    CutoverReadiness,
    GateEvidence,
    GateStatus,
    FinalDeltaPreflight,
    Population,
    ReconciliationReport,
    SnapshotManifest,
    utc_now,
)
from .normalize import canonical_value, semantic_hash
from .obligations import EFFECT_ONLY, STATE_MUTATING


BLOCKING_GATES = (
    "R01_SOURCE_SNAPSHOT_INTEGRITY",
    "R02_DETERMINISTIC_IDENTITY_COLLISION",
    "R03_REFERENCE_AND_RELATION_INTEGRITY",
    "R04_RESERVATION_EQUIVALENCE",
    "R05_CLEANING_AND_SCHEDULE_EQUIVALENCE",
    "R06_ASSIGNMENT_EQUIVALENCE",
    "R07_UNAVAILABILITY_REASSIGNMENT_RECONCILIATION",
    "R08_ROSTER_EQUIVALENCE",
    "R09_REFERENCE_SCOPE_EQUIVALENCE",
    "R10_LINEAGE_AND_IDEMPOTENCY",
    "R11_ECONOMICS_EQUIVALENCE",
    "R12_UNATTRIBUTED_OBSERVED_MUTATION_COUNT",
    "R13_PRE_CUTOVER_ZERO_EFFECT_AND_PRIVILEGE",
    "R14_UNAPPLIED_SOURCE_DELTA",
    "R15_UNRESOLVED_STATE_MUTATING_OBLIGATION",
)


ENTITY_GATE: Mapping[str, str] = {
    "reservation": "R04_RESERVATION_EQUIVALENCE",
    "cleaning_job": "R05_CLEANING_AND_SCHEDULE_EQUIVALENCE",
    "cleaning_schedule_revision": "R05_CLEANING_AND_SCHEDULE_EQUIVALENCE",
    "cleaning_offer_campaign": "R06_ASSIGNMENT_EQUIVALENCE",
    "cleaning_offer_candidate": "R06_ASSIGNMENT_EQUIVALENCE",
    "cleaning_assignment": "R06_ASSIGNMENT_EQUIVALENCE",
    "cleaner_unavailability_case": "R07_UNAVAILABILITY_REASSIGNMENT_RECONCILIATION",
    "cleaner_reassignment_request": "R07_UNAVAILABILITY_REASSIGNMENT_RECONCILIATION",
    "cleaning_schedule_reconciliation": "R07_UNAVAILABILITY_REASSIGNMENT_RECONCILIATION",
    "cleaner_property_roster": "R08_ROSTER_EQUIVALENCE",
    "organization": "R09_REFERENCE_SCOPE_EQUIVALENCE",
    "property": "R09_REFERENCE_SCOPE_EQUIVALENCE",
    "rental_unit": "R09_REFERENCE_SCOPE_EQUIVALENCE",
    "party": "R09_REFERENCE_SCOPE_EQUIVALENCE",
    "cleaner_profile": "R09_REFERENCE_SCOPE_EQUIVALENCE",
    "external_identity": "R09_REFERENCE_SCOPE_EQUIVALENCE",
}


@dataclass(frozen=True)
class ReconciliationContext:
    snapshot_integrity_findings: tuple[str, ...] = ()
    relation_findings: tuple[str, ...] = ()
    economics_findings: tuple[str, ...] = ()
    privilege_findings: tuple[str, ...] = ()


class ReconciliationEngine:
    def __init__(self, repository: MigrationRepository) -> None:
        self.repository = repository

    def reconcile(
        self,
        manifest: SnapshotManifest,
        population: Population,
        *,
        context: ReconciliationContext | None = None,
    ) -> ReconciliationReport:
        context = context or ReconciliationContext()
        findings: dict[str, list[str]] = {gate: [] for gate in BLOCKING_GATES}
        findings["R01_SOURCE_SNAPSHOT_INTEGRITY"].extend(context.snapshot_integrity_findings)
        expected_manifest_hash = semantic_hash(
            {
                "source_row_identities": sorted(manifest.source_row_identities),
                "source_semantic_hashes": dict(manifest.source_semantic_hashes),
                "file_content_hashes": dict(manifest.file_content_hashes),
            }
        )
        if expected_manifest_hash != manifest.manifest_hash:
            findings["R01_SOURCE_SNAPSHOT_INTEGRITY"].append("manifest hash does not verify")
        for source_key, observation in manifest.observations.items():
            if semantic_hash(observation.semantic_payload) != manifest.source_semantic_hashes[source_key]:
                findings["R01_SOURCE_SNAPSHOT_INTEGRITY"].append(
                    f"observation semantic hash does not verify {source_key}"
                )
        if manifest.convergence_passes < 2:
            findings["R01_SOURCE_SNAPSHOT_INTEGRITY"].append("manifest is not converged")
        findings["R02_DETERMINISTIC_IDENTITY_COLLISION"].extend(population.identity_collisions)
        findings["R03_REFERENCE_AND_RELATION_INTEGRITY"].extend(context.relation_findings)
        findings["R03_REFERENCE_AND_RELATION_INTEGRITY"].extend(population.authority_findings)
        if not self.repository.snapshot_receipt_exists(manifest.snapshot_id):
            findings["R10_LINEAGE_AND_IDEMPOTENCY"].append("missing immutable snapshot receipt")
        population_ids = {record.aggregate_id for record in population.records}
        for record in population.records:
            missing = [str(dep) for dep in record.dependencies if dep not in population_ids]
            if missing:
                findings["R03_REFERENCE_AND_RELATION_INTEGRITY"].append(
                    f"{record.source_key} dependencies not in population: {','.join(missing)}"
                )
            target = self.repository.target_payload(record)
            if target is None:
                findings[ENTITY_GATE.get(record.entity_type, "R03_REFERENCE_AND_RELATION_INTEGRITY")].append(
                    f"missing target {record.source_key}"
                )
            else:
                expected = dict(record.payload)
                if record.entity_type == "reservation":
                    expected["source_version"] = target.get("source_version")
                comparable = {key: target.get(key) for key in expected}
                if canonical_value(comparable) != canonical_value(expected):
                    findings[ENTITY_GATE.get(record.entity_type, "R03_REFERENCE_AND_RELATION_INTEGRITY")].append(
                        f"semantic mismatch {record.source_key}"
                    )
                if record.entity_type in {"cleaning_offer_campaign", "cleaning_assignment"}:
                    economics_fields = (
                        "base_fee_krw",
                        "replacement_urgency",
                        "urgent_premium_krw",
                        "total_agreed_fee_krw",
                        "urgent_premium_policy_version",
                    )
                    expected_economics = {
                        key: record.payload.get(key) for key in economics_fields
                    }
                    target_economics = {key: target.get(key) for key in economics_fields}
                    if canonical_value(expected_economics) != canonical_value(target_economics):
                        findings["R11_ECONOMICS_EQUIVALENCE"].append(
                            f"economics mismatch {record.source_key}"
                        )
                    if (
                        (record.payload.get("base_fee_krw") or 0)
                        + (record.payload.get("urgent_premium_krw") or 0)
                        != record.payload.get("total_agreed_fee_krw")
                    ):
                        findings["R11_ECONOMICS_EQUIVALENCE"].append(
                            f"source fee math invalid {record.source_key}"
                        )
            binding = self.repository.binding_for(record.source_key)
            if binding is None:
                findings["R10_LINEAGE_AND_IDEMPOTENCY"].append(
                    f"missing binding {record.source_key}"
                )
                findings["R14_UNAPPLIED_SOURCE_DELTA"].append(
                    f"unapplied source {record.source_key}"
                )
            else:
                binding_identity_mismatch = (
                    binding.aggregate_id != record.aggregate_id
                    or binding.aggregate_type not in (None, record.entity_type)
                    or binding.resource_code not in (None, record.entity_type)
                )
                if binding_identity_mismatch:
                    fact = f"source binding target identity mismatch {record.source_key}"
                    findings["R10_LINEAGE_AND_IDEMPOTENCY"].append(fact)
                    findings["R12_UNATTRIBUTED_OBSERVED_MUTATION_COUNT"].append(fact)
                    findings["R14_UNAPPLIED_SOURCE_DELTA"].append(fact)
                if binding.semantic_hash != manifest.source_semantic_hashes[record.source_key]:
                    findings["R14_UNAPPLIED_SOURCE_DELTA"].append(
                        f"source hash not applied {record.source_key}"
                    )
                if binding.snapshot_id != manifest.snapshot_id:
                    findings["R10_LINEAGE_AND_IDEMPOTENCY"].append(
                        f"binding cursor is not current {record.source_key}"
                    )
                if not self.repository.receipt_exists(manifest.snapshot_id, record.source_key):
                    findings["R10_LINEAGE_AND_IDEMPOTENCY"].append(
                        f"missing snapshot receipt {record.source_key}"
                    )
                if target is not None and binding.target_hash != semantic_hash(target):
                    findings["R12_UNATTRIBUTED_OBSERVED_MUTATION_COUNT"].append(
                        f"target hash changed outside binding {record.source_key}"
                    )
        expected_source_keys = set(manifest.source_row_identities)
        represented_source_keys = {record.source_key for record in population.records}
        for source_key in sorted(expected_source_keys - represented_source_keys):
            findings["R14_UNAPPLIED_SOURCE_DELTA"].append(
                f"manifest source row has no target representation {source_key}"
            )
        for source_key in sorted(set(self.repository.all_bindings()) - expected_source_keys):
            findings["R14_UNAPPLIED_SOURCE_DELTA"].append(
                f"source deletion/tombstone is unapplied {source_key}"
            )
        findings["R11_ECONOMICS_EQUIVALENCE"].extend(context.economics_findings)
        safety = self.repository.safety_counters()
        unattributed = int(safety.get("unattributed_mutations", 0))
        if unattributed:
            findings["R12_UNATTRIBUTED_OBSERVED_MUTATION_COUNT"].append(
                f"unattributed observed mutations={unattributed}"
            )
        for key in (
            "authority_epoch_mutations",
            "domain_event_mutations",
            "scheduled_action_mutations",
            "outbox_mutations",
        ):
            if int(safety.get(key, 0)):
                findings["R13_PRE_CUTOVER_ZERO_EFFECT_AND_PRIVILEGE"].append(
                    f"{key}={safety[key]}"
                )
        if not bool(safety.get("migration_role_safe", False)):
            findings["R13_PRE_CUTOVER_ZERO_EFFECT_AND_PRIVILEGE"].append(
                "migration role privilege proof is not clean"
            )
        findings["R13_PRE_CUTOVER_ZERO_EFFECT_AND_PRIVILEGE"].extend(
            context.privilege_findings
        )
        known_nonblocking_categories = {EFFECT_ONLY, "INFORMATIONAL"}
        state_obligations = [
            item
            for item in population.obligations
            if not item.resolved
            and (
                item.category == STATE_MUTATING
                or item.category not in known_nonblocking_categories
            )
        ]
        findings["R15_UNRESOLVED_STATE_MUTATING_OBLIGATION"].extend(
            (
                f"{item.source_key}: {item.reason}"
                if item.category == STATE_MUTATING
                else f"{item.source_key}: unknown obligation category={item.category!r}: {item.reason}"
            )
            for item in state_obligations
        )
        effect_count = sum(
            not item.resolved and item.category == EFFECT_ONLY and item.effect_only
            for item in population.obligations
        )
        gates = tuple(
            GateEvidence(
                gate=gate,
                status=GateStatus.PASS if not findings[gate] else GateStatus.BLOCKED,
                blocking_count=len(findings[gate]),
                facts=tuple(findings[gate]),
            )
            for gate in BLOCKING_GATES
        )
        return ReconciliationReport(
            manifest.snapshot_id, utc_now(), gates, int(effect_count)
        )


@dataclass
class ReadinessTracker:
    cycles: list[CleanCycleEvidence] = field(default_factory=list)

    def record(self, manifest: SnapshotManifest, report: ReconciliationReport) -> None:
        if report.snapshot_id != manifest.snapshot_id:
            raise ValueError("reconciliation report does not belong to snapshot")
        if manifest.independent_extraction_id is None:
            raise ValueError("clean-cycle snapshot lacks independent extraction identity")
        self.cycles.append(
            CleanCycleEvidence(
                manifest.snapshot_id,
                manifest.independent_extraction_id,
                manifest.extraction_evidence_hash or "",
                manifest.predecessor_snapshot_id,
                report,
            )
        )

    def readiness(self) -> CutoverReadiness:
        clean = [cycle for cycle in self.cycles if cycle.report.clean]
        blockers: list[str] = []
        if len(self.cycles) < 2:
            blockers.append("fewer than two reconciliation cycles")
        else:
            latest = self.cycles[-2:]
            if not all(cycle.report.clean for cycle in latest):
                blockers.append("last two reconciliation cycles are not both clean")
            if (
                latest[0].independent_extraction_id == latest[1].independent_extraction_id
                or latest[0].extraction_evidence_hash == latest[1].extraction_evidence_hash
            ):
                blockers.append("clean cycles are not independently extracted")
            if latest[1].predecessor_snapshot_id != latest[0].snapshot_id:
                blockers.append("clean cycles are not a contiguous snapshot chain")
        return CutoverReadiness(not blockers, len(clean), tuple(blockers))


def evaluate_final_delta_preflight(
    *,
    freeze_acquired: bool,
    final_report: ReconciliationReport,
    final_unapplied_delta_count: int,
) -> FinalDeltaPreflight:
    """Pure preflight decision; it never activates PostgreSQL authority."""
    if final_unapplied_delta_count < 0:
        raise ValueError("final delta count cannot be negative")
    ready = (
        freeze_acquired
        and final_report.clean
        and final_unapplied_delta_count == 0
    )
    return FinalDeltaPreflight(
        decision="READY_FOR_CONTROLLED_CUTOVER" if ready else "ABORT_CUTOVER",
        freeze_acquired=freeze_acquired,
        final_unapplied_delta_count=final_unapplied_delta_count,
        blocking_count=final_report.blocking_count,
        postgres_authority_activation_allowed=False,
    )


__all__ = [
    "BLOCKING_GATES",
    "ENTITY_GATE",
    "ReadinessTracker",
    "ReconciliationContext",
    "ReconciliationEngine",
    "evaluate_final_delta_preflight",
]

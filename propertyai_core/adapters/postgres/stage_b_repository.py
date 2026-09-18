from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from propertyai_core.adapters.postgres.errors import map_postgres_error
from propertyai_core.adapters.postgres.stage_b_pool import PostgresStageBMigrationPool
from propertyai_core.stage_b.backfill import BindingState
from propertyai_core.stage_b.identity import derived_identity
from propertyai_core.stage_b.models import (
    ApplyDisposition,
    ApplyResult,
    CanonicalRecord,
    ConcurrentMigrationError,
    ImmutableIdentityConflict,
    OldSnapshotError,
    SnapshotManifest,
)
from propertyai_core.stage_b.normalize import (
    canonical_value,
    reservation_semantics,
    semantic_hash,
)
from propertyai_core.stage_b.population import ENTITY_PRIMARY_KEYS, reservation_next_version


_DESTINATION_TYPE = "POSTGRES_PRE_CUTOVER"
_AUTHORITY_SCOPE = "CLEANER_SCHEDULING"

UNATTRIBUTED_MUTATION = "UNATTRIBUTED"
_ATTRIBUTION_NORMALIZATION: Mapping[str, str] = {
    "VERIFIED_KNOWN_APPLICATION_WRITER": "KNOWN_APPLICATION_WRITER",
    "KNOWN_APPLICATION_WRITER": "KNOWN_APPLICATION_WRITER",
    "ALLOWLISTED_HUMAN_NOTION_ACTOR": "ALLOWLISTED_HUMAN_NOTION_ACTOR",
    "VERIFIED_MAINTENANCE_EVIDENCE": "VERIFIED_MAINTENANCE",
    "VERIFIED_MAINTENANCE": "VERIFIED_MAINTENANCE",
    UNATTRIBUTED_MUTATION: UNATTRIBUTED_MUTATION,
}
_ATTRIBUTED_MUTATION_CATEGORIES = frozenset({
    "KNOWN_APPLICATION_WRITER",
    "ALLOWLISTED_HUMAN_NOTION_ACTOR",
    "VERIFIED_MAINTENANCE",
})


def normalize_mutation_attribution_category(category: str) -> str:
    if not isinstance(category, str):
        return UNATTRIBUTED_MUTATION
    return _ATTRIBUTION_NORMALIZATION.get(category.strip(), UNATTRIBUTED_MUTATION)


@dataclass(frozen=True)
class MutationAttributionObservation:
    category: str
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "category", normalize_mutation_attribution_category(self.category)
        )


_INSERTABLE_COLUMNS: Mapping[str, frozenset[str]] = {
    "organization": frozenset(("organization_id", "organization_code", "display_name", "organization_status", "data_environment")),
    "property": frozenset(("property_id", "organization_id", "property_code", "display_name", "timezone_name", "active")),
    "rental_unit": frozenset(("rental_unit_id", "rental_unit_code", "property_id", "display_name", "active")),
    "party": frozenset(("party_id", "party_code", "display_name", "data_environment", "active")),
    "cleaner_profile": frozenset(("cleaner_party_id", "operational_status", "max_daily_work_minutes", "max_daily_jobs")),
    "external_identity": frozenset(("external_identity_id", "party_id", "provider", "provider_user_id", "provider_chat_id", "bound_at", "revoked_at", "source_ref")),
    "cleaner_property_roster": frozenset(("roster_id", "cleaner_party_id", "property_id", "roster_status", "offer_tier", "priority_within_tier", "eligible_from", "eligible_until")),
    "reservation": frozenset(("reservation_id", "reservation_code", "property_id", "rental_unit_id", "source_channel", "external_reservation_id", "reservation_status", "check_in_at", "check_out_at", "source_version")),
    "cleaning_job": frozenset(("cleaning_id", "cleaning_code", "reservation_id", "property_id", "rental_unit_id", "schedule_source_type", "cleaning_status", "current_schedule_revision_id")),
    "cleaning_schedule_revision": frozenset(("schedule_revision_id", "cleaning_id", "revision_no", "service_window_start_at", "service_deadline_at", "required_work_minutes", "source_checkout_at", "source_reservation_version", "change_reason_code", "source_command_id")),
    "cleaning_offer_campaign": frozenset(("campaign_id", "cleaning_id", "schedule_revision_id", "campaign_no", "campaign_status", "open_tier_floor", "max_tier", "tier_expand_after_minutes", "acceptance_cutoff_at", "base_fee_krw", "replacement_urgency", "urgent_premium_krw", "total_agreed_fee_krw", "urgent_premium_policy_version", "opened_at", "closed_at", "closed_reason_code")),
    "cleaning_offer_candidate": frozenset(("offer_candidate_id", "campaign_id", "cleaner_party_id", "tier_no", "candidate_status", "proposal_version", "proposed_start_at", "proposed_end_at", "proposed_buffer_before_minutes", "proposed_buffer_after_minutes", "buffer_basis", "buffer_policy_ref", "evaluated_at", "declined_at", "accepted_at")),
    "cleaning_assignment": frozenset(("assignment_id", "cleaning_id", "assignment_no", "schedule_revision_id", "campaign_id", "offer_candidate_id", "accepted_proposal_version", "cleaner_party_id", "assignment_source", "assignment_status", "booked_at", "scheduled_start_at", "scheduled_end_at", "work_minutes_snapshot", "travel_buffer_before_minutes", "travel_buffer_after_minutes", "buffer_basis", "buffer_policy_ref", "base_fee_krw", "replacement_urgency", "urgent_premium_krw", "total_agreed_fee_krw", "urgent_premium_policy_version", "ended_at", "end_reason_code")),
    "cleaner_unavailability_case": frozenset(("unavailability_id", "cleaning_id", "schedule_revision_id", "original_assignment_id", "cleaner_party_id", "case_status", "availability_classification", "replacement_urgency", "reason_code", "reason_text", "occurred_at")),
    "cleaner_reassignment_request": frozenset(("reassignment_request_id", "unavailability_id", "cleaning_id", "cleaner_party_id", "original_assignment_id", "requested_schedule_revision_id", "request_no", "request_status", "requested_at", "decided_at", "decision_code")),
    "cleaning_schedule_reconciliation": frozenset(("schedule_reconciliation_id", "cleaning_id", "hard_booked_assignment_id", "base_assignment_revision_id", "target_schedule_revision_id", "case_version", "reconciliation_status", "reason_code", "source_ref", "resolved_at", "resolution_code")),
}

_UPDATABLE_COLUMNS: Mapping[str, frozenset[str]] = {
    "organization": frozenset(("display_name", "organization_status")),
    "property": frozenset(("display_name", "timezone_name", "active")),
    "rental_unit": frozenset(("display_name", "active")),
    "party": frozenset(("display_name", "active")),
    "cleaner_profile": frozenset(("operational_status", "max_daily_work_minutes", "max_daily_jobs")),
    "external_identity": frozenset(("revoked_at",)),
    "cleaner_property_roster": frozenset(("roster_status", "offer_tier", "priority_within_tier", "eligible_from", "eligible_until")),
    "reservation": frozenset(("reservation_status", "check_in_at", "check_out_at", "source_version")),
    "cleaning_job": frozenset(("cleaning_status", "current_schedule_revision_id")),
    "cleaning_schedule_revision": frozenset(),
    "cleaning_offer_campaign": frozenset(("campaign_status", "open_tier_floor", "closed_at", "closed_reason_code")),
    "cleaning_offer_candidate": frozenset(("candidate_status", "proposal_version", "proposed_start_at", "proposed_end_at", "proposed_buffer_before_minutes", "proposed_buffer_after_minutes", "buffer_basis", "buffer_policy_ref", "declined_at", "accepted_at")),
    "cleaning_assignment": frozenset(("assignment_status", "ended_at", "end_reason_code")),
    "cleaner_unavailability_case": frozenset(("case_status", "reason_code", "reason_text")),
    "cleaner_reassignment_request": frozenset(("request_status", "decided_at", "decision_code")),
    "cleaning_schedule_reconciliation": frozenset(("target_schedule_revision_id", "case_version", "reconciliation_status", "resolved_at", "resolution_code")),
}

_UPDATED_AT_TABLES = frozenset(
    entity_type
    for entity_type in _UPDATABLE_COLUMNS
    if entity_type != "external_identity"
)


def _json_binding(value: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise ConcurrentMigrationError("binding external_version is not Stage B JSON") from error
    required = {"semantic_hash", "target_hash", "snapshot_id", "replay_cursor"}
    if not isinstance(parsed, dict) or not required <= parsed.keys():
        raise ConcurrentMigrationError("binding lacks Stage B replay evidence")
    return parsed


class PostgresStageBMigrationRepository:
    def __init__(
        self,
        pool: PostgresStageBMigrationPool,
        *,
        mutation_attribution_observer: (
            Callable[[], Iterable[MutationAttributionObservation]] | None
        ) = None,
    ) -> None:
        self.pool = pool
        self._mutation_attribution_observer = mutation_attribution_observer
        with self.pool._connection() as connection:
            self._authority_epoch_baseline = {
                row["scope_code"]: int(row["current_epoch"])
                for row in connection.execute(
                    "SELECT scope_code, current_epoch FROM propertyai.authority_epoch"
                ).fetchall()
            }
            baseline = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM propertyai.domain_event) AS domain_events,
                    (SELECT count(*) FROM propertyai.business_scheduled_action) AS scheduled_actions,
                    (SELECT count(*) FROM propertyai.integration_outbox) AS outbox_rows
                """
            ).fetchone()
            assert baseline is not None
            self._protected_row_baseline = {
                "domain_events": int(baseline["domain_events"]),
                "scheduled_actions": int(baseline["scheduled_actions"]),
                "outbox_rows": int(baseline["outbox_rows"]),
            }

    @staticmethod
    def _binding_state(row: Mapping[str, Any]) -> BindingState:
        version = _json_binding(row["external_version"])
        return BindingState(
            source_key=row["external_resource_id"],
            aggregate_id=row["aggregate_id"],
            semantic_hash=version["semantic_hash"],
            target_hash=version["target_hash"],
            snapshot_id=UUID(version["snapshot_id"]),
            replay_cursor=version["replay_cursor"],
            aggregate_type=row.get("aggregate_type"),
            resource_code=row.get("resource_code"),
        )

    def binding_for(self, source_key: str) -> BindingState | None:
        with self.pool._connection() as connection:
            rows = connection.execute(
                """
                SELECT aggregate_type, aggregate_id, resource_code,
                       external_resource_id, external_version
                  FROM propertyai.integration_resource_binding
                 WHERE destination_type = %s AND external_resource_id = %s
                """,
                (_DESTINATION_TYPE, source_key),
            ).fetchall()
        if len(rows) > 1:
            raise ConcurrentMigrationError(f"source key has multiple target bindings: {source_key}")
        return None if not rows else self._binding_state(rows[0])

    def all_bindings(self) -> Mapping[str, BindingState]:
        with self.pool._connection() as connection:
            rows = connection.execute(
                """
                SELECT aggregate_type, aggregate_id, resource_code,
                       external_resource_id, external_version
                  FROM propertyai.integration_resource_binding
                 WHERE destination_type = %s
                """,
                (_DESTINATION_TYPE,),
            ).fetchall()
        bindings: dict[str, BindingState] = {}
        for row in rows:
            source_key = row["external_resource_id"]
            if source_key in bindings:
                raise ConcurrentMigrationError(
                    f"source key has multiple target bindings: {source_key}"
                )
            bindings[source_key] = self._binding_state(row)
        return bindings

    def target_payload(self, record: CanonicalRecord) -> Mapping[str, object] | None:
        columns = tuple(record.payload)
        if record.entity_type == "reservation" and "source_version" not in columns:
            columns += ("source_version",)
        table = record.entity_type
        primary_key = ENTITY_PRIMARY_KEYS[table]
        query = sql.SQL("SELECT {} FROM propertyai.{} WHERE {} = %s").format(
            sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.Identifier(table),
            sql.Identifier(primary_key),
        )
        with self.pool._connection() as connection:
            return connection.execute(query, (record.aggregate_id,)).fetchone()

    def receipt_exists(self, snapshot_id: UUID, source_key: str) -> bool:
        with self.pool._connection() as connection:
            row = connection.execute(
                """
                SELECT 1
                  FROM propertyai.command_receipt
                 WHERE principal_type = 'MIGRATION'
                   AND authority_epoch = 0
                   AND request_payload ->> 'snapshot_id' = %s
                   AND request_payload ->> 'source_key' = %s
                """,
                (str(snapshot_id), source_key),
            ).fetchone()
        return row is not None

    def record_snapshot(self, manifest: SnapshotManifest) -> None:
        command_id = derived_identity("migration-receipt", "snapshot", manifest.snapshot_id)
        request = {
            "run_id": str(manifest.run_id),
            "snapshot_id": str(manifest.snapshot_id),
            "predecessor_snapshot_id": (
                None
                if manifest.predecessor_snapshot_id is None
                else str(manifest.predecessor_snapshot_id)
            ),
            "manifest_hash": manifest.manifest_hash,
            "source_runtime_reference": manifest.source_runtime_reference,
            "source_row_count": len(manifest.source_row_identities),
            "independent_extraction_id": str(manifest.independent_extraction_id),
            "extraction_evidence_hash": manifest.extraction_evidence_hash,
        }
        idempotency_key = f"stage-b:snapshot:{manifest.snapshot_id}"
        with self.pool._connection() as connection, connection.transaction():
            inserted = connection.execute(
                """
                INSERT INTO propertyai.command_receipt(
                    command_id, authority_scope_code, command_type, idempotency_key,
                    request_payload, source_channel_code, principal_type, authority_epoch,
                    source_observed_at, decided_at, result_type, result_payload
                ) VALUES (%s, %s, 'STAGE_B_OFFLINE_SNAPSHOT', %s, %s,
                          'LEGACY_SNAPSHOT', 'MIGRATION', 0, %s, %s,
                          'CONVERGED_SEMANTIC_MANIFEST', %s)
                ON CONFLICT (authority_scope_code, command_type, idempotency_key) DO NOTHING
                RETURNING command_id
                """,
                (
                    command_id,
                    _AUTHORITY_SCOPE,
                    idempotency_key,
                    Jsonb(request),
                    manifest.scan_completed_at,
                    manifest.scan_completed_at,
                    Jsonb({"manifest_hash": manifest.manifest_hash}),
                ),
            ).fetchone()
            if inserted is None:
                existing = connection.execute(
                    """
                    SELECT command_id, request_payload, principal_type, authority_epoch
                      FROM propertyai.command_receipt
                     WHERE authority_scope_code=%s
                       AND command_type='STAGE_B_OFFLINE_SNAPSHOT'
                       AND idempotency_key=%s
                    """,
                    (_AUTHORITY_SCOPE, idempotency_key),
                ).fetchone()
                if existing != {
                    "command_id": command_id,
                    "request_payload": request,
                    "principal_type": "MIGRATION",
                    "authority_epoch": 0,
                }:
                    raise ConcurrentMigrationError("snapshot receipt conflict")

    def snapshot_receipt_exists(self, snapshot_id: UUID) -> bool:
        with self.pool._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM propertyai.command_receipt
                 WHERE command_type='STAGE_B_OFFLINE_SNAPSHOT'
                   AND principal_type='MIGRATION' AND authority_epoch=0
                   AND request_payload ->> 'snapshot_id' = %s
                """,
                (str(snapshot_id),),
            ).fetchone()
        return row is not None

    @staticmethod
    def _select_target(connection: psycopg.Connection, record: CanonicalRecord) -> Mapping[str, Any] | None:
        columns = tuple(record.payload)
        if record.entity_type == "reservation" and "source_version" not in columns:
            columns += ("source_version",)
        primary_key = ENTITY_PRIMARY_KEYS[record.entity_type]
        lock_clause = sql.SQL(" FOR UPDATE") if _UPDATABLE_COLUMNS[record.entity_type] else sql.SQL("")
        query = sql.SQL("SELECT {} FROM propertyai.{} WHERE {} = %s{}").format(
            sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.Identifier(record.entity_type),
            sql.Identifier(primary_key),
            lock_clause,
        )
        return connection.execute(query, (record.aggregate_id,)).fetchone()

    @staticmethod
    def _insert_target(connection: psycopg.Connection, record: CanonicalRecord, payload: Mapping[str, Any]) -> None:
        allowed = _INSERTABLE_COLUMNS[record.entity_type]
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unsupported {record.entity_type} insert columns: {sorted(unknown)}")
        columns = tuple(payload)
        query = sql.SQL("INSERT INTO propertyai.{} ({}) VALUES ({})").format(
            sql.Identifier(record.entity_type),
            sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        )
        connection.execute(query, tuple(payload[column] for column in columns))

    @staticmethod
    def _update_target(
        connection: psycopg.Connection,
        record: CanonicalRecord,
        existing: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> bool:
        changed = [
            column
            for column in _UPDATABLE_COLUMNS[record.entity_type]
            if column in payload
            and canonical_value(existing.get(column)) != canonical_value(payload.get(column))
        ]
        unhandled = [
            column
            for column in payload
            if column not in record.immutable_fields
            and column not in _UPDATABLE_COLUMNS[record.entity_type]
            and canonical_value(existing.get(column)) != canonical_value(payload.get(column))
        ]
        if unhandled:
            raise ImmutableIdentityConflict(
                f"non-refreshable {record.entity_type} facts changed: {sorted(unhandled)}"
            )
        if not changed:
            return False
        assignments = [
            sql.SQL("{} = {}").format(sql.Identifier(column), sql.Placeholder())
            for column in changed
        ]
        if record.entity_type in _UPDATED_AT_TABLES:
            assignments.append(sql.SQL("updated_at = clock_timestamp()"))
        query = sql.SQL("UPDATE propertyai.{} SET {} WHERE {} = %s").format(
            sql.Identifier(record.entity_type),
            sql.SQL(", ").join(assignments),
            sql.Identifier(ENTITY_PRIMARY_KEYS[record.entity_type]),
        )
        connection.execute(query, tuple(payload[column] for column in changed) + (record.aggregate_id,))
        return True

    @staticmethod
    def _insert_receipt(
        connection: psycopg.Connection,
        record: CanonicalRecord,
        snapshot_id: UUID,
        run_id: UUID,
        observed_at: datetime,
    ) -> None:
        command_id = derived_identity("migration-receipt", snapshot_id, record.source_key)
        idempotency_key = f"stage-b:{snapshot_id}:{record.source_key}"
        request = {
            "run_id": str(run_id),
            "snapshot_id": str(snapshot_id),
            "source_key": record.source_key,
            "source_semantic_hash": record.semantic_hash,
            "target_entity_type": record.entity_type,
            "target_aggregate_id": str(record.aggregate_id),
        }
        inserted = connection.execute(
            """
            INSERT INTO propertyai.command_receipt(
                command_id, authority_scope_code, command_type, idempotency_key,
                request_payload, source_channel_code, principal_type, authority_epoch,
                source_observed_at, decided_at, result_type, result_id, result_payload
            ) VALUES (%s, %s, 'STAGE_B_OFFLINE_APPLY', %s, %s, 'LEGACY_SNAPSHOT',
                      'MIGRATION', 0, %s, %s, 'MIGRATION_APPLY_INTENT', %s, %s)
            ON CONFLICT (authority_scope_code, command_type, idempotency_key) DO NOTHING
            RETURNING command_id
            """,
            (
                command_id,
                _AUTHORITY_SCOPE,
                idempotency_key,
                Jsonb(request),
                observed_at,
                observed_at,
                record.aggregate_id,
                Jsonb({"source_semantic_hash": record.semantic_hash}),
            ),
        ).fetchone()
        if inserted is None:
            existing = connection.execute(
                """
                SELECT command_id, request_payload, principal_type, authority_epoch,
                       result_type, result_id
                  FROM propertyai.command_receipt
                 WHERE authority_scope_code = %s
                   AND command_type = 'STAGE_B_OFFLINE_APPLY'
                   AND idempotency_key = %s
                """,
                (_AUTHORITY_SCOPE, idempotency_key),
            ).fetchone()
            expected = {
                "command_id": command_id,
                "request_payload": request,
                "principal_type": "MIGRATION",
                "authority_epoch": 0,
                "result_type": "MIGRATION_APPLY_INTENT",
                "result_id": record.aggregate_id,
            }
            if existing is None or any(existing[key] != value for key, value in expected.items()):
                raise ConcurrentMigrationError("migration receipt conflict")

    def apply_record(
        self,
        record: CanonicalRecord,
        *,
        snapshot_id: UUID,
        predecessor_snapshot_id: UUID | None,
        run_id: UUID,
        observed_at: datetime,
    ) -> ApplyResult:
        version: int | None = None
        try:
            with self.pool._connection() as connection, connection.transaction():
                binding_rows = connection.execute(
                    """
                    SELECT binding_id, aggregate_type, aggregate_id, resource_code,
                           external_resource_id, external_version
                      FROM propertyai.integration_resource_binding
                     WHERE destination_type = %s AND external_resource_id = %s
                     FOR UPDATE
                    """,
                    (_DESTINATION_TYPE, record.source_key),
                ).fetchall()
                if len(binding_rows) > 1:
                    raise ConcurrentMigrationError(
                        f"source key has multiple target bindings: {record.source_key}"
                    )
                binding = None if not binding_rows else self._binding_state(binding_rows[0])
                if binding is not None:
                    if (
                        binding.aggregate_id != record.aggregate_id
                        or binding.aggregate_type != record.entity_type
                        or binding.resource_code != record.entity_type
                    ):
                        raise ImmutableIdentityConflict(
                            f"source key target binding remap rejected: {record.source_key}"
                        )
                    if binding.snapshot_id not in {snapshot_id, predecessor_snapshot_id}:
                        raise OldSnapshotError(
                            f"source binding at {binding.snapshot_id}; expected {predecessor_snapshot_id}"
                        )
                existing = self._select_target(connection, record)
                if existing is not None and binding is None:
                    raise ConcurrentMigrationError(
                        f"target exists without Stage B binding: {record.source_key}"
                    )
                if existing is not None:
                    for field in record.immutable_fields:
                        if canonical_value(existing.get(field)) != canonical_value(record.payload.get(field)):
                            raise ImmutableIdentityConflict(
                                f"immutable target conflict: {record.entity_type}.{field}"
                            )
                payload = dict(record.payload)
                if record.entity_type == "reservation":
                    current_version = None if existing is None else int(existing["source_version"])
                    current_semantics = None if existing is None else reservation_semantics(existing)
                    version, changed = reservation_next_version(
                        current_version, current_semantics, reservation_semantics(payload)
                    )
                    payload["source_version"] = version
                else:
                    changed = existing is None
                if binding is not None and existing is not None and binding.target_hash != semantic_hash(existing):
                    raise ConcurrentMigrationError(
                        f"unattributed target change before apply: {record.source_key}"
                    )
                self._insert_receipt(connection, record, snapshot_id, run_id, observed_at)
                if existing is None:
                    self._insert_target(connection, record, payload)
                    changed = True
                elif record.entity_type == "reservation":
                    if changed:
                        self._update_target(connection, record, existing, payload)
                else:
                    changed = self._update_target(connection, record, existing, payload)
                refreshed = self._select_target(connection, record)
                if refreshed is None:
                    raise ConcurrentMigrationError("target disappeared within migration transaction")
                target_hash = semantic_hash(refreshed)
                replay_cursor = f"{snapshot_id}:{record.source_key}"
                external_version = json.dumps(
                    {
                        "semantic_hash": record.semantic_hash,
                        "target_hash": target_hash,
                        "snapshot_id": str(snapshot_id),
                        "replay_cursor": replay_cursor,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                binding_id = derived_identity("binding", _DESTINATION_TYPE, record.source_key)
                applied_version = version or 1
                connection.execute(
                    """
                    INSERT INTO propertyai.integration_resource_binding(
                        binding_id, aggregate_type, aggregate_id, destination_type,
                        resource_code, external_resource_id, external_uid, sync_status,
                        last_applied_aggregate_version, external_version, last_synced_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'SYNCED', %s, %s, %s)
                    ON CONFLICT (destination_type, resource_code, external_resource_id)
                    DO UPDATE SET
                        external_uid = EXCLUDED.external_uid,
                        sync_status = EXCLUDED.sync_status,
                        last_applied_aggregate_version = EXCLUDED.last_applied_aggregate_version,
                        external_version = EXCLUDED.external_version,
                        last_synced_at = EXCLUDED.last_synced_at,
                        updated_at = clock_timestamp()
                    """,
                    (
                        binding_id,
                        record.entity_type,
                        record.aggregate_id,
                        _DESTINATION_TYPE,
                        record.entity_type,
                        record.source_key,
                        str(snapshot_id),
                        applied_version,
                        external_version,
                        observed_at,
                    ),
                )
            return ApplyResult(
                record.source_key,
                record.aggregate_id,
                ApplyDisposition.APPLIED if changed else ApplyDisposition.NOOP,
                record.semantic_hash,
                snapshot_id,
                version,
            )
        except (
            ConcurrentMigrationError,
            ImmutableIdentityConflict,
            OldSnapshotError,
            ValueError,
        ):
            raise
        except psycopg.Error as error:
            raise map_postgres_error(error) from error

    @staticmethod
    def _migration_role_is_safe(connection: psycopg.Connection) -> bool:
        schema = connection.execute(
            """
            SELECT has_schema_privilege(session_user, 'propertyai', 'USAGE') AS usage,
                   has_schema_privilege(session_user, 'propertyai', 'CREATE') AS create_priv
            """
        ).fetchone()
        if schema is None or not schema["usage"] or schema["create_priv"]:
            return False
        memberships = connection.execute(
            """
            SELECT parent.rolname
              FROM pg_catalog.pg_auth_members membership
              JOIN pg_catalog.pg_roles member ON member.oid = membership.member
              JOIN pg_catalog.pg_roles parent ON parent.oid = membership.roleid
             WHERE member.rolname = session_user
            """
        ).fetchall()
        if memberships:
            return False
        executable = connection.execute(
            """
            SELECT p.oid
              FROM pg_catalog.pg_proc p
              JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'propertyai'
               AND has_function_privilege(session_user, p.oid, 'EXECUTE')
             LIMIT 1
            """
        ).fetchone()
        if executable is not None:
            return False
        sequence_privilege = connection.execute(
            """
            SELECT c.relname
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'propertyai'
               AND c.relkind = 'S'
               AND (has_sequence_privilege(session_user, c.oid, 'USAGE')
                    OR has_sequence_privilege(session_user, c.oid, 'SELECT')
                    OR has_sequence_privilege(session_user, c.oid, 'UPDATE'))
             LIMIT 1
            """
        ).fetchone()
        if sequence_privilege is not None:
            return False
        allowed_insert = set(_INSERTABLE_COLUMNS) | {
            "command_receipt",
            "integration_resource_binding",
        }
        required_select = allowed_insert | {
            "authority_epoch",
            "domain_event",
            "business_scheduled_action",
            "integration_outbox",
            "flyway_schema_history",
        }
        update_columns = {
            table: set(columns) | ({"updated_at"} if table in _UPDATED_AT_TABLES else set())
            for table, columns in _UPDATABLE_COLUMNS.items()
        }
        update_columns["integration_resource_binding"] = {
            "external_uid",
            "sync_status",
            "last_applied_aggregate_version",
            "external_version",
            "last_synced_at",
            "updated_at",
        }
        tables = connection.execute(
            """
            SELECT c.relname
              FROM pg_catalog.pg_class c
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'propertyai' AND c.relkind IN ('r', 'p')
             ORDER BY c.relname
            """
        ).fetchall()
        for row in tables:
            table = row["relname"]
            relation = f"propertyai.{table}"
            if bool(connection.execute(
                "SELECT has_table_privilege(session_user, %s, 'INSERT') AS value",
                (relation,),
            ).fetchone()["value"]) != (table in allowed_insert):
                return False
            if table in required_select and not connection.execute(
                "SELECT has_table_privilege(session_user, %s, 'SELECT') AS value",
                (relation,),
            ).fetchone()["value"]:
                return False
            for privilege in ("DELETE", "TRUNCATE", "TRIGGER"):
                if connection.execute(
                    "SELECT has_table_privilege(session_user, %s, %s) AS value",
                    (relation, privilege),
                ).fetchone()["value"]:
                    return False
            columns = connection.execute(
                """
                SELECT a.attname
                  FROM pg_catalog.pg_attribute a
                  JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname='propertyai' AND c.relname=%s
                   AND a.attnum > 0 AND NOT a.attisdropped
                """,
                (table,),
            ).fetchall()
            expected_updates = update_columns.get(table, set())
            for column_row in columns:
                column = column_row["attname"]
                has_update = connection.execute(
                    "SELECT has_column_privilege(session_user, %s, %s, 'UPDATE') AS value",
                    (relation, column),
                ).fetchone()["value"]
                if bool(has_update) != (column in expected_updates):
                    return False
        return True

    def _unattributed_mutation_count(self) -> int:
        if self._mutation_attribution_observer is None:
            return 1
        try:
            observations = tuple(self._mutation_attribution_observer())
        except Exception:
            return 1
        unattributed = 0
        for observation in observations:
            if not isinstance(observation, MutationAttributionObservation):
                unattributed += 1
                continue
            if (
                observation.category not in _ATTRIBUTED_MUTATION_CATEGORIES
                or not isinstance(observation.evidence_ref, str)
                or not observation.evidence_ref.strip()
            ):
                unattributed += 1
        return unattributed

    def safety_counters(self) -> Mapping[str, int | bool]:
        with self.pool._connection() as connection:
            epochs = {
                row["scope_code"]: int(row["current_epoch"])
                for row in connection.execute(
                    "SELECT scope_code, current_epoch FROM propertyai.authority_epoch"
                ).fetchall()
            }
            counts = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM propertyai.domain_event) AS domain_events,
                    (SELECT count(*) FROM propertyai.business_scheduled_action) AS scheduled_actions,
                    (SELECT count(*) FROM propertyai.integration_outbox) AS outbox_rows
                """
            ).fetchone()
            assert counts is not None
            migration_role_safe = self._migration_role_is_safe(connection)
        return {
            "authority_epoch_mutations": int(epochs != self._authority_epoch_baseline),
            "domain_event_mutations": int(counts["domain_events"]) - self._protected_row_baseline["domain_events"],
            "scheduled_action_mutations": int(counts["scheduled_actions"]) - self._protected_row_baseline["scheduled_actions"],
            "outbox_mutations": int(counts["outbox_rows"]) - self._protected_row_baseline["outbox_rows"],
            "unattributed_mutations": self._unattributed_mutation_count(),
            "migration_role_safe": migration_role_safe,
        }



__all__ = [
    "MutationAttributionObservation",
    "PostgresStageBMigrationRepository",
    "UNATTRIBUTED_MUTATION",
    "normalize_mutation_attribution_category",
]

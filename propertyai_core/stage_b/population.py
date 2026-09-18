from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from .identity import (
    IdentityRegistry,
    assignment_identity,
    derived_identity,
    deterministic_id,
    notion_identity,
    telegram_identity,
)
from .models import AmbiguousIdentityError, CanonicalRecord, Population, ReviewRequired, SnapshotManifest
from .normalize import (
    canonical_value,
    normalize_datetime,
    normalize_integer,
    normalize_page_uuid,
    normalize_status,
    semantic_hash,
)
from .obligations import STATE_MUTATING, obligation


DOMAIN_SOURCE_AUTHORITY: Mapping[str, str] = {
    "roster": "NOTION_CLEANER_PROPERTY_ACCESS",
    "runtime_telegram_projection": "CLEANERS_JSON_AFTER_EXACT_PARTY_ROSTER_MATCH",
    "reservation": "CANONICAL_RESERVATION_PROJECTION",
    "cleaning": "CLEANING_PROJECTION",
    "assignment": "ASSIGNMENT_HISTORY_19_PLUS_EXACT_EXECUTION_EVIDENCE",
    "economics": "IMMUTABLE_ASSIGNMENT_SNAPSHOT_OR_RETAINED_ACTION_EVIDENCE",
    "unavailability": "ASSIGNMENT_END_PLUS_UNAVAILABLE_ACTION_OR_PERFORMANCE_EVIDENCE",
    "reassignment": "DURABLE_REQUEST_PLUS_COMMITTED_DECISION_EVIDENCE",
}


# These source-type names are the frozen Stage B adapter identities.  A semantic
# payload may describe an entity, but it may not grant itself authority to emit
# that entity.  Only the adapter identity can do that.
SOURCE_TYPE_ENTITY_AUTHORITY: Mapping[str, frozenset[str]] = {
    "NOTION_CLEANER_PROPERTY_ACCESS": frozenset({"cleaner_property_roster"}),
    "CLEANERS_JSON_AFTER_EXACT_PARTY_ROSTER_MATCH": frozenset({"external_identity"}),
    "CANONICAL_RESERVATION_PROJECTION": frozenset({"reservation"}),
    "CLEANING_PROJECTION": frozenset({"cleaning_job", "cleaning_schedule_revision"}),
    "ASSIGNMENT_HISTORY_19_PLUS_EXACT_EXECUTION_EVIDENCE": frozenset(
        {"cleaning_offer_campaign", "cleaning_offer_candidate", "cleaning_assignment"}
    ),
    "ASSIGNMENT_END_PLUS_UNAVAILABLE_ACTION_OR_PERFORMANCE_EVIDENCE": frozenset(
        {"cleaner_unavailability_case"}
    ),
    "DURABLE_REQUEST_PLUS_COMMITTED_DECISION_EVIDENCE": frozenset(
        {"cleaner_reassignment_request", "cleaning_schedule_reconciliation"}
    ),
}

_AUTHORITY_GUARDED_ENTITIES = frozenset(
    entity
    for entities in SOURCE_TYPE_ENTITY_AUTHORITY.values()
    for entity in entities
)


ENTITY_PRIMARY_KEYS: Mapping[str, str] = {
    "organization": "organization_id",
    "property": "property_id",
    "rental_unit": "rental_unit_id",
    "party": "party_id",
    "cleaner_profile": "cleaner_party_id",
    "external_identity": "external_identity_id",
    "cleaner_property_roster": "roster_id",
    "reservation": "reservation_id",
    "cleaning_job": "cleaning_id",
    "cleaning_schedule_revision": "schedule_revision_id",
    "cleaning_offer_campaign": "campaign_id",
    "cleaning_offer_candidate": "offer_candidate_id",
    "cleaning_assignment": "assignment_id",
    "cleaner_unavailability_case": "unavailability_id",
    "cleaner_reassignment_request": "reassignment_request_id",
    "cleaning_schedule_reconciliation": "schedule_reconciliation_id",
}


IMMUTABLE_FIELDS: Mapping[str, tuple[str, ...]] = {
    "organization": ("organization_id", "organization_code", "data_environment"),
    "property": ("property_id", "organization_id", "property_code"),
    "rental_unit": ("rental_unit_id", "property_id", "rental_unit_code"),
    "party": ("party_id", "party_code", "data_environment"),
    "cleaner_profile": ("cleaner_party_id",),
    "external_identity": (
        "external_identity_id",
        "party_id",
        "provider",
        "provider_user_id",
        "bound_at",
    ),
    "cleaner_property_roster": ("roster_id", "cleaner_party_id", "property_id"),
    "reservation": (
        "reservation_id",
        "reservation_code",
        "property_id",
        "rental_unit_id",
        "source_channel",
        "external_reservation_id",
    ),
    "cleaning_job": (
        "cleaning_id",
        "cleaning_code",
        "reservation_id",
        "property_id",
        "rental_unit_id",
        "schedule_source_type",
    ),
    "cleaning_schedule_revision": (
        "schedule_revision_id",
        "cleaning_id",
        "revision_no",
        "service_window_start_at",
        "service_deadline_at",
        "required_work_minutes",
        "source_checkout_at",
        "source_reservation_version",
    ),
    "cleaning_offer_campaign": (
        "campaign_id",
        "cleaning_id",
        "schedule_revision_id",
        "campaign_no",
        "base_fee_krw",
        "replacement_urgency",
        "urgent_premium_krw",
        "total_agreed_fee_krw",
        "opened_at",
    ),
    "cleaning_offer_candidate": (
        "offer_candidate_id",
        "campaign_id",
        "cleaner_party_id",
        "tier_no",
        "evaluated_at",
    ),
    "cleaning_assignment": (
        "assignment_id",
        "cleaning_id",
        "assignment_no",
        "schedule_revision_id",
        "campaign_id",
        "offer_candidate_id",
        "accepted_proposal_version",
        "cleaner_party_id",
        "assignment_source",
        "booked_at",
        "scheduled_start_at",
        "scheduled_end_at",
        "work_minutes_snapshot",
        "base_fee_krw",
        "replacement_urgency",
        "urgent_premium_krw",
        "total_agreed_fee_krw",
    ),
    "cleaner_unavailability_case": (
        "unavailability_id",
        "cleaning_id",
        "schedule_revision_id",
        "original_assignment_id",
        "cleaner_party_id",
        "occurred_at",
    ),
    "cleaner_reassignment_request": (
        "reassignment_request_id",
        "unavailability_id",
        "cleaning_id",
        "cleaner_party_id",
        "original_assignment_id",
        "requested_schedule_revision_id",
        "request_no",
        "requested_at",
    ),
    "cleaning_schedule_reconciliation": (
        "schedule_reconciliation_id",
        "cleaning_id",
        "hard_booked_assignment_id",
        "base_assignment_revision_id",
    ),
}


DEPENDENCY_FIELDS = (
    "organization_id",
    "party_id",
    "property_id",
    "rental_unit_id",
    "cleaner_party_id",
    "reservation_id",
    "cleaning_id",
    "schedule_revision_id",
    "campaign_id",
    "offer_candidate_id",
    "original_assignment_id",
    "unavailability_id",
    "requested_schedule_revision_id",
    "hard_booked_assignment_id",
    "base_assignment_revision_id",
    "target_schedule_revision_id",
)


@dataclass(frozen=True)
class ScheduleWindow:
    service_window_start_at: datetime
    service_deadline_at: datetime
    required_work_minutes: int | None
    needed_by: frozenset[str]
    source_checkout_at: datetime | None = None
    source_reservation_version: int | None = None
    change_reason_code: str = "LEGACY_EXACT_RECONSTRUCTION"

    def __post_init__(self) -> None:
        allowed = {"ACCEPTED_ASSIGNMENT", "CURRENT_OPEN_ACTION", "CURRENT_CLEANING"}
        if not self.needed_by or not self.needed_by <= allowed:
            raise ValueError("schedule window lacks an allowed exact retention reason")
        delta = self.service_deadline_at - self.service_window_start_at
        if delta <= timedelta(0):
            raise ValueError("schedule window is not positive")
        if delta.microseconds != 0:
            raise ValueError("schedule window is not an exact whole-second interval")
        exact_seconds = delta.days * 86_400 + delta.seconds
        if exact_seconds % 60 != 0:
            raise ValueError("schedule window is not an exact whole-minute interval")
        derived_minutes = exact_seconds // 60
        if derived_minutes <= 0:
            raise ValueError("schedule window has zero required work minutes")
        if self.required_work_minutes is not None:
            supplied = normalize_integer(self.required_work_minutes, minimum=1)
            if supplied != derived_minutes:
                raise ValueError(
                    f"required_work_minutes mismatch supplied={supplied} derived={derived_minutes}"
                )
        object.__setattr__(self, "required_work_minutes", derived_minutes)
        if (self.source_checkout_at is None) != (self.source_reservation_version is None):
            raise ValueError("reservation checkout/version provenance must be paired")


def reconstruct_schedule_revisions(
    cleaning_id: UUID, windows: Sequence[ScheduleWindow]
) -> tuple[Mapping[str, Any], ...]:
    """Keep exact needed windows only and assign local contiguous revisions."""
    unique: dict[str, ScheduleWindow] = {}
    for window in windows:
        facts = {
            "start": normalize_datetime(window.service_window_start_at),
            "end": normalize_datetime(window.service_deadline_at),
            "work": window.required_work_minutes,
            "checkout": window.source_checkout_at,
            "reservation_version": window.source_reservation_version,
        }
        unique.setdefault(semantic_hash(facts), window)
    ordered = sorted(
        unique.items(),
        key=lambda pair: (
            normalize_datetime(pair[1].service_window_start_at),
            normalize_datetime(pair[1].service_deadline_at),
            pair[0],
        ),
    )
    result: list[Mapping[str, Any]] = []
    for revision_no, (fingerprint, window) in enumerate(ordered, start=1):
        result.append(
            {
                "schedule_revision_id": derived_identity(
                    "schedule-revision", cleaning_id, fingerprint
                ),
                "cleaning_id": cleaning_id,
                "revision_no": revision_no,
                "service_window_start_at": normalize_datetime(window.service_window_start_at),
                "service_deadline_at": normalize_datetime(window.service_deadline_at),
                "required_work_minutes": window.required_work_minutes,
                "source_checkout_at": (
                    None
                    if window.source_checkout_at is None
                    else normalize_datetime(window.source_checkout_at)
                ),
                "source_reservation_version": window.source_reservation_version,
                "change_reason_code": window.change_reason_code,
                "source_command_id": None,
                "needed_by": tuple(sorted(window.needed_by)),
            }
        )
    return tuple(result)


def reservation_next_version(
    current_version: int | None,
    current_semantics: Mapping[str, Any] | None,
    observed_semantics: Mapping[str, Any],
) -> tuple[int, bool]:
    if current_version is None:
        return 1, True
    if current_version < 1 or current_semantics is None:
        raise ValueError("existing reservation version requires existing semantics")
    changed = canonical_value(current_semantics) != canonical_value(observed_semantics)
    return current_version + 1 if changed else current_version, changed


def minimal_exact_assignment_ancestry(action: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    """Normalize one persisted actual assignment action to one campaign/candidate.

    No candidate other than the one actually offered by the retained action can
    enter this result.
    """
    mandatory = (
        "assignment_page_id",
        "cleaning_id",
        "schedule_revision_id",
        "cleaner_party_id",
        "offered_at",
        "accepted_at",
        "acceptance_cutoff_at",
        "scheduled_start_at",
        "scheduled_end_at",
        "base_fee_krw",
        "urgent_premium_krw",
    )
    missing = [field for field in mandatory if action.get(field) is None]
    if missing or action.get("persisted_actual_action") is not True:
        raise ReviewRequired(
            "mandatory active assignment ancestry lacks exact persisted action evidence"
        )
    assignment_id = assignment_identity(action["assignment_page_id"])
    campaign_id = derived_identity("campaign", assignment_id)
    candidate_id = derived_identity("candidate", assignment_id, action["cleaner_party_id"])
    base_fee = normalize_integer(action["base_fee_krw"], minimum=0)
    premium = normalize_integer(action["urgent_premium_krw"], minimum=0)
    start = normalize_datetime(action["scheduled_start_at"])
    end = normalize_datetime(action["scheduled_end_at"])
    duration_seconds = (end - start).total_seconds()
    minutes = int(duration_seconds // 60)
    if minutes <= 0 or duration_seconds % 60:
        raise ReviewRequired("assignment duration is not a positive exact window")
    urgency = normalize_status(action.get("replacement_urgency", "NORMAL"))
    if premium and not action.get("urgent_premium_policy_version"):
        raise ReviewRequired("premium economics lack exact policy provenance")
    campaign = {
        "campaign_id": campaign_id,
        "cleaning_id": action["cleaning_id"],
        "schedule_revision_id": action["schedule_revision_id"],
        # Stage B retains one exact ancestry only; numbering is local to the
        # retained graph and upstream/caller sequence values are not authority.
        "campaign_no": 1,
        "campaign_status": "CLOSED",
        "open_tier_floor": 1,
        "max_tier": 1,
        "tier_expand_after_minutes": None,
        "acceptance_cutoff_at": normalize_datetime(action["acceptance_cutoff_at"]),
        "base_fee_krw": base_fee,
        "replacement_urgency": urgency,
        "urgent_premium_krw": premium,
        "total_agreed_fee_krw": base_fee + premium,
        "urgent_premium_policy_version": action.get("urgent_premium_policy_version"),
        "opened_at": normalize_datetime(action["offered_at"]),
        "closed_at": normalize_datetime(action["accepted_at"]),
        "closed_reason_code": "ACCEPTED",
    }
    candidate = {
        "offer_candidate_id": candidate_id,
        "campaign_id": campaign_id,
        "cleaner_party_id": action["cleaner_party_id"],
        "tier_no": 1,
        "candidate_status": "ACCEPTED",
        "proposal_version": 1,
        "proposed_start_at": start,
        "proposed_end_at": end,
        "proposed_buffer_before_minutes": normalize_integer(
            action.get("travel_buffer_before_minutes", 0), minimum=0
        ),
        "proposed_buffer_after_minutes": normalize_integer(
            action.get("travel_buffer_after_minutes", 0), minimum=0
        ),
        "buffer_basis": normalize_status(action.get("buffer_basis", "NOT_APPLIED")),
        "buffer_policy_ref": action.get("buffer_policy_ref"),
        "evaluated_at": normalize_datetime(action["offered_at"]),
        "declined_at": None,
        "accepted_at": normalize_datetime(action["accepted_at"]),
    }
    assignment_status = normalize_status(action.get("assignment_status", "HARD_BOOKED"))
    ended_at = action.get("ended_at")
    end_reason = action.get("end_reason_code")
    if assignment_status == "HARD_BOOKED":
        if ended_at is not None or end_reason is not None:
            raise ReviewRequired("active assignment has terminal fields")
    elif assignment_status in {"RELEASED", "COMPLETED", "CANCELLED"}:
        if ended_at is None or not end_reason:
            raise ReviewRequired("terminal assignment lacks exact end evidence")
        ended_at = normalize_datetime(ended_at)
    else:
        raise ReviewRequired("unsupported assignment status")
    if candidate["buffer_basis"] == "NOT_APPLIED" and (
        candidate["proposed_buffer_before_minutes"]
        or candidate["proposed_buffer_after_minutes"]
        or candidate["buffer_policy_ref"] is not None
    ):
        raise ReviewRequired("NOT_APPLIED buffer provenance conflicts with retained values")
    assignment = {
        "assignment_id": assignment_id,
        "cleaning_id": action["cleaning_id"],
        "assignment_no": 1,
        "schedule_revision_id": action["schedule_revision_id"],
        "campaign_id": campaign_id,
        "offer_candidate_id": candidate_id,
        "accepted_proposal_version": 1,
        "cleaner_party_id": action["cleaner_party_id"],
        "assignment_source": "OFFER_ACCEPTED",
        "assignment_status": assignment_status,
        "booked_at": normalize_datetime(action["accepted_at"]),
        "scheduled_start_at": start,
        "scheduled_end_at": end,
        "work_minutes_snapshot": minutes,
        "travel_buffer_before_minutes": candidate["proposed_buffer_before_minutes"],
        "travel_buffer_after_minutes": candidate["proposed_buffer_after_minutes"],
        "buffer_basis": candidate["buffer_basis"],
        "buffer_policy_ref": candidate["buffer_policy_ref"],
        "base_fee_krw": base_fee,
        "replacement_urgency": urgency,
        "urgent_premium_krw": premium,
        "total_agreed_fee_krw": base_fee + premium,
        "urgent_premium_policy_version": action.get("urgent_premium_policy_version"),
        "ended_at": ended_at,
        "end_reason_code": end_reason,
    }
    return {"campaign": campaign, "candidate": candidate, "assignment": assignment}


def minimal_exact_assignment_ancestry_batch(
    actions: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Mapping[str, Any]], ...]:
    """Reconstruct retained exact ancestry and derive Cleaner-local sequences in batch."""
    reconstructed: list[dict[str, dict[str, Any]]] = []
    ordering: list[tuple[UUID, datetime, datetime, str, int]] = []
    seen_durable_identities: set[str] = set()
    for index, action in enumerate(actions):
        ancestry = {
            key: dict(value)
            for key, value in minimal_exact_assignment_ancestry(action).items()
        }
        try:
            cleaning_id = UUID(str(action["cleaning_id"]))
            durable_identity = normalize_page_uuid(str(action["assignment_page_id"]))
            offered_at = normalize_datetime(action["offered_at"])
            accepted_at = normalize_datetime(action["accepted_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise ReviewRequired(
                "retained assignment ancestry lacks exact local-sequence ordering evidence"
            ) from error
        if durable_identity in seen_durable_identities:
            raise ReviewRequired("duplicate retained assignment durable source identity")
        seen_durable_identities.add(durable_identity)
        reconstructed.append(ancestry)
        ordering.append((cleaning_id, offered_at, accepted_at, durable_identity, index))

    by_cleaning: dict[UUID, list[tuple[UUID, datetime, datetime, str, int]]] = {}
    for item in ordering:
        by_cleaning.setdefault(item[0], []).append(item)
    for group in by_cleaning.values():
        for sequence, item in enumerate(sorted(group, key=lambda value: (value[1], value[3])), start=1):
            reconstructed[item[4]]["campaign"]["campaign_no"] = sequence
        for sequence, item in enumerate(sorted(group, key=lambda value: (value[2], value[3])), start=1):
            reconstructed[item[4]]["assignment"]["assignment_no"] = sequence
    return tuple(reconstructed)


def reconstruct_runtime_telegram_projection(
    runtime_cleaners: Sequence[Mapping[str, Any]],
    notion_parties: Sequence[Mapping[str, Any]],
    roster_rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Bind cleaners.json only after exact Notion Party and roster matching.

    The runtime ``identity_id``, nickname, display name, and list position are
    intentionally never consulted.
    """
    parties_by_page: dict[str, Mapping[str, Any]] = {}
    for party in notion_parties:
        page = str(party.get("notion_page_id", "")).strip()
        if not page:
            raise ReviewRequired("Notion Party row lacks exact page identity")
        if page in parties_by_page:
            raise AmbiguousIdentityError("duplicate exact Party page identity")
        parties_by_page[page] = party
    roster_pages = [str(row.get("party_notion_page_id", "")).strip() for row in roster_rows]
    if len(roster_pages) != len(set(roster_pages)):
        raise AmbiguousIdentityError("Party has ambiguous roster membership rows")
    roster_set = set(roster_pages)
    projected: list[Mapping[str, Any]] = []
    seen_users: set[str] = set()
    for cleaner in runtime_cleaners:
        page = str(cleaner.get("party_notion_page_id", "")).strip()
        user_id = str(cleaner.get("telegram_user_id", "")).strip()
        if not page or not user_id:
            raise ReviewRequired("runtime Telegram row lacks exact Party/user identity")
        party = parties_by_page.get(page)
        if party is None or page not in roster_set:
            raise ReviewRequired("runtime Telegram row lacks exact Party/roster match")
        if user_id in seen_users:
            raise AmbiguousIdentityError("Telegram user appears more than once")
        seen_users.add(user_id)
        party_id = party.get("party_id") or notion_identity("party", page)
        projected.append(
            {
                "external_identity_id": telegram_identity(user_id),
                "party_id": party_id,
                "provider": "TELEGRAM",
                "provider_user_id": user_id,
                "provider_chat_id": (
                    None
                    if cleaner.get("telegram_chat_id") is None
                    else str(cleaner["telegram_chat_id"])
                ),
                "bound_at": normalize_datetime(cleaner["bound_at"]),
                "revoked_at": (
                    None
                    if cleaner.get("revoked_at") is None
                    else normalize_datetime(cleaner["revoked_at"])
                ),
                "source_ref": cleaner.get("source_ref"),
            }
        )
    return tuple(projected)


def require_unavailability_evidence(evidence: Mapping[str, Any]) -> Mapping[str, Any]:
    if not evidence.get("assignment_end_evidence") or not (
        evidence.get("unavailable_action_evidence")
        or evidence.get("performance_evidence")
    ):
        raise ReviewRequired("unavailability lacks assignment-end plus exact action evidence")
    return canonical_value(evidence)


def require_reassignment_evidence(evidence: Mapping[str, Any]) -> Mapping[str, Any]:
    if not evidence.get("durable_request_evidence") or not evidence.get(
        "committed_decision_evidence"
    ):
        raise ReviewRequired("reassignment lacks durable request plus committed decision")
    return canonical_value(evidence)


def _derive_retained_local_sequences(
    records: Sequence[CanonicalRecord],
) -> tuple[CanonicalRecord, ...]:
    """Assign contiguous local ancestry numbers from the complete retained record batch."""
    result = list(records)
    specifications = {
        "cleaning_offer_campaign": ("campaign_no", "opened_at"),
        "cleaning_assignment": ("assignment_no", "booked_at"),
    }
    for entity_type, (sequence_field, timestamp_field) in specifications.items():
        grouped: dict[UUID, list[tuple[datetime, str, int]]] = {}
        for index, record in enumerate(result):
            if record.entity_type != entity_type:
                continue
            try:
                cleaning_id = UUID(str(record.payload["cleaning_id"]))
                event_at = normalize_datetime(record.payload[timestamp_field])
            except (KeyError, TypeError, ValueError) as error:
                raise ReviewRequired(
                    f"{entity_type} lacks exact local-sequence ordering evidence"
                ) from error
            grouped.setdefault(cleaning_id, []).append((event_at, record.source_key, index))
        for group in grouped.values():
            for sequence, (_, _, index) in enumerate(
                sorted(group, key=lambda item: (item[0], item[1])), start=1
            ):
                payload = dict(result[index].payload)
                payload[sequence_field] = sequence
                result[index] = replace(result[index], payload=payload)
    return tuple(result)


class PopulationBuilder:
    """Convert already-authoritative semantic observations into schema records."""

    _META_FIELDS = {"entity_type", "durable_identity_seed", "dependencies"}

    def __init__(self) -> None:
        # V2.2.1 intentionally uses the Party UUID as cleaner_profile's PK/FK.
        # Identity collision detection therefore has to be scoped by aggregate
        # type: UUID equality across different tables is valid, while conflicting
        # immutable facts for the same entity type remain fail-closed.
        self.identities: dict[str, IdentityRegistry] = {}

    def _aggregate_id(self, entity_type: str, payload: Mapping[str, Any]) -> UUID:
        primary_key = ENTITY_PRIMARY_KEYS[entity_type]
        supplied = payload.get(primary_key)
        if supplied is not None:
            return supplied if isinstance(supplied, UUID) else UUID(str(supplied))
        seed = payload.get("durable_identity_seed")
        if not seed:
            raise ReviewRequired(f"{entity_type} lacks an exact durable identity seed")
        return deterministic_id(str(seed))

    def build(self, manifest: SnapshotManifest) -> Population:
        records: list[CanonicalRecord] = []
        obligations = []
        collisions: list[str] = []
        authority_findings: list[str] = []
        for source_key in manifest.source_row_identities:
            observation = manifest.observations[source_key]
            raw = dict(observation.semantic_payload)
            entity_type = str(raw.get("entity_type", ""))
            if entity_type not in ENTITY_PRIMARY_KEYS:
                obligations.append(
                    obligation(
                        source_key,
                        STATE_MUTATING,
                        f"unsupported or missing entity_type {entity_type!r}",
                    )
                )
                continue
            if entity_type in _AUTHORITY_GUARDED_ENTITIES:
                permitted = SOURCE_TYPE_ENTITY_AUTHORITY.get(observation.source_type, frozenset())
                if entity_type not in permitted:
                    authority_findings.append(
                        f"{source_key}: source_type={observation.source_type!r} "
                        f"is not authoritative for entity_type={entity_type!r}"
                    )
                    continue
            try:
                aggregate_id = self._aggregate_id(entity_type, raw)
                primary_key = ENTITY_PRIMARY_KEYS[entity_type]
                payload = {
                    key: value for key, value in raw.items() if key not in self._META_FIELDS
                }
                payload[primary_key] = aggregate_id
                if entity_type == "reservation":
                    payload.pop("source_version", None)
                elif entity_type == "cleaning_offer_campaign":
                    payload["campaign_no"] = 0
                elif entity_type == "cleaning_assignment":
                    payload["assignment_no"] = 0
                immutable_fields = IMMUTABLE_FIELDS[entity_type]
                self.identities.setdefault(entity_type, IdentityRegistry()).register(
                    source_key,
                    aggregate_id,
                    {field: payload.get(field) for field in immutable_fields},
                )
                dependencies: list[UUID] = []
                explicit = raw.get("dependencies", ())
                for value in explicit:
                    dependencies.append(value if isinstance(value, UUID) else UUID(str(value)))
                for field in DEPENDENCY_FIELDS:
                    value = payload.get(field)
                    if value is None or field == primary_key:
                        continue
                    try:
                        candidate = value if isinstance(value, UUID) else UUID(str(value))
                    except (TypeError, ValueError):
                        continue
                    if candidate != aggregate_id and candidate not in dependencies:
                        dependencies.append(candidate)
                records.append(
                    CanonicalRecord(
                        entity_type=entity_type,
                        aggregate_id=aggregate_id,
                        source_key=source_key,
                        semantic_hash=manifest.source_semantic_hashes[source_key],
                        payload=payload,
                        immutable_fields=immutable_fields,
                        dependencies=tuple(dependencies),
                        provenance={
                            "source_ref": observation.source_ref,
                            "diagnostic_last_edited_time": observation.last_edited_time,
                            "source_authority": observation.source_type,
                        },
                    )
                )
            except Exception as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                collisions.append(f"{source_key}: {error}")
        sequenced_records = _derive_retained_local_sequences(records)
        return Population(
            sequenced_records,
            tuple(obligations),
            tuple(collisions),
            tuple(authority_findings),
        )


__all__ = [
    "DEPENDENCY_FIELDS",
    "DOMAIN_SOURCE_AUTHORITY",
    "ENTITY_PRIMARY_KEYS",
    "IMMUTABLE_FIELDS",
    "PopulationBuilder",
    "SOURCE_TYPE_ENTITY_AUTHORITY",
    "ScheduleWindow",
    "minimal_exact_assignment_ancestry",
    "minimal_exact_assignment_ancestry_batch",
    "reconstruct_schedule_revisions",
    "reconstruct_runtime_telegram_projection",
    "require_reassignment_evidence",
    "require_unavailability_evidence",
    "reservation_next_version",
]

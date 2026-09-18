from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import UUID

from propertyai_core.notion_resources import cleaning_source_id, reservation_source_id

from ..identity import (
    ORGANIZATION_SEED,
    assignment_identity,
    deterministic_id,
    notion_identity,
)
from ..models import ReviewRequired, SnapshotIntegrityError, SnapshotManifest, SourceObservation
from ..normalize import normalize_datetime, normalize_page_uuid, semantic_hash
from ..population import (
    ScheduleWindow,
    minimal_exact_assignment_ancestry,
    reconstruct_runtime_telegram_projection,
    reconstruct_schedule_revisions,
    require_reassignment_evidence,
    require_unavailability_evidence,
)
from ..snapshot import FreshSourceValidator
from .files import FileSnapshotSource, read_stable_file
from .notion import NotionPage, NotionSnapshotSource


S1_ROSTER_SOURCE_ID = "b66af4a1-97e3-419a-8fd2-1d65c63e3d6d"
S2_RENTAL_UNIT_SOURCE_ID = "7fdd641c-9847-46ab-b91f-77e3a2063ec3"
S5_ASSIGNMENT_SOURCE_ID = "3b43edac-140d-4699-8db1-00020d10652c"
S5_ASSIGNMENT_DATABASE_ID = "e9278ba6-ad32-440c-b1c3-75f4abadf8cb"
S7_PERFORMANCE_SOURCE_ID = "53206406-b7b7-4444-b5a8-b549b9af20f5"

S1_SOURCE_TYPE = "NOTION_CLEANER_PROPERTY_ACCESS"
S2_SOURCE_TYPE = "BOUNDED_RENTAL_UNIT_REFERENCE"
S3_SOURCE_TYPE = "CANONICAL_RESERVATION_PROJECTION"
S4_SOURCE_TYPE = "CLEANING_PROJECTION"
S5_SOURCE_TYPE = "ASSIGNMENT_HISTORY_19_PLUS_EXACT_EXECUTION_EVIDENCE"
S6_SOURCE_TYPE = "CLEANERS_JSON_AFTER_EXACT_PARTY_ROSTER_MATCH"
S7_SOURCE_TYPE = "ASSIGNMENT_END_PLUS_UNAVAILABLE_ACTION_OR_PERFORMANCE_EVIDENCE"
S8_SOURCE_TYPE = "DURABLE_REQUEST_PLUS_COMMITTED_DECISION_EVIDENCE"

_PRODUCTION_ROOT = Path("/Users/kate/PropertyAI/openclaw-workspace")
_NOTION_VERSION = "2026-03-11"
_ORGANIZATION_ID = deterministic_id(ORGANIZATION_SEED)


class ProductionSourceConfigurationError(SnapshotIntegrityError):
    pass


@dataclass(frozen=True)
class ProductionStageBSourceConfig:
    data_environment: str
    postgres_mode: str
    performance_source_id: str
    root: Path = _PRODUCTION_ROOT
    notion_token_path: Path | None = None
    cleaners_json_path: Path | None = None
    request_dir: Path | None = None
    assignment_provenance_dir: Path | None = None

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "ProductionStageBSourceConfig":
        env = os.environ if environment is None else environment
        root = Path(env.get("PROPERTYAI_STAGE_B_PRODUCTION_ROOT", str(_PRODUCTION_ROOT))).expanduser()
        performance = str(env.get("CLEANER_PERFORMANCE_EVENT_SOURCE_ID", "")).strip()
        config = cls(
            data_environment=str(env.get("PROPERTYAI_DATA_ENVIRONMENT", "")).strip(),
            postgres_mode=str(env.get("PROPERTYAI_CLEANER_POSTGRES_MODE", "")).strip(),
            performance_source_id=performance,
            root=root,
            notion_token_path=Path(
                env.get("PROPERTYAI_NOTION_TOKEN_PATH", str(root / "secrets/notion/token"))
            ).expanduser(),
            cleaners_json_path=Path(
                env.get(
                    "PROPERTYAI_CLEANER_IDENTITIES_PATH",
                    str(root / "secrets/telegram/cleaners.json"),
                )
            ).expanduser(),
            request_dir=Path(
                env.get(
                    "PROPERTYAI_CLEANER_REQUEST_DIR",
                    str(root / "telegram_approval/runtime/cleaner/requests"),
                )
            ).expanduser(),
            assignment_provenance_dir=Path(
                env.get(
                    "PROPERTYAI_ASSIGNMENT_PROVENANCE_DIR",
                    str(
                        root
                        / "telegram_approval/runtime/cleaner/requests/.assignment-execution-provenance"
                    ),
                )
            ).expanduser(),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.data_environment != "PRODUCTION":
            raise ProductionSourceConfigurationError(
                "Production Stage B source factory requires PROPERTYAI_DATA_ENVIRONMENT=PRODUCTION"
            )
        if self.postgres_mode != "PRE_CUTOVER_MIGRATION":
            raise ProductionSourceConfigurationError(
                "Production Stage B source factory requires PRE_CUTOVER_MIGRATION mode"
            )
        performance_source_id = _validate_uuid(
            "CLEANER_PERFORMANCE_EVENT_SOURCE_ID", self.performance_source_id
        )
        if performance_source_id != S7_PERFORMANCE_SOURCE_ID:
            raise ProductionSourceConfigurationError(
                "CLEANER_PERFORMANCE_EVENT_SOURCE_ID does not match frozen Production authority"
            )
        for label, path in (
            ("Notion credential", self.notion_token_path),
            ("Telegram durable identity", self.cleaners_json_path),
        ):
            if path is None or not path.is_file():
                raise ProductionSourceConfigurationError(f"required {label} source is missing")
        for label, path in (
            ("Cleaner runtime evidence directory", self.request_dir),
            ("Assignment execution provenance directory", self.assignment_provenance_dir),
        ):
            if path is None or not path.is_dir():
                raise ProductionSourceConfigurationError(f"required {label} is missing")


class ReadOnlyNotionClient:
    """Narrow Production reader. It exposes no page/database mutation operation."""

    def __init__(
        self,
        *,
        token_path: Path,
        transport: Callable[[str, str, Mapping[str, Any] | None], Mapping[str, Any]] | None = None,
    ) -> None:
        self.token_path = Path(token_path)
        self._transport = transport

    def _request(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        if method not in {"GET", "POST"}:
            raise ProductionSourceConfigurationError("read-only Notion client rejected mutation method")
        if method == "POST" and not path.endswith("/query"):
            raise ProductionSourceConfigurationError("read-only Notion client rejected non-query POST")
        if self._transport is not None:
            result = self._transport(method, path, body)
            if not isinstance(result, Mapping):
                raise SnapshotIntegrityError("Notion read returned a non-object response")
            return result
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ProductionSourceConfigurationError("approved Notion credential is unreadable") from error
        if not token:
            raise ProductionSourceConfigurationError("approved Notion credential is empty")
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            "https://api.notion.com" + path,
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": _NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise SnapshotIntegrityError("authoritative Notion source is unreadable") from error
        if not isinstance(parsed, Mapping):
            raise SnapshotIntegrityError("authoritative Notion source returned a non-object response")
        return parsed

    def validate_data_source(self, source_id: str) -> None:
        exact = _validate_uuid("Notion source id", source_id)
        value = self._request("GET", f"/v1/data_sources/{exact}")
        actual = _validate_uuid("Notion response source id", str(value.get("id", "")))
        if actual != exact:
            raise SnapshotIntegrityError("Notion data-source identity mismatch")

    def query_data_source(
        self,
        source_id: str,
        cursor: str | None,
        *,
        filter_payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        exact = _validate_uuid("Notion source id", source_id)
        body: dict[str, Any] = {"page_size": 100}
        if cursor is not None:
            body["start_cursor"] = cursor
        if filter_payload is not None:
            body["filter"] = dict(filter_payload)
        return self._request("POST", f"/v1/data_sources/{exact}/query", body)

    def get_page(self, page_id: str, *, expected_source_id: str | None = None) -> Mapping[str, Any]:
        exact_page = normalize_page_uuid(page_id)
        value = self._request("GET", f"/v1/pages/{exact_page}")
        actual_page = normalize_page_uuid(str(value.get("id", "")))
        if actual_page != exact_page:
            raise SnapshotIntegrityError("Notion page identity mismatch")
        if expected_source_id is not None:
            expected = _validate_uuid("expected Notion source id", expected_source_id)
            parent = value.get("parent")
            if not isinstance(parent, Mapping):
                raise SnapshotIntegrityError("Notion page lacks data-source parent evidence")
            actual_parent = str(parent.get("data_source_id") or parent.get("database_id") or "")
            if not actual_parent:
                raise SnapshotIntegrityError("Notion page parent source identity is absent")
            if _validate_uuid("Notion page parent source id", actual_parent) != expected:
                raise SnapshotIntegrityError("Notion page belongs to an unexpected source")
        return value


class _MappedNotionSource:
    def __init__(
        self,
        *,
        client: ReadOnlyNotionClient,
        source_id: str,
        source_type: str,
        mapper: Callable[[Mapping[str, Any]], Sequence[tuple[str, Mapping[str, Any]]]],
        filter_payload: Mapping[str, Any] | None = None,
        global_payloads: Sequence[tuple[str, Mapping[str, Any]]] = (),
    ) -> None:
        self.client = client
        self.source_id = _validate_uuid("Notion source id", source_id)
        self.source_type = source_type
        self._mapper = mapper
        self._filter = filter_payload
        self._global = tuple(global_payloads)
        self.runtime_reference = f"notion-data-source:{self.source_id}"
        self._raw = NotionSnapshotSource(
            f"RAW_{source_type}",
            lambda cursor: self.client.query_data_source(
                self.source_id, cursor, filter_payload=self._filter
            ),
            lambda page_id: self.client.get_page(page_id, expected_source_id=self.source_id),
            semantic_normalizer=lambda row: dict(row),
            runtime_reference=self.runtime_reference,
        )

    def _expand(self, raw: Sequence[SourceObservation]) -> tuple[SourceObservation, ...]:
        result: dict[str, SourceObservation] = {}
        for durable, payload in self._global:
            observation = SourceObservation(self.source_type, durable, payload, source_ref=self.runtime_reference)
            result[observation.row_key] = observation
        for source in raw:
            page = dict(source.semantic_payload)
            for suffix, payload in self._mapper(page):
                durable = source.durable_identity if not suffix else f"{source.durable_identity}#{suffix}"
                observation = SourceObservation(
                    self.source_type,
                    durable,
                    payload,
                    source_ref=source.source_ref,
                    last_edited_time=source.last_edited_time,
                )
                if observation.row_key in result:
                    raise SnapshotIntegrityError("Production Notion mapper emitted duplicate identity")
                result[observation.row_key] = observation
        return tuple(result[key] for key in sorted(result))

    def scan(self) -> tuple[SourceObservation, ...]:
        return self._expand(self._raw.scan())

    def point_read(self, durable_identity: str) -> SourceObservation | None:
        if any(durable_identity == durable for durable, _ in self._global):
            for item in self._expand(()) :
                if item.durable_identity == durable_identity:
                    return item
            return None
        page_id = durable_identity.split("#", 1)[0]
        raw = self._raw.point_read(page_id)
        if raw is None:
            return None
        for item in self._expand((raw,)):
            if item.durable_identity == durable_identity:
                return item
        return None

    @property
    def file_content_hashes(self) -> Mapping[str, str]:
        return {}


class _JsonDirectoryEvidence:
    def __init__(self, root: Path, *, label: str, include_hidden: bool = True) -> None:
        self.root = Path(root)
        self.label = label
        self.include_hidden = include_hidden
        self._last_objects: dict[str, Any] = {}
        self._last_hashes: dict[str, str] = {}

    def scan(self) -> Mapping[str, Any]:
        if not self.root.is_dir():
            raise SnapshotIntegrityError(f"required {self.label} directory is missing")
        names_before = tuple(
            sorted(
                path.name
                for path in self.root.iterdir()
                if path.is_file()
                and path.suffix == ".json"
                and (self.include_hidden or not path.name.startswith("."))
            )
        )
        objects: dict[str, Any] = {}
        hashes: dict[str, str] = {}
        for name in names_before:
            stable = read_stable_file(self.root / name)
            try:
                decoded = json.loads(stable.content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SnapshotIntegrityError(f"invalid JSON in {self.label} evidence") from error
            objects[name] = decoded
            hashes[f"file:{self.label}:{name}"] = stable.content_hash
            hashes[f"path:{self.label}:{name}"] = stable.path_identity_hash
        names_after = tuple(
            sorted(
                path.name
                for path in self.root.iterdir()
                if path.is_file()
                and path.suffix == ".json"
                and (self.include_hidden or not path.name.startswith("."))
            )
        )
        if names_before != names_after:
            raise SnapshotIntegrityError(f"{self.label} directory membership changed during scan")
        hashes[f"directory:{self.label}"] = semantic_hash(
            {"root": str(self.root.resolve()), "members": names_after}
        )
        self._last_objects = objects
        self._last_hashes = hashes
        return objects

    @property
    def objects(self) -> Mapping[str, Any]:
        return dict(self._last_objects)

    def require_assignment_pair(self, action_id: str) -> None:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", action_id) is None:
            raise ReviewRequired("Assignment Action ID is malformed")
        intent = self._last_objects.get(f"{action_id}.intent.json")
        success = self._last_objects.get(f"{action_id}.success.json")
        if not isinstance(intent, Mapping) or not isinstance(success, Mapping):
            raise ReviewRequired("accepted binding Assignment lacks exact intent/success provenance")
        operation_identity = intent.get("operation_identity")
        if not isinstance(operation_identity, str) or not operation_identity:
            raise ReviewRequired("Assignment execution intent identity is missing")
        if success.get("pre_effect_operation_identity") != operation_identity:
            raise ReviewRequired("Assignment execution intent/success identity mismatch")
        receipt = success.get("assignment_execution_receipt")
        if not isinstance(receipt, Mapping) or receipt.get("action_id") != action_id:
            raise ReviewRequired("Assignment execution receipt Action ID mismatch")
        if receipt.get("action_type") != "CLEANING_ASSIGNMENT":
            raise ReviewRequired("Assignment execution receipt type mismatch")
        result = receipt.get("execution_result")
        if not isinstance(result, Mapping) or result.get("cleaning_assignment") != "ACCEPTED":
            raise ReviewRequired("Assignment execution receipt is not factual ACCEPTED evidence")

    @property
    def file_hashes(self) -> Mapping[str, str]:
        return dict(self._last_hashes)


class _EvidenceAugmentedSource:
    def __init__(self, base: Any, *evidence: _JsonDirectoryEvidence) -> None:
        self.base = base
        self.evidence = tuple(evidence)
        self.source_type = base.source_type
        refs = "|".join(f"files:{item.root.resolve()}" for item in self.evidence)
        self.runtime_reference = f"{base.runtime_reference}|{refs}"
        self._hashes: dict[str, str] = {}

    def _scan_evidence(self) -> None:
        hashes: dict[str, str] = {}
        for evidence in self.evidence:
            evidence.scan()
            hashes.update(evidence.file_hashes)
        self._hashes = hashes

    def scan(self) -> tuple[SourceObservation, ...]:
        self._scan_evidence()
        return tuple(self.base.scan())

    def point_read(self, durable_identity: str) -> SourceObservation | None:
        self._scan_evidence()
        return self.base.point_read(durable_identity)

    @property
    def file_content_hashes(self) -> Mapping[str, str]:
        return dict(self._hashes)


class _TelegramIdentitySource:
    def __init__(
        self,
        *,
        cleaners_path: Path,
        roster_raw: NotionSnapshotSource,
    ) -> None:
        self.source_type = S6_SOURCE_TYPE
        self.runtime_reference = f"file:{Path(cleaners_path).resolve()}|{roster_raw.runtime_reference}"
        self._cleaners = FileSnapshotSource(
            "RAW_CLEANERS_JSON",
            {"cleaners": Path(cleaners_path)},
            semantic_normalizer=lambda _identity, decoded: {"document": decoded},
            runtime_reference=f"file:{Path(cleaners_path).resolve()}",
        )
        self._roster_raw = roster_raw
        self._hashes: dict[str, str] = {}

    def _scan_rows(self) -> tuple[SourceObservation, ...]:
        files = self._cleaners.scan()
        if len(files) != 1:
            raise SnapshotIntegrityError("Telegram durable identity file coverage is incomplete")
        document = files[0].semantic_payload.get("document")
        runtime_cleaners = _runtime_cleaner_records(document)
        roster_pages = self._roster_raw.scan()
        roster_rows: list[Mapping[str, Any]] = []
        notion_parties: list[Mapping[str, Any]] = []
        seen_parties: set[str] = set()
        for row in roster_pages:
            page = dict(row.semantic_payload)
            if _select(page, "데이터 환경") != "PRODUCTION":
                continue
            relations = _relations(page, "인력")
            if len(relations) != 1:
                raise ReviewRequired("Cleaner Property Access row lacks exact Party relation")
            party_page = normalize_page_uuid(relations[0])
            roster_rows.append({"party_notion_page_id": party_page})
            if party_page not in seen_parties:
                notion_parties.append(
                    {"notion_page_id": party_page, "party_id": notion_identity("party", party_page)}
                )
                seen_parties.add(party_page)
        projected = reconstruct_runtime_telegram_projection(runtime_cleaners, notion_parties, roster_rows)
        result = []
        for item in projected:
            payload = {"entity_type": "external_identity", **dict(item)}
            result.append(
                SourceObservation(
                    self.source_type,
                    str(item["external_identity_id"]),
                    payload,
                    source_ref=str(self._cleaners.paths["cleaners"]),
                )
            )
        self._hashes = dict(self._cleaners.file_content_hashes)
        return tuple(sorted(result, key=lambda row: row.row_key))

    def scan(self) -> tuple[SourceObservation, ...]:
        return self._scan_rows()

    def point_read(self, durable_identity: str) -> SourceObservation | None:
        for row in self._scan_rows():
            if row.durable_identity == durable_identity:
                return row
        return None

    @property
    def file_content_hashes(self) -> Mapping[str, str]:
        return dict(self._hashes)


class _UnavailabilitySource:
    def __init__(
        self,
        *,
        assignment_raw: NotionSnapshotSource,
        performance_raw: NotionSnapshotSource,
        request_evidence: _JsonDirectoryEvidence,
        client: ReadOnlyNotionClient,
    ) -> None:
        self.source_type = S7_SOURCE_TYPE
        self.assignment_raw = assignment_raw
        self.performance_raw = performance_raw
        self.request_evidence = request_evidence
        self.client = client
        self.runtime_reference = (
            f"{assignment_raw.runtime_reference}|{performance_raw.runtime_reference}|"
            f"files:{request_evidence.root.resolve()}"
        )
        self._hashes: dict[str, str] = {}

    def _scan_rows(self) -> tuple[SourceObservation, ...]:
        runtime = self.request_evidence.scan()
        performance = self.performance_raw.scan()
        perf_by_assignment: dict[str, list[Mapping[str, Any]]] = {}
        for observation in performance:
            page = dict(observation.semantic_payload)
            if _select(page, "Data Environment") not in (None, "PRODUCTION") and _select(page, "데이터 환경") != "PRODUCTION":
                continue
            for assignment_page in _relations_any(page, ("Assignment", "배정 제안 이력")):
                perf_by_assignment.setdefault(normalize_page_uuid(assignment_page), []).append(page)
        rows: list[SourceObservation] = []
        for observation in self.assignment_raw.scan():
            page = dict(observation.semantic_payload)
            if _select(page, "데이터 환경") != "PRODUCTION":
                continue
            if _select(page, "Hold 상태") != "RELEASED" or _select(page, "Assignment End Reason") != "CLEANER_UNAVAILABLE":
                continue
            assignment_page = normalize_page_uuid(str(page["id"]))
            runtime_match = _find_runtime_unavailable_evidence(runtime.values(), assignment_page, page)
            perf_matches = perf_by_assignment.get(assignment_page, ())
            require_unavailability_evidence(
                {
                    "assignment_end_evidence": True,
                    "unavailable_action_evidence": runtime_match is not None,
                    "performance_evidence": bool(perf_matches),
                }
            )
            cleaning_page = _one_relation(page, "Cleaning")
            cleaner_page = _one_relation(page, "후보 인력")
            cleaning_page_data = self.client.get_page(cleaning_page, expected_source_id=cleaning_source_id())
            cleaning_payload, revision_payload = _cleaning_payloads(cleaning_page_data)
            classification = _first_nonblank(
                _select(candidate, "Performance Classification")
                for candidate in perf_matches
            ) or _runtime_value(runtime_match, "performance_classification", "availability_classification")
            if classification not in {"EARLY_UNAVAILABLE", "SAME_DAY_UNAVAILABLE"}:
                raise ReviewRequired("unavailability classification lacks exact durable evidence")
            urgency = _first_nonblank(
                _select(candidate, "Replacement Urgency") for candidate in perf_matches
            ) or _select(page, "Replacement Urgency")
            if urgency not in {"NORMAL", "URGENT"}:
                raise ReviewRequired("unavailability replacement urgency is unresolved")
            occurred = _date(page, "Assignment Ended At")
            if occurred is None:
                raise ReviewRequired("unavailability assignment-end timestamp is absent")
            payload = {
                "entity_type": "cleaner_unavailability_case",
                "durable_identity_seed": f"unavailability:notion19:{assignment_page}",
                "cleaning_id": cleaning_payload["cleaning_id"],
                "schedule_revision_id": revision_payload["schedule_revision_id"],
                "original_assignment_id": assignment_identity(assignment_page),
                "cleaner_party_id": notion_identity("party", cleaner_page),
                "case_status": "CONFIRMED",
                "availability_classification": classification,
                "replacement_urgency": urgency,
                "reason_code": _select(page, "Unavailable Reason"),
                "reason_text": None,
                "occurred_at": normalize_datetime(occurred),
            }
            rows.append(
                SourceObservation(
                    self.source_type,
                    assignment_page,
                    payload,
                    source_ref=observation.source_ref,
                    last_edited_time=observation.last_edited_time,
                )
            )
        self._hashes = dict(self.request_evidence.file_hashes)
        return tuple(sorted(rows, key=lambda row: row.row_key))

    def scan(self) -> tuple[SourceObservation, ...]:
        return self._scan_rows()

    def point_read(self, durable_identity: str) -> SourceObservation | None:
        for row in self._scan_rows():
            if row.durable_identity == durable_identity:
                return row
        return None

    @property
    def file_content_hashes(self) -> Mapping[str, str]:
        return dict(self._hashes)


class _ReassignmentSource:
    def __init__(
        self,
        *,
        request_evidence: _JsonDirectoryEvidence,
        client: ReadOnlyNotionClient,
    ) -> None:
        self.source_type = S8_SOURCE_TYPE
        self.request_evidence = request_evidence
        self.client = client
        self.runtime_reference = f"files:{request_evidence.root.resolve()}|notion:{S5_ASSIGNMENT_SOURCE_ID}"
        self._hashes: dict[str, str] = {}

    def _scan_rows(self) -> tuple[SourceObservation, ...]:
        documents = self.request_evidence.scan()
        records = [value for value in documents.values() if isinstance(value, Mapping)]
        requests = {
            str(value.get("action_id")): value
            for value in records
            if value.get("action_type") == "CLEANER_REASSIGNMENT_REQUEST" and value.get("action_id")
        }
        decisions = [
            value
            for value in records
            if value.get("action_type") == "CLEANER_REASSIGNMENT_DECISION"
            and value.get("decision_committed") is True
        ]
        rows: list[SourceObservation] = []
        for decision in sorted(decisions, key=lambda item: str(item.get("action_id", ""))):
            request_id = str(decision.get("request_action_id") or "")
            source = requests.get(request_id)
            if source is None:
                raise ReviewRequired("committed reassignment decision lacks durable source request")
            require_reassignment_evidence(
                {"durable_request_evidence": True, "committed_decision_evidence": True}
            )
            history_page = normalize_page_uuid(str(decision.get("history_page_id") or source.get("history_page_id") or ""))
            history = self.client.get_page(history_page, expected_source_id=S5_ASSIGNMENT_SOURCE_ID)
            expected_key = str(decision.get("request_key") or source.get("request_key") or "")
            if not expected_key or _rich_text(history, "Reassignment Request Key") != expected_key:
                raise ReviewRequired("reassignment durable request key does not match Assignment History")
            cleaning_page = normalize_page_uuid(str(decision.get("cleaning_page_id") or source.get("cleaning_page_id") or ""))
            cleaner_page = normalize_page_uuid(str(decision.get("cleaner_party_page_id") or source.get("cleaner_party_page_id") or ""))
            cleaning = self.client.get_page(cleaning_page, expected_source_id=cleaning_source_id())
            cleaning_payload, revision_payload = _cleaning_payloads(cleaning)
            unavailability_seed = f"unavailability:notion19:{history_page}"
            unavailability_id = deterministic_id(unavailability_seed)
            decision_code = str(decision.get("decision") or "").strip()
            status_map = {
                "REASSIGNED_ORIGINAL": "REASSIGNED_ORIGINAL",
                "CONTINUE_REPLACEMENT": "CONTINUE_REPLACEMENT",
            }
            if decision_code not in status_map:
                raise ReviewRequired("committed reassignment decision code is unsupported")
            requested_at = source.get("reassignment_requested_at") or source.get("created_at")
            decided_at = decision.get("decision_at") or decision.get("consumed_at")
            if requested_at is None or decided_at is None:
                raise ReviewRequired("reassignment request/decision timestamps are incomplete")
            payload = {
                "entity_type": "cleaner_reassignment_request",
                "durable_identity_seed": f"reassignment:notion19:{history_page}",
                "unavailability_id": unavailability_id,
                "cleaning_id": cleaning_payload["cleaning_id"],
                "cleaner_party_id": notion_identity("party", cleaner_page),
                "original_assignment_id": assignment_identity(history_page),
                "requested_schedule_revision_id": revision_payload["schedule_revision_id"],
                "request_no": 1,
                "request_status": status_map[decision_code],
                "requested_at": normalize_datetime(requested_at),
                "decided_at": normalize_datetime(decided_at),
                "decision_code": decision_code,
            }
            rows.append(
                SourceObservation(
                    self.source_type,
                    history_page,
                    payload,
                    source_ref=str(self.request_evidence.root),
                )
            )
        self._hashes = dict(self.request_evidence.file_hashes)
        return tuple(sorted(rows, key=lambda row: row.row_key))

    def scan(self) -> tuple[SourceObservation, ...]:
        return self._scan_rows()

    def point_read(self, durable_identity: str) -> SourceObservation | None:
        for row in self._scan_rows():
            if row.durable_identity == durable_identity:
                return row
        return None

    @property
    def file_content_hashes(self) -> Mapping[str, str]:
        return dict(self._hashes)


def _validate_uuid(label: str, value: str) -> str:
    try:
        return str(UUID(str(value).strip()))
    except (ValueError, TypeError, AttributeError) as error:
        raise ProductionSourceConfigurationError(f"invalid {label}") from error



def _property(page: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    properties = page.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    value = properties.get(name)
    return value if isinstance(value, Mapping) else {}


def _plain_text_items(items: Any) -> str | None:
    if not isinstance(items, list):
        return None
    text = "".join(
        str(item.get("plain_text", ""))
        for item in items
        if isinstance(item, Mapping)
    ).strip()
    return text or None


def _title(page: Mapping[str, Any], name: str) -> str | None:
    return _plain_text_items(_property(page, name).get("title"))


def _rich_text(page: Mapping[str, Any], name: str) -> str | None:
    return _plain_text_items(_property(page, name).get("rich_text"))


def _text(page: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        value = _title(page, name) or _rich_text(page, name)
        if value:
            return value
    return None


def _select(page: Mapping[str, Any], name: str) -> str | None:
    value = _property(page, name).get("select")
    return str(value.get("name")) if isinstance(value, Mapping) and value.get("name") else None


def _number(page: Mapping[str, Any], name: str) -> int | float | None:
    value = _property(page, name).get("number")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _checkbox(page: Mapping[str, Any], name: str) -> bool | None:
    value = _property(page, name).get("checkbox")
    return value if isinstance(value, bool) else None


def _date(page: Mapping[str, Any], name: str) -> str | None:
    value = _property(page, name).get("date")
    return str(value.get("start")) if isinstance(value, Mapping) and value.get("start") else None


def _relations(page: Mapping[str, Any], name: str) -> list[str]:
    relation = _property(page, name).get("relation")
    if not isinstance(relation, list):
        return []
    result = []
    for item in relation:
        if isinstance(item, Mapping) and item.get("id"):
            result.append(normalize_page_uuid(str(item["id"])))
    return result


def _relations_any(page: Mapping[str, Any], names: Sequence[str]) -> list[str]:
    for name in names:
        values = _relations(page, name)
        if values:
            return values
    return []


def _one_relation(page: Mapping[str, Any], name: str) -> str:
    values = _relations(page, name)
    if len(values) != 1:
        raise ReviewRequired(f"{name} requires one exact Notion relation")
    return values[0]


def _one_relation_any(page: Mapping[str, Any], names: Sequence[str], *, optional: bool = False) -> str | None:
    values = _relations_any(page, names)
    if not values and optional:
        return None
    if len(values) != 1:
        raise ReviewRequired("required Notion relation is missing or ambiguous")
    return values[0]


def _environment_filter(property_name: str = "데이터 환경") -> Mapping[str, Any]:
    return {"property": property_name, "select": {"equals": "PRODUCTION"}}


def _and_filter(*filters: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"and": [dict(value) for value in filters]}


def _status_active(value: str | None) -> bool:
    return value in {"APPROVED", "ACTIVE", "운영", "활성"}


def _roster_mapper(page: Mapping[str, Any]) -> Sequence[tuple[str, Mapping[str, Any]]]:
    if _select(page, "데이터 환경") != "PRODUCTION":
        raise ReviewRequired("Cleaner Property Access row is not Production")
    page_id = normalize_page_uuid(str(page.get("id", "")))
    party_page = _one_relation(page, "인력")
    property_page = _one_relation(page, "집")
    application = _select(page, "신청 상태")
    roster_status = {
        "APPROVED": "ACTIVE",
        "REQUESTED": "PAUSED",
        "REJECTED": "REMOVED",
        "REVOKED": "REMOVED",
    }.get(application or "")
    if roster_status is None:
        raise ReviewRequired("Cleaner Property Access status is unsupported")
    priority = _number(page, "우선순위")
    party_id = notion_identity("party", party_page)
    return (
        (
            "party",
            {
                "entity_type": "party",
                "party_id": party_id,
                "party_code": f"NOTION:{party_page}",
                "display_name": f"NOTION:{party_page}",
                "data_environment": "PRODUCTION",
                "active": application == "APPROVED",
            },
        ),
        (
            "profile",
            {
                "entity_type": "cleaner_profile",
                "cleaner_party_id": party_id,
                # Same UUID as Party by V2.2.1 PK/FK design; keep the dependency
                # explicit so topological apply emits Party before CleanerProfile.
                "dependencies": (party_id,),
                "operational_status": "ACTIVE" if application == "APPROVED" else "PAUSED",
                "max_daily_work_minutes": None,
                "max_daily_jobs": None,
            },
        ),
        (
            "",
            {
                "entity_type": "cleaner_property_roster",
                "roster_id": notion_identity("roster", page_id),
                "cleaner_party_id": party_id,
                "property_id": notion_identity("property", property_page),
                "roster_status": roster_status,
                "offer_tier": 1,
                "priority_within_tier": None if priority is None else int(priority),
                "eligible_from": _date(page, "승인일"),
                "eligible_until": None,
            },
        ),
    )


def _rental_unit_mapper(page: Mapping[str, Any]) -> Sequence[tuple[str, Mapping[str, Any]]]:
    if _select(page, "데이터 환경") not in (None, "PRODUCTION"):
        raise ReviewRequired("Rental Unit bounded reference row is not Production")
    page_id = normalize_page_uuid(str(page.get("id", "")))
    property_page = _one_relation_any(page, ("연결 집", "집"))
    assert property_page is not None
    active = _select(page, "등록 상태") in (None, "APPROVED")
    property_id = notion_identity("property", property_page)
    return (
        (
            f"property-{property_page}",
            {
                "entity_type": "property",
                "property_id": property_id,
                "organization_id": _ORGANIZATION_ID,
                "property_code": f"NOTION:{property_page}",
                "display_name": f"NOTION:{property_page}",
                "timezone_name": "Asia/Seoul",
                "active": True,
            },
        ),
        (
            "",
            {
                "entity_type": "rental_unit",
                "rental_unit_id": notion_identity("rental-unit", page_id),
                "rental_unit_code": f"NOTION:{page_id}",
                "property_id": property_id,
                "display_name": f"NOTION:{page_id}",
                "active": active,
            },
        ),
    )


def _reservation_mapper(page: Mapping[str, Any]) -> Sequence[tuple[str, Mapping[str, Any]]]:
    if _select(page, "데이터 환경") not in (None, "PRODUCTION"):
        raise ReviewRequired("Reservation projection row is not Production")
    page_id = normalize_page_uuid(str(page.get("id", "")))
    property_page = _one_relation_any(page, ("연결 집", "Property", "집"))
    rental_page = _one_relation_any(page, ("연결 운영상품", "Rental Unit", "운영상품"), optional=True)
    checkout = _date(page, "체크아웃") or _date(page, "체크아웃 시각")
    if checkout is None:
        raise ReviewRequired("Reservation projection lacks checkout authority")
    status_raw = _select(page, "상태")
    status = {"확정": "CONFIRMED", "CONFIRMED": "CONFIRMED", "취소": "CANCELLED", "CANCELLED": "CANCELLED"}.get(status_raw or "")
    if status is None:
        raise ReviewRequired("Reservation projection status is unsupported")
    reservation_code = _text(page, "예약번호", "Reservation Code")
    if not reservation_code:
        raise ReviewRequired("Reservation projection lacks canonical reservation code")
    channel = _select(page, "플랫폼/채널") or _text(page, "플랫폼/채널", "채널")
    payload = {
        "entity_type": "reservation",
        "reservation_id": notion_identity("reservation", page_id),
        "reservation_code": reservation_code,
        "property_id": notion_identity("property", property_page),
        "rental_unit_id": None if rental_page is None else notion_identity("rental-unit", rental_page),
        "source_channel": channel,
        "external_reservation_id": reservation_code if channel else None,
        "reservation_status": status,
        "check_in_at": _date(page, "체크인") or _date(page, "체크인 시각"),
        "check_out_at": checkout,
    }
    return (("", payload),)


def _cleaning_payloads(page: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if _select(page, "데이터 환경") not in (None, "PRODUCTION"):
        raise ReviewRequired("Cleaning projection row is not Production")
    page_id = normalize_page_uuid(str(page.get("id", "")))
    property_page = _one_relation_any(page, ("연결 집", "Property", "집"))
    rental_page = _one_relation_any(page, ("연결 운영상품", "Rental Unit"), optional=True)
    reservation_page = _one_relation_any(page, ("관련 Reservation", "Reservation"), optional=True)
    start = _date(page, "시작 예정") or _date(page, "서비스 시작")
    end = _date(page, "완료 목표") or _date(page, "서비스 종료")
    if start is None or end is None:
        raise ReviewRequired("Cleaning projection lacks an exact service window")
    cleaning_id = notion_identity("cleaning", page_id)
    revision = reconstruct_schedule_revisions(
        cleaning_id,
        [
            ScheduleWindow(
                normalize_datetime(start),
                normalize_datetime(end),
                None,
                frozenset({"CURRENT_CLEANING"}),
            )
        ],
    )[0]
    state = _select(page, "상태")
    status = {
        "예정": "PLANNED",
        "담당자 배정": "ASSIGNED",
        "진행중": "IN_PROGRESS",
        "완료 보고": "COMPLETED",
        "관리자 확인 완료": "COMPLETED",
        "재방문 필요": "PLANNED",
        "취소": "CANCELLED",
        "PLANNED": "PLANNED",
        "ASSIGNED": "ASSIGNED",
        "IN_PROGRESS": "IN_PROGRESS",
        "COMPLETED": "COMPLETED",
        "CANCELLED": "CANCELLED",
    }.get(state or "")
    if status is None:
        raise ReviewRequired("Cleaning projection status is unsupported")
    code = _text(page, "Idempotency Key", "Cleaning Code") or f"NOTION:{page_id}"
    job = {
        "entity_type": "cleaning_job",
        "cleaning_id": cleaning_id,
        "cleaning_code": code,
        "reservation_id": None if reservation_page is None else notion_identity("reservation", reservation_page),
        "property_id": notion_identity("property", property_page),
        "rental_unit_id": None if rental_page is None else notion_identity("rental-unit", rental_page),
        "schedule_source_type": "RESERVATION_CHECKOUT" if reservation_page is not None else "MANUAL",
        "cleaning_status": status,
        "current_schedule_revision_id": revision["schedule_revision_id"],
    }
    return job, {"entity_type": "cleaning_schedule_revision", **dict(revision)}


def _cleaning_mapper(page: Mapping[str, Any]) -> Sequence[tuple[str, Mapping[str, Any]]]:
    job, revision = _cleaning_payloads(page)
    return (("", job), ("schedule", revision))


def _assignment_mapper_factory(
    client: ReadOnlyNotionClient,
    provenance: _JsonDirectoryEvidence,
    requests: _JsonDirectoryEvidence,
) -> Callable[[Mapping[str, Any]], Sequence[tuple[str, Mapping[str, Any]]]]:
    def mapper(page: Mapping[str, Any]) -> Sequence[tuple[str, Mapping[str, Any]]]:
        if _select(page, "데이터 환경") != "PRODUCTION" or _select(page, "제안 상태") != "ACCEPTED":
            raise ReviewRequired("Assignment source query returned a non-accepted Production row")
        page_id = normalize_page_uuid(str(page.get("id", "")))
        action_id = _text(page, "Action ID")
        if not action_id:
            raise ReviewRequired("accepted Assignment lacks durable Action ID")
        binding_offer = _checkbox(page, "Binding Offer")
        if binding_offer is True:
            provenance.require_assignment_pair(action_id)
        elif binding_offer is False:
            decision = requests.objects.get(f"{action_id}.json")
            if not isinstance(decision, Mapping):
                raise ReviewRequired("direct reassignment Assignment lacks durable decision evidence")
            if (
                decision.get("action_id") != action_id
                or decision.get("action_type") != "CLEANER_REASSIGNMENT_DECISION"
                or decision.get("decision_committed") is not True
                or decision.get("decision") != "REASSIGNED_ORIGINAL"
            ):
                raise ReviewRequired("direct reassignment Assignment decision evidence is incomplete")
        else:
            raise ReviewRequired("accepted Assignment lacks exact Binding Offer authority")
        cleaning_page = _one_relation(page, "Cleaning")
        cleaner_page = _one_relation(page, "후보 인력")
        cleaning = client.get_page(cleaning_page, expected_source_id=cleaning_source_id())
        _, revision = _cleaning_payloads(cleaning)
        start = revision["service_window_start_at"]
        end = revision["service_deadline_at"]
        offered = _date(page, "제안 시각")
        accepted = _date(page, "응답 시각")
        cutoff = _date(page, "응답 만료 시각")
        base_fee = _number(page, "Base Fee Snapshot")
        premium = _number(page, "Urgent Premium Snapshot")
        if None in (offered, accepted, cutoff, base_fee):
            raise ReviewRequired("accepted Assignment lacks exact retained action evidence")
        hold = _select(page, "Hold 상태")
        if hold not in {"HARD_BOOKED", "RELEASED"}:
            raise ReviewRequired("accepted Assignment hold state is unsupported")
        ended_at = _date(page, "Assignment Ended At") if hold == "RELEASED" else None
        end_reason = _select(page, "Assignment End Reason") if hold == "RELEASED" else None
        action = {
            "assignment_page_id": page_id,
            "cleaning_id": notion_identity("cleaning", cleaning_page),
            "schedule_revision_id": revision["schedule_revision_id"],
            "cleaner_party_id": notion_identity("party", cleaner_page),
            "persisted_actual_action": True,
            "offered_at": normalize_datetime(offered),
            "accepted_at": normalize_datetime(accepted),
            "acceptance_cutoff_at": normalize_datetime(cutoff),
            "scheduled_start_at": start,
            "scheduled_end_at": end,
            "base_fee_krw": int(base_fee),
            "urgent_premium_krw": int(premium or 0),
            "urgent_premium_policy_version": _text(page, "Urgent Premium Policy Version"),
            "replacement_urgency": _select(page, "Replacement Urgency") or "NORMAL",
            "assignment_status": hold,
            "ended_at": ended_at,
            "end_reason_code": end_reason,
        }
        ancestry = minimal_exact_assignment_ancestry(action)
        return tuple(
            (name, {"entity_type": f"cleaning_offer_{name}" if name in {"campaign", "candidate"} else "cleaning_assignment", **dict(payload)})
            for name, payload in ancestry.items()
        )

    return mapper


def _runtime_cleaner_records(document: Any) -> Sequence[Mapping[str, Any]]:
    if isinstance(document, list):
        rows = document
    elif isinstance(document, Mapping):
        for key in ("cleaners", "items", "records"):
            value = document.get(key)
            if isinstance(value, list):
                rows = value
                break
        else:
            if all(isinstance(value, Mapping) for value in document.values()):
                rows = list(document.values())
            else:
                raise SnapshotIntegrityError("Telegram durable identity document shape is unsupported")
    else:
        raise SnapshotIntegrityError("Telegram durable identity document shape is unsupported")
    result = []
    for value in rows:
        if not isinstance(value, Mapping):
            raise SnapshotIntegrityError("Telegram durable identity row is malformed")
        result.append(value)
    return tuple(result)


def _recursive_values(value: Any, key: str) -> Iterable[Any]:
    if isinstance(value, Mapping):
        for name, item in value.items():
            if name == key:
                yield item
            yield from _recursive_values(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from _recursive_values(item, key)


def _find_runtime_unavailable_evidence(
    documents: Iterable[Any], assignment_page: str, assignment: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    action_id = _text(assignment, "Action ID")
    end_key = _text(assignment, "Assignment End Key")
    for document in documents:
        if not isinstance(document, Mapping):
            continue
        action_type = str(document.get("action_type") or "")
        if "UNAVAILABLE" not in action_type:
            continue
        values = {str(value) for value in _recursive_values(document, "history_page_id") if value is not None}
        values.update(str(value) for value in _recursive_values(document, "assignment_page_id") if value is not None)
        if assignment_page not in values:
            if action_id and action_id not in {str(value) for value in _recursive_values(document, "action_id")}:
                continue
            if end_key and end_key not in {str(value) for value in _recursive_values(document, "end_key")}:
                continue
            if not action_id and not end_key:
                continue
        return document
    return None


def _runtime_value(document: Mapping[str, Any] | None, *keys: str) -> str | None:
    if document is None:
        return None
    for key in keys:
        for value in _recursive_values(document, key):
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def _first_nonblank(values: Iterable[str | None]) -> str | None:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _raw_notion_source(
    client: ReadOnlyNotionClient,
    source_id: str,
    source_type: str,
    *,
    filter_payload: Mapping[str, Any] | None = None,
) -> NotionSnapshotSource:
    exact = _validate_uuid("Notion source id", source_id)
    return NotionSnapshotSource(
        source_type,
        lambda cursor: client.query_data_source(exact, cursor, filter_payload=filter_payload),
        lambda page_id: client.get_page(page_id, expected_source_id=exact),
        semantic_normalizer=lambda row: dict(row),
        runtime_reference=f"notion-data-source:{exact}",
    )


def build_production_sources(
    config: ProductionStageBSourceConfig,
    *,
    notion_client: ReadOnlyNotionClient | None = None,
) -> tuple[Any, ...]:
    config.validate()
    client = notion_client or ReadOnlyNotionClient(token_path=config.notion_token_path or Path(""))
    for source_id in (
        S1_ROSTER_SOURCE_ID,
        S2_RENTAL_UNIT_SOURCE_ID,
        reservation_source_id(),
        cleaning_source_id(),
        S5_ASSIGNMENT_SOURCE_ID,
        S7_PERFORMANCE_SOURCE_ID,
    ):
        client.validate_data_source(source_id)

    production_filter = _environment_filter()
    roster_raw = _raw_notion_source(client, S1_ROSTER_SOURCE_ID, "RAW_ROSTER", filter_payload=production_filter)
    assignment_filter = _and_filter(
        production_filter,
        {"property": "제안 상태", "select": {"equals": "ACCEPTED"}},
    )
    assignment_raw = _raw_notion_source(
        client, S5_ASSIGNMENT_SOURCE_ID, "RAW_ASSIGNMENT", filter_payload=assignment_filter
    )
    performance_raw = _raw_notion_source(
        client,
        S7_PERFORMANCE_SOURCE_ID,
        "RAW_PERFORMANCE",
        filter_payload=_environment_filter("Data Environment"),
    )
    requests = _JsonDirectoryEvidence(config.request_dir or Path(""), label="cleaner-requests")
    provenance = _JsonDirectoryEvidence(
        config.assignment_provenance_dir or Path(""), label="assignment-execution-provenance"
    )

    s1 = _MappedNotionSource(
        client=client,
        source_id=S1_ROSTER_SOURCE_ID,
        source_type=S1_SOURCE_TYPE,
        mapper=_roster_mapper,
        filter_payload=production_filter,
    )
    s2 = _MappedNotionSource(
        client=client,
        source_id=S2_RENTAL_UNIT_SOURCE_ID,
        source_type=S2_SOURCE_TYPE,
        mapper=_rental_unit_mapper,
        filter_payload=production_filter,
        global_payloads=(
            (
                "organization",
                {
                    "entity_type": "organization",
                    "organization_id": _ORGANIZATION_ID,
                    "organization_code": "PROPERTYAI_PRODUCTION",
                    "display_name": "PROPERTYAI_PRODUCTION",
                    "organization_status": "ACTIVE",
                    "data_environment": "PRODUCTION",
                },
            ),
        ),
    )
    s3 = _MappedNotionSource(
        client=client,
        source_id=reservation_source_id(),
        source_type=S3_SOURCE_TYPE,
        mapper=_reservation_mapper,
        filter_payload=production_filter,
    )
    s4 = _MappedNotionSource(
        client=client,
        source_id=cleaning_source_id(),
        source_type=S4_SOURCE_TYPE,
        mapper=_cleaning_mapper,
        filter_payload=production_filter,
    )
    s5_base = _MappedNotionSource(
        client=client,
        source_id=S5_ASSIGNMENT_SOURCE_ID,
        source_type=S5_SOURCE_TYPE,
        mapper=_assignment_mapper_factory(client, provenance, requests),
        filter_payload=assignment_filter,
    )
    s5 = _EvidenceAugmentedSource(s5_base, provenance, requests)
    s6 = _TelegramIdentitySource(
        cleaners_path=config.cleaners_json_path or Path(""), roster_raw=roster_raw
    )
    s7 = _UnavailabilitySource(
        assignment_raw=assignment_raw,
        performance_raw=performance_raw,
        request_evidence=requests,
        client=client,
    )
    s8 = _ReassignmentSource(request_evidence=requests, client=client)
    sources = (s1, s2, s3, s4, s5, s6, s7, s8)
    if {source.source_type for source in sources} != {
        S1_SOURCE_TYPE,
        S2_SOURCE_TYPE,
        S3_SOURCE_TYPE,
        S4_SOURCE_TYPE,
        S5_SOURCE_TYPE,
        S6_SOURCE_TYPE,
        S7_SOURCE_TYPE,
        S8_SOURCE_TYPE,
    }:
        raise ProductionSourceConfigurationError("Production source authority coverage is incomplete")
    return sources


def production_source_adapter_factory(
    frozen_snapshot: SnapshotManifest,
    *,
    environment: Mapping[str, str] | None = None,
    config: ProductionStageBSourceConfig | None = None,
    notion_client: ReadOnlyNotionClient | None = None,
    preflight_freshness: bool = True,
) -> FreshSourceValidator:
    """Construct trusted Production sources; never derive freshness authority from caller JSON."""
    if not isinstance(frozen_snapshot, SnapshotManifest):
        raise ProductionSourceConfigurationError("Production source factory requires a trusted frozen snapshot")
    resolved = config or ProductionStageBSourceConfig.from_environment(environment)
    sources = build_production_sources(resolved, notion_client=notion_client)
    validator = FreshSourceValidator(sources, frozen_snapshot=frozen_snapshot, max_scans=6)
    if preflight_freshness:
        # This is intentionally before any Stage B PostgreSQL pool construction.
        validator.validate(frozen_snapshot)
    return validator


__all__ = [
    "ProductionSourceConfigurationError",
    "ProductionStageBSourceConfig",
    "ReadOnlyNotionClient",
    "S1_ROSTER_SOURCE_ID",
    "S2_RENTAL_UNIT_SOURCE_ID",
    "reservation_source_id",
    "cleaning_source_id",
    "S5_ASSIGNMENT_DATABASE_ID",
    "S5_ASSIGNMENT_SOURCE_ID",
    "S7_PERFORMANCE_SOURCE_ID",
    "build_production_sources",
    "production_source_adapter_factory",
]

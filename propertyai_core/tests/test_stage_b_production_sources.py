from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from propertyai_core.stage_b.models import SnapshotIntegrityError
from propertyai_core.stage_b.population import PopulationBuilder
from propertyai_core.stage_b.snapshot import ConvergentSnapshotBuilder, FreshSourceValidator
from propertyai_core.stage_b.sources import production as prod


P = "11111111-1111-4111-8111-111111111111"
U = "22222222-2222-4222-8222-222222222222"
CLEANER = "33333333-3333-4333-8333-333333333333"
ROSTER = "44444444-4444-4444-8444-444444444444"
RES = "55555555-5555-4555-8555-555555555555"
CLEANING = "66666666-6666-4666-8666-666666666666"
ASSIGNMENT = "77777777-7777-4777-8777-777777777777"
PERF = "88888888-8888-4888-8888-888888888888"
RESERVATION_SOURCE_ID = "11111111-1111-4111-8111-111111111111"
CLEANING_SOURCE_ID = "22222222-2222-4222-8222-222222222222"
PERF_SOURCE = "53206406-b7b7-4444-b5a8-b549b9af20f5"
WRONG_PERF_SOURCE = "99999999-9999-4999-8999-999999999999"
NOW = "2026-09-05T01:00:00+00:00"
START = "2026-09-06T02:00:00+00:00"
END = "2026-09-06T04:00:00+00:00"


def _select(value):
    return {"type": "select", "select": {"name": value}}


def _date(value):
    return {"type": "date", "date": {"start": value, "end": None}}


def _relation(*ids):
    return {"type": "relation", "relation": [{"id": value} for value in ids]}


def _text(value):
    return {"type": "rich_text", "rich_text": [{"plain_text": value}]}


def _number(value):
    return {"type": "number", "number": value}


def _checkbox(value):
    return {"type": "checkbox", "checkbox": value}


def _page(page_id, source_id, properties):
    return {
        "id": page_id,
        "parent": {"type": "data_source_id", "data_source_id": source_id},
        "last_edited_time": NOW,
        "url": f"https://www.notion.so/{page_id}",
        "properties": properties,
    }


def _fixture_pages():
    roster = _page(
        ROSTER,
        prod.S1_ROSTER_SOURCE_ID,
        {
            "데이터 환경": _select("PRODUCTION"),
            "신청 상태": _select("APPROVED"),
            "인력": _relation(CLEANER),
            "집": _relation(P),
            "우선순위": _number(1),
            "승인일": _date(NOW),
        },
    )
    rental = _page(
        U,
        prod.S2_RENTAL_UNIT_SOURCE_ID,
        {
            "데이터 환경": _select("PRODUCTION"),
            "등록 상태": _select("APPROVED"),
            "연결 집": _relation(P),
        },
    )
    reservation = _page(
        RES,
        RESERVATION_SOURCE_ID,
        {
            "데이터 환경": _select("PRODUCTION"),
            "상태": _select("확정"),
            "연결 집": _relation(P),
            "연결 운영상품": _relation(U),
            "예약번호": _text("RSV-001"),
            "플랫폼/채널": _select("AIRBNB"),
            "체크인": _date("2026-09-05T06:00:00+00:00"),
            "체크아웃": _date("2026-09-06T02:00:00+00:00"),
        },
    )
    cleaning = _page(
        CLEANING,
        CLEANING_SOURCE_ID,
        {
            "데이터 환경": _select("PRODUCTION"),
            "등록 상태": _select("APPROVED"),
            "상태": _select("담당자 배정"),
            "연결 집": _relation(P),
            "연결 운영상품": _relation(U),
            "관련 Reservation": _relation(RES),
            "시작 예정": _date(START),
            "완료 목표": _date(END),
            "Idempotency Key": _text("CLEANING-001"),
        },
    )
    assignment = _page(
        ASSIGNMENT,
        prod.S5_ASSIGNMENT_SOURCE_ID,
        {
            "데이터 환경": _select("PRODUCTION"),
            "제안 상태": _select("ACCEPTED"),
            "Hold 상태": _select("RELEASED"),
            "Binding Offer": _checkbox(True),
            "Cleaning": _relation(CLEANING),
            "후보 인력": _relation(CLEANER),
            "제안 시각": _date("2026-09-05T00:00:00+00:00"),
            "응답 시각": _date("2026-09-05T00:05:00+00:00"),
            "응답 만료 시각": _date("2026-09-05T01:00:00+00:00"),
            "Base Fee Snapshot": _number(55000),
            "Urgent Premium Snapshot": _number(0),
            "Replacement Urgency": _select("NORMAL"),
            "Assignment Ended At": _date("2026-09-05T00:30:00+00:00"),
            "Assignment End Reason": _select("CLEANER_UNAVAILABLE"),
            "Unavailable Reason": _select("PERSONAL"),
            "Action ID": _text("ACTION-001"),
            "Assignment End Key": _text("END-001"),
            "Reassignment Request Key": _text("REQ-KEY-001"),
        },
    )
    performance = _page(
        PERF,
        PERF_SOURCE,
        {
            "Data Environment": _select("PRODUCTION"),
            "Assignment": _relation(ASSIGNMENT),
            "Performance Classification": _select("EARLY_UNAVAILABLE"),
            "Replacement Urgency": _select("NORMAL"),
        },
    )
    return {
        prod.S1_ROSTER_SOURCE_ID: [roster],
        prod.S2_RENTAL_UNIT_SOURCE_ID: [rental],
        RESERVATION_SOURCE_ID: [reservation],
        CLEANING_SOURCE_ID: [cleaning],
        prod.S5_ASSIGNMENT_SOURCE_ID: [assignment],
        PERF_SOURCE: [performance],
    }


class FakeNotion:
    def __init__(self, pages=None):
        self.pages = pages or _fixture_pages()
        self.calls = []
        self.by_id = {
            row["id"]: row for rows in self.pages.values() for row in rows
        }
        self.query_override = None

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if path.startswith("/v1/data_sources/") and not path.endswith("/query"):
            source_id = path.rsplit("/", 1)[-1]
            return {"id": source_id, "object": "data_source"}
        if path.endswith("/query"):
            source_id = path.split("/")[3]
            if self.query_override is not None:
                return self.query_override(source_id, body)
            return {"results": self.pages.get(source_id, []), "has_more": False, "next_cursor": None}
        if path.startswith("/v1/pages/"):
            page_id = path.rsplit("/", 1)[-1]
            return self.by_id[page_id]
        raise AssertionError((method, path, body))


def _config(tmp_path):
    token = tmp_path / "token"
    token.write_text("fixture-not-a-production-secret")
    cleaners = tmp_path / "cleaners.json"
    cleaners.write_text(
        json.dumps(
            [
                {
                    "party_notion_page_id": CLEANER,
                    "telegram_user_id": "10001",
                    "bound_at": NOW,
                    "nickname": "ignored",
                }
            ]
        )
    )
    requests = tmp_path / "requests"
    requests.mkdir()
    (requests / "unavailable.json").write_text(
        json.dumps(
            {
                "action_type": "CLEANER_UNAVAILABLE",
                "history_page_id": ASSIGNMENT,
                "action_id": "ACTION-001",
                "end_key": "END-001",
                "performance_classification": "EARLY_UNAVAILABLE",
            }
        )
    )
    (requests / "reassign-request.json").write_text(
        json.dumps(
            {
                "action_type": "CLEANER_REASSIGNMENT_REQUEST",
                "action_id": "REQ-ACTION-001",
                "history_page_id": ASSIGNMENT,
                "cleaning_page_id": CLEANING,
                "cleaner_party_page_id": CLEANER,
                "request_key": "REQ-KEY-001",
                "created_at": "2026-09-05T00:40:00+00:00",
            }
        )
    )
    (requests / "reassign-decision.json").write_text(
        json.dumps(
            {
                "action_type": "CLEANER_REASSIGNMENT_DECISION",
                "action_id": "DECISION-001",
                "request_action_id": "REQ-ACTION-001",
                "history_page_id": ASSIGNMENT,
                "cleaning_page_id": CLEANING,
                "cleaner_party_page_id": CLEANER,
                "request_key": "REQ-KEY-001",
                "decision": "REASSIGNED_ORIGINAL",
                "decision_committed": True,
                "decision_at": "2026-09-05T00:45:00+00:00",
            }
        )
    )
    provenance = tmp_path / "provenance"
    provenance.mkdir()
    (provenance / "ACTION-001.intent.json").write_text(
        json.dumps({"operation_identity": "EFFECT-001"})
    )
    (provenance / "ACTION-001.success.json").write_text(
        json.dumps(
            {
                "pre_effect_operation_identity": "EFFECT-001",
                "assignment_execution_receipt": {
                    "action_id": "ACTION-001",
                    "action_type": "CLEANING_ASSIGNMENT",
                    "execution_result": {"cleaning_assignment": "ACCEPTED"},
                },
            }
        )
    )
    return prod.ProductionStageBSourceConfig(
        data_environment="PRODUCTION",
        postgres_mode="PRE_CUTOVER_MIGRATION",
        performance_source_id=PERF_SOURCE,
        root=tmp_path,
        notion_token_path=token,
        cleaners_json_path=cleaners,
        request_dir=requests,
        assignment_provenance_dir=provenance,
    )


def _client(tmp_path, fake=None):
    fake = fake or FakeNotion()
    token = tmp_path / "client-token"
    token.write_text("fixture")
    return prod.ReadOnlyNotionClient(token_path=token, transport=fake), fake


def test_frozen_source_ids_are_exact_and_no_title_discovery_symbol_exists(monkeypatch):
    monkeypatch.setenv("PROPERTYAI_NOTION_RESERVATION_SOURCE_ID", RESERVATION_SOURCE_ID)
    monkeypatch.setenv("PROPERTYAI_NOTION_CLEANING_SOURCE_ID", CLEANING_SOURCE_ID)
    assert prod.S1_ROSTER_SOURCE_ID == "b66af4a1-97e3-419a-8fd2-1d65c63e3d6d"
    assert prod.S2_RENTAL_UNIT_SOURCE_ID == "7fdd641c-9847-46ab-b91f-77e3a2063ec3"
    assert prod.reservation_source_id() == RESERVATION_SOURCE_ID
    assert prod.cleaning_source_id() == CLEANING_SOURCE_ID
    assert prod.S5_ASSIGNMENT_SOURCE_ID == "3b43edac-140d-4699-8db1-00020d10652c"
    assert prod.S7_PERFORMANCE_SOURCE_ID == PERF_SOURCE
    assert not hasattr(prod.ReadOnlyNotionClient, "search")


def test_config_missing_required_source_and_performance_binding_fail_closed(tmp_path):
    with pytest.raises(prod.ProductionSourceConfigurationError, match="invalid CLEANER_PERFORMANCE"):
        prod.ProductionStageBSourceConfig(
            data_environment="PRODUCTION",
            postgres_mode="PRE_CUTOVER_MIGRATION",
            performance_source_id="",
        ).validate()
    cfg = _config(tmp_path)
    cfg.cleaners_json_path.unlink()
    with pytest.raises(prod.ProductionSourceConfigurationError, match="Telegram durable identity"):
        cfg.validate()


def test_performance_source_binding_rejects_malformed_and_wrong_valid_uuid(tmp_path):
    cfg = _config(tmp_path)
    malformed = replace(cfg, performance_source_id="not-a-uuid")
    with pytest.raises(prod.ProductionSourceConfigurationError, match="invalid CLEANER_PERFORMANCE"):
        malformed.validate()

    wrong = replace(cfg, performance_source_id=WRONG_PERF_SOURCE)
    with pytest.raises(
        prod.ProductionSourceConfigurationError, match="does not match frozen Production authority"
    ):
        wrong.validate()


def test_factory_rejects_wrong_but_resolvable_performance_source_before_bundle(tmp_path):
    cfg = _config(tmp_path)
    client, fake = _client(tmp_path)
    frozen = ConvergentSnapshotBuilder(
        prod.build_production_sources(cfg, notion_client=client)
    ).capture()

    # The fake Notion service can resolve any syntactically valid UUID, including the attack ID.
    client.validate_data_source(WRONG_PERF_SOURCE)
    fake.calls.clear()

    wrong = replace(cfg, performance_source_id=WRONG_PERF_SOURCE)
    with pytest.raises(
        prod.ProductionSourceConfigurationError, match="does not match frozen Production authority"
    ):
        prod.production_source_adapter_factory(
            frozen,
            config=wrong,
            notion_client=client,
            preflight_freshness=False,
        )
    assert fake.calls == []


def test_factory_accepts_exact_frozen_performance_source_and_binds_s7(tmp_path):
    cfg = _config(tmp_path)
    client, _ = _client(tmp_path)
    frozen = ConvergentSnapshotBuilder(
        prod.build_production_sources(cfg, notion_client=client)
    ).capture()
    validator = prod.production_source_adapter_factory(
        frozen,
        config=cfg,
        notion_client=client,
        preflight_freshness=False,
    )
    s7 = next(source for source in validator.sources if source.source_type == prod.S7_SOURCE_TYPE)
    assert f"notion-data-source:{prod.S7_PERFORMANCE_SOURCE_ID}" in s7.runtime_reference
    assert len(validator.sources) == 8


def test_exact_frozen_performance_source_unresolved_fails_closed(tmp_path):
    cfg = _config(tmp_path)
    good_client, _ = _client(tmp_path)
    frozen = ConvergentSnapshotBuilder(
        prod.build_production_sources(cfg, notion_client=good_client)
    ).capture()

    def unresolved(method, path, body):
        if path == f"/v1/data_sources/{prod.S7_PERFORMANCE_SOURCE_ID}":
            return {"id": WRONG_PERF_SOURCE}
        if path.startswith("/v1/data_sources/") and not path.endswith("/query"):
            source_id = path.rsplit("/", 1)[-1]
            return {"id": source_id}
        raise AssertionError((method, path, body))

    token = tmp_path / "unresolved-token"
    token.write_text("fixture")
    client = prod.ReadOnlyNotionClient(token_path=token, transport=unresolved)
    with pytest.raises(SnapshotIntegrityError, match="identity mismatch"):
        prod.production_source_adapter_factory(
            frozen,
            config=cfg,
            notion_client=client,
            preflight_freshness=False,
        )


def test_runtime_provenance_directory_missing_fails_closed(tmp_path):
    cfg = _config(tmp_path)
    for path in cfg.assignment_provenance_dir.iterdir():
        path.unlink()
    cfg.assignment_provenance_dir.rmdir()
    with pytest.raises(prod.ProductionSourceConfigurationError, match="provenance directory"):
        cfg.validate()


def test_binding_assignment_missing_success_provenance_fails_closed(tmp_path):
    cfg = _config(tmp_path)
    (cfg.assignment_provenance_dir / "ACTION-001.success.json").unlink()
    client, _ = _client(tmp_path)
    sources = prod.build_production_sources(cfg, notion_client=client)
    with pytest.raises(Exception, match="intent/success provenance"):
        ConvergentSnapshotBuilder(sources).capture()


def test_read_only_notion_rejects_mutation_and_invalid_identity(tmp_path):
    client, fake = _client(tmp_path)
    with pytest.raises(prod.ProductionSourceConfigurationError, match="mutation"):
        client._request("PATCH", f"/v1/pages/{ROSTER}", {})
    with pytest.raises(prod.ProductionSourceConfigurationError, match="invalid Notion source id"):
        client.validate_data_source("not-a-uuid")
    assert all(call[0] in {"GET", "POST"} for call in fake.calls)


def test_paginated_repeated_cursor_fails_closed(tmp_path):
    fake = FakeNotion()
    def loop(source_id, body):
        return {"results": [], "has_more": True, "next_cursor": "same"}
    fake.query_override = loop
    client, _ = _client(tmp_path, fake)
    source = prod._raw_notion_source(client, prod.S1_ROSTER_SOURCE_ID, "X")
    with pytest.raises(SnapshotIntegrityError, match="cursor repeated"):
        source.scan()


def test_production_bundle_represents_s1_through_s8_and_builds_population(tmp_path):
    cfg = _config(tmp_path)
    client, fake = _client(tmp_path)
    sources = prod.build_production_sources(cfg, notion_client=client)
    assert [source.source_type for source in sources] == [
        prod.S1_SOURCE_TYPE,
        prod.S2_SOURCE_TYPE,
        prod.S3_SOURCE_TYPE,
        prod.S4_SOURCE_TYPE,
        prod.S5_SOURCE_TYPE,
        prod.S6_SOURCE_TYPE,
        prod.S7_SOURCE_TYPE,
        prod.S8_SOURCE_TYPE,
    ]
    manifest = ConvergentSnapshotBuilder(sources).capture()
    population = PopulationBuilder().build(manifest)
    assert not population.obligations
    assert not population.identity_collisions
    assert not population.authority_findings
    entity_types = {row.entity_type for row in population.records}
    assert {
        "organization", "property", "rental_unit", "party", "cleaner_profile",
        "cleaner_property_roster", "external_identity", "reservation", "cleaning_job",
        "cleaning_schedule_revision", "cleaning_offer_campaign", "cleaning_offer_candidate",
        "cleaning_assignment", "cleaner_unavailability_case", "cleaner_reassignment_request",
    } <= entity_types
    assert all("search" not in path for _, path, _ in fake.calls)


def test_factory_returns_real_validator_and_same_sources_reextract(tmp_path):
    cfg = _config(tmp_path)
    client, _ = _client(tmp_path)
    frozen = ConvergentSnapshotBuilder(
        prod.build_production_sources(cfg, notion_client=client)
    ).capture()
    validator = prod.production_source_adapter_factory(
        frozen,
        config=cfg,
        notion_client=client,
        preflight_freshness=False,
    )
    assert isinstance(validator, FreshSourceValidator)
    trusted = validator.validate(frozen)
    for source_key in trusted.source_row_identities:
        validator.validate_point_read(trusted, source_key)
    assert validator.sources[0].source_type == prod.S1_SOURCE_TYPE


def test_repackaged_caller_manifest_cannot_replace_real_extraction(tmp_path):
    cfg = _config(tmp_path)
    client, _ = _client(tmp_path)
    frozen = ConvergentSnapshotBuilder(
        prod.build_production_sources(cfg, notion_client=client)
    ).capture()
    validator = prod.production_source_adapter_factory(
        frozen, config=cfg, notion_client=client, preflight_freshness=False
    )
    caller = replace(frozen, run_id=UUID("99999999-9999-4999-8999-999999999999"))
    with pytest.raises(SnapshotIntegrityError, match="validator-bound frozen"):
        validator.validate(caller)


def test_production_environment_without_required_config_fails_closed(tmp_path):
    env = {
        "PROPERTYAI_DATA_ENVIRONMENT": "PRODUCTION",
        "PROPERTYAI_CLEANER_POSTGRES_MODE": "PRE_CUTOVER_MIGRATION",
        "PROPERTYAI_STAGE_B_PRODUCTION_ROOT": str(tmp_path),
    }
    with pytest.raises(prod.ProductionSourceConfigurationError):
        prod.ProductionStageBSourceConfig.from_environment(env)


def test_invalid_notion_service_identity_response_fails_closed(tmp_path):
    wrong = "99999999-9999-4999-8999-999999999999"

    def transport(method, path, body):
        assert method == "GET"
        return {"id": wrong}

    token = tmp_path / "token-identity"
    token.write_text("fixture")
    client = prod.ReadOnlyNotionClient(token_path=token, transport=transport)
    with pytest.raises(SnapshotIntegrityError, match="identity mismatch"):
        client.validate_data_source(prod.S1_ROSTER_SOURCE_ID)


def test_runtime_json_directory_membership_instability_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    root.mkdir()
    first = root / "one.json"
    first.write_text(json.dumps({"ok": True}))
    evidence = prod._JsonDirectoryEvidence(root, label="unstable-runtime")
    real = prod.read_stable_file
    mutated = False

    def mutate_once(path):
        nonlocal mutated
        result = real(path)
        if not mutated:
            mutated = True
            (root / "two.json").write_text(json.dumps({"late": True}))
        return result

    monkeypatch.setattr(prod, "read_stable_file", mutate_once)
    with pytest.raises(SnapshotIntegrityError, match="membership changed"):
        evidence.scan()


def test_production_cli_default_factory_is_resolved_only_for_exact_mode(tmp_path, monkeypatch):
    from propertyai_core.stage_b import cli

    cfg = _config(tmp_path)
    client, _ = _client(tmp_path)
    sources = prod.build_production_sources(cfg, notion_client=client)
    frozen = ConvergentSnapshotBuilder(sources).capture()
    trusted = FreshSourceValidator(sources, frozen_snapshot=frozen)
    calls = []

    def factory(manifest, **kwargs):
        calls.append((manifest, kwargs["environment"]))
        return trusted

    monkeypatch.setattr(prod, "production_source_adapter_factory", factory)
    resolved = cli._configured_fresh_source_validator(
        frozen_snapshot=frozen,
        environment={
            "PROPERTYAI_DATA_ENVIRONMENT": "PRODUCTION",
            "PROPERTYAI_CLEANER_POSTGRES_MODE": "PRE_CUTOVER_MIGRATION",
        },
    )
    assert resolved is trusted
    assert calls and calls[0][0] is frozen
    with pytest.raises(SnapshotIntegrityError, match="not configured"):
        cli._configured_fresh_source_validator(
            frozen_snapshot=frozen,
            environment={"PROPERTYAI_DATA_ENVIRONMENT": "DEVELOPMENT"},
        )


def test_entity_scoped_identity_registry_still_rejects_same_entity_conflict():
    from propertyai_core.stage_b.models import SourceObservation

    party_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    class Source:
        source_type = "SYNTHETIC_IDENTITY_GUARD"
        runtime_reference = "test:identity-guard"
        file_content_hashes = {}

        def scan(self):
            return (
                SourceObservation(
                    self.source_type,
                    "one",
                    {
                        "entity_type": "party",
                        "party_id": party_id,
                        "party_code": "ONE",
                        "display_name": "one",
                        "data_environment": "PRODUCTION",
                        "active": True,
                    },
                ),
                SourceObservation(
                    self.source_type,
                    "two",
                    {
                        "entity_type": "party",
                        "party_id": party_id,
                        "party_code": "TWO",
                        "display_name": "two",
                        "data_environment": "PRODUCTION",
                        "active": True,
                    },
                ),
            )

        def point_read(self, durable_identity):
            return next((row for row in self.scan() if row.durable_identity == durable_identity), None)

    population = PopulationBuilder().build(ConvergentSnapshotBuilder((Source(),)).capture())
    assert population.identity_collisions
    assert "conflicting immutable identities" in population.identity_collisions[0]

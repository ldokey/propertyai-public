from __future__ import annotations

from copy import deepcopy

import pytest

import propertyai_core.global_writer as global_writer
from propertyai_core.global_writer import ProductionWriterError


pytestmark = pytest.mark.real_global_writer


class BuildIdentityStub:
    def __init__(self, payload):
        self.payload = payload

    def as_dict(self):
        return self.payload


def _verify(monkeypatch: pytest.MonkeyPatch, payload) -> None:
    monkeypatch.setattr(global_writer, "client_build_identity", lambda: BuildIdentityStub(payload))
    global_writer._assert_accepted_thin_build()


def test_exact_imported_thin_artifact_is_one_of_the_accepted_profiles() -> None:
    # This deliberately exercises the public build identity API of whichever exact
    # Thin wheel is supplied on PYTHONPATH by the verification command.
    global_writer._assert_accepted_thin_build()


@pytest.mark.parametrize(
    "profile",
    (
        global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_LEGACY_0_1,
        global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_TRANSITION_0_2,
        global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_3,
        global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_4,
        global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_5,
    ),
)
def test_each_exact_transition_profile_passes(monkeypatch: pytest.MonkeyPatch, profile) -> None:
    _verify(monkeypatch, deepcopy(profile))


@pytest.mark.parametrize(
    ("profile_name", "mutate"),
    (
        ("legacy_source", lambda p: p.__setitem__("source_commit", "0" * 40)),
        ("legacy_artifact", lambda p: p.__setitem__("artifact_identity", "source-commit:" + "0" * 40)),
        ("legacy_schema", lambda p: p.__setitem__("schema_contract_version", 7)),
        ("legacy_contract", lambda p: p.__setitem__("schema_contract_identity", "sha256:" + "0" * 64)),
        ("transition_source", lambda p: p.__setitem__("source_commit", "0" * 40)),
        ("transition_artifact", lambda p: p.__setitem__("artifact_identity", "source-commit:" + "0" * 40)),
        ("transition_format", lambda p: p.__setitem__("thin_contract_format_version", 3)),
        ("transition_schemas_broader", lambda p: p.__setitem__("supported_dcs_schema_versions", (6, 7, 8))),
        ("transition_contract", lambda p: p.__setitem__("schema_contract_identity", "sha256:" + "0" * 64)),
        ("future_version", lambda p: p.__setitem__("version", "0.3.0")),
        ("future_build", lambda p: p.__setitem__("build_id", "adcp-global-writer-client@0.3.0+gfuture")),
        ("missing_required", lambda p: p.pop("artifact_identity")),
        ("lookalike_extra_field", lambda p: p.__setitem__("compatible", True)),
    ),
)
def test_near_match_unknown_and_future_profiles_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    profile_name: str,
    mutate,
) -> None:
    if profile_name.startswith("legacy_"):
        payload = deepcopy(global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_LEGACY_0_1)
    else:
        payload = deepcopy(global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_TRANSITION_0_2)
    mutate(payload)
    monkeypatch.setattr(global_writer, "client_build_identity", lambda: BuildIdentityStub(payload))
    with pytest.raises(ProductionWriterError, match="GLOBAL_WRITER_THIN_IDENTITY_MISMATCH"):
        global_writer._assert_accepted_thin_build()


@pytest.mark.parametrize(
    ("case", "mutate"),
    (
        ("wrong_source", lambda p: p.__setitem__("source_commit", "0" * 40)),
        ("wrong_artifact", lambda p: p.__setitem__("artifact_identity", "source-commit:" + "0" * 40)),
        ("wrong_contract", lambda p: p.__setitem__("schema_contract_identity", "sha256:" + "0" * 64)),
        ("wrong_schema_set", lambda p: p.__setitem__("supported_dcs_schema_versions", (6, 7))),
        ("malformed_build", lambda p: p.__setitem__("build_id", "not-a-build-identity")),
        ("source_tree_build", lambda p: p.__setitem__("build_id", "adcp-global-writer-client@0.3.0+source-tree")),
        ("source_tree_source", lambda p: p.__setitem__("source_commit", "UNBUILT_SOURCE_TREE")),
        ("source_tree_artifact", lambda p: p.__setitem__("artifact_identity", "source-tree:unbuilt")),
        ("patch_version", lambda p: p.__setitem__("version", "0.3.1")),
        ("next_minor", lambda p: p.__setitem__("version", "0.4.0")),
        ("unknown_future_build", lambda p: p.__setitem__("build_id", "adcp-global-writer-client@9.9.9+gfuture")),
        ("lookalike_extra_field", lambda p: p.__setitem__("compatible", True)),
    ),
)
def test_exact_0_3_profile_near_matches_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    mutate,
) -> None:
    payload = deepcopy(global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_3)
    mutate(payload)
    monkeypatch.setattr(global_writer, "client_build_identity", lambda: BuildIdentityStub(payload))
    with pytest.raises(ProductionWriterError, match="GLOBAL_WRITER_THIN_IDENTITY_MISMATCH"):
        global_writer._assert_accepted_thin_build()


@pytest.mark.parametrize(
    ("case", "mutate"),
    (
        ("wrong_source", lambda p: p.__setitem__("source_commit", "0" * 40)),
        ("wrong_artifact", lambda p: p.__setitem__("artifact_identity", "source-commit:" + "0" * 40)),
        ("wrong_contract", lambda p: p.__setitem__("schema_contract_identity", "sha256:" + "0" * 64)),
        ("wrong_schema_set", lambda p: p.__setitem__("supported_dcs_schema_versions", (6, 7, 8))),
        ("wrong_build_suffix", lambda p: p.__setitem__("build_id", "adcp-global-writer-client@0.4.0+gffffffffffff")),
        ("patch_version", lambda p: p.__setitem__("version", "0.4.1")),
        ("future_version", lambda p: p.__setitem__("version", "0.5.0")),
        ("lookalike_extra_field", lambda p: p.__setitem__("compatible", True)),
    ),
)
def test_exact_0_4_profile_near_matches_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    mutate,
) -> None:
    payload = deepcopy(global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_4)
    mutate(payload)
    monkeypatch.setattr(global_writer, "client_build_identity", lambda: BuildIdentityStub(payload))
    with pytest.raises(ProductionWriterError, match="GLOBAL_WRITER_THIN_IDENTITY_MISMATCH"):
        global_writer._assert_accepted_thin_build()


@pytest.mark.parametrize(
    ("case", "mutate"),
    (
        ("wrong_source", lambda p: p.__setitem__("source_commit", "0" * 40)),
        ("wrong_artifact", lambda p: p.__setitem__("artifact_identity", "source-commit:" + "0" * 40)),
        ("wrong_contract", lambda p: p.__setitem__("schema_contract_identity", "sha256:" + "0" * 64)),
        ("wrong_schema_set", lambda p: p.__setitem__("supported_dcs_schema_versions", (6, 7, 8, 9))),
        ("wrong_build_suffix", lambda p: p.__setitem__("build_id", "adcp-global-writer-client@0.5.0+gffffffffffff")),
        ("patch_version", lambda p: p.__setitem__("version", "0.5.1")),
        ("future_version", lambda p: p.__setitem__("version", "0.6.0")),
        ("lookalike_extra_field", lambda p: p.__setitem__("compatible", True)),
    ),
)
def test_exact_0_5_profile_near_matches_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    mutate,
) -> None:
    payload = deepcopy(global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_5)
    mutate(payload)
    monkeypatch.setattr(global_writer, "client_build_identity", lambda: BuildIdentityStub(payload))
    with pytest.raises(ProductionWriterError, match="GLOBAL_WRITER_THIN_IDENTITY_MISMATCH"):
        global_writer._assert_accepted_thin_build()


@pytest.mark.parametrize("payload", (None, (), "not-a-mapping"))
def test_malformed_identity_payload_fails_closed(monkeypatch: pytest.MonkeyPatch, payload) -> None:
    monkeypatch.setattr(global_writer, "client_build_identity", lambda: BuildIdentityStub(payload))
    with pytest.raises(ProductionWriterError, match="GLOBAL_WRITER_THIN_IDENTITY_INVALID"):
        global_writer._assert_accepted_thin_build()


def test_build_identity_api_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail():
        raise RuntimeError("malformed-runtime-build-identity")

    monkeypatch.setattr(global_writer, "client_build_identity", fail)
    with pytest.raises(ProductionWriterError, match="GLOBAL_WRITER_THIN_IDENTITY_INVALID"):
        global_writer._assert_accepted_thin_build()

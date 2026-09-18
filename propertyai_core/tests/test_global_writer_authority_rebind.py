from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import propertyai_core.global_writer as global_writer
from adcp_global_writer_client import (
    LiveOSProcessIdentityVerifier,
    RuntimeIdentity,
    RuntimeIdentityError,
    authorize_new_mutation,
    write_runtime_identity,
)
from propertyai_core.global_writer_authority_rebind import (
    AuthorizedIdentityRebindError,
    FinalAcceptedProductReleaseIdentity,
    REBIND_CONTRACT_DEFINITION_IDENTITY,
    THIN_CLIENT_RUNTIME_BUILD_0_3,
    THIN_CLIENT_RUNTIME_BUILD_0_4,
    THIN_CLIENT_RUNTIME_BUILD_0_5,
    WRITER_SERVICE_CODES,
    build_authorized_identity_rebind_contract,
)


pytestmark = pytest.mark.real_global_writer

PRODUCT_COMMIT = "a" * 40
PRODUCT_ARTIFACT = f"source-commit:{PRODUCT_COMMIT}"
PRODUCT_IDENTITY = (
    f"product:PropertyAI@g{PRODUCT_COMMIT[:12]}|source={PRODUCT_COMMIT}|artifact={PRODUCT_ARTIFACT}"
)


def _runtime_build_from_profile(profile: dict) -> str:
    return (
        f"{profile['build_id']}|source={profile['source_commit']}"
        f"|artifact={profile['artifact_identity']}"
    )


def _release(commit: str = PRODUCT_COMMIT) -> FinalAcceptedProductReleaseIdentity:
    artifact = f"source-commit:{commit}"
    identity = f"product:PropertyAI@g{commit[:12]}|source={commit}|artifact={artifact}"
    return FinalAcceptedProductReleaseIdentity(commit, identity, artifact)


def _runtime_identity(
    writer: str,
    *,
    release: FinalAcceptedProductReleaseIdentity | None = None,
    thin_build: str = THIN_CLIENT_RUNTIME_BUILD_0_4,
) -> RuntimeIdentity:
    release = release or _release()
    process = LiveOSProcessIdentityVerifier().current_process_identity()
    return RuntimeIdentity(
        service_code=WRITER_SERVICE_CODES[writer],
        pid=os.getpid(),
        process_started_at=process.process_started_at,
        process_incarnation_id=process.incarnation_id,
        product_build_commit=release.product_build_commit,
        product_build_identity=release.product_build_identity,
        global_writer_client_build=thin_build,
        interpreter_or_executable=os.sys.executable,
        source_root_or_artifact_identity=release.source_root_or_artifact_identity,
        identity_generated_at=process.process_started_at,
        config_artifact_identity=None,
    )


def _authorize(tmp_path: Path, runtime: RuntimeIdentity, authorized: dict) -> str:
    runtime_path = tmp_path / "runtime.json"
    authorized_path = tmp_path / "authorized.json"
    write_runtime_identity(runtime_path, runtime)
    authorized_path.write_text(
        json.dumps(authorized, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return authorize_new_mutation(
        runtime_identity_path=runtime_path,
        authorized_identity_path=authorized_path,
    )


def test_rebind_thin_build_constants_match_runtime_acceptance_profiles() -> None:
    assert THIN_CLIENT_RUNTIME_BUILD_0_3 == _runtime_build_from_profile(
        global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_3
    )
    assert THIN_CLIENT_RUNTIME_BUILD_0_4 == _runtime_build_from_profile(
        global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_4
    )
    assert THIN_CLIENT_RUNTIME_BUILD_0_5 == _runtime_build_from_profile(
        global_writer.TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_5
    )


@pytest.mark.parametrize(
    "thin_build",
    (
        THIN_CLIENT_RUNTIME_BUILD_0_3,
        THIN_CLIENT_RUNTIME_BUILD_0_4,
        THIN_CLIENT_RUNTIME_BUILD_0_5,
    ),
)
def test_exact_0_3_0_4_and_0_5_pairs_authorize(tmp_path: Path, thin_build: str) -> None:
    contract = build_authorized_identity_rebind_contract(
        product_release=_release(), thin_client_runtime_build=thin_build
    )
    assert (
        _authorize(
            tmp_path,
            _runtime_identity("W02", thin_build=thin_build),
            contract.payload_for("W02"),
        )
        == "MATCH"
    )


@pytest.mark.parametrize("writer", tuple(WRITER_SERVICE_CODES))
def test_each_w01_to_w06_exact_0_4_payload_authorizes(
    tmp_path: Path, writer: str
) -> None:
    contract = build_authorized_identity_rebind_contract(
        product_release=_release(),
        thin_client_runtime_build=THIN_CLIENT_RUNTIME_BUILD_0_4,
    )
    assert _authorize(
        tmp_path,
        _runtime_identity(writer, thin_build=THIN_CLIENT_RUNTIME_BUILD_0_4),
        contract.payload_for(writer),
    ) == "MATCH"


@pytest.mark.parametrize("writer", tuple(WRITER_SERVICE_CODES))
def test_each_w01_to_w06_exact_0_5_payload_authorizes(
    tmp_path: Path, writer: str
) -> None:
    contract = build_authorized_identity_rebind_contract(
        product_release=_release(),
        thin_client_runtime_build=THIN_CLIENT_RUNTIME_BUILD_0_5,
    )
    assert _authorize(
        tmp_path,
        _runtime_identity(writer, thin_build=THIN_CLIENT_RUNTIME_BUILD_0_5),
        contract.payload_for(writer),
    ) == "MATCH"


def test_w01_to_w06_payloads_are_complete_deterministic_and_distinct_by_service() -> None:
    first = build_authorized_identity_rebind_contract(
        product_release=_release(),
        thin_client_runtime_build=THIN_CLIENT_RUNTIME_BUILD_0_4,
    )
    second = build_authorized_identity_rebind_contract(
        product_release=_release(),
        thin_client_runtime_build=THIN_CLIENT_RUNTIME_BUILD_0_4,
    )
    assert first.contract_identity == second.contract_identity
    assert first.as_dict() == second.as_dict()
    assert set(first.payloads()) == set(WRITER_SERVICE_CODES)
    assert len({payload["service_code"] for payload in first.payloads().values()}) == 6
    assert all(
        payload["global_writer_client_build"] == THIN_CLIENT_RUNTIME_BUILD_0_4
        for payload in first.payloads().values()
    )
    assert all(payload["schema_version"] == 2 for payload in first.payloads().values())
    assert REBIND_CONTRACT_DEFINITION_IDENTITY.startswith("sha256:")


def test_0_3_0_4_mixed_pair_fails_authorize_new_mutation(tmp_path: Path) -> None:
    contract = build_authorized_identity_rebind_contract(
        product_release=_release(),
        thin_client_runtime_build=THIN_CLIENT_RUNTIME_BUILD_0_3,
    )
    with pytest.raises(RuntimeIdentityError, match="RUNTIME_IDENTITY_MISMATCH"):
        _authorize(
            tmp_path,
            _runtime_identity("W03", thin_build=THIN_CLIENT_RUNTIME_BUILD_0_4),
            contract.payload_for("W03"),
        )


def test_wrong_product_release_pair_fails_authorize_new_mutation(tmp_path: Path) -> None:
    runtime_release = _release("b" * 40)
    authorized_release = _release("c" * 40)
    contract = build_authorized_identity_rebind_contract(
        product_release=authorized_release,
        thin_client_runtime_build=THIN_CLIENT_RUNTIME_BUILD_0_4,
    )
    with pytest.raises(RuntimeIdentityError, match="RUNTIME_IDENTITY_MISMATCH"):
        _authorize(
            tmp_path,
            _runtime_identity("W04", release=runtime_release),
            contract.payload_for("W04"),
        )


@pytest.mark.parametrize(
    "thin_build",
    (
        "adcp-global-writer-client@0.4.0+gffffffffffff|source="
        + "f" * 40
        + "|artifact=source-commit:"
        + "f" * 40,
        "adcp-global-writer-client@0.4.1+g23ce586dd369|source="
        "23ce586dd369a60ac7bbbd24b33175deb05a402d|artifact=source-commit:"
        "23ce586dd369a60ac7bbbd24b33175deb05a402d",
        "adcp-global-writer-client@9.9.9+g23ce586dd369|source="
        "23ce586dd369a60ac7bbbd24b33175deb05a402d|artifact=source-commit:"
        "23ce586dd369a60ac7bbbd24b33175deb05a402d",
        "adcp-global-writer-client@0.5.0+g4e3bd2691b2e|source="
        "23ce586dd369a60ac7bbbd24b33175deb05a402d|artifact=source-commit:"
        "23ce586dd369a60ac7bbbd24b33175deb05a402d",
        "adcp-global-writer-client@0.5.0+g4e3bd2691b2e|source="
        "4e3bd2691b2ed58eccb65c32ad127467c89a6ebb|artifact=source-commit:"
        "23ce586dd369a60ac7bbbd24b33175deb05a402d",
        "adcp-global-writer-client@0.6.0+g4e3bd2691b2e|source="
        "4e3bd2691b2ed58eccb65c32ad127467c89a6ebb|artifact=source-commit:"
        "4e3bd2691b2ed58eccb65c32ad127467c89a6ebb",
    ),
)
def test_unknown_thin_builds_are_not_materializable(thin_build: str) -> None:
    with pytest.raises(
        AuthorizedIdentityRebindError, match="REBIND_THIN_CLIENT_BUILD_UNSUPPORTED"
    ):
        build_authorized_identity_rebind_contract(
            product_release=_release(), thin_client_runtime_build=thin_build
        )


@pytest.mark.parametrize(
    "release",
    (
        lambda: FinalAcceptedProductReleaseIdentity(
            PRODUCT_COMMIT, PRODUCT_IDENTITY, "source-commit:" + "b" * 40
        ),
        lambda: FinalAcceptedProductReleaseIdentity(
            PRODUCT_COMMIT,
            "product:PropertyAI@gbad|source=bad|artifact=bad",
            PRODUCT_ARTIFACT,
        ),
        lambda: FinalAcceptedProductReleaseIdentity(
            "NOT_A_COMMIT", PRODUCT_IDENTITY, PRODUCT_ARTIFACT
        ),
    ),
)
def test_invalid_final_product_release_identity_fails_closed(release) -> None:
    with pytest.raises(AuthorizedIdentityRebindError):
        release()


def test_existing_e9adb4f_0_3_rollback_pair_authorizes(tmp_path: Path) -> None:
    rollback_release = _release("e9adb4f3635e3b13aa22f674487f589befdbbd41")
    contract = build_authorized_identity_rebind_contract(
        product_release=rollback_release,
        thin_client_runtime_build=THIN_CLIENT_RUNTIME_BUILD_0_3,
    )
    assert (
        _authorize(
            tmp_path,
            _runtime_identity(
                "W02",
                release=rollback_release,
                thin_build=THIN_CLIENT_RUNTIME_BUILD_0_3,
            ),
            contract.payload_for("W02"),
        )
        == "MATCH"
    )


def test_direct_contract_construction_cannot_bypass_exact_thin_allowlist() -> None:
    from propertyai_core.global_writer_authority_rebind import AuthorizedIdentityRebindContract

    config = tuple((writer, None) for writer in WRITER_SERVICE_CODES)
    with pytest.raises(
        AuthorizedIdentityRebindError, match="REBIND_THIN_CLIENT_BUILD_UNSUPPORTED"
    ):
        AuthorizedIdentityRebindContract(
            product_release=_release(),
            thin_client_runtime_build=(
                "adcp-global-writer-client@0.4.0+gffffffffffff|source="
                + "f" * 40
                + "|artifact=source-commit:"
                + "f" * 40
            ),
            config_artifact_identities=config,
        )


def test_direct_contract_construction_requires_exact_w01_to_w06_config_set() -> None:
    from propertyai_core.global_writer_authority_rebind import AuthorizedIdentityRebindContract

    with pytest.raises(
        AuthorizedIdentityRebindError, match="REBIND_CONFIG_ARTIFACT_SET_MISMATCH"
    ):
        AuthorizedIdentityRebindContract(
            product_release=_release(),
            thin_client_runtime_build=THIN_CLIENT_RUNTIME_BUILD_0_4,
            config_artifact_identities=(("W01", None),),
        )


def test_config_artifact_binding_is_exact_and_all_writers_required() -> None:
    config = {writer: None for writer in WRITER_SERVICE_CODES}
    config["W06"] = "sha256:" + "1" * 64
    contract = build_authorized_identity_rebind_contract(
        product_release=_release(),
        thin_client_runtime_build=THIN_CLIENT_RUNTIME_BUILD_0_4,
        config_artifact_identities=config,
    )
    assert contract.payload_for("W06")["config_artifact_identity"] == config["W06"]
    incomplete = dict(config)
    incomplete.pop("W01")
    with pytest.raises(
        AuthorizedIdentityRebindError, match="REBIND_CONFIG_ARTIFACT_SET_MISMATCH"
    ):
        build_authorized_identity_rebind_contract(
            product_release=_release(),
            thin_client_runtime_build=THIN_CLIENT_RUNTIME_BUILD_0_4,
            config_artifact_identities=incomplete,
        )

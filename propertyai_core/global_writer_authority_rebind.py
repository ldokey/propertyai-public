"""Deterministic non-Production contract for W01-W06 authority rebind payloads.

This module does not write authority files. It freezes the exact payload shape
needed by the existing Global Writer runtime identity trust path so a later
Production controller can materialize W01-W06 only after a FINAL_ACCEPTED Product
release identity is known.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping


REBIND_CONTRACT_FORMAT = "PROPERTYAI_GLOBAL_WRITER_AUTHORIZED_IDENTITY_REBIND_V1"
AUTHORIZED_IDENTITY_SCHEMA_VERSION = 2

WRITER_SERVICE_CODES: dict[str, str] = {
    "W01": "PROPERTYAI_W01_GMAIL_INGEST_PROJECTION",
    "W02": "PROPERTYAI_W02_OPS_TELEGRAM_MUTATION",
    "W03": "PROPERTYAI_W03_CLEANER_TELEGRAM_MUTATION",
    "W04": "PROPERTYAI_W04_CLEANING_OPERATIONS_DISPATCH",
    "W05": "PROPERTYAI_W05_CLEANING_COMPLETION_DISPATCH",
    "W06": "PROPERTYAI_W06_HEALTH_RECOVERY_MAINTENANCE",
}

THIN_CLIENT_RUNTIME_BUILD_0_3 = (
    "adcp-global-writer-client@0.3.0+geb06cb8a8c4f"
    "|source=eb06cb8a8c4f6a0a1534b9bd9d5c49cec54fe79c"
    "|artifact=source-commit:eb06cb8a8c4f6a0a1534b9bd9d5c49cec54fe79c"
)
THIN_CLIENT_RUNTIME_BUILD_0_4 = (
    "adcp-global-writer-client@0.4.0+g23ce586dd369"
    "|source=23ce586dd369a60ac7bbbd24b33175deb05a402d"
    "|artifact=source-commit:23ce586dd369a60ac7bbbd24b33175deb05a402d"
)
THIN_CLIENT_RUNTIME_BUILD_0_5 = (
    "adcp-global-writer-client@0.5.0+g4e3bd2691b2e"
    "|source=4e3bd2691b2ed58eccb65c32ad127467c89a6ebb"
    "|artifact=source-commit:4e3bd2691b2ed58eccb65c32ad127467c89a6ebb"
)
ALLOWED_THIN_CLIENT_RUNTIME_BUILDS = frozenset(
    (
        THIN_CLIENT_RUNTIME_BUILD_0_3,
        THIN_CLIENT_RUNTIME_BUILD_0_4,
        THIN_CLIENT_RUNTIME_BUILD_0_5,
    )
)

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA64 = re.compile(r"^[0-9a-f]{64}$")


class AuthorizedIdentityRebindError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


def _fail(code: str, detail: str = "") -> None:
    raise AuthorizedIdentityRebindError(code, detail)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_identity(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_artifact_identity(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("REBIND_ARTIFACT_IDENTITY_INVALID", field)
    if value.startswith("source-commit:") and _SHA40.fullmatch(value[14:]):
        return value
    if value.startswith("sha256:") and _SHA64.fullmatch(value[7:]):
        return value
    if value.startswith("wheel-sha256:") and _SHA64.fullmatch(value[13:]):
        return value
    _fail("REBIND_ARTIFACT_IDENTITY_INVALID", field)


@dataclass(frozen=True)
class FinalAcceptedProductReleaseIdentity:
    """Exact Product release identity supplied only after Product acceptance."""

    product_build_commit: str
    product_build_identity: str
    source_root_or_artifact_identity: str

    def __post_init__(self) -> None:
        commit = self.product_build_commit
        if not isinstance(commit, str) or _SHA40.fullmatch(commit) is None:
            _fail("REBIND_PRODUCT_COMMIT_INVALID")
        expected_artifact = f"source-commit:{commit}"
        if self.source_root_or_artifact_identity != expected_artifact:
            _fail("REBIND_PRODUCT_ARTIFACT_MISMATCH")
        expected_identity = (
            f"product:PropertyAI@g{commit[:12]}|source={commit}|artifact={expected_artifact}"
        )
        if self.product_build_identity != expected_identity:
            _fail("REBIND_PRODUCT_BUILD_IDENTITY_MISMATCH")

    def as_dict(self) -> dict[str, str]:
        return {
            "product_build_commit": self.product_build_commit,
            "product_build_identity": self.product_build_identity,
            "source_root_or_artifact_identity": self.source_root_or_artifact_identity,
        }


@dataclass(frozen=True)
class AuthorizedIdentityRebindContract:
    """Frozen W01-W06 payload set for one Product release / Thin pair."""

    product_release: FinalAcceptedProductReleaseIdentity
    thin_client_runtime_build: str
    config_artifact_identities: tuple[tuple[str, str | None], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.product_release, FinalAcceptedProductReleaseIdentity):
            _fail("REBIND_PRODUCT_RELEASE_INVALID")
        if self.thin_client_runtime_build not in ALLOWED_THIN_CLIENT_RUNTIME_BUILDS:
            _fail("REBIND_THIN_CLIENT_BUILD_UNSUPPORTED")
        if not isinstance(self.config_artifact_identities, tuple):
            _fail("REBIND_CONFIG_ARTIFACT_MAP_INVALID")
        if len(self.config_artifact_identities) != len(WRITER_SERVICE_CODES):
            _fail("REBIND_CONFIG_ARTIFACT_SET_MISMATCH")
        config = dict(self.config_artifact_identities)
        if len(config) != len(self.config_artifact_identities) or set(config) != set(WRITER_SERVICE_CODES):
            _fail("REBIND_CONFIG_ARTIFACT_SET_MISMATCH")
        for writer, value in config.items():
            _validate_artifact_identity(value, f"{writer}.config_artifact_identity")

    def _config_map(self) -> dict[str, str | None]:
        return dict(self.config_artifact_identities)

    def payload_for(self, writer_code: str) -> dict[str, Any]:
        try:
            service_code = WRITER_SERVICE_CODES[writer_code]
            config_identity = self._config_map()[writer_code]
        except KeyError as error:
            raise AuthorizedIdentityRebindError(
                "REBIND_WRITER_CODE_UNSUPPORTED", writer_code
            ) from error
        release = self.product_release
        return {
            "service_code": service_code,
            "product_build_commit": release.product_build_commit,
            "product_build_identity": release.product_build_identity,
            "global_writer_client_build": self.thin_client_runtime_build,
            "source_root_or_artifact_identity": release.source_root_or_artifact_identity,
            "config_artifact_identity": config_identity,
            "authorized_at": None,
            "schema_version": AUTHORIZED_IDENTITY_SCHEMA_VERSION,
        }

    def payloads(self) -> dict[str, dict[str, Any]]:
        return {writer: self.payload_for(writer) for writer in WRITER_SERVICE_CODES}

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": REBIND_CONTRACT_FORMAT,
            "definition_identity": REBIND_CONTRACT_DEFINITION_IDENTITY,
            "product_release": self.product_release.as_dict(),
            "thin_client_runtime_build": self.thin_client_runtime_build,
            "payloads": self.payloads(),
        }

    @property
    def contract_identity(self) -> str:
        return _sha256_identity(self.as_dict())


_REBIND_CONTRACT_DEFINITION = {
    "format": REBIND_CONTRACT_FORMAT,
    "authorized_identity_schema_version": AUTHORIZED_IDENTITY_SCHEMA_VERSION,
    "writer_service_codes": WRITER_SERVICE_CODES,
    "allowed_thin_client_runtime_builds": sorted(ALLOWED_THIN_CLIENT_RUNTIME_BUILDS),
    "product_release_binding": (
        "exact source-commit:<40hex> plus canonical "
        "product:PropertyAI@g<commit12>|source=<commit>|artifact=source-commit:<commit>"
    ),
}
REBIND_CONTRACT_DEFINITION_IDENTITY = _sha256_identity(_REBIND_CONTRACT_DEFINITION)


def build_authorized_identity_rebind_contract(
    *,
    product_release: FinalAcceptedProductReleaseIdentity,
    thin_client_runtime_build: str,
    config_artifact_identities: Mapping[str, str | None] | None = None,
) -> AuthorizedIdentityRebindContract:
    if not isinstance(product_release, FinalAcceptedProductReleaseIdentity):
        _fail("REBIND_PRODUCT_RELEASE_INVALID")
    if thin_client_runtime_build not in ALLOWED_THIN_CLIENT_RUNTIME_BUILDS:
        _fail("REBIND_THIN_CLIENT_BUILD_UNSUPPORTED")
    if config_artifact_identities is None:
        config = {writer: None for writer in WRITER_SERVICE_CODES}
    else:
        if not isinstance(config_artifact_identities, Mapping):
            _fail("REBIND_CONFIG_ARTIFACT_MAP_INVALID")
        config = dict(config_artifact_identities)
        if set(config) != set(WRITER_SERVICE_CODES):
            _fail("REBIND_CONFIG_ARTIFACT_SET_MISMATCH")
        for writer, value in config.items():
            config[writer] = _validate_artifact_identity(
                value, f"{writer}.config_artifact_identity"
            )
    return AuthorizedIdentityRebindContract(
        product_release=product_release,
        thin_client_runtime_build=thin_client_runtime_build,
        config_artifact_identities=tuple(
            (writer, config[writer]) for writer in WRITER_SERVICE_CODES
        ),
    )


__all__ = [
    "ALLOWED_THIN_CLIENT_RUNTIME_BUILDS",
    "AUTHORIZED_IDENTITY_SCHEMA_VERSION",
    "AuthorizedIdentityRebindContract",
    "AuthorizedIdentityRebindError",
    "FinalAcceptedProductReleaseIdentity",
    "REBIND_CONTRACT_DEFINITION_IDENTITY",
    "REBIND_CONTRACT_FORMAT",
    "THIN_CLIENT_RUNTIME_BUILD_0_3",
    "THIN_CLIENT_RUNTIME_BUILD_0_4",
    "THIN_CLIENT_RUNTIME_BUILD_0_5",
    "WRITER_SERVICE_CODES",
    "build_authorized_identity_rebind_contract",
]

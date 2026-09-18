"""PropertyAI integration boundary for the accepted Global Production Writer Thin Client.

This module intentionally imports only ``adcp-global-writer-client``.  ADCP Core is
not a PropertyAI dependency.  The Thin Client seals Product authority roots at
module import; deployment must therefore provide the authority environment before
starting a writer process.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping, Protocol

from adcp_global_writer_client import (
    MATCH,
    GlobalWriterLeaseClient,
    RuntimeIdentity,
    authorize_new_mutation,
    capture_runtime_identity,
    client_build_identity,
    write_runtime_identity,
)

CHANGE_ID = "GLOBAL-PRODUCTION-WRITER-LEASE-01B-B"

# TRANSITIONAL_LEGACY_BRIDGE: REMOVE_OR_REDESIGN_IN_JAVA_VNEXT.
# These are exact artifact identities, not a generic compatibility/version policy.
TRANSITIONAL_LEGACY_BRIDGE_PROFILE_LEGACY_0_1 = {
    "package_name": "adcp-global-writer-client",
    "version": "0.1.0",
    "build_id": "adcp-global-writer-client@0.1.0+g267d79e3122d",
    "source_commit": "267d79e3122da1eae0233e462326f8d096122d07",
    "artifact_identity": "source-commit:267d79e3122da1eae0233e462326f8d096122d07",
    "schema_contract_version": 6,
    "schema_contract_identity": "sha256:047dd3c4cb1449bd428c0c8519f2baf29e876a9a96e5427767a577f5d5be94dd",
}
TRANSITIONAL_LEGACY_BRIDGE_PROFILE_TRANSITION_0_2 = {
    "package_name": "adcp-global-writer-client",
    "version": "0.2.0",
    "build_id": "adcp-global-writer-client@0.2.0+gd535e00b8997",
    "source_commit": "d535e00b8997e470c45dddc9efb9e8c10e656dbe",
    "artifact_identity": "source-commit:d535e00b8997e470c45dddc9efb9e8c10e656dbe",
    "thin_contract_format_version": 2,
    "supported_dcs_schema_versions": (6, 7),
    "schema_contract_identity": "sha256:966387cc5ea17df6e6bdb89993c32f0c72d59fdaf33ebfaaba1b5e31e81a1e56",
}
TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_3 = {
    "package_name": "adcp-global-writer-client",
    "version": "0.3.0",
    "build_id": "adcp-global-writer-client@0.3.0+geb06cb8a8c4f",
    "source_commit": "eb06cb8a8c4f6a0a1534b9bd9d5c49cec54fe79c",
    "artifact_identity": "source-commit:eb06cb8a8c4f6a0a1534b9bd9d5c49cec54fe79c",
    "thin_contract_format_version": 2,
    "supported_dcs_schema_versions": (6, 7, 8),
    "schema_contract_identity": "sha256:b5601f47b0447e5ad47a7a1d5cee7d20692c96b5105b925ed71cd4ddfe98ba6b",
}
TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_4 = {
    "package_name": "adcp-global-writer-client",
    "version": "0.4.0",
    "build_id": "adcp-global-writer-client@0.4.0+g23ce586dd369",
    "source_commit": "23ce586dd369a60ac7bbbd24b33175deb05a402d",
    "artifact_identity": "source-commit:23ce586dd369a60ac7bbbd24b33175deb05a402d",
    "thin_contract_format_version": 2,
    "supported_dcs_schema_versions": (6, 7, 8, 9),
    "schema_contract_identity": "sha256:1deb066c05c1cfec1b5945e8e2b31bfd212eb80bd4021b0696ee1ee93364a0f6",
}
TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_5 = {
    "package_name": "adcp-global-writer-client",
    "version": "0.5.0",
    "build_id": "adcp-global-writer-client@0.5.0+g4e3bd2691b2e",
    "source_commit": "4e3bd2691b2ed58eccb65c32ad127467c89a6ebb",
    "artifact_identity": "source-commit:4e3bd2691b2ed58eccb65c32ad127467c89a6ebb",
    "thin_contract_format_version": 2,
    "supported_dcs_schema_versions": (6, 7, 8, 9, 10),
    "schema_contract_identity": "sha256:288a69e75d8bd4399bbac1973a8632d54a79c0052debec79636a184b715db2a5",
}

DEFAULT_TTL_SECONDS = 60
DEFAULT_HEARTBEAT_SECONDS = 15

DCS_PATH_ENV = "PROPERTYAI_GLOBAL_WRITER_DCS_PATH"
RUNTIME_IDENTITY_PATH_ENV = "PROPERTYAI_GLOBAL_WRITER_RUNTIME_IDENTITY_PATH"
AUTHORIZED_IDENTITY_PATH_ENV = "PROPERTYAI_GLOBAL_WRITER_AUTHORIZED_IDENTITY_PATH"
CONFIG_ARTIFACT_IDENTITY_ENV = "PROPERTYAI_GLOBAL_WRITER_CONFIG_ARTIFACT_IDENTITY"

_WRITER_SERVICES = {
    "W01": "PROPERTYAI_W01_GMAIL_INGEST_PROJECTION",
    "W02": "PROPERTYAI_W02_OPS_TELEGRAM_MUTATION",
    "W03": "PROPERTYAI_W03_CLEANER_TELEGRAM_MUTATION",
    "W04": "PROPERTYAI_W04_CLEANING_OPERATIONS_DISPATCH",
    "W05": "PROPERTYAI_W05_CLEANING_COMPLETION_DISPATCH",
    "W06": "PROPERTYAI_W06_HEALTH_RECOVERY_MAINTENANCE",
    "W07": "PROPERTYAI_W07_CORE_OUTBOX_REPLAY",
}


class ProductionWriterError(RuntimeError):
    """Fail-closed PropertyAI integration error."""


class LeaseClient(Protocol):
    def acquire(self, **kwargs: Any) -> dict[str, Any]: ...
    def assert_current(self, owner_id: str, fencing_token: int) -> dict[str, Any]: ...
    def heartbeat(self, owner_id: str, fencing_token: int, ttl_seconds: int = 60) -> dict[str, Any]: ...
    def release(self, **kwargs: Any) -> dict[str, Any]: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class WriterConfig:
    dcs_path: Path
    runtime_identity_path: Path
    authorized_identity_path: Path
    config_artifact_identity: str | None = None
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "WriterConfig":
        env = os.environ if environment is None else environment
        required: dict[str, Path] = {}
        for name in (DCS_PATH_ENV, RUNTIME_IDENTITY_PATH_ENV, AUTHORIZED_IDENTITY_PATH_ENV):
            raw = env.get(name)
            if not isinstance(raw, str) or not raw.strip():
                raise ProductionWriterError(f"GLOBAL_WRITER_CONFIG_MISSING:{name}")
            path = Path(raw).expanduser()
            if not path.is_absolute():
                raise ProductionWriterError(f"GLOBAL_WRITER_CONFIG_NOT_ABSOLUTE:{name}")
            required[name] = path.resolve()
        config_identity = env.get(CONFIG_ARTIFACT_IDENTITY_ENV)
        if config_identity is not None and (not config_identity or config_identity != config_identity.strip()):
            raise ProductionWriterError("GLOBAL_WRITER_CONFIG_ARTIFACT_INVALID")
        return cls(
            dcs_path=required[DCS_PATH_ENV],
            runtime_identity_path=required[RUNTIME_IDENTITY_PATH_ENV],
            authorized_identity_path=required[AUTHORIZED_IDENTITY_PATH_ENV],
            config_artifact_identity=config_identity,
        )


def _semantic_hash(value: Mapping[str, Any]) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _service_code(writer_code: str) -> str:
    try:
        return _WRITER_SERVICES[writer_code]
    except KeyError as error:
        raise ProductionWriterError(f"GLOBAL_WRITER_CODE_UNSUPPORTED:{writer_code}") from error


def _assert_accepted_thin_build() -> None:
    try:
        build = client_build_identity()
        identity = build.as_dict()
    except Exception as error:
        raise ProductionWriterError("GLOBAL_WRITER_THIN_IDENTITY_INVALID") from error

    if not isinstance(identity, dict):
        raise ProductionWriterError("GLOBAL_WRITER_THIN_IDENTITY_INVALID")
    if identity == TRANSITIONAL_LEGACY_BRIDGE_PROFILE_LEGACY_0_1:
        return
    if identity == TRANSITIONAL_LEGACY_BRIDGE_PROFILE_TRANSITION_0_2:
        return
    if identity == TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_3:
        return
    if identity == TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_4:
        return
    if identity == TRANSITIONAL_LEGACY_BRIDGE_PROFILE_ACCEPTED_0_5:
        return
    raise ProductionWriterError("GLOBAL_WRITER_THIN_IDENTITY_MISMATCH")


class GlobalProductionWriterCoordinator:
    """One process/service binding to the shared GLOBAL_PRODUCTION namespace."""

    def __init__(
        self,
        writer_code: str,
        *,
        config: WriterConfig | None = None,
        client_factory: Callable[[Path], LeaseClient] = GlobalWriterLeaseClient,
        authority_check: Callable[..., str] = authorize_new_mutation,
        capture_identity: Callable[..., RuntimeIdentity] = capture_runtime_identity,
        write_identity: Callable[[Path, RuntimeIdentity], None] = write_runtime_identity,
        verify_thin_build: Callable[[], None] = _assert_accepted_thin_build,
        start_heartbeat: bool = True,
    ) -> None:
        self.writer_code = writer_code
        self.service_code = _service_code(writer_code)
        self.config = config or WriterConfig.from_environment()
        self._client_factory = client_factory
        self._authority_check = authority_check
        self._start_heartbeat = start_heartbeat
        verify_thin_build()
        identity = capture_identity(
            service_code=self.service_code,
            config_artifact_identity=self.config.config_artifact_identity,
        )
        write_identity(self.config.runtime_identity_path, identity)
        self.runtime_identity = identity
        self._authorize()

    def _authorize(self) -> None:
        # Rechecked at every authorization, including cached coordinators. This
        # cannot grant authority; it only excludes retired Cleaner writer paths.
        from propertyai_core.runtime.cleaner_topology import assert_legacy_cleaner_writer_allowed
        try:
            assert_legacy_cleaner_writer_allowed(self.writer_code)
        except RuntimeError as error:
            raise ProductionWriterError(str(error)) from error
        try:
            status = self._authority_check(
                runtime_identity_path=self.config.runtime_identity_path,
                authorized_identity_path=self.config.authorized_identity_path,
            )
        except ProductionWriterError:
            raise
        except BaseException as error:
            raise ProductionWriterError("GLOBAL_WRITER_RUNTIME_AUTHORITY_INVALID") from error
        if status != MATCH:
            raise ProductionWriterError(f"GLOBAL_WRITER_RUNTIME_AUTHORITY_{status}")

    def mutation(
        self,
        *,
        unit_id: str,
        operation_class: str,
        target: str,
    ) -> "GlobalProductionWriterLease":
        if not isinstance(unit_id, str) or not unit_id:
            raise ProductionWriterError("GLOBAL_WRITER_UNIT_ID_REQUIRED")
        return GlobalProductionWriterLease(
            coordinator=self,
            unit_id=unit_id,
            operation_class=operation_class,
            target=target,
        )


_CURRENT_LEASE: contextvars.ContextVar["GlobalProductionWriterLease | None"] = contextvars.ContextVar(
    "propertyai_global_production_writer_lease", default=None
)
_COORDINATORS: dict[str, GlobalProductionWriterCoordinator] = {}
_COORDINATOR_LOCK = threading.Lock()


def coordinator_for(writer_code: str) -> GlobalProductionWriterCoordinator:
    with _COORDINATOR_LOCK:
        coordinator = _COORDINATORS.get(writer_code)
        if coordinator is None:
            coordinator = GlobalProductionWriterCoordinator(writer_code)
            _COORDINATORS[writer_code] = coordinator
        return coordinator


def publish_startup_runtime_identity(writer_code: str) -> RuntimeIdentity:
    """Publish and verify this process identity without acquiring a writer lease.

    This is an observational startup attestation only.  Coordinator construction
    captures the already startup-sealed Product/Thin authority, atomically writes
    the canonical runtime identity file, and verifies it against the authorized
    identity.  The DCS client is not constructed until a later mutation lease is
    entered, and that mutation path independently revalidates runtime authority.
    """

    return coordinator_for(writer_code).runtime_identity


def reset_coordinators_for_tests() -> None:
    """Clear process-local composition cache. Tests only; does not change authority."""
    with _COORDINATOR_LOCK:
        _COORDINATORS.clear()


class GlobalProductionWriterLease(ContextManager["GlobalProductionWriterLease"]):
    def __init__(
        self,
        *,
        coordinator: GlobalProductionWriterCoordinator,
        unit_id: str,
        operation_class: str,
        target: str,
    ) -> None:
        self.coordinator = coordinator
        self.unit_id = unit_id
        self.operation_class = operation_class
        self.target = target
        self.attempt_id = uuid.uuid4().hex
        self.owner_id = (
            f"{coordinator.service_code}:{coordinator.runtime_identity.pid}:"
            f"{coordinator.runtime_identity.process_incarnation_id[:16]}"
        )
        self.owner_execution_id = coordinator.runtime_identity.process_incarnation_id
        request_identity = {
            "change_id": CHANGE_ID,
            "writer_code": coordinator.writer_code,
            "service_code": coordinator.service_code,
            "unit_id": unit_id,
            "operation_class": operation_class,
            "target": target,
            "attempt_id": self.attempt_id,
        }
        self.acquire_operation_key = _semantic_hash({**request_identity, "verb": "ACQUIRE"})
        self.release_operation_key = _semantic_hash({**request_identity, "verb": "RELEASE"})
        self.client: LeaseClient | None = None
        self.fencing_token: int | None = None
        self._heartbeat_error: BaseException | None = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._context_token: contextvars.Token[GlobalProductionWriterLease | None] | None = None

    def __enter__(self) -> "GlobalProductionWriterLease":
        if _CURRENT_LEASE.get() is not None:
            raise ProductionWriterError("GLOBAL_WRITER_NESTED_LEASE_FORBIDDEN")
        # Authority is checked before opening the DCS write client. Any unknown,
        # stale, mismatched, malformed or wrong-artifact identity fails closed.
        self.coordinator._authorize()
        interval = self.coordinator.config.heartbeat_seconds
        if self.coordinator._start_heartbeat and (
            interval <= 0 or interval > 20 or interval >= self.coordinator.config.ttl_seconds
        ):
            raise ProductionWriterError("GLOBAL_WRITER_HEARTBEAT_POLICY_INVALID")
        client = self.coordinator._client_factory(self.coordinator.config.dcs_path)
        self.client = client
        try:
            acquired = client.acquire(
                operation_key=self.acquire_operation_key,
                owner_id=self.owner_id,
                owner_execution_id=self.owner_execution_id,
                change_id=CHANGE_ID,
                slice_id=self.coordinator.writer_code,
                writer_class=self.coordinator.writer_code,
                owner_session_role=self.coordinator.service_code,
                track="PHASE_0A_PROPERTYAI",
                repository_or_runtime="PropertyAI",
                operation_class=self.operation_class,
                target=self.target,
                ttl_seconds=self.coordinator.config.ttl_seconds,
            )
            token = acquired.get("fencing_token")
            if isinstance(token, bool) or not isinstance(token, int) or token <= 0:
                raise ProductionWriterError("GLOBAL_WRITER_ACQUIRE_INVALID_READBACK")
            self.fencing_token = token
            # Required post-acquisition source/runtime revalidation.
            self.coordinator._authorize()
            client.assert_current(self.owner_id, token)
            self._context_token = _CURRENT_LEASE.set(self)
            if self.coordinator._start_heartbeat:
                self._start_heartbeat_worker()
            return self
        except BaseException as error:
            if self._context_token is not None:
                _CURRENT_LEASE.reset(self._context_token)
                self._context_token = None
            # If acquisition succeeded but post-acquire authority failed, release
            # only the coordination lease; never continue into business mutation.
            if self.fencing_token is not None:
                try:
                    client.release(
                        operation_key=self.release_operation_key,
                        owner_id=self.owner_id,
                        fencing_token=self.fencing_token,
                        reason="GLOBAL_PRODUCTION_WRITER_POST_ACQUIRE_AUTHORITY_FAILED",
                    )
                except BaseException:
                    pass
            client.close()
            self.client = None
            if isinstance(error, ProductionWriterError):
                raise
            raise ProductionWriterError("GLOBAL_WRITER_ACQUIRE_OR_REVALIDATE_FAILED") from error

    def _start_heartbeat_worker(self) -> None:
        interval = self.coordinator.config.heartbeat_seconds

        def run() -> None:
            while not self._heartbeat_stop.wait(interval):
                heartbeat_client: LeaseClient | None = None
                try:
                    heartbeat_client = self.coordinator._client_factory(self.coordinator.config.dcs_path)
                    heartbeat_client.heartbeat(
                        self.owner_id,
                        self._require_token(),
                        self.coordinator.config.ttl_seconds,
                    )
                except BaseException as error:
                    self._heartbeat_error = error
                    return
                finally:
                    if heartbeat_client is not None:
                        heartbeat_client.close()

        self._heartbeat_thread = threading.Thread(
            target=run,
            name=f"global-writer-heartbeat-{self.coordinator.writer_code}",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _require_token(self) -> int:
        token = self.fencing_token
        if token is None:
            raise ProductionWriterError("GLOBAL_WRITER_NOT_ACQUIRED")
        return token

    def assert_current(self) -> dict[str, Any]:
        if self._heartbeat_error is not None:
            raise ProductionWriterError("GLOBAL_WRITER_HEARTBEAT_FAILED") from self._heartbeat_error
        self.coordinator._authorize()
        if self.client is None:
            raise ProductionWriterError("GLOBAL_WRITER_NOT_ACQUIRED")
        try:
            return self.client.assert_current(self.owner_id, self._require_token())
        except ProductionWriterError:
            raise
        except BaseException as error:
            raise ProductionWriterError("GLOBAL_WRITER_FENCING_ASSERT_FAILED") from error

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=max(1, self.coordinator.config.heartbeat_seconds + 1))
        if self._context_token is not None:
            _CURRENT_LEASE.reset(self._context_token)
            self._context_token = None
        client = self.client
        token = self.fencing_token
        self.client = None
        release_error: BaseException | None = None
        if client is not None:
            try:
                if token is not None:
                    client.release(
                        operation_key=self.release_operation_key,
                        owner_id=self.owner_id,
                        fencing_token=token,
                    )
            except BaseException as error:
                release_error = error
            finally:
                client.close()
        if release_error is not None and exc_type is None:
            raise ProductionWriterError("GLOBAL_WRITER_RELEASE_FAILED") from release_error
        return False


def current_lease(*, required: bool = True) -> GlobalProductionWriterLease | None:
    lease = _CURRENT_LEASE.get()
    if lease is None and required:
        raise ProductionWriterError("GLOBAL_WRITER_LEASE_REQUIRED")
    return lease


def assert_current_production_writer() -> dict[str, Any]:
    """Revalidate runtime authority and fencing immediately before an effect."""
    lease = current_lease(required=True)
    assert lease is not None
    return lease.assert_current()


def mutation_scope(
    writer_code: str,
    *,
    unit_id: str,
    operation_class: str,
    target: str,
) -> GlobalProductionWriterLease:
    return coordinator_for(writer_code).mutation(
        unit_id=unit_id,
        operation_class=operation_class,
        target=target,
    )

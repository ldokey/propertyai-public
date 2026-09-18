from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from adcp_global_writer_client import MATCH, RuntimeIdentity
from propertyai_core.global_writer import (
    GlobalProductionWriterCoordinator,
    ProductionWriterError,
    WriterConfig,
)


pytestmark = pytest.mark.real_global_writer
WRITERS = tuple(f"W{i:02d}" for i in range(1, 8))


def _identity(writer: str) -> RuntimeIdentity:
    return RuntimeIdentity(
        service_code=f"PROPERTYAI_{writer}",
        pid=12345,
        process_started_at="2026-08-29T00:00:00.000000+00:00",
        process_incarnation_id=(writer[-1] * 64),
        product_build_commit="49feecc08dc0e97359f9a645948a075c35426f99",
        product_build_identity="propertyai@test",
        global_writer_client_build="adcp-global-writer-client@0.1.0+g267d79e3122d",
        interpreter_or_executable="python3.12",
        source_root_or_artifact_identity="source-commit:49feecc08dc0e97359f9a645948a075c35426f99",
        identity_generated_at="2026-08-29T00:00:00.100000+00:00",
    )


class FakeClient:
    def __init__(self, *, acquire_error: Exception | None = None, fail_assert_number: int | None = None):
        self.acquire_error = acquire_error
        self.fail_assert_number = fail_assert_number
        self.assert_count = 0
        self.acquire_calls: list[dict] = []
        self.release_calls: list[dict] = []
        self.closed = False

    def acquire(self, **kwargs):
        self.acquire_calls.append(kwargs)
        if self.acquire_error is not None:
            raise self.acquire_error
        return {"fencing_token": 7}

    def assert_current(self, owner_id: str, fencing_token: int):
        self.assert_count += 1
        if self.fail_assert_number == self.assert_count:
            raise RuntimeError("stale-or-lost-fence")
        return {"owner_id": owner_id, "fencing_token": fencing_token}

    def heartbeat(self, owner_id: str, fencing_token: int, ttl_seconds: int = 60):
        return {"owner_id": owner_id, "fencing_token": fencing_token, "ttl_seconds": ttl_seconds}

    def release(self, **kwargs):
        self.release_calls.append(kwargs)
        return {"released": True}

    def close(self):
        self.closed = True


def _coordinator(tmp_path: Path, writer: str, client: FakeClient, *, authority=None):
    config = WriterConfig(
        dcs_path=tmp_path / "control.sqlite3",
        runtime_identity_path=tmp_path / f"{writer}.runtime.json",
        authorized_identity_path=tmp_path / f"{writer}.authorized.json",
        ttl_seconds=60,
        heartbeat_seconds=15,
    )
    identity = _identity(writer)
    checks = authority if authority is not None else (lambda **_kwargs: MATCH)
    return GlobalProductionWriterCoordinator(
        writer,
        config=config,
        client_factory=lambda _path: client,
        authority_check=checks,
        capture_identity=lambda **_kwargs: identity,
        write_identity=lambda _path, _identity: None,
        verify_thin_build=lambda: None,
        start_heartbeat=False,
    )


@pytest.mark.parametrize("writer", WRITERS)
def test_each_writer_normal_success_is_bounded_and_releases(tmp_path: Path, writer: str):
    client = FakeClient()
    coordinator = _coordinator(tmp_path, writer, client)
    with coordinator.mutation(unit_id="one-item", operation_class="TEST", target="one-target") as lease:
        lease.assert_current()
    assert len(client.acquire_calls) == 1
    assert client.acquire_calls[0]["writer_class"] == writer
    assert client.acquire_calls[0]["ttl_seconds"] == 60
    assert len(client.release_calls) == 1
    assert client.closed is True


@pytest.mark.parametrize("writer", WRITERS)
def test_each_writer_lease_unavailable_fails_before_body(tmp_path: Path, writer: str):
    client = FakeClient(acquire_error=RuntimeError("lease-unavailable"))
    coordinator = _coordinator(tmp_path, writer, client)
    body_ran = False
    with pytest.raises(ProductionWriterError, match="ACQUIRE_OR_REVALIDATE_FAILED"):
        with coordinator.mutation(unit_id="one-item", operation_class="TEST", target="one-target"):
            body_ran = True
    assert body_ran is False
    assert client.release_calls == []


@pytest.mark.parametrize("writer", WRITERS)
def test_each_writer_post_acquire_authority_mismatch_releases_without_body(tmp_path: Path, writer: str):
    client = FakeClient()
    answers = iter((MATCH, MATCH, "MISMATCH"))
    coordinator = _coordinator(tmp_path, writer, client, authority=lambda **_kwargs: next(answers))
    body_ran = False
    with pytest.raises(ProductionWriterError, match="RUNTIME_AUTHORITY_MISMATCH"):
        with coordinator.mutation(unit_id="one-item", operation_class="TEST", target="one-target"):
            body_ran = True
    assert body_ran is False
    assert len(client.release_calls) == 1


@pytest.mark.parametrize("writer", WRITERS)
def test_each_writer_stale_owner_assert_blocks_external_call(tmp_path: Path, writer: str):
    # __enter__ performs assert #1. The required pre-effect assert is #2.
    client = FakeClient(fail_assert_number=2)
    coordinator = _coordinator(tmp_path, writer, client)
    external_calls = 0
    with coordinator.mutation(unit_id="one-item", operation_class="TEST", target="one-target") as lease:
        with pytest.raises(ProductionWriterError, match="FENCING_ASSERT_FAILED"):
            lease.assert_current()
            external_calls += 1
    assert external_calls == 0
    assert len(client.release_calls) == 1


@pytest.mark.parametrize("writer", WRITERS)
def test_each_writer_external_exception_still_releases(tmp_path: Path, writer: str):
    client = FakeClient()
    coordinator = _coordinator(tmp_path, writer, client)
    with pytest.raises(RuntimeError, match="external-failure"):
        with coordinator.mutation(unit_id="one-item", operation_class="TEST", target="one-target") as lease:
            lease.assert_current()
            raise RuntimeError("external-failure")
    assert len(client.release_calls) == 1


def test_heartbeat_contract_is_60s_ttl_and_15s_target(tmp_path: Path):
    client = FakeClient()
    coordinator = _coordinator(tmp_path, "W01", client)
    assert coordinator.config.ttl_seconds == 60
    assert coordinator.config.heartbeat_seconds == 15
    assert coordinator.config.heartbeat_seconds <= 20
    assert coordinator.config.heartbeat_seconds < coordinator.config.ttl_seconds


def test_startup_coordinator_publication_is_observational_and_future_authority_is_fresh(tmp_path: Path):
    client_factory_calls = []
    writes = []
    authority = iter((MATCH, "MISMATCH"))
    config = WriterConfig(
        dcs_path=tmp_path / "must-not-open.sqlite3",
        runtime_identity_path=tmp_path / "runtime.json",
        authorized_identity_path=tmp_path / "authorized.json",
        ttl_seconds=60,
        heartbeat_seconds=15,
    )
    identity = _identity("W02")
    coordinator = GlobalProductionWriterCoordinator(
        "W02",
        config=config,
        client_factory=lambda path: client_factory_calls.append(path),
        authority_check=lambda **_kwargs: next(authority),
        capture_identity=lambda **_kwargs: identity,
        write_identity=lambda path, value: writes.append((path, value)),
        verify_thin_build=lambda: None,
        start_heartbeat=False,
    )

    # Construction is the startup proof: publication + MATCH only, no DCS client/lease.
    assert coordinator.runtime_identity is identity
    assert writes == [(config.runtime_identity_path, identity)]
    assert client_factory_calls == []

    # Startup MATCH is not future mutation authority. The next mutation rechecks
    # authority before constructing/opening the DCS client.
    body_ran = False
    with pytest.raises(ProductionWriterError, match="RUNTIME_AUTHORITY_MISMATCH"):
        with coordinator.mutation(unit_id="drift", operation_class="TEST", target="none"):
            body_ran = True
    assert body_ran is False
    assert client_factory_calls == []

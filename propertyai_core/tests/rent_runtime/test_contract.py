from __future__ import annotations
import copy
import io
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from propertyai_core.rent_runtime.artifact import build, git, included, materialize, verify
from propertyai_core.rent_runtime.common import LocalTarget, OperationalError, canonical, checked_file, clean_env, digest, observe, write_new
from propertyai_core.rent_runtime.web import load_config
from propertyai_core.tests.fixture_lifecycle import FixtureLifecycle
from propertyai_core.tests.postgres_stage_a_cluster import _postgres_bin
from .helpers import BASE, ROOT, evidence, record


def test_artifact_manifest_source_tree_hash_lock_and_reproducibility(package):
    r, m = package["receipt"], package["manifest"]
    again = build(ROOT, evidence() / "reproducibility", **package["source"])
    assert r["artifact_sha256"] == again["artifact_sha256"]
    assert r["manifest_sha256"] == again["manifest_sha256"]
    assert r["source_tree"] == (os.environ.get("W3B_SOURCE_TREE") or git(ROOT, "rev-parse", os.environ["W3B_SOURCE_COMMIT"] + "^{tree}").decode().strip())
    if os.environ.get("W3B_SOURCE_COMMIT"):
        assert r["source_commit"] == os.environ["W3B_SOURCE_COMMIT"] and r["binding_kind"] == "EXACT_COMMIT"
    else:
        assert r["source_commit"] is None and r["binding_kind"] == "CANDIDATE_TREE_ONLY"
    assert m["dependency_lock"]["sha256"] == digest(git(ROOT, "show", r["source_tree"] + ":uv.lock"))
    assert len(m["migrations"]) == 12 and m["migrations"][-1]["version"] == "20260904.112"
    assert "propertyai_core/web/empty_shell.html" in m["static_templates"]
    assert "propertyai_core/web/rent_operations.html" in m["static_templates"]
    worker = m["roles"]["WORKER"]
    assert worker["entrypoint"] == ["python", "-m", "propertyai_core.rent_runtime", "worker"]
    assert worker["callable"] == "propertyai_core.rent_recurring_billing.run_recurring_billing_once"
    assert worker["callable_version"] == 1 and worker["binding"] == "I3_EXACT_W3_A"
    assert worker["source_checkpoint"] == "7fe5e7479bfd862a60ae4ef4f81490ede407481d"
    assert worker["scheduler_default"] == m["scheduler_default"] == "OFF"
    assert worker["automatic_scheduler_activated"] is False and worker["timeout_seconds"] == 60
    assert m["integration_sources"] == {"common_i2_base": BASE,
                                         "w3_a_checkpoint": "7fe5e7479bfd862a60ae4ef4f81490ede407481d",
                                         "w3_b_checkpoint": "41ed5e936f1ec5e52204de4f1e6038e9db2004d5",
                                         "w3_a_blob_parity": "EXACT_6_OF_6"}
    assert m["roles"]["MIGRATOR"]["db_login"] == "propertyai_flyway"
    assert all("tests" not in Path(r["path"]).parts and "fixtures" not in r["path"] for r in m["files"])
    assert not any(included(p) for p in (".env", "config/secrets.json", "propertyai_core/tests/fixture.sql"))
    record("artifact-contract.json", {"result": "PASS", "reproducible": True, **r})


def test_materialized_tamper_and_archive_identity_fail_closed(package):
    path = package["root"] / "propertyai_core/web/empty_shell.html"
    saved = checked_file(path)
    try:
        path.write_bytes(saved + b"\nTAMPER")
        with pytest.raises(OperationalError):
            verify(package["root"], package["receipt"]["manifest_sha256"])
    finally:
        path.write_bytes(saved)
    verify(package["root"], package["receipt"]["manifest_sha256"])
    injected = package["root"] / "propertyai_core/tests"
    injected.mkdir()
    canary = injected / "fixture.py"
    try:
        canary.write_text("# forbidden fixture injection")
        with pytest.raises(OperationalError):
            verify(package["root"], package["receipt"]["manifest_sha256"])
    finally:
        canary.unlink()
        injected.rmdir()
    with pytest.raises(OperationalError):
        materialize(evidence() / "artifact/runtime.tar", "0" * 64, evidence() / "must-not-exist", package["receipt"]["manifest_sha256"])
    assert not (evidence() / "must-not-exist").exists()


def test_config_fail_closed_secrets_environment_fixture_public_scheduler_boundary():
    owner = FixtureLifecycle.create()
    try:
        raw = {"environment": "ISOLATED_TEST", "target_root": str(owner.root),
               "target_marker_sha256": digest(checked_file(owner.root / ".propertyai-stage-a-owned.json")),
               "pg_port": 12345, "web_login": "rent_test_unit", "session_login": "rent_session_unit",
               "principal": {"organization_id": str(uuid4()), "actor_party_id": str(uuid4()), "subject": "synthetic:unit", "capabilities": ["READ"]},
               "bind_host": "127.0.0.1", "bind_port": 0, "scheduler": "OFF", "load_fixtures": False}
        valid = owner.root / "config.json"
        write_new(valid, canonical(raw))
        assert load_config(valid, digest(canonical(raw))).port == 0
        invalids = [("environment", "PRODUCTION"), ("bind_host", "0.0.0.0"), ("scheduler", "ON"),
                    ("load_fixtures", True), ("password", "SYNTHETIC_SECRET_CANARY"), ("web_login", "postgres"),
                    ("target_root", "/Users/kate/Production"), ("principal", {})]
        for index, (key, value) in enumerate(invalids):
            bad = copy.deepcopy(raw)
            bad[key] = value
            path = owner.root / f"invalid-{index}.json"
            write_new(path, canonical(bad))
            with pytest.raises((OperationalError, KeyError, ValueError, FileNotFoundError)):
                load_config(path, digest(canonical(bad)))
        with pytest.raises(OperationalError):
            load_config(valid, "0" * 64)
        valid.chmod(0o644)
        with pytest.raises(OperationalError):
            load_config(valid, digest(canonical(raw)))
    finally:
        cleanup = owner.finish(_postgres_bin("pg_ctl"))
        assert cleanup["classification"] == "PASS" and cleanup["root_absent"]
        record("config-boundary-cleanup.json", cleanup)


def test_structured_logs_closed_allowlist_redacts_all_untrusted_fields():
    stream = io.StringIO()
    canary = "SYNTHETIC_PASSWORD_BEARER_SESSION_ACCOUNT_PII"
    observe(canary, canary, error_class=canary, correlation_id=canary, stream=stream,
            password=canary, session=canary, account=canary, exception=ValueError(canary))
    assert canary not in stream.getvalue()
    event = json.loads(stream.getvalue())
    assert event["result_class"] == event["error_class"] == "UNKNOWN"
    assert set(event) == {"timestamp", "process_role", "correlation_id", "result_class", "error_class"}


def test_worker_bound_contract_refuses_missing_config_without_activation(package):
    result = subprocess.run([str(package["python"]), "-m", "propertyai_core.rent_runtime", "worker"],
                            cwd=package["root"], env=clean_env(), capture_output=True, timeout=10)
    assert result.returncode == 78 and result.stderr == b""
    payload = json.loads(result.stdout)
    assert payload["result_class"] == "CONFIG_ERROR" and payload["worker_result"] is None
    assert payload["worker_entrypoint"] == "propertyai_core.rent_recurring_billing.run_recurring_billing_once"
    assert payload["scheduler_default"] == "OFF" and payload["automatic_scheduler_activated"] is False
    result = subprocess.run([str(package["python"]), "-m", "propertyai_core.rent_runtime", "web"],
                            cwd=package["root"], env=clean_env(), capture_output=True, timeout=10)
    assert result.returncode == 78 and result.stderr == b""
    record("bound-worker-missing-config.json", {"worker": "I3_EXACT_W3_A", "scheduler": "OFF",
                                                "missing_config_exit": 78, "result": "PASS"})


def test_constraint_equivalence_is_exact_and_does_not_hide_real_drift():
    from propertyai_core.rent_runtime.recovery import constraint_fact, _AUTH_SUBJECT_GROUPED, _AUTH_SUBJECT_FLAT
    expected = constraint_fact("propertyai.rent_auth_session", "ck_rent_auth_subject", True, _AUTH_SUBJECT_FLAT)
    assert constraint_fact("propertyai.rent_auth_session", "ck_rent_auth_subject", True, _AUTH_SUBJECT_GROUPED) == expected
    assert constraint_fact("propertyai.rent_auth_session", "ck_rent_auth_subject", False, _AUTH_SUBJECT_GROUPED) != expected
    assert constraint_fact("propertyai.rent_auth_session", "ck_rent_auth_subject", True, _AUTH_SUBJECT_GROUPED.replace("512", "513")) != expected
    assert constraint_fact("propertyai.other", "ck_rent_auth_subject", True, _AUTH_SUBJECT_GROUPED)[3] == _AUTH_SUBJECT_GROUPED

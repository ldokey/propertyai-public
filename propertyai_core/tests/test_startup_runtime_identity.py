from __future__ import annotations

import ast
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from adcp_global_writer_client import installed_global_writer_client_build

from gmail_ingest import poll_once
from health_monitor import health_monitor
from telegram_approval import cleaner_bot_runtime, ops_bot_runtime
from telegram_approval import send_cleaning_operations, send_due_completions


pytestmark = pytest.mark.real_global_writer
ROOT = Path(__file__).resolve().parents[2]
TEST_PRODUCT_COMMIT = "10f0466f4b08b5302d9010093b8f9d12c4301761"
SERVICE_CODES = {
    "W02": "PROPERTYAI_W02_OPS_TELEGRAM_MUTATION",
    "W03": "PROPERTYAI_W03_CLEANER_TELEGRAM_MUTATION",
}


def _product_identity(commit: str) -> tuple[str, str]:
    artifact = f"source-commit:{commit}"
    identity = f"product:PropertyAI@g{commit[:12]}|source={commit}|artifact={artifact}"
    return artifact, identity


def _write_product_identity(root: Path, commit: str) -> None:
    artifact, identity = _product_identity(commit)
    path = root / "propertyai_core" / "_global_writer_build_identity.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            (
                'PRODUCT_IDENTITY_MODULE = "propertyai_core._global_writer_build_identity"',
                'PRODUCT_NAME = "PropertyAI"',
                f'PRODUCT_BUILD_COMMIT = "{commit}"',
                f'SOURCE_ARTIFACT_IDENTITY = "{artifact}"',
                f'PRODUCT_BUILD_IDENTITY = "{identity}"',
                "",
            )
        )
    )


def _authorized_payload(service_code: str, commit: str) -> dict:
    artifact, identity = _product_identity(commit)
    return {
        "service_code": service_code,
        "product_build_commit": commit,
        "product_build_identity": identity,
        "global_writer_client_build": installed_global_writer_client_build(),
        "source_root_or_artifact_identity": artifact,
        "config_artifact_identity": None,
        "authorized_at": None,
        "schema_version": 2,
    }


def _runtime_payload(service_code: str, commit: str) -> dict:
    payload = _authorized_payload(service_code, commit)
    payload.pop("authorized_at")
    payload.update(
        {
            "pid": 999_999_999,
            "process_started_at": "2026-08-29T00:00:00.000000+00:00",
            "process_incarnation_id": "0" * 64,
            "interpreter_or_executable": sys.executable,
            "identity_generated_at": "2026-08-29T00:00:00.100000+00:00",
            "schema_version": 3,
        }
    )
    return payload


def _subprocess_environment(tmp_path: Path, runtime_path: Path, authorized_path: Path, dcs_path: Path) -> dict[str, str]:
    authority_root = tmp_path / "authorized-product"
    _write_product_identity(authority_root, TEST_PRODUCT_COMMIT)
    environment = os.environ.copy()
    environment.update(
        {
            "ADCP_GLOBAL_WRITER_AUTHORIZED_PRODUCT_ROOT": str(authority_root),
            "ADCP_GLOBAL_WRITER_EXPECTED_PRODUCT_COMMIT": TEST_PRODUCT_COMMIT,
            "PROPERTYAI_GLOBAL_WRITER_DCS_PATH": str(dcs_path),
            "PROPERTYAI_GLOBAL_WRITER_RUNTIME_IDENTITY_PATH": str(runtime_path),
            "PROPERTYAI_GLOBAL_WRITER_AUTHORIZED_IDENTITY_PATH": str(authorized_path),
            "PYTHONPATH": str(ROOT) + os.pathsep + environment.get("PYTHONPATH", ""),
        }
    )
    return environment


@pytest.mark.parametrize("writer", ("W02", "W03"))
def test_idle_startup_identity_is_current_observational_and_fresh_gate_survives_drift(tmp_path: Path, writer: str):
    service_code = SERVICE_CODES[writer]
    runtime_path = tmp_path / f"{writer}.runtime.json"
    authorized_path = tmp_path / f"{writer}.authorized.json"
    dcs_path = tmp_path / "control.sqlite3"
    offset_path = tmp_path / f"{writer}.telegram-state.json"
    offset_bytes = b'{"offset":41,"marker":"unchanged"}\n'
    offset_path.write_bytes(offset_bytes)

    # A structurally valid identity from another process must not authorize this process.
    runtime_path.write_text(json.dumps(_runtime_payload(service_code, TEST_PRODUCT_COMMIT)) + "\n")
    authorized_path.write_text(json.dumps(_authorized_payload(service_code, TEST_PRODUCT_COMMIT)) + "\n")

    drift_commit = "f" * 40
    drift_path = tmp_path / f"{writer}.drift-authorized.json"
    drift_path.write_text(json.dumps(_authorized_payload(service_code, drift_commit)) + "\n")
    env = _subprocess_environment(tmp_path, runtime_path, authorized_path, dcs_path)
    env["DRIFT_AUTH_PATH"] = str(drift_path)

    script = r'''
import json
import os
from pathlib import Path
from adcp_global_writer_client import RuntimeIdentityError, authorize_new_mutation

runtime_path = Path(os.environ["PROPERTYAI_GLOBAL_WRITER_RUNTIME_IDENTITY_PATH"])
authorized_path = Path(os.environ["PROPERTYAI_GLOBAL_WRITER_AUTHORIZED_IDENTITY_PATH"])
dcs_path = Path(os.environ["PROPERTYAI_GLOBAL_WRITER_DCS_PATH"])
try:
    authorize_new_mutation(runtime_identity_path=runtime_path, authorized_identity_path=authorized_path)
    stale_code = "UNEXPECTED_MATCH"
except RuntimeIdentityError as error:
    stale_code = error.code

from propertyai_core.global_writer import mutation_scope, publish_startup_runtime_identity
proof = publish_startup_runtime_identity(os.environ["TEST_WRITER"])
proof_file = json.loads(runtime_path.read_text())
Path(authorized_path).write_bytes(Path(os.environ["DRIFT_AUTH_PATH"]).read_bytes())
body_ran = False
try:
    with mutation_scope(
        os.environ["TEST_WRITER"],
        unit_id="post-startup-drift-attack",
        operation_class="TEST_DRIFT_ATTACK",
        target="must-not-open-dcs",
    ):
        body_ran = True
    drift_error = "UNEXPECTED_ALLOWED"
except Exception as error:
    drift_error = f"{type(error).__name__}:{error}"
print(json.dumps({
    "process_pid": os.getpid(),
    "stale_code": stale_code,
    "proof": proof.as_dict(),
    "proof_file": proof_file,
    "body_ran": body_ran,
    "drift_error": drift_error,
    "dcs_exists": dcs_path.exists(),
}, sort_keys=True))
'''
    env["TEST_WRITER"] = writer
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    result = json.loads(completed.stdout)
    proof = result["proof"]

    assert result["stale_code"] == "RUNTIME_IDENTITY_STALE"
    assert proof == result["proof_file"]
    assert proof["pid"] == result["process_pid"]
    assert re.fullmatch(r"[0-9a-f]{64}", proof["process_incarnation_id"])
    assert proof["product_build_commit"] == TEST_PRODUCT_COMMIT
    assert proof["source_root_or_artifact_identity"] == f"source-commit:{TEST_PRODUCT_COMMIT}"
    assert proof["global_writer_client_build"] == installed_global_writer_client_build()
    assert Path(proof["interpreter_or_executable"]).resolve() == Path(sys.executable).resolve()
    assert proof["service_code"] == service_code
    assert result["body_ran"] is False
    assert "GLOBAL_WRITER_RUNTIME_AUTHORITY_INVALID" in result["drift_error"]
    assert result["dcs_exists"] is False
    assert offset_path.read_bytes() == offset_bytes


@pytest.mark.parametrize("writer", ("W02", "W03"))
def test_startup_identity_mismatch_fails_closed_without_dcs_or_business_effect(tmp_path: Path, writer: str):
    service_code = SERVICE_CODES[writer]
    runtime_path = tmp_path / f"{writer}.runtime.json"
    authorized_path = tmp_path / f"{writer}.authorized.json"
    dcs_path = tmp_path / "control.sqlite3"
    sentinel = tmp_path / "business-mutation-count"
    sentinel.write_text("0")

    authorized_path.write_text(json.dumps(_authorized_payload(service_code, "e" * 40)) + "\n")
    env = _subprocess_environment(tmp_path, runtime_path, authorized_path, dcs_path)
    env["TEST_WRITER"] = writer
    script = r'''
import json
import os
from pathlib import Path
from propertyai_core.global_writer import publish_startup_runtime_identity
try:
    publish_startup_runtime_identity(os.environ["TEST_WRITER"])
    outcome = "UNEXPECTED_MATCH"
except Exception as error:
    outcome = f"{type(error).__name__}:{error}"
print(json.dumps({"outcome": outcome, "dcs_exists": Path(os.environ["PROPERTYAI_GLOBAL_WRITER_DCS_PATH"]).exists()}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    result = json.loads(completed.stdout)
    assert "GLOBAL_WRITER_RUNTIME_AUTHORITY_INVALID" in result["outcome"]
    assert result["dcs_exists"] is False
    assert sentinel.read_text() == "0"


@pytest.mark.parametrize(
    ("main_function", "writer"),
    (
        (poll_once.main, "W01"),
        (ops_bot_runtime.main, "W02"),
        (cleaner_bot_runtime.main, "W03"),
        (send_cleaning_operations.main, "W04"),
        (send_due_completions.main, "W05"),
        (health_monitor.main, "W06"),
    ),
)
def test_all_mutation_capable_writer_entrypoints_use_shared_startup_publication(main_function, writer: str):
    tree = ast.parse(inspect.getsource(main_function))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "publish_startup_runtime_identity"
    ]
    assert len(calls) == 1
    assert len(calls[0].args) == 1
    assert isinstance(calls[0].args[0], ast.Constant)
    assert calls[0].args[0].value == writer

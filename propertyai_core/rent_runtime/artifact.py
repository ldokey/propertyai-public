"""Deterministic Git-object packaging; candidate evidence is never labelled a commit."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from pathlib import Path, PurePosixPath
import platform
import re
import stat
import subprocess
import tarfile
import zlib

from .common import OperationalError, canonical, checked_file, digest, require, write_new
from .staging import (EXTERNAL_ACTIVATION, ISOLATED_TEST, PERSISTENT_STAGING,
                      RUNTIME_CONTRACT_VERSION, SUPPORTED_ENVIRONMENTS,
                      protected_reference, validate_persistent_common)

MANIFEST = "rent-runtime-manifest.json"
FLYWAY_ENTRY_SHA256 = "7aa02a1013d3b80a0fccdd7d342fa9816e54d0221111db4b08432db7cc705722"
FLYWAY_DISTRIBUTION_SHA256 = "6f0333dda5671e49cdb4ce7e9851d0cf3bf01323cf5b191c7f5c7fb0381a88f5"
I3_COMMON_BASE = "9d85d8c84e1756e174cec1836a2719cdf7a6ab4c"
W3_A_CHECKPOINT = "7fe5e7479bfd862a60ae4ef4f81490ede407481d"
W3_B_CHECKPOINT = "41ed5e936f1ec5e52204de4f1e6038e9db2004d5"
WORKER_CALLABLE = "propertyai_core.rent_recurring_billing.run_recurring_billing_once"
W3_A_PATHS = (
    "docs/rent/recurring_billing_worker_v1.md",
    "propertyai_core/adapters/postgres/recurring_rent.py",
    "propertyai_core/application/recurring_rent.py",
    "propertyai_core/rent_recurring_billing.py",
    "propertyai_core/tests/rent_billing_worker/test_recurring_postgres.py",
    "propertyai_core/tests/rent_billing_worker/test_recurring_rent.py",
)


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, timeout=30).stdout


def included(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if any(p in {"tests", "fixtures", "__pycache__"} or p.startswith(".") for p in parts):
        return False
    if path in {"pyproject.toml", "uv.lock"}:
        return True
    if path.startswith("propertyai_core/"):
        return path.endswith(".py") or (path.startswith("propertyai_core/web/")
                                        and Path(path).suffix in {".html", ".css", ".js"})
    return path.startswith(("db/v2_2_1/bootstrap/", "db/v2_2_1/migration/", "db/v2_2_1/flyway/")) \
        and Path(path).suffix in {".sql", ".conf"}


def migration_checksum(data: bytes) -> int:
    checksum = 0
    for line in data.decode("utf-8-sig").splitlines():
        checksum = zlib.crc32(line.encode("utf-8"), checksum)
    return checksum if checksum < 2**31 else checksum - 2**32


def require_i3_sources(root: Path, tree: str) -> None:
    """Bind the integrated tree to the two frozen sibling checkpoints.

    W3-A is immutable through I3, so all six accepted blobs must remain byte-identical.
    W3-B runtime files are integration-owned after exact consumption, so its immutable
    identity is bound here without incorrectly claiming those files remain unchanged.
    """
    for checkpoint in (I3_COMMON_BASE, W3_A_CHECKPOINT, W3_B_CHECKPOINT):
        require(git(root, "cat-file", "-t", checkpoint).strip() == b"commit", "I3_SOURCE_IDENTITY_INVALID")
    require(git(root, "rev-parse", W3_A_CHECKPOINT + "^").decode().strip() == I3_COMMON_BASE,
            "I3_SOURCE_IDENTITY_INVALID")
    require(git(root, "rev-parse", W3_B_CHECKPOINT + "^").decode().strip() == I3_COMMON_BASE,
            "I3_SOURCE_IDENTITY_INVALID")
    for path in W3_A_PATHS:
        require(git(root, "rev-parse", tree + ":" + path).strip()
                == git(root, "rev-parse", W3_A_CHECKPOINT + ":" + path).strip(),
                "W3_A_BLOB_PARITY_INVALID")


def build(root: Path, destination: Path, *, commit: str | None = None,
          candidate_tree: str | None = None, base_commit: str | None = None) -> dict:
    require((commit is None) != (candidate_tree is None), "SOURCE_IDENTITY_REQUIRED")
    if commit is not None:
        require(re.fullmatch(r"[0-9a-f]{40}", commit) is not None, "SOURCE_IDENTITY_REQUIRED")
        require(git(root, "cat-file", "-t", commit).strip() == b"commit", "SOURCE_IDENTITY_REQUIRED")
        tree = git(root, "rev-parse", commit + "^{tree}").decode().strip()
        source = {"binding_kind": "EXACT_COMMIT", "source_commit": commit, "source_tree": tree}
    else:
        require(re.fullmatch(r"[0-9a-f]{40}", candidate_tree or "") is not None, "SOURCE_IDENTITY_REQUIRED")
        require(git(root, "cat-file", "-t", candidate_tree).strip() == b"tree", "SOURCE_IDENTITY_REQUIRED")
        require(git(root, "cat-file", "-t", base_commit or "").strip() == b"commit", "SOURCE_IDENTITY_REQUIRED")
        tree = candidate_tree
        source = {"binding_kind": "CANDIDATE_TREE_ONLY", "source_commit": None,
                  "source_tree": tree, "candidate_base_commit": base_commit}
    require_i3_sources(root, tree)
    files = {}
    for record in git(root, "ls-tree", "-rz", tree).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        path = raw_path.decode()
        if included(path):
            mode, kind, oid = metadata.decode().split()
            require(mode in {"100644", "100755"} and kind == "blob", "NON_REGULAR_SOURCE")
            files[path] = git(root, "cat-file", "blob", oid)
    require("propertyai_core/rent_runtime/__main__.py" in files and "uv.lock" in files, "PACKAGE_ENTRYPOINT_MISSING")
    records = [{"path": p, "bytes": len(b), "sha256": digest(b)} for p, b in sorted(files.items())]
    migrations = []
    for p, b in sorted(files.items()):
        if p.startswith("db/v2_2_1/migration/V"):
            migrations.append({"version": Path(p).name.split("__")[0][1:], "script": Path(p).name,
                               "checksum": migration_checksum(b), "sha256": digest(b)})
    require([m["version"] for m in migrations] == [f"20260904.{n}" for n in range(101, 113)], "MIGRATION_RANGE_INVALID")
    payload_hash = digest(canonical(records))
    manifest = {
        "format": RUNTIME_CONTRACT_VERSION, **source,
        "runtime_contract_version": "2.0",
        "supported_environments": list(SUPPORTED_ENVIRONMENTS),
        "production_supported": False,
        "integration_sources": {"common_i2_base": I3_COMMON_BASE,
                                "w3_a_checkpoint": W3_A_CHECKPOINT,
                                "w3_b_checkpoint": W3_B_CHECKPOINT,
                                "w3_a_blob_parity": "EXACT_6_OF_6"},
        "artifact_id": "rent-runtime-" + payload_hash, "payload_sha256": payload_hash,
        "identity_algorithm": "sha256(canonical-json(sorted per-file path,bytes,sha256)); manifest excluded",
        "canonical_json": "UTF-8, sorted keys, separators comma/colon, ensure_ascii=false, no newline",
        "python": platform.python_version(), "python_constraint": ">=3.12,<3.13",
        "dependency_lock": {"path": "uv.lock", "sha256": digest(files["uv.lock"]),
                            "install": ["uv", "sync", "--frozen", "--no-dev"]},
        "migrations": migrations,
        "roles": {
            "WEB": {"entrypoint": ["python", "-m", "propertyai_core.rent_runtime", "web"],
                    "required_args": ["--config", "--config-sha256", "--manifest-sha256", "--state"],
                    "stop": "SIGTERM to caller-owned process; wait <=10s", "readiness": "/health/ready",
                    "liveness": "/health/live", "start_timeout_seconds": 15, "stop_timeout_seconds": 10,
                    "exit": {"0": "STOPPED", "78": "FAILED_PRE_EFFECT_CONFIG_OR_PACKAGE"},
                    "db_roles": ["propertyai_rent_runtime", "propertyai_app_runtime"]},
            "WORKER": {"entrypoint": ["python", "-m", "propertyai_core.rent_runtime", "worker"],
                       "callable": WORKER_CALLABLE, "callable_version": 1,
                       "binding": "I3_EXACT_W3_A", "source_checkpoint": W3_A_CHECKPOINT,
                       "role": "RECURRING_BILLING_CALLER", "effect": "CREATE_ELIGIBLE_RECEIVABLE_ONLY",
                       "db_role": "propertyai_rent_scheduler", "scheduler_default": "OFF",
                       "automatic_scheduler_activated": False, "start": "EXPLICIT_SINGLE_RUN_ONLY",
                       "required_args": ["--config", "--config-sha256", "--manifest-sha256"],
                       "optional_args": ["--dry-run", "--run-id", "--after-file", "--after-sha256"],
                       "stop": "caller-owned process group; timeout <=60s; terminate on timeout; no daemon",
                       "readiness": "EXPLICIT_CONFIG_AND_PACKAGE_VALIDATION_PER_INVOCATION",
                       "timeout_seconds": 60,
                       "exit": {"0": "ZERO_TARGET_NOOP_OR_DRY_RUN_SUCCESS_OR_COMPLETED",
                                "3": "PARTIAL_SCAN_REQUIRES_CONTINUATION",
                                "4": "COMPLETED_WITH_CONTRACT_FAILURES",
                                "70": "RUNTIME_ERROR", "75": "UNKNOWN_EFFECT", "78": "CONFIG_ERROR"},
                       "result_contract": ["schema_version", "run_id", "request_id", "started_at", "ended_at",
                                           "dry_run", "scheduler_enabled", "automatic_scheduler_activated",
                                           "evaluated_count", "targeted_count", "created_count", "skipped_count",
                                           "failed_count", "unknown_count", "would_create_count", "recovered_count",
                                           "dispositions", "result_class", "process_reason", "scan_complete",
                                           "next_cursor"]},
            "MIGRATOR": {"entrypoint_ref": "APPROVED_FLYWAY_13_5_0", "db_login": "propertyai_flyway",
                         "effective_role": "propertyai_owner",
                         "start": "explicit target-bound invocation only; never Web startup",
                         "persistent_staging_binding": "EXTERNAL_FLYWAY_CONFIG_REFERENCE",
                         "stop": "terminate caller-owned process group on timeout", "timeout_seconds": 60,
                         "readiness": "exact binary hash and package SQL verified; validate exit 0",
                         "exit": {"0": "COMPLETED", "nonzero": "FAILED_UNKNOWN_EFFECT_VALIDATE_BEFORE_RETRY"},
                         "scheduler_default": "OFF"},
            "BACKUP": {"entrypoint": ["python", "-m", "propertyai_core.rent_runtime", "backup"],
                       "required_args": ["--config", "--config-sha256", "--manifest-sha256"],
                       "db_login": "propertyai_flyway", "effective_role": "propertyai_owner",
                       "persistent_staging_binding": "EXTERNAL_CANONICAL_HASHED_BACKUP_PROFILE",
                       "archive_format": "PG_CUSTOM_PROPERTYAI_SCHEMA",
                       "snapshot": "REPEATABLE_READ_EXPORTED_SNAPSHOT",
                       "overwrite": False, "prune": False,
                       "restore_validation": "SEPARATE_CLEAN_OWNED_DISPOSABLE_CLUSTER",
                       "start": "explicit target-bound invocation only; never Web startup",
                       "timeout_seconds": 60,
                       "exit": {"0": "COMPLETED", "70": "FAILED_PRE_EFFECT_OR_COMMAND",
                                "75": "FAILED_UNKNOWN_EFFECT", "78": "CONFIG_OR_PACKAGE_INVALID"},
                       "scheduler_default": "OFF"},
        },
        "flyway": {"version": "13.5.0", "entrypoint_sha256": FLYWAY_ENTRY_SHA256,
                   "distribution_manifest_sha256": FLYWAY_DISTRIBUTION_SHA256,
                   "config": "db/v2_2_1/flyway/flyway.conf", "artifact_external": True},
        "config_references": ["PROPERTYAI_RENT_CONFIG", "OWNED_DISPOSABLE_TARGET_MARKER",
                              "PERSISTENT_STAGING_ROLE_DSN_REFERENCES", "PERSISTENT_STAGING_FLYWAY_CONFIG_REFERENCE",
                              "PERSISTENT_STAGING_BACKUP_PROFILE", "APPROVED_FLYWAY_13_5_0",
                              "BACKUP_DESTINATION"],
        "configuration_identity": "external canonical config hash, separate from artifact and secret identity",
        "release_config_layout": {
            "artifact": "IMMUTABLE",
            "non_secret_config": "EXTERNAL_CANONICAL_HASHED",
            "secret_values": "EXTERNAL_PROTECTED_REFERENCES_ONLY",
            "target_identity": "EXPLICIT_TARGET_ID",
            "process_role": "EXPLICIT_PER_CONFIG",
        },
        "target_contract": {
            "ISOLATED_TEST": "OWNED_DISPOSABLE_LOCAL_TARGET",
            "PERSISTENT_STAGING": "EXPLICIT_EXTERNAL_POSTGRES_REFERENCES_NO_LIFECYCLE_AUTHORITY",
        },
        "auth_contract": {
            "ISOLATED_TEST": "SYNTHETIC_PRINCIPAL_ALLOWED",
            "PERSISTENT_STAGING": "PROVIDER_VERIFIED_SUBJECT_DIRECTORY_NON_SYNTHETIC",
        },
        "static_templates": [r["path"] for r in records if r["path"].endswith((".html", ".css", ".js"))],
        "excluded": ["secrets", "credentials", ".env", "test modules", "fixtures", "business data", "runtime config", "evidence"],
        "build_timestamp": {"authority": "NON_AUTHORITATIVE", "reference": "build-receipt.json#built_at_non_authoritative"},
        "external_activation": EXTERNAL_ACTIVATION, "real_business_data_expected": False,
        "fixture_loading": "ABSENT", "scheduler_default": "OFF", "files": records,
    }
    destination = Path(destination).resolve()
    destination.mkdir(mode=0o700, exist_ok=False)
    payload = {**files, MANIFEST: canonical(manifest)}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for p, b in sorted(payload.items()):
            member = tarfile.TarInfo(p)
            member.size, member.mode, member.mtime = len(b), 0o644, 0
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            archive.addfile(member, io.BytesIO(b))
    data = buffer.getvalue()
    write_new(destination / "runtime.tar", data)
    receipt = {**source, "artifact_id": manifest["artifact_id"], "artifact_sha256": digest(data),
               "manifest_sha256": digest(payload[MANIFEST]), "dependency_lock_sha256": digest(files["uv.lock"]),
               "payload_sha256": payload_hash, "file_count": len(records), "artifact_bytes": len(data),
               "built_at_non_authoritative": datetime.now(timezone.utc).isoformat()}
    write_new(destination / "build-receipt.json", canonical(receipt))
    return receipt


def verify(root: Path, manifest_sha256: str) -> dict:
    root = Path(root).resolve()
    manifest = json.loads(checked_file(root / MANIFEST, manifest_sha256))
    require(manifest["format"] == RUNTIME_CONTRACT_VERSION
            and manifest.get("supported_environments") == list(SUPPORTED_ENVIRONMENTS)
            and manifest.get("production_supported") is False
            and manifest.get("scheduler_default") == "OFF"
            and manifest.get("external_activation") == EXTERNAL_ACTIVATION,
            "PACKAGE_INVALID")
    require(manifest["python"] == platform.python_version() and platform.python_version_tuple()[:2] == ("3", "12"), "PYTHON_IDENTITY_MISMATCH")
    records = manifest["files"]
    require(records == sorted(records, key=lambda r: r["path"]) and len({r["path"] for r in records}) == len(records), "PACKAGE_INVALID")
    for rec in records:
        p = PurePosixPath(rec["path"])
        require(not p.is_absolute() and ".." not in p.parts and included(str(p)), "PACKAGE_INVALID")
        require(len(checked_file(root / p, rec["sha256"])) == rec["bytes"], "PACKAGE_INVALID")
    require(digest(canonical(records)) == manifest["payload_sha256"], "PACKAGE_INVALID")
    require(manifest["artifact_id"] == "rent-runtime-" + manifest["payload_sha256"], "PACKAGE_INVALID")
    checked_file(root / "uv.lock", manifest["dependency_lock"]["sha256"])
    actual = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] == ".venv" or "__pycache__" in relative.parts or str(relative) == MANIFEST:
            continue
        require(not path.is_symlink(), "PACKAGE_EXTRA_FILE")
        if path.is_file():
            actual.add(str(relative))
    require(actual == {r["path"] for r in records}, "PACKAGE_EXTRA_FILE")
    return manifest


def materialize(archive_path: Path, expected_sha256: str, destination: Path, manifest_sha256: str) -> dict:
    data = checked_file(Path(archive_path), expected_sha256)
    require(len(data) <= 256 * 1024 * 1024, "PACKAGE_TOO_LARGE")
    destination = Path(destination).resolve()
    destination.mkdir(mode=0o700, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        names = set()
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            require(member.isfile() and not path.is_absolute() and ".." not in path.parts
                    and member.name not in names and (included(member.name) or member.name == MANIFEST), "UNSAFE_ARCHIVE")
            names.add(member.name)
            out = destination / path
            out.parent.mkdir(parents=True, exist_ok=True)
            write_new(out, archive.extractfile(member).read())
    return verify(destination, manifest_sha256)


def migrator_plan(root: Path, manifest_sha256: str, flyway_root: Path, bridge_host: str, bridge_port: int,
                  operation: str) -> list[str]:
    """Plan only. Executor must own/attest the loopback -> exact disposable Unix bridge.

    No credentials, implicit discovery, network connection, or subprocess here.
    """
    verify(root, manifest_sha256)
    require(operation in {"migrate", "validate"} and bridge_host == "127.0.0.1"
            and type(bridge_port) is int and 1024 <= bridge_port <= 65535, "MIGRATOR_TARGET_INVALID")
    binary = Path(flyway_root) / "flyway"
    checked_file(binary, FLYWAY_ENTRY_SHA256)
    return [str(binary), "-configFiles=" + str(root / "db/v2_2_1/flyway/flyway.conf"),
            "-locations=filesystem:" + str(root / "db/v2_2_1/migration") + ",filesystem:" + str(root / "db/v2_2_1/flyway/callbacks"),
            f"-url=jdbc:postgresql://127.0.0.1:{bridge_port}/postgres?sslmode=disable",
            "-user=propertyai_flyway", "-password=", operation]


def persistent_migrator_plan(root: Path, manifest_sha256: str, flyway_root: Path,
                             config_path: Path, config_sha256: str, operation: str) -> list[str]:
    """Plan a PERSISTENT_STAGING migration using one explicit external Flyway config reference.

    The returned argv carries only reference paths. Connection URL/user/password remain in the
    protected external Flyway config and are never copied into source, artifact, or evidence.
    """
    verify(root, manifest_sha256)
    config_path = Path(config_path)
    require(config_path.is_absolute() and stat.S_IMODE(config_path.stat().st_mode) & 0o077 == 0,
            "MIGRATOR_TARGET_INVALID")
    raw = json.loads(checked_file(config_path, config_sha256))
    keys = {"environment", "target_id", "database_connection_ref", "runtime_role", "scheduler",
            "real_business_data_expected", "external_activation"}
    require(isinstance(raw, dict) and set(raw) == keys and operation in {"migrate", "validate"},
            "MIGRATOR_TARGET_INVALID")
    try:
        validate_persistent_common(raw, runtime_role="MIGRATOR")
        connection_ref = protected_reference(raw["database_connection_ref"])
    except Exception as error:
        raise OperationalError("MIGRATOR_TARGET_INVALID") from error
    binary = Path(flyway_root) / "flyway"
    checked_file(binary, FLYWAY_ENTRY_SHA256)
    return [
        str(binary),
        "-configFiles=" + str(root / "db/v2_2_1/flyway/flyway.conf") + "," + str(connection_ref),
        "-locations=filesystem:" + str(root / "db/v2_2_1/migration") + ",filesystem:" + str(root / "db/v2_2_1/flyway/callbacks"),
        operation,
    ]

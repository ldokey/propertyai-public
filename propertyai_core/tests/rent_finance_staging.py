"""Exact frozen Rent test inputs. This module never contacts a database.

The G1 V5 supplement overrides only its two named historical overlay hashes.
Staging uses accepted Git blobs, not a mutable checkout or a runtime directory.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
from typing import Any

PRODUCT_COMMIT = "33255216dc4e128f97a29da2b70cec332f616e56"
PRODUCT_TREE = "661459e76a7d0d94a7c6af9a06d57b360fe8c946"
CANDIDATE_ID = "rent-g1-contract-v5-1a16f24dfa83d59c3b0e5ecc"
CANDIDATE_MANIFEST_SHA256 = "e00cfe162217d0aceb7ae899d71c3136d7952879bc06becf86aeda640429548a"
BASE_CONTRACT_SHA256 = "324b4694a5c7272df3eecc53230727f04c0c03ec93e6a262f042250c1cbdf030"
SUPPLEMENT_SHA256 = "256aff08b7d67820d284857f9727a831c9aa760677721ab8367a1870eaeeec0f"
PROFILE_SUPPLEMENT_SHA256 = "ec959370046435687e6341a0f3ddf93fcf0f042e80fa7e63d905e285ad7b91c2"
PROFILE_ID = "rent.finance.g1.p1.v1"
FILE_FIELDS = {"phase", "ordinal", "version", "source_kind", "source_commit_or_candidate_id", "source_path", "staged_relative_path", "bytes", "sha256"}
PROTECTED_PATHS = ("propertyai_core/tests/postgres_stage_a_cluster.py", "propertyai_core/tests/fixture_lifecycle.py", "db/v2_2_1/flyway/flyway.conf")

class RentPrerequisiteError(RuntimeError):
    """A prerequisite failure is not a skipped or passing required test."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise RentPrerequisiteError(reason)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked_bytes(path: Path, expected: str | None = None, *, single_link: bool = False) -> bytes:
    path = Path(path)
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and not path.is_symlink(), "INPUT_NOT_REGULAR_FILE")
    if single_link:
        require(before.st_nlink == 1, "INPUT_HARDLINK_ALIAS")
    require(path.absolute() == path.resolve(), "INPUT_PATH_SYMLINK_ALIAS")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        data = stream.read()
        after = os.fstat(stream.fileno())
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns), "INPUT_CHANGED_DURING_READ")
    if expected is not None:
        require(sha256(data) == expected, "INPUT_SHA256_MISMATCH:" + path.name)
    return data


def git(root: Path, *args: str) -> bytes:
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home()), "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    return subprocess.run(["/usr/bin/git", "-C", str(root), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=env).stdout


def exact_blob(root: Path, path: str) -> bytes:
    require(not PurePosixPath(path).is_absolute() and ".." not in PurePosixPath(path).parts, "INVALID_GIT_INPUT_PATH")
    return git(root, "show", PRODUCT_COMMIT + ":" + path)


def frozen_contracts(candidate_root: Path) -> tuple[dict, dict, dict, dict]:
    candidate_root = Path(candidate_root).resolve()
    manifest = json.loads(checked_bytes(candidate_root / "candidate_manifest.json", CANDIDATE_MANIFEST_SHA256))
    require(manifest["candidate_id"] == CANDIDATE_ID, "CANDIDATE_ID_MISMATCH")
    require(sha256(canonical({k: manifest[k] for k in manifest["identity_payload_keys"]})) ==
            manifest["content_identity_sha256"] == "9fdcbbdcd16802840728ce41f7fc8582cfeb74f634e68d8464bc3ee3b538c0da", "CANDIDATE_CONTENT_MISMATCH")
    expected = {x["path"]: x for x in manifest["files"]}
    require(len(expected) == len(manifest["files"]) == 33, "CANDIDATE_FILE_COUNT")
    actual = {p.relative_to(candidate_root).as_posix() for p in (candidate_root / "candidate").rglob("*") if p.is_file()}
    require(actual == set(expected), "CANDIDATE_PATH_SET_MISMATCH")
    for rel, record in expected.items():
        data = checked_bytes(candidate_root / rel, record["sha256"])
        require(len(data) == record["bytes"], "CANDIDATE_LENGTH_MISMATCH")
    base = json.loads(checked_bytes(candidate_root / "candidate/test_contract/Finance_Test_Execution_Contract.json", BASE_CONTRACT_SHA256))
    supplement = json.loads(checked_bytes(candidate_root / "candidate/test_contract/Finance_Test_Execution_Supplement_v5.json", SUPPLEMENT_SHA256))
    profile = json.loads(checked_bytes(candidate_root / "candidate/test_contract/Rent_Required_Profile_Supplement_v5.json", PROFILE_SUPPLEMENT_SHA256))
    overrides = {x["path"]: x for x in supplement["SUCCESSOR_OVERLAY_HASH_OVERRIDES"]}
    require(len(overrides) == 2, "OVERLAY_OVERRIDE_COUNT")
    effective = {x["path"]: x for x in supplement["EFFECTIVE_STAGING_BINDINGS"]}
    require(len(effective) == 4 and set(effective) == set(supplement["EXPECTED_STAGING_PATH_SET"]), "OVERLAY_PATH_SET")
    for record in base["FINANCE_TEST_STAGING_MANIFEST"]["input_order"]:
        if record["source_kind"] != "EXACT_SUCCESSOR_FILE":
            continue
        rel = record["candidate_path"]
        digest = record["source_sha256"]
        if rel in overrides:
            require(digest == overrides[rel]["base_historical_sha256"], "OVERRIDE_PREDECESSOR_MISMATCH")
            digest = overrides[rel]["effective_sha256"]
        require(digest == effective[rel]["EFFECTIVE_STAGING_SHA256"] == expected[rel]["sha256"], "EFFECTIVE_OVERLAY_HASH_MISMATCH")
        require(effective[rel]["bytes"] == expected[rel]["bytes"], "EFFECTIVE_OVERLAY_BYTES_MISMATCH")
    return manifest, base, supplement, profile


def build_staging_manifest(*, product_root: Path, candidate_root: Path,
                           destination: Path, provenance_sha256: str,
                           scenario: str) -> tuple[Path, str]:
    require(scenario in {"FRESH", "UPGRADE_108"}, "UNKNOWN_SCENARIO")
    product_root, candidate_root, destination = Path(product_root), Path(candidate_root), Path(destination)
    require(not destination.exists() and not destination.is_symlink(), "STAGING_ROOT_ALREADY_EXISTS")
    require(git(product_root, "rev-parse", "HEAD").decode().strip() == PRODUCT_COMMIT, "PRODUCT_HEAD_DRIFT")
    require(git(product_root, "rev-parse", PRODUCT_COMMIT + "^{tree}").decode().strip() == PRODUCT_TREE, "PRODUCT_TREE_DRIFT")
    manifest, base, supplement, profile = frozen_contracts(candidate_root)
    identities = base["bindings"]
    for name in ("controller", "dx"):
        item = identities[name]
        observed = git(Path(item["root"]), "rev-parse", "HEAD", "HEAD^{tree}").decode().splitlines()
        require(observed == [item["head"], item["tree"]], name.upper() + "_SOURCE_BINDING_DRIFT")
    paths = git(product_root, "ls-tree", "-r", "--name-only", PRODUCT_COMMIT, "--", "db/v2_2_1").decode().splitlines()
    effective = {x["path"]: x for x in supplement["EFFECTIVE_STAGING_BINDINGS"]}
    records, payloads = [], {}
    for item in base["FINANCE_TEST_STAGING_MANIFEST"]["input_order"]:
        if item["source_kind"] == "EXACT_PRODUCT_GIT_OBJECT":
            matches = [p for p in paths if fnmatch.fnmatchcase(p, item["source_selector"])]
            require(len(matches) == 1, "BASELINE_SELECTOR_CARDINALITY")
            source_path = matches[0]
            data = exact_blob(product_root, source_path)
            if "expected_source_sha256" in item:
                require(sha256(data) == item["expected_source_sha256"], "BASELINE_SQL_SHA256_MISMATCH")
            source_id, rel = PRODUCT_COMMIT, source_path
        else:
            source_path = item["candidate_path"]
            bound = effective[source_path]
            data = checked_bytes(candidate_root / source_path, bound["EFFECTIVE_STAGING_SHA256"])
            require(len(data) == bound["bytes"], "OVERLAY_LENGTH_MISMATCH")
            source_id = CANDIDATE_ID
            rel = "db/v2_2_1/" + ("bootstrap/" if item["phase"] == "PRIVILEGED_BOOTSTRAP" else "migration/") + Path(source_path).name
            require(checked_bytes(product_root / rel) == data, "MATERIALIZED_SQL_DRIFT")
        records.append({"phase": item["phase"], "ordinal": item["ordinal"], "version": item["version"],
                        "source_kind": item["source_kind"], "source_commit_or_candidate_id": source_id,
                        "source_path": source_path, "staged_relative_path": rel,
                        "bytes": len(data), "sha256": sha256(data)})
        payloads[rel] = data
    config_path = "db/v2_2_1/flyway/flyway.conf"
    config = exact_blob(product_root, config_path)
    records.append({"phase": "CONFIG", "ordinal": 13, "version": None, "source_kind": "EXACT_PRODUCT_GIT_OBJECT",
                    "source_commit_or_candidate_id": PRODUCT_COMMIT, "source_path": config_path,
                    "staged_relative_path": config_path, "bytes": len(config), "sha256": sha256(config)})
    payloads[config_path] = config
    identities_by_path = {}
    for rel in PROTECTED_PATHS:
        data = exact_blob(product_root, rel)
        require(checked_bytes(product_root / rel) == data, "PROTECTED_BASELINE_SOURCE_MUTATED")
        identities_by_path[rel] = {"path": rel, "bytes": len(data), "sha256": sha256(data)}
    require(len(records) == len(payloads) == 14 and all(set(x) == FILE_FIELDS for x in records), "STAGING_RECORD_SHAPE")
    result = {"schema_version": 1, "change_id": base["bindings"].get("change_id", "PROPERTYAI-LONGSTAY-RENT-BATCH-01"),
              "profile_id": PROFILE_ID, "product_commit": PRODUCT_COMMIT, "product_tree": PRODUCT_TREE,
              "controller_commit": identities["controller"]["head"], "controller_tree": identities["controller"]["tree"],
              "dx_commit": identities["dx"]["head"], "dx_tree": identities["dx"]["tree"],
              "successor_candidate_id": CANDIDATE_ID, "successor_manifest_sha256": CANDIDATE_MANIFEST_SHA256,
              "test_contract_file_sha256": BASE_CONTRACT_SHA256, "test_contract_supplement_sha256": SUPPLEMENT_SHA256,
              "required_profile_supplement_sha256": PROFILE_SUPPLEMENT_SHA256,
              "baseline_harness_path_bytes_sha256": identities_by_path[PROTECTED_PATHS[0]],
              "fixture_lifecycle_path_bytes_sha256": identities_by_path[PROTECTED_PATHS[1]],
              "flyway_config_path_bytes_sha256": identities_by_path[PROTECTED_PATHS[2]],
              "ordered_inputs": records[:13], "ordered_staged_files": records,
              "staging_content_sha256": sha256(canonical(records)), "scenario": scenario,
              "flyway_provenance_sha256": provenance_sha256,
              "baseline_history_checksums": {x["version"]: x["expected_flyway_history_checksum"] for x in base["FINANCE_TEST_STAGING_MANIFEST"]["input_order"] if "expected_flyway_history_checksum" in x}}
    require(all(k in result for k in base["FINANCE_TEST_STAGING_MANIFEST"]["manifest_required_fields"]), "STAGING_REQUIRED_FIELD_MISSING")
    destination.mkdir(mode=0o700)
    for rel, data in payloads.items():
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(0o444)
    raw = canonical(result)
    output = destination / "finance_test_staging_manifest.json"
    with output.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    output.chmod(0o444)
    validate_staging_manifest(output, sha256(raw))
    return output, sha256(raw)


def validate_staging_manifest(path: Path, expected_sha256: str) -> dict:
    path = Path(path)
    raw = checked_bytes(path, expected_sha256, single_link=True)
    manifest = json.loads(raw)
    require(raw == canonical(manifest), "NONCANONICAL_STAGING_MANIFEST")
    require(manifest["profile_id"] == PROFILE_ID, "CLEANER_OR_UNKNOWN_PROFILE_SUBSTITUTION")
    require(manifest["product_commit"] == PRODUCT_COMMIT and manifest["product_tree"] == PRODUCT_TREE, "STAGING_PRODUCT_BINDING")
    require(manifest["successor_candidate_id"] == CANDIDATE_ID and manifest["successor_manifest_sha256"] == CANDIDATE_MANIFEST_SHA256, "STAGING_CANDIDATE_BINDING")
    require(manifest["test_contract_file_sha256"] == BASE_CONTRACT_SHA256 and manifest["test_contract_supplement_sha256"] == SUPPLEMENT_SHA256, "STAGING_CONTRACT_BINDING")
    require(manifest["scenario"] in {"FRESH", "UPGRADE_108"}, "UNKNOWN_SCENARIO")
    records = manifest["ordered_staged_files"]
    require(len(records) == 14 and [x["ordinal"] for x in records] == list(range(14)), "STAGING_ORDINALS")
    require(manifest["ordered_inputs"] == records[:13], "STAGING_INPUT_ORDER")
    versions = ["001", "002"] + ["20260904." + str(i) for i in range(101, 112)] + [None]
    require([x["version"] for x in records] == versions, "STAGING_VERSION_SET")
    require(sha256(canonical(records)) == manifest["staging_content_sha256"], "STAGING_CONTENT_HASH")
    expected_paths = set()
    for record in records:
        require(set(record) == FILE_FIELDS, "STAGING_FILE_RECORD_FIELDS")
        rel = PurePosixPath(record["staged_relative_path"])
        require(not rel.is_absolute() and ".." not in rel.parts and str(rel) == record["staged_relative_path"], "STAGING_PATH_ESCAPE")
        require(str(rel) not in expected_paths, "STAGING_DUPLICATE_PATH")
        expected_paths.add(str(rel))
        require(bool(record["source_commit_or_candidate_id"]), "MISSING_SOURCE_IDENTITY")
        try:
            data = checked_bytes(path.parent / str(rel), record["sha256"], single_link=True)
        except OSError as exc:
            raise RentPrerequisiteError("STAGING_MISSING_OR_UNREADABLE_FILE") from exc
        require(len(data) == record["bytes"], "STAGING_FILE_LENGTH")
    actual = set()
    for root, dirs, files in os.walk(path.parent, followlinks=False):
        for name in dirs:
            require(not (Path(root) / name).is_symlink(), "STAGING_DIRECTORY_ALIAS")
        for name in files:
            candidate = Path(root) / name
            if candidate == path:
                continue
            require(candidate.is_file() and not candidate.is_symlink(), "STAGING_SPECIAL_FILE")
            actual.add(candidate.relative_to(path.parent).as_posix())
    require(actual == expected_paths, "STAGING_MISSING_OR_EXTRA_FILES")
    return manifest

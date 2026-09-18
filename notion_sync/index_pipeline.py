#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
from pathlib import Path

try:
    from notion_sync.incremental_ingest import (
        CONFIG_PATH,
        DEFAULT_RUNTIME,
        atomic_json,
        canonical_hash,
        load_json,
        redact,
        safe_id,
        utc_now,
    )
except ModuleNotFoundError:
    from incremental_ingest import (
        CONFIG_PATH,
        DEFAULT_RUNTIME,
        atomic_json,
        canonical_hash,
        load_json,
        redact,
        safe_id,
        utc_now,
    )


def validation_path(runtime, source_id, digest):
    return Path(runtime) / "validated" / safe_id(source_id) / f"{digest}.json"


def staging_path(runtime, source_id, digest):
    return Path(runtime) / "staging" / safe_id(source_id) / f"{digest}.json"


def validate_index(runtime=DEFAULT_RUNTIME, write=True):
    runtime = Path(runtime)
    config = load_json(CONFIG_PATH)
    state = load_json(runtime / "state" / "state.json", {"documents": {}})
    policies = {item["id"]: item for item in config["sources"]}
    blocked = set(config["blocked_sources"])
    results = []

    for source_id, policy in policies.items():
        errors = []
        warnings = []
        current = state.get("documents", {}).get(source_id)
        digest = current.get("content_hash") if current else None
        record = None

        if source_id in blocked:
            errors.append("source is blocked")
        if not current:
            errors.append("source missing from state")
        elif current.get("status") != "present":
            errors.append(f"source state is {current.get('status')}")
        elif not digest:
            errors.append("state has no content_hash")
        else:
            source_path = staging_path(runtime, source_id, digest)
            if not source_path.exists():
                errors.append("staging record missing")
            else:
                try:
                    record = load_json(source_path)
                except Exception as exc:
                    errors.append(f"invalid staging JSON: {exc}")

        if record:
            if record.get("source_id") != source_id:
                errors.append("source_id mismatch")
            if record.get("stage") != "STAGING":
                errors.append("record is not STAGING")
            if record.get("collection_mode") != policy["collection_mode"]:
                errors.append("collection_mode exceeds policy")
            if record.get("kind") != policy["kind"]:
                errors.append("kind differs from policy")
            if record.get("sensitivity") != policy["sensitivity"]:
                errors.append("sensitivity differs from policy")
            if not str(record.get("content", "")).strip():
                errors.append("content is empty")
            calculated = canonical_hash(record)
            if calculated != digest or record.get("content_hash") != digest:
                errors.append("content hash integrity failure")
            _, pii_hits = redact(str(record.get("content", "")))
            if pii_hits:
                errors.append("unredacted personal data: " + ",".join(sorted(pii_hits)))
            if record.get("collection_mode") == "schema_only" and record.get("record_count") not in (None, 0):
                errors.append("schema_only record includes rows")
            scope = record.get("content_scope", "unspecified")
            if record.get("kind") == "page" and scope != "canonical_full_page":
                warnings.append(f"non-canonical page scope: {scope}")
            elif record.get("collection_mode") == "metadata_rows" and scope != "safe_metadata_summary":
                warnings.append(f"unexpected metadata scope: {scope}")
            elif record.get("collection_mode") == "schema_only" and scope != "schema_summary":
                warnings.append(f"unexpected schema scope: {scope}")

        status = "passed" if not errors else "failed"
        result = {
            "source_id": source_id,
            "title": policy["title"],
            "status": status,
            "content_hash": digest,
            "errors": errors,
            "warnings": warnings,
        }
        results.append(result)

        if status == "passed" and write:
            validated = {
                **record,
                "stage": "VALIDATED",
                "validation": {
                    "validated_at": utc_now(),
                    "checks": [
                        "allowlist",
                        "source_state",
                        "content_hash",
                        "collection_mode",
                        "sensitivity",
                        "personal_data_scan",
                    ],
                    "warnings": warnings,
                },
            }
            target = validation_path(runtime, source_id, digest)
            if not target.exists():
                atomic_json(target, validated)

    report = {
        "version": 1,
        "validated_at": utc_now(),
        "passed": sum(item["status"] == "passed" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "total": len(results),
        "results": results,
    }
    if write:
        report_id = re.sub(r"[^0-9TZ]", "", report["validated_at"])
        atomic_json(runtime / "validation_runs" / f"{report_id}.json", report)
    return report


def manifest_fingerprint(mode, documents):
    stable = {"promotion_policy_version": 2, "mode": mode, "documents": {key: documents[key]["content_hash"] for key in sorted(documents)}}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def promote(runtime=DEFAULT_RUNTIME, mode="SAFE_POLICY_INDEX"):
    runtime = Path(runtime)
    validation = validate_index(runtime, write=True)
    current_path = runtime / "active" / "manifest.json"
    current = load_json(current_path)

    if validation["failed"]:
        return {
            "status": "blocked",
            "reason": "validation failed",
            "passed": validation["passed"],
            "failed": validation["failed"],
            "active_generation_unchanged": current.get("generation_id") if current else None,
            "validation": validation,
        }

    documents = {}
    all_warnings = []
    for item in validation["results"]:
        source_id = item["source_id"]
        digest = item["content_hash"]
        relative_path = validation_path(runtime, source_id, digest).relative_to(runtime)
        documents[source_id] = {
            "title": item["title"],
            "content_hash": digest,
            "validated_path": str(relative_path),
            "warnings": item["warnings"],
        }
        all_warnings.extend(f"{source_id}: {warning}" for warning in item["warnings"])

    fingerprint = manifest_fingerprint(mode, documents)
    if current and current.get("fingerprint") == fingerprint:
        return {
            "status": "unchanged",
            "generation_id": current["generation_id"],
            "documents": len(documents),
            "production_rag_eligible": current.get("production_rag_eligible", False),
            "canonical_content_eligible": current.get("canonical_content_eligible", False),
        }

    activated_at = utc_now()
    generation_id = re.sub(r"[^0-9TZ]", "", activated_at)
    content_eligible = not all_warnings and mode == "CANONICAL_FULL_INDEX"
    production_eligible = False
    manifest = {
        "version": 2,
        "stage": "ACTIVE",
        "mode": mode,
        "generation_id": generation_id,
        "activated_at": activated_at,
        "fingerprint": fingerprint,
        "previous_generation_id": current.get("generation_id") if current else None,
        "production_rag_eligible": production_eligible,
        "canonical_content_eligible": content_eligible,
        "production_gate_reason": "RAG quality gate has not been attached to this generation",
        "documents": documents,
        "warnings": sorted(all_warnings),
    }
    atomic_json(runtime / "active" / "history" / f"{generation_id}.json", manifest)
    atomic_json(current_path, manifest)
    return {
        "status": "promoted",
        "generation_id": generation_id,
        "documents": len(documents),
        "warnings": len(all_warnings),
        "production_rag_eligible": production_eligible,
        "canonical_content_eligible": content_eligible,
    }


def main():
    parser = argparse.ArgumentParser(description="Validate STAGING documents and atomically promote an ACTIVE manifest")
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--mode", choices=["SAFE_POLICY_INDEX", "CANONICAL_FULL_INDEX"], default="SAFE_POLICY_INDEX")
    args = parser.parse_args()
    result = validate_index(args.runtime) if args.validate_only else promote(args.runtime, args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") == "blocked" or result.get("failed", 0):
        raise SystemExit(2)


if __name__ == "__main__":
    main()

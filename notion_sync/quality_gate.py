#!/usr/bin/env python3
import argparse
import json
from datetime import datetime
from pathlib import Path

try:
    from notion_sync.incremental_ingest import DEFAULT_RUNTIME, atomic_json, load_json
except ModuleNotFoundError:
    from incremental_ingest import DEFAULT_RUNTIME, atomic_json, load_json


def evaluate(report_path, runtime=DEFAULT_RUNTIME, write=True):
    runtime = Path(runtime)
    report = load_json(Path(report_path))
    manifest = load_json(runtime / "active" / "manifest.json")
    checks = {
        "active_generation_matches": report.get("active_generation_id") == manifest.get("generation_id"),
        "active_fingerprint_matches": report.get("active_fingerprint") == manifest.get("fingerprint"),
        "canonical_content_eligible": manifest.get("canonical_content_eligible") is True,
        "all_questions_answered": report.get("questions") == 10 and len(report.get("results", [])) == 10,
        "retrieval_recall_at_5_is_1": report.get("summary", {}).get("retrieval_recall_at_5") == 1.0,
        "answer_accuracy_is_1": report.get("summary", {}).get("answer_accuracy") == 1.0,
        "policy_contract_attached": bool(report.get("policy_contract_hash")),
    }
    passed = all(checks.values())
    gate = {
        "schema_version": 1,
        "evaluated_at": datetime.now().astimezone().isoformat(),
        "generation_id": manifest.get("generation_id"),
        "fingerprint": manifest.get("fingerprint"),
        "status": "PASS" if passed else "FAIL",
        "deployment_changed": False,
        "checks": checks,
        "benchmark_report": str(Path(report_path).resolve()),
    }
    if write:
        atomic_json(runtime / "quality_gates" / f"{manifest['generation_id']}.json", gate)
    return gate


def main():
    parser = argparse.ArgumentParser(description="Evaluate a canonical RAG report without deploying it")
    parser.add_argument("report", type=Path)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    args = parser.parse_args()
    result = evaluate(args.report, args.runtime)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()

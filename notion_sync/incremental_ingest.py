#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME = ROOT / "runtime"
CONFIG_PATH = ROOT / "config" / "sources.json"

REDACTIONS = [
    ("email", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[REDACTED_EMAIL]"),
    ("phone", re.compile(r"(?<!\d)(?:\+?82[- ]?|0)1[016789][ -]?\d{3,4}[ -]?\d{4}(?!\d)"), "[REDACTED_PHONE]"),
    ("rrn", re.compile(r"(?<!\d)\d{6}[ -]?[-]\s?[1-4]\d{6}(?!\d)"), "[REDACTED_RRN]"),
    ("booking_code", re.compile(r"\bHM[A-Z0-9]{7,}\b"), "[REDACTED_BOOKING_CODE]"),
]


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_id(source_id):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", source_id).strip("_")


def redact(value):
    counts = {}
    output = value
    for name, pattern, replacement in REDACTIONS:
        output, count = pattern.subn(replacement, output)
        if count:
            counts[name] = count
    return output, counts


def canonical_hash(document):
    stable = {
        "source_id": document["source_id"],
        "kind": document["kind"],
        "title": document["title"],
        "url": document["url"],
        "last_edited_time": document.get("last_edited_time"),
        "content": document["content"],
        "content_scope": document.get("content_scope", "unspecified"),
        "collection_mode": document["collection_mode"],
        "sensitivity": document["sensitivity"],
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def validate_document(raw, allowed, blocked):
    required = {"source_id", "kind", "title", "url", "content", "collection_mode", "sensitivity"}
    missing = sorted(required - set(raw))
    if missing:
        raise ValueError("missing fields: " + ", ".join(missing))
    source_id = raw["source_id"]
    if source_id in blocked:
        raise ValueError("blocked source")
    policy = allowed.get(source_id)
    if not policy:
        raise ValueError("source is not allowlisted")
    if raw["collection_mode"] != policy["collection_mode"]:
        raise ValueError("collection mode exceeds allowlist")
    if raw["collection_mode"] == "schema_only" and raw.get("record_count") not in (None, 0):
        raise ValueError("schema_only source contains rows")


def ingest(snapshot_path, runtime=DEFAULT_RUNTIME, dry_run=False):
    config = load_json(CONFIG_PATH)
    snapshot = load_json(Path(snapshot_path))
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("documents"), list):
        raise ValueError("snapshot must contain a documents array")

    allowed = {item["id"]: item for item in config["sources"]}
    blocked = set(config["blocked_sources"])
    state_path = Path(runtime) / "state" / "state.json"
    state = load_json(state_path, {"version": 1, "documents": {}})
    previous = state["documents"]
    next_state = json.loads(json.dumps(state))
    seen = set()
    events = []

    for raw in snapshot["documents"]:
        source_id = raw.get("source_id", "<missing>")
        try:
            validate_document(raw, allowed, blocked)
            document = dict(raw)
            document["content"], redactions = redact(str(document["content"]))
            digest = canonical_hash(document)
            seen.add(source_id)
            prior = previous.get(source_id)
            status = "unchanged" if prior and prior.get("content_hash") == digest else ("modified" if prior else "added")
            record = {
                **document,
                "content_hash": digest,
                "ingested_at": snapshot.get("fetched_at") or utc_now(),
                "stage": "STAGING",
                "redactions": redactions,
            }
            if status != "unchanged" and not dry_run:
                target = Path(runtime) / "staging" / safe_id(source_id) / f"{digest}.json"
                if not target.exists():
                    atomic_json(target, record)
            next_state["documents"][source_id] = {
                "content_hash": digest,
                "title": document["title"],
                "url": document["url"],
                "last_seen_at": snapshot.get("fetched_at") or utc_now(),
                "missing_complete_runs": 0,
                "status": "present",
            }
            events.append({"source_id": source_id, "status": status, "content_hash": digest, "redactions": redactions})
        except Exception as exc:
            events.append({"source_id": source_id, "status": "rejected", "reason": str(exc)})

    if snapshot.get("complete_inventory") is True:
        for source_id, prior in previous.items():
            if source_id in seen:
                continue
            missing_runs = int(prior.get("missing_complete_runs", 0)) + 1
            status = "tombstone_candidate" if missing_runs >= 3 else "missing_observed"
            updated = dict(prior)
            updated.update({"missing_complete_runs": missing_runs, "status": status})
            next_state["documents"][source_id] = updated
            events.append({"source_id": source_id, "status": status, "missing_complete_runs": missing_runs})

    run_started_at = utc_now()
    run_id = re.sub(r"[^0-9TZ]", "", run_started_at)
    report = {
        "version": 1,
        "run_id": run_id,
        "run_started_at": run_started_at,
        "source_fetched_at": snapshot.get("fetched_at"),
        "provider": snapshot.get("provider", "unknown"),
        "complete_inventory": snapshot.get("complete_inventory") is True,
        "counts": {name: sum(1 for event in events if event["status"] == name) for name in sorted({e["status"] for e in events})},
        "events": events,
    }
    if not dry_run:
        next_state["updated_at"] = snapshot.get("fetched_at") or utc_now()
        atomic_json(state_path, next_state)
        atomic_json(Path(runtime) / "runs" / f"{run_id}.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description="Import a read-only Notion snapshot into local STAGING")
    parser.add_argument("snapshot")
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = ingest(args.snapshot, args.runtime, args.dry_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

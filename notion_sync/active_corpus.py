#!/usr/bin/env python3
import argparse
import re
from pathlib import Path

try:
    from notion_sync.incremental_ingest import DEFAULT_RUNTIME, atomic_json, load_json
except ModuleNotFoundError:
    from incremental_ingest import DEFAULT_RUNTIME, atomic_json, load_json


LOGICAL_IDS = {
    "00000000-0000-4000-8000-000000000001": "NOTION_HQ",
    "00000000-0000-4000-8000-000000000002": "OPS_GUIDE",
    "00000000-0000-4000-8000-000000000003": "EMAIL_AUTOMATION_MVP",
    "00000000-0000-4000-8000-000000000004": "OPERATIONS_MANUAL",
    "00000000-0000-4000-8000-000000000005": "OPS_STATE_TRANSITIONS_DRAFT",
    "00000000-0000-4000-8000-000000000006": "CONTRACT_BOOKING_MODEL",
    "collection://00000000-0000-4000-8000-000000000007": "RESOURCE_REGISTRY",
    "collection://00000000-0000-4000-8000-000000000008": "PROJECT_CONTEXT",
    "collection://00000000-0000-4000-8000-000000000009": "RESERVATION_SCHEMA",
    "collection://00000000-0000-4000-8000-000000000010": "CLEANING_SCHEMA",
}


def extract_page_content(value):
    match = re.search(r"<content>\n(.*)\n</content>\n</page>\s*$", value, flags=re.S)
    if not match:
        raise ValueError("canonical page wrapper is missing")
    return match.group(1).strip()


def load_active_corpus(runtime=DEFAULT_RUNTIME):
    runtime = Path(runtime)
    manifest = load_json(runtime / "active" / "manifest.json")
    if not manifest:
        raise ValueError("ACTIVE manifest is missing")
    content_eligible = manifest.get("canonical_content_eligible", manifest.get("production_rag_eligible", False))
    if manifest.get("mode") != "CANONICAL_FULL_INDEX" or not content_eligible:
        raise ValueError("ACTIVE manifest is not a content-eligible canonical index")

    documents = []
    for source_id, pointer in manifest["documents"].items():
        logical_id = LOGICAL_IDS.get(source_id)
        if not logical_id:
            raise ValueError(f"no logical document ID for {source_id}")
        record = load_json(runtime / pointer["validated_path"])
        content = record["content"]
        if record["kind"] == "page":
            content = extract_page_content(content)
        documents.append({
            "id": logical_id,
            "notion_id": source_id,
            "title": record["title"],
            "url": record["url"],
            "fetched_at": record.get("ingested_at"),
            "content": content,
            "content_hash": record["content_hash"],
        })
    return {
        "schema_version": 1,
        "active_generation_id": manifest["generation_id"],
        "active_fingerprint": manifest["fingerprint"],
        "documents": documents,
    }


def main():
    parser = argparse.ArgumentParser(description="Export the canonical ACTIVE manifest as a RAG corpus")
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    corpus = load_active_corpus(args.runtime)
    output = args.output or args.runtime / "exports" / "canonical_active_corpus.json"
    atomic_json(output, corpus)
    print(f"documents={len(corpus['documents'])}")
    print(f"generation={corpus['active_generation_id']}")
    print(f"saved={output}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import hashlib
import json
from pathlib import Path

from notion_sync.incremental_ingest import atomic_json


CONFIG = Path(__file__).resolve().parent / "config" / "canonical_policy_contract.json"


def derive_policy_contract(corpus, output=None):
    config = json.loads(CONFIG.read_text())
    documents = {item["id"]: item for item in corpus["documents"]}
    verified = []
    for rule in config["rules"]:
        source = documents.get(rule["authoritative_document"])
        if not source:
            raise ValueError(f"authoritative document missing: {rule['authoritative_document']}")
        missing = [term for term in rule["required_evidence"] if term not in source["content"]]
        if missing:
            raise ValueError(f"{rule['id']} evidence missing: {missing}")
        verified.append({
            **rule,
            "source_content_hash": source["content_hash"],
            "evidence_verified": True,
        })
    contract = {
        "schema_version": 1,
        "source_generation_id": corpus["active_generation_id"],
        "source_fingerprint": corpus["active_fingerprint"],
        "config_hash": hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
        "authority": config["authority"],
        "rules": verified,
    }
    if output:
        atomic_json(output, contract)
    return contract


def relevant_rules(contract, question):
    folded = question.casefold()
    return [
        rule for rule in contract["rules"]
        if all(term.casefold() in folded for term in rule["question_terms"])
    ]


def policy_context(contract, question):
    rules = relevant_rules(contract, question)
    if not rules:
        return ""
    lines = [
        "[VERIFIED POLICY CONTRACT]",
        "상태와 전환 모드는 상태 전이 매트릭스를 우선한다. 운영 가이드의 경고·체크 항목을 상태나 전환 모드로 바꾸어 해석하지 않는다.",
    ]
    lines.extend(f"- {rule['id']}: {rule['normalized_rule']}" for rule in rules)
    return "\n".join(lines)

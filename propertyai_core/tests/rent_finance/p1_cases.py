"""Exact frozen V5 parameter oracle; fail closed if the input bytes drift."""
from __future__ import annotations
from hashlib import sha256
import json
from pathlib import Path

V5_CASES=Path("/Users/kate/outputs/propertyai-rent-g1-v5-20260915T220400Z/rent-g1-contract-v5-1a16f24dfa83d59c3b0e5ecc/candidate/fixtures/V4_Three_Finding_Cases.json")
V5_CASES_SHA256="993695a81fe4507c4f892816f78821bf9f26b24862722c6107a58ac48914de0b"

def cases():
    raw=V5_CASES.read_bytes()
    assert sha256(raw).hexdigest()==V5_CASES_SHA256
    value=json.loads(raw)
    assert value["classification"]=="STATIC_REQUIREMENTS_AND_SYNTHETIC_CONTRACT_ORACLES_NOT_PRODUCT_OR_DB_TESTS"
    return value

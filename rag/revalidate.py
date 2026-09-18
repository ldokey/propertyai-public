#!/usr/bin/env python3
import json
import statistics
from copy import deepcopy
from pathlib import Path

from rag_pipeline import TOP_K, validate_answer

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
cases = {case["id"]: case for case in json.loads((ROOT / "evals" / "questions.json").read_text())}
source = json.loads((WORKSPACE / "outputs" / "rag-ab-test.json").read_text())
report = deepcopy(source)

for row in report["results"]:
    passed, details = validate_answer(cases[row["id"]], row["answer"])
    row["answer_passed"] = passed
    row["validation"] = details

for mode in report["summary"]:
    rows = [row for row in report["results"] if row["mode"] == mode]
    report["summary"][mode]["answer_accuracy"] = round(
        sum(row["answer_passed"] for row in rows) / len(rows), 3
    )

report["revalidation"] = {
    "source": "rag-ab-test.json",
    "reason": "R02 negation and R10 underscore-aware portal name false negatives corrected",
    "model_responses_rerun": False,
}
target = WORKSPACE / "outputs" / "rag-ab-test-revalidated.json"
target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
print(f"saved: {target}")

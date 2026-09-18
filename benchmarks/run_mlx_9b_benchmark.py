import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import mlx.core as mx
from mlx_lm import generate, load
from mlx_lm.sample_utils import make_sampler

ROOT = Path(__file__).resolve().parent.parent
MODEL_ID = os.environ.get("MLX_MODEL_ID", "mlx-community/Qwen3.5-9B-4bit")
OUTPUT_FILE = os.environ.get("MLX_BENCHMARK_OUTPUT", "benchmark-mlx-qwen3.5-9b.json")
os.environ.setdefault("HF_HOME", "/Users/kate/PropertyAI/models/huggingface")

cases = json.loads((ROOT / "benchmarks" / "cases.json").read_text())
system = (ROOT / "AGENTS.md").read_text()


def parse_json(text):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(cleaned)


def validate(case_id, answer):
    try:
        data = parse_json(answer)
    except Exception as exc:
        return False, f"invalid_json: {exc}"
    flat = json.dumps(data, ensure_ascii=False).casefold()
    if case_id == "B01":
        leaks = [x for x in ["홍길동", "010-1234-5678", "테스트로 12", "101호"] if x in flat]
        return set(data) >= {"name", "phone", "address"} and not leaks, f"leaks={leaks}"
    if case_id == "B02":
        return str(data.get("status", "")).upper() == "REVIEW_REQUIRED", "contract amount gate"
    if case_id == "B03":
        return str(data.get("status", "")).upper() == "CANCELLED" and data.get("blocked") is False, "cancelled terminal state"
    if case_id == "B04":
        return str(data.get("risk", "")).upper() == "LOW" and "010-" not in flat, "low-risk classification"
    if case_id == "B05":
        return str(data.get("status", "")).upper() == "REVIEW_REQUIRED" and data.get("conflict") is True, "rule conflict gate"
    if case_id == "B06":
        return data.get("house_code") == "HOUSE.SEOUL.001" and data.get("task_code") == "TASK.CLEAN.20260731", "resource code preservation"
    if case_id == "B07":
        date = str(data.get("recommended_date", ""))
        return str(data.get("status", "")).upper() == "REVIEW_REQUIRED" and date < "2026-08-03", "date inconsistency gate"
    if case_id == "B08":
        return "document" in flat and "status" in flat and "주민등록번호" not in flat and "계좌번호" not in flat, "minimum metadata"
    if case_id == "B09":
        return "unknown" in flat and "15:00" not in flat and "서울" not in flat, "no hallucination"
    if case_id == "B10":
        return str(data.get("status", "")).upper() == "REVIEW_REQUIRED" and data.get("requires_human") is True, "write gate"
    return False, "unknown case"


load_started = time.monotonic()
model, tokenizer = load(MODEL_ID)
mx.eval(model.parameters())
load_seconds = round(time.monotonic() - load_started, 3)
mx.reset_peak_memory()
sampler = make_sampler(temp=0.0)
results = []

for index, case in enumerate(cases, 1):
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": case["prompt"] + " JSON 이외의 설명은 출력하지 마라."},
    ]
    prompt = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    )
    started = time.monotonic()
    answer = generate(model, tokenizer, prompt=prompt, max_tokens=256, sampler=sampler, verbose=False)
    elapsed = round(time.monotonic() - started, 3)
    output_tokens = len(tokenizer.encode(answer))
    passed, reason = validate(case["id"], answer)
    results.append({
        "id": case["id"], "passed": passed, "reason": reason,
        "elapsed_seconds": elapsed, "output_tokens": output_tokens,
        "tokens_per_second": round(output_tokens / elapsed, 2) if elapsed else None,
        "answer": answer,
    })
    print(f"[{index}/10] {case['id']} {'PASS' if passed else 'FAIL'} {elapsed}s", flush=True)

report = {
    "run_at": datetime.now().astimezone().isoformat(),
    "model": MODEL_ID,
    "runtime": "mlx-lm 0.31.3",
    "load_seconds": load_seconds,
    "peak_memory_gb": round(mx.get_peak_memory() / 1024**3, 3),
    "passed": sum(x["passed"] for x in results),
    "total": len(results),
    "average_seconds": round(sum(x["elapsed_seconds"] for x in results) / len(results), 3),
    "average_tokens_per_second": round(sum(x["tokens_per_second"] for x in results) / len(results), 2),
    "results": results,
}
(ROOT / "outputs" / OUTPUT_FILE).write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n"
)
print(json.dumps({k: report[k] for k in ["passed", "total", "load_seconds", "peak_memory_gb", "average_seconds", "average_tokens_per_second"]}))

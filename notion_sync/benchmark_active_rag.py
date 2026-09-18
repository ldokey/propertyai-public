#!/usr/bin/env python3
import argparse
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))

from notion_sync.active_corpus import load_active_corpus
from notion_sync.incremental_ingest import DEFAULT_RUNTIME, atomic_json, load_json
from notion_sync.policy_contract import derive_policy_contract, policy_context
from rag import rag_pipeline as rp


def chunk_documents(documents):
    chunks = []
    for doc in documents:
        content = rp.clean_markup(doc["content"])
        sections = __import__("re").split(r"(?m)^(#{1,4}\s+.+)$", content)
        pairs = []
        if sections[0].strip():
            pairs.append((doc["title"], sections[0].strip()))
        for index in range(1, len(sections), 2):
            heading = sections[index].lstrip("# ").strip()
            body = sections[index + 1].strip() if index + 1 < len(sections) else ""
            pairs.append((heading, body))
        for heading, body in pairs:
            current = ""
            pieces = []
            for paragraph in [part.strip() for part in body.split("\n") if part.strip()]:
                if current and len(current) + len(paragraph) + 1 > rp.MAX_CHUNK_CHARS:
                    pieces.append(current)
                    current = paragraph
                else:
                    current = (current + "\n" + paragraph).strip()
            if current:
                pieces.append(current)
            for index, piece in enumerate(pieces):
                text = f"{heading}\n{piece}".strip()
                if len(text) < 40:
                    continue
                digest = hashlib.sha1((doc["id"] + heading + str(index)).encode()).hexdigest()[:10]
                chunks.append({
                    "chunk_id": f"{doc['id']}:{digest}",
                    "doc_id": doc["id"],
                    "title": doc["title"],
                    "heading": heading,
                    "source_url": doc["url"],
                    "text": text,
                    "chars": len(text),
                })
    return chunks


def cached_embeddings(runtime, fingerprint, chunks, cases):
    cache = Path(runtime) / "rag_cache" / fingerprint
    chunk_path = cache / "chunk_embeddings.json"
    query_path = cache / "query_embeddings.json"
    chunk_vectors = load_json(chunk_path)
    query_vectors = load_json(query_path)
    if chunk_vectors is None:
        chunk_vectors = []
        for start in range(0, len(chunks), 16):
            batch = [item["text"] for item in chunks[start:start + 16]]
            response = rp.post_json("/api/embed", {"model": rp.EMBED_MODEL, "input": batch, "truncate": True})
            chunk_vectors.extend(response["embeddings"])
            print(f"embedded_chunks={min(start + 16, len(chunks))}/{len(chunks)}", flush=True)
        atomic_json(chunk_path, chunk_vectors)
    if query_vectors is None:
        prompts = ["Instruct: 한국어 부동산 운영 질의에 답할 가장 관련성 높은 규칙 문단을 검색하라.\nQuery: " + case["question"] for case in cases]
        response = rp.post_json("/api/embed", {"model": rp.EMBED_MODEL, "input": prompts, "truncate": True})
        query_vectors = {case["id"]: vector for case, vector in zip(cases, response["embeddings"])}
        atomic_json(query_path, query_vectors)
    return chunk_vectors, query_vectors


def unload(model):
    try:
        rp.post_json("/api/generate", {"model": model, "keep_alive": 0}, timeout=60)
    except Exception:
        pass


def run(runtime=DEFAULT_RUNTIME, output=None):
    corpus = load_active_corpus(runtime)
    contract_path = Path(runtime) / "derived" / "policy_contract.json"
    contract = derive_policy_contract(corpus, contract_path)
    cases = json.loads((WORKSPACE / "rag" / "evals" / "questions.json").read_text())
    chunks = chunk_documents(corpus["documents"])
    bm25 = rp.BM25(chunks)

    unload(rp.GEN_MODEL)
    unload(rp.EMBED_MODEL)
    chunk_vectors, query_vectors = cached_embeddings(runtime, corpus["active_fingerprint"], chunks, cases)
    unload(rp.EMBED_MODEL)

    results = []
    try:
        for case in cases:
            selected, retrieval_seconds = rp.retrieve(
                case["question"], "hybrid", chunks, bm25, chunk_vectors, query_vectors[case["id"]]
            )
            selected_docs = list(dict.fromkeys(item["doc_id"] for item in selected))
            recall = int(any(doc in selected_docs for doc in case["gold_docs"]))
            verified_policy = policy_context(contract, case["question"])
            response = rp.answer_question(
                case["question"], selected, "hybrid-canonical-active", policy_context=verified_policy
            )
            answer = response.get("message", {}).get("content", "")
            passed, validation = rp.validate_answer(case, answer)
            row = {
                "id": case["id"],
                "question": case["question"],
                "gold_docs": case["gold_docs"],
                "selected_docs": selected_docs,
                "selected_chunks": [item["chunk_id"] for item in selected],
                "verified_policy_rules": [
                    rule["id"] for rule in contract["rules"] if rule["normalized_rule"] in verified_policy
                ],
                f"retrieval_recall_at_{rp.TOP_K}": recall,
                "retrieval_seconds": retrieval_seconds,
                "context_chars": sum(item["chars"] for item in selected),
                "answer": answer,
                "answer_passed": passed,
                "validation": validation,
                "prompt_tokens": response.get("prompt_eval_count"),
                "output_tokens": response.get("eval_count"),
                "total_seconds": round(response.get("total_duration", 0) / 1e9, 3),
                "generation_tokens_per_second": round(response.get("eval_count", 0) / (response.get("eval_duration", 1) / 1e9), 2),
            }
            results.append(row)
            print(f"{case['id']}: recall={recall} answer={'PASS' if passed else 'FAIL'}", flush=True)
    finally:
        unload(rp.GEN_MODEL)

    summary = {
        f"retrieval_recall_at_{rp.TOP_K}": round(sum(row[f"retrieval_recall_at_{rp.TOP_K}"] for row in results) / len(results), 3),
        "answer_accuracy": round(sum(row["answer_passed"] for row in results) / len(results), 3),
        "average_context_chars": round(statistics.mean(row["context_chars"] for row in results)),
        "average_retrieval_seconds": round(statistics.mean(row["retrieval_seconds"] for row in results), 4),
        "average_prompt_tokens": round(statistics.mean(row["prompt_tokens"] for row in results)),
        "average_total_seconds": round(statistics.mean(row["total_seconds"] for row in results), 3),
        "average_generation_tokens_per_second": round(statistics.mean(row["generation_tokens_per_second"] for row in results), 2),
    }
    report = {
        "run_at": datetime.now().astimezone().isoformat(),
        "active_generation_id": corpus["active_generation_id"],
        "active_fingerprint": corpus["active_fingerprint"],
        "generator_model": rp.GEN_MODEL,
        "embedding_model": rp.EMBED_MODEL,
        "documents": len(corpus["documents"]),
        "chunks": len(chunks),
        "questions": len(cases),
        "policy_contract_hash": contract["config_hash"],
        "summary": summary,
        "results": results,
    }
    target = Path(output) if output else WORKSPACE / "outputs" / "rag-canonical-active.json"
    atomic_json(target, report)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"saved={target}")
    return report


def main():
    parser = argparse.ArgumentParser(description="Benchmark canonical ACTIVE Notion content without changing production RAG")
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run(args.runtime, args.output)


if __name__ == "__main__":
    main()

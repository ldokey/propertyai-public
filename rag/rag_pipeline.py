#!/usr/bin/env python3
import argparse
import hashlib
import json
import math
import re
import statistics
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
OLLAMA = "http://127.0.0.1:11434"
GEN_MODEL = "qwen3:30b"
EMBED_MODEL = "qwen3-embedding:0.6b"
MAX_CHUNK_CHARS = 1400
TOP_K = 5


def post_json(path, payload, timeout=600):
    req = urllib.request.Request(
        OLLAMA + path,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def clean_markup(text):
    text = re.sub(r"<page[^>]*>.*?</page>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_documents():
    snapshot = json.loads((ROOT / "corpus" / "notion_snapshot.json").read_text())
    docs = snapshot["documents"]
    extra = json.loads((ROOT / "corpus" / "ops_state_transitions.json").read_text())
    docs.append(extra)
    return docs


def chunk_documents(docs):
    chunks = []
    for doc in docs:
        content = clean_markup(doc["content"])
        sections = re.split(r"(?m)^(#{1,4}\s+.+)$", content)
        heading = doc["title"]
        section_pairs = []
        if sections[0].strip():
            section_pairs.append((heading, sections[0].strip()))
        for i in range(1, len(sections), 2):
            heading = sections[i].lstrip("# ").strip()
            body = sections[i + 1].strip() if i + 1 < len(sections) else ""
            section_pairs.append((heading, body))
        for section_heading, body in section_pairs:
            paragraphs = [p.strip() for p in body.split("\n") if p.strip()]
            current = ""
            pieces = []
            for paragraph in paragraphs:
                if current and len(current) + len(paragraph) + 1 > MAX_CHUNK_CHARS:
                    pieces.append(current)
                    current = paragraph
                else:
                    current = (current + "\n" + paragraph).strip()
            if current:
                pieces.append(current)
            for index, piece in enumerate(pieces):
                text = f"{section_heading}\n{piece}".strip()
                if len(text) < 40:
                    continue
                digest = hashlib.sha1((doc["id"] + section_heading + str(index)).encode()).hexdigest()[:10]
                chunks.append({
                    "chunk_id": f"{doc['id']}:{digest}",
                    "doc_id": doc["id"],
                    "title": doc["title"],
                    "heading": section_heading,
                    "source_url": doc["url"],
                    "text": text,
                    "chars": len(text),
                })
    (ROOT / "chunks").mkdir(exist_ok=True)
    (ROOT / "chunks" / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False, indent=2) + "\n")
    return chunks


def tokens(text):
    return re.findall(r"[가-힣]+|[a-zA-Z0-9_.:-]+", text.casefold())


class BM25:
    def __init__(self, chunks, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.docs = [tokens(c["text"]) for c in chunks]
        self.tf = [Counter(d) for d in self.docs]
        self.lengths = [len(d) for d in self.docs]
        self.avgdl = sum(self.lengths) / max(len(self.lengths), 1)
        df = Counter()
        for doc in self.docs:
            df.update(set(doc))
        n = len(self.docs)
        self.idf = {term: math.log(1 + (n - count + 0.5) / (count + 0.5)) for term, count in df.items()}

    def scores(self, query):
        q = tokens(query)
        out = []
        for tf, dl in zip(self.tf, self.lengths):
            score = 0.0
            for term in q:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                denom = freq + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                score += self.idf.get(term, 0) * freq * (self.k1 + 1) / denom
            out.append(score)
        return out


def normalize(values):
    low, high = min(values), max(values)
    if high <= low:
        return [0.0 for _ in values]
    return [(x - low) / (high - low) for x in values]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def embeddings_for_chunks(chunks):
    cache_path = ROOT / "chunks" / "embeddings.json"
    fingerprint = hashlib.sha256("\n".join(c["chunk_id"] + c["text"] for c in chunks).encode()).hexdigest()
    if cache_path.exists():
        cached = json.loads(cache_path.read_text())
        if cached.get("fingerprint") == fingerprint and cached.get("model") == EMBED_MODEL:
            return cached["embeddings"]
    embeddings = []
    batch_size = 16
    for start in range(0, len(chunks), batch_size):
        batch = [c["text"] for c in chunks[start:start + batch_size]]
        response = post_json("/api/embed", {"model": EMBED_MODEL, "input": batch, "truncate": True})
        embeddings.extend(response["embeddings"])
        print(f"embedded {min(start + batch_size, len(chunks))}/{len(chunks)}", flush=True)
    cache_path.write_text(json.dumps({"model": EMBED_MODEL, "fingerprint": fingerprint, "embeddings": embeddings}) + "\n")
    return embeddings


def embeddings_for_queries(cases):
    cache_path = ROOT / "chunks" / "query_embeddings.json"
    fingerprint = hashlib.sha256("\n".join(c["id"] + c["question"] for c in cases).encode()).hexdigest()
    if cache_path.exists():
        cached = json.loads(cache_path.read_text())
        if cached.get("fingerprint") == fingerprint and cached.get("model") == EMBED_MODEL:
            return cached["embeddings"]
    prompts = [
        "Instruct: 한국어 부동산 운영 질의에 답할 가장 관련성 높은 규칙 문단을 검색하라.\nQuery: " + case["question"]
        for case in cases
    ]
    response = post_json("/api/embed", {"model": EMBED_MODEL, "input": prompts, "truncate": True})
    vectors = {case["id"]: vector for case, vector in zip(cases, response["embeddings"])}
    cache_path.write_text(json.dumps({"model": EMBED_MODEL, "fingerprint": fingerprint, "embeddings": vectors}) + "\n")
    return vectors


def retrieve(question, mode, chunks, bm25, chunk_embeddings=None, query_embedding=None):
    started = time.monotonic()
    lexical = bm25.scores(question)
    if mode == "keyword":
        scores = lexical
    elif mode == "hybrid":
        semantic = [cosine(query_embedding, vector) for vector in chunk_embeddings]
        ln, sn = normalize(lexical), normalize(semantic)
        scores = [0.45 * l + 0.55 * s for l, s in zip(ln, sn)]
    elif mode == "full":
        selected = chunks
        return selected, round(time.monotonic() - started, 4)
    else:
        raise ValueError(mode)
    order = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)[:TOP_K]
    return [dict(chunks[i], retrieval_score=round(scores[i], 6)) for i in order], round(time.monotonic() - started, 4)


def answer_question(question, selected, mode, policy_context=""):
    context = "\n\n".join(
        f"[SOURCE {i+1}] doc={c['doc_id']} | {c['title']} | {c['heading']} | {c['source_url']}\n{c['text']}"
        for i, c in enumerate(selected)
    )
    system = """너는 부동산 운영 원장의 근거 기반 판정기다.
제공된 CONTEXT에 있는 사실만 사용한다. 근거가 없으면 UNKNOWN이라고 답한다.
추측하거나 상태·금액·날짜를 만들지 않는다. answer는 한 문단의 간결한 한국어로 작성한다."""
    policy = f"\n\n{policy_context}" if policy_context else ""
    prompt = f"""QUESTION:
{question}

CONTEXT ({mode}):
{context}{policy}"""
    response = post_json("/api/chat", {
        "model": GEN_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "format": {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "source_docs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["answer", "source_docs"],
        },
        "options": {"temperature": 0, "num_ctx": 32768, "num_predict": 256},
        "keep_alive": "10m",
    })
    return response


def validate_answer(case, text):
    folded = text.casefold()
    missing = []
    for group in case["expected_groups"]:
        if not any(term.casefold() in folded for term in group):
            missing.append(group)
    forbidden = [term for term in case.get("forbidden", []) if term.casefold() in folded]
    return not missing and not forbidden, {"missing_groups": missing, "forbidden_hits": forbidden}


def run(retrieval_only=False, limit=None, selected_modes=None):
    docs = load_documents()
    chunks = chunk_documents(docs)
    cases = json.loads((ROOT / "evals" / "questions.json").read_text())
    if limit:
        cases = cases[:limit]
    bm25 = BM25(chunks)
    modes = selected_modes or ["full", "keyword", "hybrid"]
    chunk_embeddings = embeddings_for_chunks(chunks) if "hybrid" in modes else None
    query_embeddings = embeddings_for_queries(cases) if "hybrid" in modes else {}
    results = []
    for case in cases:
        for mode in modes:
            selected, retrieval_seconds = retrieve(
                case["question"], mode, chunks, bm25, chunk_embeddings, query_embeddings[case["id"]]
                if mode == "hybrid" else None
            )
            selected_docs = list(dict.fromkeys(c["doc_id"] for c in selected))
            recall = int(any(doc in selected_docs for doc in case["gold_docs"]))
            row = {
                "id": case["id"], "mode": mode, "question": case["question"],
                "gold_docs": case["gold_docs"], "selected_docs": selected_docs,
                "selected_chunks": [c["chunk_id"] for c in selected],
                f"retrieval_recall_at_{TOP_K}": recall,
                "retrieval_seconds": retrieval_seconds,
                "context_chars": sum(c["chars"] for c in selected),
            }
            if not retrieval_only:
                response = answer_question(case["question"], selected, mode)
                answer = response.get("message", {}).get("content", "")
                passed, details = validate_answer(case, answer)
                row.update({
                    "answer": answer, "answer_passed": passed, "validation": details,
                    "prompt_tokens": response.get("prompt_eval_count"),
                    "output_tokens": response.get("eval_count"),
                    "total_seconds": round(response.get("total_duration", 0) / 1e9, 3),
                    "prompt_seconds": round(response.get("prompt_eval_duration", 0) / 1e9, 3),
                    "generation_tokens_per_second": round(
                        response.get("eval_count", 0) / (response.get("eval_duration", 1) / 1e9), 2
                    ),
                })
            results.append(row)
            checkpoint = WORKSPACE / "outputs" / "rag-ab-test.partial.json"
            checkpoint.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
            print(f"{case['id']} {mode}: recall={recall}" + (f" answer={'PASS' if row.get('answer_passed') else 'FAIL'}" if not retrieval_only else ""), flush=True)
    summary = {}
    for mode in modes:
        rows = [r for r in results if r["mode"] == mode]
        summary[mode] = {
            f"retrieval_recall_at_{TOP_K}": round(sum(r[f"retrieval_recall_at_{TOP_K}"] for r in rows) / len(rows), 3),
            "average_context_chars": round(statistics.mean(r["context_chars"] for r in rows)),
            "average_retrieval_seconds": round(statistics.mean(r["retrieval_seconds"] for r in rows), 4),
        }
        if not retrieval_only:
            summary[mode].update({
                "answer_accuracy": round(sum(r["answer_passed"] for r in rows) / len(rows), 3),
                "average_prompt_tokens": round(statistics.mean(r["prompt_tokens"] for r in rows)),
                "average_total_seconds": round(statistics.mean(r["total_seconds"] for r in rows), 3),
                "average_generation_tokens_per_second": round(statistics.mean(r["generation_tokens_per_second"] for r in rows), 2),
            })
    report = {
        "run_at": datetime.now().astimezone().isoformat(),
        "generator_model": GEN_MODEL, "embedding_model": EMBED_MODEL,
        "documents": len(docs), "chunks": len(chunks), "questions": len(cases),
        "retrieval_only": retrieval_only, "summary": summary, "results": results,
    }
    output = WORKSPACE / "outputs" / ("rag-retrieval-only.json" if retrieval_only else "rag-ab-test.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"saved: {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval-only", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--modes", nargs="+", choices=["full", "keyword", "hybrid"])
    args = parser.parse_args()
    run(args.retrieval_only, args.limit, args.modes)

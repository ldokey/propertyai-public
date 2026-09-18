#!/bin/zsh
set -euo pipefail

workspace_dir="${0:A:h:h}"
ollama_url="http://127.0.0.1:11434"
generation_model="qwen3:30b"
embedding_model="qwen3-embedding:0.6b"

unload_model() {
  local model_name="$1"
  curl -fsS "${ollama_url}/api/generate" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"${model_name}\",\"keep_alive\":0}" >/dev/null
}

cleanup() {
  unload_model "$generation_model" >/dev/null 2>&1 || true
  unload_model "$embedding_model" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

curl -fsS "${ollama_url}/api/tags" >/dev/null

# The 32 GB Mac mini cannot safely keep the 30B generator and embedder resident together.
unload_model "$generation_model"
unload_model "$embedding_model"

curl -fsS "${ollama_url}/api/embed" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"${embedding_model}\",\"input\":\"PropertyAI embedding preflight\",\"keep_alive\":\"30s\"}" \
  | python3 -c 'import json,sys; data=json.load(sys.stdin); vectors=data.get("embeddings", []); assert vectors and len(vectors[0]) == 1024, data.get("error", "invalid embedding"); print("embedding_preflight=1024")'

unload_model "$embedding_model"

cd "$workspace_dir"
python3 rag/rag_pipeline.py --modes hybrid

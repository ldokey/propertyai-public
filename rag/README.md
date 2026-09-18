# Notion RAG A/B/C 실험

Mac mini M4 32GB에서 Notion 고정 스냅샷을 대상으로 다음 세 방식을 비교한다.

- `full`: 모든 청크를 Qwen3 30B에 전달
- `keyword`: Python BM25 상위 3개 청크 전달
- `hybrid`: BM25 45% + Qwen3 Embedding 0.6B 코사인 유사도 55%

## 실행

```bash
python3 rag/rag_pipeline.py --retrieval-only
python3 rag/rag_pipeline.py
```

## 결과

- `outputs/rag-retrieval-only.json`
- `outputs/rag-ab-test.json`

스냅샷은 실제 개인정보 대신 운영 규칙 문서만 포함한다. 무인 증분 동기화는 이 고정 스냅샷 실험이 통과한 뒤 별도 Notion Integration으로 구현한다.

## 2026-08-01 채택 결과

- 전체 입력: 0/10, 평균 16,386 입력토큰, 21.222초
- BM25: 9/10, 평균 1,398 입력토큰, 9.154초
- 하이브리드: 10/10, 평균 1,452 입력토큰, 8.476초

고정 스냅샷 MVP에는 하이브리드 검색을 채택한다. 원시 결과는 `outputs/rag-ab-test.json`, 판정기 오탐 교정 결과는 `outputs/rag-ab-test-revalidated.json`에 있다.

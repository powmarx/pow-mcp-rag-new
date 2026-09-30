# RAG-MCP Relevance Score Benchmark: rag-mcp vs rag-mcp-old

**Date:** 2026-08-14

## Setup

| | rag-mcp (new, uv/native) | rag-mcp-old (Docker) |
|---|---|---|
| Embedding model | BAAI/bge-m3 | BAAI/bge-small-en-v1.5 |
| Reranker model | BAAI/bge-reranker-v2-m3 | cross-encoder/ms-marco-MiniLM-L-6-v2 |
| Reason for new config | Better multilingual (PT/EN/ES) retrieval | Original default |

Confirmed via MCP client log: rag-mcp's reranker (`bge-reranker-v2-m3`) loads successfully at
first search-tool call and no fallback-to-bi-encoder message appears — the reranker is
genuinely active, not silently failing.

---

## Test 1 — Code search, keyword-style query ("reranker")

| Rank | rag-mcp score | rag-mcp-old score |
|---|---|---|
| 1 | 0.8954 | 0.9954 |
| 2 | 0.8827 | 0.9945 |
| 3 | 0.8599 | 0.9913 |
| 4 | 0.8447 | 0.9903 |
| 5 | 0.8444 | 0.9897 |

rag-mcp-old scored higher and flatter for this simple keyword query. Both returned the same
underlying files.

## Test 2 — Code search, natural-language query ("ensure_reranker_loaded fallback bi-encoder load failure")

rag-mcp only (used to inspect fallback code path); confirmed `search.py`'s
`_ensure_reranker_loaded` / try-except fallback logic as top hits.

## Test 3 — Code search, second natural-language query ("sigmoid normalization relevance score clamp cross-encoder")

rag-mcp score curve: 0.2961 → 0.0306 → 0.0154 → 0.0135 → 0.0125 — steep, polarized drop,
consistent with active cross-encoder sigmoid scoring (vs. the flatter Test 1 curve, which
reflected several genuinely similar top hits rather than fallback behavior).

## Test 4 — Code search benchmark, English ("embedding model configuration")

| Rank | rag-mcp file | Score | rag-mcp-old file | Score |
|---|---|---|---|---|
| 1 | _server.py | 0.9808 | server.py | 0.9974 |
| 2 | config.template.yaml | 0.9676 | config_loader.py | 0.9969 |
| 3 | server.py | 0.9610 | management.py | 0.9791 |
| 4 | indexer.py | 0.9466 | embedding_generator.py | 0.9680 |
| 5 | config_loader.py | 0.9308 | _server.py | 0.9633 |

Both found the same core relevant files; rag-mcp-old scored consistently higher with tighter
spread (~0.034 vs ~0.050). rag-mcp additionally surfaced `config.template.yaml`, which
rag-mcp-old missed from its top 5.

## Test 5 — Code search, PT vs EN ("how does the server load the reranking model")

**Portuguese** — "como o servidor carrega o modelo de reranking"

| Server | Top score | Range | Correct top hit? |
|---|---|---|---|
| rag-mcp | 0.3913 | 0.15–0.39 | Yes — `_server.py` reranker wiring |
| rag-mcp-old | 0.017 | 0.001–0.017 | Weak/near-zero confidence |

**English** — "how does the server load the reranking model"

| Server | Top score | Range | Correct top hit? |
|---|---|---|---|
| rag-mcp | 0.8439 | 0.71–0.84 | Yes — `server.py` reranker loader |
| rag-mcp-old | 0.9978 | 0.975–0.998 | Yes — `_server.py` reranker wiring |

**Finding:** in English both models perform well (rag-mcp-old even scores higher). In
Portuguese, rag-mcp-old's scores collapse to near-zero — a sign of poor semantic
understanding — while rag-mcp holds a plausible confidence level and still returns the
correct file.

## Test 6 — Code search, "find the right implementation" query (PT vs EN)

**Portuguese** — "função que calcula o número de candidatos para overfetch antes do rerank"

| Server | Top result | Score | Correct? |
|---|---|---|---|
| rag-mcp | `search.py` (actual `fetch_k = top_k * overfetch_factor` line) | 0.2354 | ✅ |
| rag-mcp-old | `test_reranker.py` (test asserting config defaults) | 0.0015 | ❌ |

**English** — "function that computes overfetch candidate count before rerank"

| Server | Top result | Score | Correct? |
|---|---|---|---|
| rag-mcp | `search.py` (same correct implementation line) | 0.29 | ✅ |
| rag-mcp-old | `test_reranker.py` (test file, not real implementation) | 0.8186 | ❌ (high confidence, wrong file) |

**Finding:** rag-mcp correctly identifies the real implementation in both languages.
rag-mcp-old misses it in both languages, and in English does so with *high* confidence
(0.82) — likely latching onto surface keyword overlap ("overfetch_factor") in test
assertions rather than understanding "the function that computes X". This is the most
concerning result: rag-mcp-old's confidence doesn't track correctness for this class of
query.

## Test 7 — Spec/doc search, PT vs EN ("reranker configuration requirements")

**Portuguese** — "requisitos de configuração do reranker"

| Server | Top result | Score |
|---|---|---|
| rag-mcp | `doc/ARCHITECTURE.md` — Retrieval Model section (correct) | 0.0653 |
| rag-mcp-old | `doc/ARCHITECTURE.md` — same section (correct) | 0.0167 |

**English** — "reranker configuration requirements"

| Server | Top result | Score |
|---|---|---|
| rag-mcp | `doc/ARCHITECTURE.md` — Retrieval Model section (correct) | 0.4477 |
| rag-mcp-old | `doc/ARCHITECTURE.md` — same section (correct) | 0.1531 |

**Finding:** for spec/doc search, both servers agree on the correct top file in both
languages, but rag-mcp scores markedly higher and more confidently in both PT (0.065 vs
0.017, ~4x) and EN (0.448 vs 0.153, ~3x). Unlike the code-search tests, rag-mcp-old didn't
pick a wrong file here — it just under-scored the right one. Spec/doc content (prose-heavy
Markdown) seems to close the PT/EN confidence gap somewhat compared to code search, though
rag-mcp still leads on both languages.

---

## Overall Summary

| Search type | rag-mcp-old competitive? | Notes |
|---|---|---|
| Doc/code, simple keyword, EN only | Yes, sometimes higher-scoring | Flat, high scores; both agree on files |
| Code, natural-language, EN | Mixed | rag-mcp-old scores higher but occasionally recalls fewer relevant files (e.g. missed config.template.yaml) |
| Code, natural-language, PT | No | Scores collapse near zero; poor semantic understanding |
| Code, "find implementation" query, EN or PT | No | Wrong file (test file instead of real implementation); overconfident in EN (0.82 on wrong file) |
| Spec/doc, PT or EN | Partially | Same correct file in both, but rag-mcp scores 3–4x higher/more confident |

**Conclusion:** rag-mcp-old (bge-small-en-v1.5 + MiniLM) remains competitive or even
higher-scoring on simple English keyword/doc queries, but degrades significantly on
Portuguese queries and on code queries that require understanding *what code does* rather
than matching surface tokens — in the latter case it can be confidently wrong. rag-mcp
(bge-m3 + bge-reranker-v2-m3) is consistently correct across all tested query types and
languages, at generally lower/more conservative confidence scores. This supports the
earlier decision to move the uv/native server to the multilingual model pair.

# ADR: Separate versioned Source RAG and semantic History RAG

- **Status:** Accepted in the M3 implementation per historical milestone results (reconstructed 2026-10-27).
- **Scope:** Public repository evidence indexing, retrieval and evaluation.

## Context

Repository source and GitHub discussion history have different identity and update semantics. Source evidence must be attributable to a fixed revision/file/line range; history comments/Issues/PRs can change. Retrieved snippets are only useful for trusted investigation reports if their provenance is explicit. A small V1 dataset does not justify costly approximate indexing or reranking before observing a bottleneck.

## Decision

- Source indexing pins commit/tree/blob content and stores syntax-aware chunks with traceable file/line/symbol metadata. Index identity includes repository, immutable revision, embedding provider/model/dimensions and chunker version. Build claims/leases and fenced writes protect ready-index consistency.
- Source retrieval uses PostgreSQL `pgvector` exact cosine semantic search and optional escaped `ILIKE` literal-identifier matching, fused via reciprocal rank fusion (RRF) when the query merits both.
- History RAG ingests typed Issues/PRs/comments with source URLs and bounded synchronization. **Semantic search is the default**; keyword retrieval is available for exact identifiers.
- V1 **does not use HNSW, a reranker, BM25, or a separate search engine** until more representative evaluation identifies a specific need.

## Evidence and limitations

Source sample: eight bilingual queries against a tiny pinned `pypa/sampleproject` index, only two different evidence targets; both semantic and automatic hybrid Recall@5/MRR@5 1.000. History sample: 40 `encode/httpx` candidate Issues, 14 queries; semantic Top-1 92.9%, Recall@5 100%, MRR@5 0.964 versus keyword Top-1 57.1%, Recall@5 64.3%, MRR@5 0.607. The datasets are small, partially labeled and not robust production-quality claims. No observed advantage for a reranker or HNSW is established here.

## Consequences

Preserve independent, typed, callable search interfaces. LangGraph should orchestrate these services, never reimplement pgvector, Embedding, indexing or RRF inside graph nodes. Use separate AsyncSessions under concurrency, validate repository/index scope and check citation anchors before using retrieved text as evidence. Query/embedding format changes require deliberate index-version migration or rebuild.

## October 28 source audit and maintenance (validation pending)

- Source format detection includes additional text/config formats using window
  fallback; Python/JS/TS/Go/Java have syntax analyzers and Markdown has headings.
- The Graph source adapter explicitly ends its index ownership-check transaction
  before M3 retrieval opens its own transactions or calls query embeddings.
  `tests/integration/test_investigation_source_searcher_postgres.py` exercises the
  real adapter/Graph/pgvector path with fake embeddings and isolated cleanup.
- History sync, resumable backfill, comment/PR enrichment and embedding refresh
  are distinct services. The small preparation pipeline does not advance sync
  cursors. History embedding bodies are truncated to 5,000 characters; source
  updates do not trigger embedding refresh automatically. PR metadata is not a
  full diff or a verified link to an issue's fix.
- The extended history benchmark has six labeled targets and 34 dynamically
  fetched distractors; its 14 queries do not measure PR/comment quality. It
  prints metrics without asserting the historical scores as quality thresholds.
- GitHub issue/history/comment/PR readers verify public repository metadata
  before fetching content; source revision resolution validates the metadata it
  already needs. Missing/ambiguous visibility is rejected, with one additional
  metadata request per content read. Backend-selected tree/blob readers assume
  a validated revision/manifest. No private authorization is introduced.
- Historical citation URLs must match the declared repository, object number
  and comment anchor. The investigated issue and its comments are excluded from
  independent history evidence. Citation validity does not prove entailment,
  current freshness or immutable historical content; durable report evidence
  snapshots remain future work.

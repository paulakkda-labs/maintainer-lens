# ADR: Explicit Source identity, bounded Worker tools and derived reports

- **Status:** Accepted; implemented 2026-10-28, expanded acceptance pending.
- **Scope:** Supplement the modular investigation and versioned RAG decisions.

## Context

A prepared Source index must be selected by a caller-owned UUID rather than by
an arbitrary latest index. Passing it only in a broker body would allow request
identity to diverge from the durable task. A human-readable report is needed,
but the existing structured result already contains validated findings and the
actual analysis context; another model call would add cost and unverified text.

## Decision

- Persist optional `source_index_id` in the existing Task together with its Outbox
  event. Carry it through message publication and Worker. Absent legacy fields
  mean NULL and still must match the task. Keep language/Graph budgets backend-owned. Clients may choose a backend-defined
  knowledge-preparation profile; persist its resolved versioned values on Task,
  without adding a second budget copy to broker messages.
- Use a nullable native UUID with no foreign key/default/backfill. Index retention
  must not erase the requested identity or cascade-delete investigations. A saved
  UUID does not prove availability: explicitly validate repository, ready status
  and embedding provider/model/dimensions before GitHub/retrieval, then retain
  existing adapter checks. Do not build, choose latest or silently fall back.
- Compare repository, Issue and selected UUID at active-claim preflight, fenced
  result write and successful-result decode. Keep the original Runner, leases,
  task statuses, transactional Outbox and ACK-after-handling semantics.
- Retry building indexes through the existing bounded delivery policy. Failed,
  missing, foreign or incompatible selections fail explicitly. Preserve other
  dependency errors, cancellation and existing ownership rules.
- Bind model-selected tools to the default Worker: Chinese, maximum three rounds,
  Top-5 per retriever, whole-Graph 120 seconds and tool step 60 seconds. Omit Source
  from the tool whitelist for History-only requests. Query Planner remains an
  independent mutually exclusive option. Stop duplicate/non-novel searches.
- Derive `report_markdown` on GET from succeeded validated results. Render summary,
  cited findings, unverified hypotheses, open questions and saved analysis excerpts
  separately. Reuse evidence/URL and ready-routing checks; escape untrusted text.
  Preserve insufficient-evidence outcomes. No extra LLM call, report table, result
  schema-version change or report-writing Graph node is needed.

## Consequences and verification

At most three structured analyses plus two selection/feedback pairs produce
seven Chat requests per attempt. Delivery retries can repeat external work;
these controls are not a monetary budget or remote cancellation guarantee.
Snapshot, result and terminal-status commits remain separate short transactions.
Reports remain readable after Source index deletion because saved evidence is
self-contained; citation traceability is not semantic fact verification.

Regressions use real compiled Graphs and fake providers. Opt-in PostgreSQL/MQ
full-chain tests cover POST/Outbox/default Worker/pre-ACK writes/GET and duplicate
delivery. Paid Source smoke prepares an owned, deliberately partial real index
and obtains its UUID internally, uses bounded requests and preserves all existing
indexes. Production automatic preparation remains a separate product-layer task. Tests supplied is not tests passed; final semantic
quality review, CI/review and deployment remain distinct acceptance levels.

## Preparation-budget interface supplement

Before applying revision e8b3c7d9a204, the maintainer requested selectable
preparation profiles. That pending migration now also adds non-null JSONB
`knowledge_budget` with a frozen standard-v1 legacy default and object check.
API profile discovery and submission/readback snapshots are implemented; execution
integration remains pending. The default is 500 files/8 MiB/2,000 chunks/three
History parents/900 seconds. Unlimited means null task-level preparation limits,
while Graph rounds/deadlines, transport and content controls remain separate.
Existing aggregate-memory and bounded History services require adaptation before
unlimited preparation can be enabled. Stored values must survive preset changes
and retries unchanged; the profile label alone is insufficient task intent.

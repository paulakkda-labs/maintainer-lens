# ADR: Modular, bounded, evidence-first LangGraph investigation

- **Status:** Accepted for the current M4 implementation per user-confirmed tests; branch merge and production deployment **not verified**.
- **Scope:** The standalone LangGraph investigation workflow and its retrieval/tool adapters.

## Context

A single large graph file mixing orchestration, provider requests, evidence transformation, search sessions and branching is hard to reason about and regression-test. LLM outputs may select invalid queries or hallucinate citations. An unbounded investigation loop creates uncontrolled latency and cost. The existing M2 Worker owns reliable task execution and must not be duplicated.

## Decision

- Keep `agents/investigation_graph.py` narrowly responsible for registering graph nodes and edges.
- Separate typed State/evidence IDs and merges, RAG search adapters, structured analysis, routing, follow-up planning, actual tool-calling protocol, and execution-time controls into `agents/investigation/` modules.
- Run initial History RAG and optionally Source RAG, analyze evidence with a Pydantic structure containing cited findings/hypotheses/unanswered questions, then either stop or perform bounded follow-up. Configure a maximum search round count, deduplicate queries and evidence, and stop when no genuinely new evidence appears.
- Support distinct follow-up strategies, including an LLM Query Planner and actual model-selected `search_history` / `search_source` Function Calling; they should not independently cause redundant RAG searches on the same round.
- Python, not the LLM, validates Tool Schema, JSON parameters, tool whitelist, trusted repository/Index context, and `tool_call_id` association. Limit the first Tool Calling integration to a single allowed retrieval call per step. Treat free-text Tool feedback as commentary, not a validated Finding.
- Offer explicit caller-applied tool-step timeout and metadata-only logging wrappers, without swallowing cancellation or assuming remote costs stop immediately. The graph builder does not apply the wrapper automatically.

## Consequences

- **Benefits:** Small testable units, explicit trust boundaries, less unnecessary retrieval work and more predictable cost.
- **Costs:** More adapter interfaces and testing; additional integration work is needed to map graph failures and reports into the existing M2 Runner/Worker status, retry, lease and persistence contracts.
- **Not decided yet:** Whole-graph runtime controller placement, persistent Checkpointer, HITL, full multi-agent roles or report DB schema. The previously suggested new `services/investigation_runner.py` is **not approved** because a distinct existing root-level investigation runner already owns task leases and business transitions.
- **Verification scope:** M4 Tasks 53–62 tests user-confirmed; Task 59 and 60 real DeepSeek protocol tests succeeded, but Task 60's evidence search was mocked; production RabbitMQ Worker/Graph E2E remains pending.

## October 28 maintenance and validation boundary

Tool selection has a 600-token output cap, and feedback has a 500-token cap.
Each successful tool step makes two Chat requests before optional structured
reanalysis; with three search rounds, up to seven Chat requests can occur.
No whole-Graph deadline or monetary budget is implemented. Full evidence fields
are retained for the selected top five records, rather than for unlimited hits.
History URL/identity checks and self-issue exclusion strengthen input boundaries;
free-text feedback remains outside verified findings.

`mark_ready_for_report` only sets a next-step marker. There is no report-writing
node, report table, report HTTP response, or Worker-to-Graph wiring. The generic
processor error boundary now exists in the original task runner, but actual
Graph/Worker integration and provider-specific policies still need validation.
New regression files cover source transactions, processing errors, cancellation,
malformed deliveries, visibility and provenance. They are prepared for local
execution; historical Task 53–62 results do not validate this modified revision.

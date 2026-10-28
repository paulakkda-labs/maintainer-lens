# ADR: Modular, bounded, evidence-first LangGraph investigation

- **Status:** Accepted; application execution and durable result boundaries added 2026-10-28.
- **Scope:** LangGraph investigation, its application boundary and preliminary result contract. Default Worker integration remains separate.

## Context

A single large graph file mixing orchestration, provider requests, evidence transformation, search sessions and branching is hard to reason about and regression-test. LLM outputs may select invalid queries or hallucinate citations. An unbounded investigation loop creates uncontrolled latency and cost. The existing M2 Worker owns reliable task execution and must not be duplicated.

## Decision

- Keep `agents/investigation_graph.py` narrowly responsible for registering graph nodes and edges.
- Separate typed State/evidence IDs and merges, RAG search adapters, structured analysis, routing, follow-up planning, actual tool-calling protocol, and execution-time controls into `agents/investigation/` modules.
- Run initial History RAG and optionally Source RAG, analyze evidence with a Pydantic structure containing cited findings/hypotheses/unanswered questions, then either stop or perform bounded follow-up. Configure a maximum search round count, deduplicate queries and evidence, and stop when no genuinely new evidence appears.
- Support distinct follow-up strategies, including an LLM Query Planner and actual model-selected `search_history` / `search_source` Function Calling; they should not independently cause redundant RAG searches on the same round.
- Python, not the LLM, validates Tool Schema, JSON parameters, tool whitelist, trusted repository/Index context, and `tool_call_id` association. Limit the first Tool Calling integration to a single allowed retrieval call per step. Treat free-text Tool feedback as commentary, not a validated Finding.
- Offer explicit caller-applied tool-step timeout and metadata-only logging wrappers, without swallowing cancellation or assuming remote costs stop immediately. The graph builder does not apply the wrapper automatically.
- Execute a complete investigation through `services/investigation_execution.py:execute_graph_investigation()`: validate shared input/options, compile the existing Graph, create fresh State, apply one whole-Graph deadline and validate a stable result. Keep client ownership, knowledge preparation and task lifecycle outside this boundary.
- Preserve the latest completed analysis input as `analysis_evidence`. Validate findings against that context and returned evidence, rather than reconstructing context from final rankings. Citation identity/URL checks do not establish semantic entailment.
- Persist one versioned JSONB preliminary result per task through a claim-fenced writer. The injected business processor commits Snapshot and result before returning to the original Runner. Readback exposes an artifact only for a succeeded task, supports legacy null results and rejects unsupported/corrupt representations.

## Consequences

- **Benefits:** Small testable units, explicit trust boundaries, less unnecessary retrieval work and more predictable cost.
- **Costs:** More adapter interfaces and testing; default Worker assembly still needs to supply a concrete provider/knowledge profile while preserving existing lifecycle and delivery semantics.
- **Ownership:** The root-level investigation runner remains the only claim/lease/status owner. The application boundary is not a competing task runner and adds no retry loop.
- **Deferred:** Persistent Checkpointer, HITL, multi-agent roles, monetary budgets and final report generation. A stored preliminary investigation is not a generated final report or a checkpoint.

## Runtime and verification boundaries

Tool selection has a 600-token output cap, and feedback has a 500-token cap.
Each successful tool step makes two Chat requests before optional structured
reanalysis; with three search rounds, up to seven Chat requests can occur.
The application entry supplies a whole-Graph deadline, distinct from tool-step
controls; no monetary budget is implemented. Full evidence fields
are retained for the selected top five records, rather than for unlimited hits.
History URL/identity checks and self-issue exclusion strengthen input boundaries;
free-text feedback remains outside verified findings.

`mark_ready_for_report` only sets a next-step marker. Preliminary result storage
and HTTP readback are implemented; a final report-writing node and default
Worker-to-Graph wiring are not. Own deadline expiry is distinguished from
upstream timeout using `timeout_scope.expired()`, including a check after normal
return when a dependency swallowed timeout cancellation. External cancellation
and dependency exception groups propagate at the application boundary.

Regression coverage includes real compiled Graphs with fake providers and
opt-in PostgreSQL persistence/Runner/readback tests. Historical live planner and
function-calling checks used DeepSeek, but the latter used mock retrieval data.
These checks do not establish a live default Worker-to-Graph E2E.

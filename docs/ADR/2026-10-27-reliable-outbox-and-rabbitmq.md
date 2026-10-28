# ADR: Transactional Outbox with RabbitMQ for asynchronous investigations

- **Status:** Accepted in the established M1–M2 implementation (reconstructed 2026-10-27).
- **Scope:** V1 task submission, message publication, worker delivery.

## Context

Investigating Issues through GitHub and ultimately through RAG/LLMs is too slow and failure-prone for a synchronous HTTP request. Creating a task in PostgreSQL and separately publishing to a broker is a dual-write problem: one can succeed while the other fails. Broker delivery can be duplicated or delayed.

## Decision

A FastAPI request transactionally creates an `InvestigationTask` and its `InvestigationOutboxEvent`. A separate outbox dispatcher claims unpublished events, validates typed payloads, publishes persistent RabbitMQ messages with publisher confirms, then marks the outbox event published in a second database transaction. On publish failure, the claimed delivery is released/scheduled for retry with ownership checks. Worker messages travel through pending, retry and dead-letter routes. Consumers ACK only after their handler returns successfully. PostgreSQL remains authoritative for task state; broker messages are signals to perform work.

## Consequences

- **Benefits:** HTTP remains responsive; a committed task survives broker outages; dispatcher and worker scale independently; retries and dead-letter behavior are explicit.
- **Costs:** PostgreSQL/broker/dispatcher operations are more complex; the broker-confirm-to-DB-mark crash window allows duplicate messages; there is no end-to-end exactly-once guarantee.
- **Malformed deliveries:** Invalid JSON/schema bodies and invalid retry metadata are republished to the dead queue with safe retry headers. The Consumer ACK occurs only after confirmed dead-letter publication; a publication failure leaves the original unacknowledged.
- **Required controls:** atomic task claim, claim tokens, lease fencing and idempotent DB changes. No long-running database transaction around the network publish.
- **Alternatives considered:** synchronous Issue fetching in HTTP request; a PostgreSQL-only polling task queue. The latter is valid for smaller deployments; this design uses RabbitMQ as an independently operated transport with explicit routing, confirms and retry/dead-letter queues.

## Source anchors and verification

Under `src/maintainer_lens/`: `services/investigation_submission.py`, `db/outbox.py`, `services/outbox_dispatcher.py`, process `outbox_dispatcher.py`, `messaging/publisher.py`, `messaging/handler.py`, `messaging/consumer.py`, `worker.py` and `db/investigation_tasks.py`. The dispatcher uses an explicit publish deadline and token-matched finalization. Snapshot-path E2E and broker integration coverage do not establish a default Worker-to-Graph investigation.

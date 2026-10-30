# Using MaintainerLens

[Documentation](README.md) · [Installation](INSTALLATION.md) · [Configuration](CONFIGURATION.md)

## Investigate an Issue

Open `http://127.0.0.1:8000/` after starting the stack. Paste a public URL such as
`https://github.com/pypa/sampleproject/issues/225`, choose a preparation budget,
and submit. The backend selects/prepares knowledge; you do not need a Source UUID.

The workspace shows Task ID, repository/Issue, language, current state and recorded
execution phases. It polls committed detail/events approximately every two seconds
while active. Progress is a sequence of recorded stages, not an estimated percentage
or a separate completed event for every upstream HTTP request.

| Displayed outcome | What to do |
| --- | --- |
| Pending/running | Keep the page open or return later using the Task ID |
| Completed report | Read the preview, inspect evidence, copy or download Markdown |
| More evidence needed | Read the completed preliminary analysis and its explicit gaps |
| Waiting for review | Inspect the reason and available explicit decisions |
| Failed | Read the safe error and use execution records for investigation |

Navigating away does not cancel server work. GET polling failures display read
uncertainty and retry reads; they do not automatically repeat submissions.

## Languages and reports

The first visit is English. The EN/中文 switch remembers browser preference and
translates interface labels, recorded phases and safe error messages. Submission
snapshots `response_language` for the task. Original source/comment text and server
technical logs are not automatically translated.

Changing the UI language does not rewrite a saved report or call a model.
An explicit alternate-language report action submits/resolves an investigation
in that language. Language-specific tasks can share compatible prepared knowledge.

Reports default to preview. Raw Markdown, copy and `.md` download are available.
The report separates findings, hypotheses, unanswered questions, actual analysis
excerpts and execution notes. Source links identify an immutable commit/file/line
range; history links refer to GitHub objects. Citations are not proof of a root cause.

`needs_more_evidence` is a legitimate succeeded investigation. The model may need
more evidence than the bounded index/context provides. V1 does not run repository
tests or apply a patch to confirm a suggested fix.

## Repeated URLs and freshness

Repeated submission can resolve active work, reuse a saved report, start an updated
investigation, or recover an eligible failed task. Identity includes language,
preparation budget and execution configuration; the same URL does not always mean
the same Task.

A saved-report check observes GitHub metadata for up to 15 seconds: Issue title,
body, state, update time and the default-branch commit. A change can queue new work
while keeping the previous report. A failed check can return the old report with
latest status marked unknown. The check does not cover all linked/unlinked PRs,
comments or discussions. An earlier displayed report identifies its owning Task.

Closed Issue state is displayed with priority; whether a code fix exists and
which revision it applies to remain evidence questions. An eligible failed Task
can have at most three user-triggered recoveries, retaining compatible checkpoint
progress. Permanent failures are not automatically resumed.

## Human review

Normal reports do not require approval. Review can pause a task when Source
preparation exceeds a supported limit or its deadline, or when analysis output
is truncated by its output allowance.

| Decision | Boundary |
| --- | --- |
| Increase budget and continue | Select an eligible preparation profile or finite output allowance; no silent Unlimited approval |
| Accept complete stage analysis | Available only if a complete, structurally/reference-valid earlier analysis exists; later unfinished work remains disclosed |
| Reject and finish | Ends this review path without presenting incomplete JSON as a report |

The interface exposes only eligible actions and lets you inspect an available
stage analysis and its evidence before accepting it. Budget approval is limited
to three continuations per Task, separate from the three failed-task user recoveries.
Waiting does not expire automatically. Repeating the URL only displays waiting;
it does not approve the request. Duplicate decisions use idempotency protection.

## Execution records and retained workspaces

Open `/logs` and enter a Task ID or Issue URL. URL lookup is read-only and lists
existing tasks; it does not prepare knowledge, contact GitHub or call a model.
Select a task to see saved phases, generations and safe errors. Technical Docker
logs are a separate developer view: [troubleshooting](TROUBLESHOOTING.md).

Investigation and records have independent retained state across navigation.
Refresh restores task references and fetches saved results with GET rather than
submitting again. Session storage keeps references/profile and local storage keeps
language; PostgreSQL owns the report. Clearing/closing browser sessions can lose
local references. There is no account-based history or conversational memory.

## HTTP API

Swagger is at `/docs` and the schema is at `/openapi.json`. API clients should
send the desired language explicitly; the public default is English.

```bash
curl -X POST http://127.0.0.1:8000/investigations/from-url \
  -H 'Content-Type: application/json' \
  -d '{"issue_url":"https://github.com/pypa/sampleproject/issues/225","response_language":"en","knowledge_budget_profile":"small"}'
```

Copy the returned Task ID, then read without creating more work:

```bash
INVESTIGATION_TASK_ID='paste-returned-task-id'
curl "http://127.0.0.1:8000/investigations/$INVESTIGATION_TASK_ID"
curl "http://127.0.0.1:8000/investigations/$INVESTIGATION_TASK_ID/events?after=0&limit=100"
```

| Endpoint | Purpose |
| --- | --- |
| `POST /investigations/from-url` | Resolve/create work with automatic knowledge preparation; may incur charges |
| `GET /investigations/{task_id}` | Task, Issue snapshot, progress/errors and available result/`report_markdown` |
| `GET /investigations/{task_id}/events` | Paginated committed events; continue with `next_after` |
| `GET /investigations/lookup?issue_url=...` | Read-only URL task history; no freshness or provider call |
| `GET /investigations/budget-profiles` | Available knowledge-preparation profiles |
| `GET /investigations/{task_id}/reviews` | Review requests and eligible decisions |
| `POST /investigations/{task_id}/reviews/{review_id}/decision` | Explicit validated decision; use the Swagger request contract |

The detail's `result` and `report_markdown` are available only after success;
legacy snapshot-only tasks may legitimately have no report. Preview is rendered
from stored validated analysis, so reading an existing report is not another LLM
request. The Web renderer excludes raw HTML, remote images and unsafe links.

For API contracts and readback boundaries, see [architecture](ARCHITECTURE.md).

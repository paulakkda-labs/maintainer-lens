# Deployed report acceptance and human quality review

This first M6 slice separates a working deployed pipeline, structural evidence
checks and human assessment. It is not a broad accuracy benchmark. The maintainer
confirmed offline protocol/evaluation tests and one selected cached deployment
check passed. The other two deployed cases and final human assessment are pending.

## Small review set

The [JSON review set](../tests/fixtures/reports/sampleproject.json) contains three
public Issues from one small repository, `pypa/sampleproject`. Linked pages
provide reference observations, not frozen expected answers. Current state,
source HEAD and discussions can change; each run captures the actual snapshot,
result and freshness response.

| Case ID | Public source | Review focus |
| --- | --- | --- |
| `license-metadata` | [Issue #225](https://github.com/pypa/sampleproject/issues/225) | Compare reported configuration warnings with retrieved source; avoid unsupported release/build claims |
| `cli-usage` | [Issue #221](https://github.com/pypa/sampleproject/issues/221) | Distinguish a usage request from a demonstrated defect; support entry-point statements and qualify debugging suggestions |
| `misdirected-closed-issue` | [Issue #200](https://github.com/pypa/sampleproject/issues/200) | Distinguish closure from a fix; the public discussion identifies a report sent to the wrong repository |

The closed example is deliberately difficult for bounded retrieval: relevant
comments might not be in prepared History, and the investigated Issue is not
independent historical evidence. Missing context should be uncertainty, not an
invented fix. This set establishes no multi-language coverage, root-cause
accuracy rate or PR/comment ranking score. See [Supported languages](SUPPORTED_LANGUAGES.md).

## Automated checks versus human judgments

The pure [evaluation module](../src/maintainer_lens/evaluation/report_quality.py)
reuses report/provenance validators, then records finding counts, distinct cited
IDs, retrieved/analyzed/cited History and Source counts, saved rounds, terminal
marker, hypotheses and unanswered questions. These are observations, not scores.
A later retrieval does not mean that evidence was analyzed. Valid
`needs_more_evidence`, zero findings and explicit gaps are legitimate outcomes.
No LLM judge, keyword match or citation count certifies correctness.

Review each finding against its **saved analysis excerpt**, then open its
original GitHub object or commit-pinned source. Record one judgment per finding:

| Judgment | Meaning |
| --- | --- |
| Supported | Cited material supports the claim within its scope/version |
| Unsupported | The claim exceeds or contradicts its cited material |
| Uncertain | Evidence is insufficient; name the missing check |

Also assess retrieval relevance, fact/hypothesis separation, GitHub state versus
technical repair, version applicability and whether the next action is useful.
A correct but irrelevant finding is not useful evidence. Record `pass`,
`needs_improvement` or `fail` with a reason; pytest alone is not a pass. A critical
unsupported root-cause/fix claim fails human review. Report individual cases and
coverage gaps rather than extrapolating percentages from three prompts.

## Initial observed review gaps

The first selected `license-metadata` deployment check reused an existing report;
it validates current cached readback/freshness, not cold preparation or model
latency. Comparing its saved analysis context with the returned evidence exposed
two issues for focused follow-up:

- A release workflow containing an upgrade/build command was cited for a definite
  warning/failure conclusion. The excerpt establishes configuration, not actual
  resolved versions or execution output; that outcome needs qualification.
- A relevant License classifier was present in a retrieved source chunk but
  outside the first 1,600 characters supplied to analysis. The report correctly
  lacked direct visibility of it, while retrieval itself had found the material.

These are assistant-assisted review observations, not a human-approved accuracy
result. Preserve the original report and its actual context; do not silently
repair saved findings or count the single sample as general report quality.
These observations describe the original v1 report, which remains unchanged.

### Focused v2 correction — tests and one deployed sample confirmed

Analysis now preserves normal Source chunks up to the existing automatic
chunker's 4,000-character boundary rather than taking only 1,600 characters.
Oversized external/legacy chunks retain a bounded, explicitly marked excerpt.
History, Top-5 selection, Graph rounds/deadlines and output allowances are
unchanged. This improves visibility when relevant lines are inside a selected
chunk; it does not guarantee retrieval of every relevant chunk.

The analysis prompt now distinguishes configuration from execution evidence,
attributes reported warnings/deadlines to their source and qualifies unverified
outcomes. The maintainer confirmed correction tests and one updated-deployment
case passed. Assistant-assisted comparison of its saved excerpts confirmed that
the previously omitted classifier was supplied and recognized, while reported
warnings were attributed and execution outcomes remained unverified.

The trace selected a new Task for `execution_changed`, retained the previous
report, reused the existing Source index and subsequently reused the new report
with current scoped freshness. This is a new investigation with prepared
knowledge, not an empty-database cold build or an isolated model latency test.
One redundant adjacent citation and weak History relevance remain improvement
notes. These observations validate the two targeted corrections in one sample;
other case outcomes, final human assessment and general semantic quality are
not certified. Mocked-provider tests alone do not prove model obedience.

The v2 correction changed execution identity while preserving old reports and
Source knowledge. The current v3 correction below is a further identity change.
No table or storage-format migration is required. Rebuild the shared application
image and recreate API/Dispatcher/Worker together as described in README. A
frozen Run from a previous execution version cannot resume under the new one;
review approvals also require compatible execution. Waiting reviews are not
silently approved: retain the compatible deployment to finish them, or explicitly
reject and resubmit to create new work.

### Closed-Issue output contract correction — deployed recheck passed

The CLI deployment check subsequently passed, but the closed-Issue case failed
during first analysis. Its saved checkpoint error identified an empty
`findings[0].evidence_ids` array. The existing schema correctly rejected that
unsupported finding; this was not a database failure or normal insufficient
evidence result. HTTP 200 alone did not establish valid model output.

The current `investigation-v3` prompt supplies the JSON Schema generated from
the same local analysis model and explicitly distinguishes an empty `findings`
array (valid) from an empty citation array inside a finding (invalid). When
retrieved material does not support a finding, the model should explain the
scope/gaps in the other fields. The provider request stays in compatible
`json_object` mode; this does not require vendor-specific strict schema support.
It is guidance, not a guarantee of model compliance.

Runtime constraints and error classification remain unchanged. No invalid
finding is deleted, supplied with fabricated citations or converted into a
successful report; no automatic repair/model retry is introduced. Regression
tests cover a valid empty-findings result with nonempty unrelated evidence and
the exact empty-citation failure, requiring one provider call in either case.
The maintainer subsequently passed the selected v3 closed-Issue deployment
recheck: one passed, two unselected cases skipped. Its trace created a new Task
for changed execution, retained the failed Task as history, reused existing
Source knowledge and finally reused the new report with current scoped freshness.
The measured 28.179 seconds cover the HTTP acceptance flow, not isolated model
latency or a cold knowledge build. The maintainer also confirmed the focused
v3 offline regression files passed, without reporting an additional test count.

Saved-context review confirmed a valid empty `findings` array, three search
rounds and an insufficient-evidence result. Closure was displayed separately
from repair claims; possible dependency causes remained hypotheses. No supported
finding or cross-repository source investigation was fabricated. The actual
analysis context did not include the comment explaining misdirection, and the
retrieved History item was unrelated; the report therefore asked about repository
relevance rather than establishing why the Issue was closed. These remain
coverage/relevance limitations. Final human semantic assessment is pending.

Rebuild/recreate API and Worker together for v3. Earlier v1/v2 Runs are not
silently resumed with the changed prompt; normal URL freshness selects a new
investigation for changed execution, including the previously failed v2 case.
Old failed records/checkpoints remain available for diagnostics. No migration
or direct database status change is required.

Run the focused offline regressions first:

```bash
uv run --locked pytest -q \
  tests/agents/test_investigation_analysis_context_quality.py \
  tests/agents/test_investigation_application_execution.py \
  tests/agents/test_investigation_analysis.py \
  tests/agents/test_investigation_graph_analysis.py \
  tests/test_investigation_checkpoint_contracts.py \
  tests/test_investigation_checkpoint_execution.py \
  tests/test_investigation_url_resolution.py \
  tests/test_worker_production_wiring.py
```

Then run default regression and the existing real PG/MQ suites under their
documented isolation rules before updating the deployment. The following live
command should exercise the updated API/Worker; earlier v1/v2 passes do not
validate this correction. Select `misdirected-closed-issue` for the focused recheck.

## Offline acceptance

This command uses fixtures and HTTP MockTransport only. No running services,
database, broker or paid calls are needed:

```bash
uv run --locked pytest -q \
  tests/evaluation/test_investigation_report_quality.py \
  tests/integration/test_deployed_investigation_report_quality.py
```

Offline protocol tests cover pending-to-success, early-report rejection,
failed/waiting readback, unverified freshness, changed Task and client wait
expiry. They do not prove a deployed installation works.

## Live HTTP acceptance: keep application services running

Keep PostgreSQL, RabbitMQ, API, Dispatcher and Worker up. Do not use the
stop-services recipe intended for shared-Outbox integration fixtures. Prefer a
dedicated validation deployment.

```bash
RUN_DEPLOYED_REPORT_QUALITY=1 \
REPORT_QUALITY_BASE_URL=http://127.0.0.1:8000 \
REPORT_QUALITY_CASES=license-metadata \
uv run --locked pytest -q -s \
  tests/integration/test_deployed_investigation_report_quality.py::test_deployed_url_to_report_quality
```

Default selection is the first case; others skip. To deliberately review all
three, replace the selection in that command with:

```bash
REPORT_QUALITY_CASES=license-metadata,cli-usage,misdirected-closed-issue
```

Use the port configured by `API_HOST_PORT` in `REPORT_QUALITY_BASE_URL`. This
first test accepts only loopback origins and carries no API credentials. LLM
and Embedding keys remain in the deployment's configuration; pytest does not
load those keys or call providers directly.

The test checks liveness, submits a URL with the existing `small` profile,
repeats active work when observed, polls details, validates result/report and
observed state, reads events and verifies same-Task reuse with current scoped
freshness. A cached result is recorded as such; it does not prove cold knowledge
preparation. Use a clean validation deployment for a cold run. The test neither
resets databases nor forces new work through an invented API parameter.

Failure or review waiting ends the check with its saved safe reason. There are
no automatic approvals, budget increases, recovery loops or deployed fault
injection. Changed inputs or failed metadata checks may prevent a reuse
assertion; inspect trace decisions/freshness before retrying. A report can still
be usable as historical/unverified information. Controlled fault handling
remains covered by separate offline and PG/MQ suites.

## Costs, resources and artifacts

Live submissions may call real GitHub, Embedding and LLM providers through the
Worker. Small-repository cases and the bounded preparation profile constrain
scope; configured Worker output allowances and provider limits still apply.
This is not a monetary cap or a provider-request interceptor. Start with one
case and inspect provider-side usage before selecting all three.

The client waits at most 600 seconds with per-request timeouts. Expiry or
stopping pytest does not cancel server work. Tasks, knowledge and reports
remain in the deployment for normal reuse. There is no deletion API, so this
HTTP-only test does not erase records through SQL. It performs no migrations,
broker manipulation or application process startup/shutdown.

Each selected run prints its Task ID and ignored artifact directory:

```text
.local/report-quality/<run UUID>/<case ID>/
  trace.json     HTTP responses, decisions and structural observations
  report.md      successful report, when the complete smoke passes
  review.md      case-specific human review form, initially pending
```

On failure, the trace retains completed HTTP exchanges and known Task IDs. Error
type is recorded without dumping local configuration. Public-repository reports
and evidence stay local rather than being automatically committed.

`elapsed_http_seconds` includes queue waiting, polling and freshness checks;
it is not model execution time or a cold-run comparison. Exact tokens/cost are
not exposed by the public API and must be recorded from provider measurements.
Keep semantic review pending until the per-finding checks are complete. No new
table, metric persistence service or migration is introduced.

# Retrieval and report evaluation

[Documentation](README.md) · [Development/tests](DEVELOPMENT.md) · [Supported languages](SUPPORTED_LANGUAGES.md)

A working pipeline, relevant retrieval and a correct explanation are different
properties. V1 release readiness focuses on installability and normal application
flows; retrieval/report correctness remains an ongoing improvement area. None of
the checks below establishes universal accuracy or a high-availability SLA.

## What each check establishes

| Layer | Useful observations | What it does not prove |
| --- | --- | --- |
| Workflow | Startup, migration, delivery, progress, result persistence, reuse/recovery/review | Correct root cause or useful evidence |
| Structure/provenance | Valid analysis fields, repository/index boundaries, actual-context citation IDs and URLs | That the cited text entails every statement |
| Retrieval | Labeled relevant results, Recall@k, MRR@k, rank comparisons | A correct final explanation |
| Report semantics | Per-claim support, relevance, version applicability, calibrated uncertainty | Correctness across arbitrary repositories/models |

## Retrieval evaluation

Use questions with repository/revision scope and explicit relevance labels.
Evaluate Source and History separately, and record provider/model, dimensions,
corpus contents and retrieval strategy. Compare semantic retrieval with Source's
optional literal/RRF path on the same data.

- **Recall@k:** how many labeled relevant targets appear in the first k results.
- **MRR@k:** reciprocal rank of the first relevant result, averaged across queries.
- Also inspect failure cases, irrelevant high-ranked results, incomplete labels
  and how many distinct targets the questions actually cover.

Small existing fixtures and Python-oriented measurements are useful regressions,
not population accuracy estimates or evidence of equal performance across all
listed source languages. See [retrieval fixtures](../tests/fixtures/retrieval/)
and [evaluation code](../src/maintainer_lens/evaluation/). Broaden labels/corpora
before drawing broader conclusions or adding ranking infrastructure.

## Report review

Review each finding against **its own cited saved `analysis_evidence`**, not
whatever ranks highest after later retrieval. Open the original public object or
commit-pinned source when useful, and distinguish stored-context review from
independent verification.

| Judgment | Meaning |
| --- | --- |
| Supported | The cited excerpt supports all factual clauses within its revision/scope |
| Unsupported | A clause exceeds or contradicts the cited material |
| Uncertain | Name the missing code, documentation or execution check |

Check hypotheses and suggested actions too: an unverified explanation may be
reasonable, but a false factual premise or invalid concrete configuration edit
is still a quality defect. Distinguish reporter observations, repository
configuration, proposed PR changes, merged code and actual execution evidence.
Receiving an argument is not proof that it is used or forwarded.

A valid `findings: []` result and `needs_more_evidence` can be appropriate.
Conversely, valid IDs and a passing pytest do not certify semantic support.
Record critical unsupported claims and useful next steps rather than assigning
an accuracy percentage from a handful of examples. Known limitations include
weak History relevance and possible incorrect code explanations despite valid citations.

## Existing review cases

These catalogs contain human review prompts, not frozen model answers:

| Case | Focus | Preparation profile |
| --- | --- | --- |
| `license-metadata` | Sampleproject configuration/warning attribution | Small |
| `cli-usage` | Usage question versus a demonstrated defect | Small |
| `misdirected-closed-issue` | Closure versus a verified repair | Small |
| `grounding-reporter-attribution` | A citation must support the reporter-related claim | Standard |
| `grounding-keyword-scope` | Actual parameter flow and variable scopes | Standard |
| `grounding-config-semantics` | Distinct configuration fields and actionable suggestions | Standard |

Catalogs: [sampleproject](../tests/fixtures/reports/sampleproject.json) and
[grounding regressions](../tests/fixtures/reports/grounding_regressions.json).
Two cases intentionally inspect the same Issue with different focus/budgets;
they are not independent benchmark samples. Live Issue/HEAD/discussions can change.

## Offline and deployed checks

```bash
uv run --locked pytest -q \
  tests/evaluation/test_investigation_report_quality.py \
  tests/integration/test_deployed_investigation_report_quality.py
```

Without explicit live opt-in, deployed cases skip and the offline protocol tests
use HTTP MockTransport. The pure evaluation helper validates existing
structure/provenance and counts retrieved/analyzed/cited evidence; it is not an LLM judge.

For the paid HTTP command and service-isolation rules, follow
[deployed acceptance](DEVELOPMENT.md#deployed-url-acceptance). Select case IDs with
`REPORT_QUALITY_CASES`, comma-separated. Grounding cases use Standard and may
prepare more knowledge than the default Small case.

The HTTP path checks liveness, submission/active deduplication, detail/events,
validated report readback and current scoped reuse. It never approves a human
review automatically. A client deadline stops waiting without cancelling the
server task. A failure or waiting state is preserved for diagnosis.

Each run exports `trace.json`, `report.md` when available and `review.md` under
ignored local storage. The review starts `pending`; keep it that way until a
person records their assessment. Record provider-measured token usage/cost
separately. End-to-end HTTP duration is not isolated model latency or a cost measurement.

## Improving the system

Keep input, revision, evidence context and model settings fixed when comparing a
change. Track first-pass schema validity, the single correction's outcome,
retrieval metrics and semantic errors separately. Do not change evaluation labels
to hide a regression or count an old cached report as a new model response.

Use concrete counterexamples to prioritize changes, then verify workflow and
content effects independently. No automatic rule set, citation counter or
schema-correction call currently certifies the factual content of a report.

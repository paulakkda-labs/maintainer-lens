"""HTTP-only deployment smoke with offline protocol regressions.

Keep API/Dispatcher/Worker running. Live checks are opt-in and may incur model
charges and leave durable tasks/knowledge. No SQL, broker client, ASGI transport,
Worker startup or human approval is used by the live path.
"""

import asyncio
import ipaddress
import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from maintainer_lens.evaluation.report_quality import (
    ReportReviewCase,
    inspect_report_evidence,
    load_report_review_dataset,
)
from maintainer_lens.schemas.investigation_api import InvestigationDetailResponse
from maintainer_lens.schemas.investigation_budget import (
    make_knowledge_preparation_budget,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationResult
from maintainer_lens.schemas.investigation_progress import InvestigationEventPage
from maintainer_lens.schemas.investigation_reuse import ResolveInvestigationResponse
from maintainer_lens.services.investigation_report import render_investigation_report

ROOT = Path(__file__).resolve().parents[2]
DATASET = load_report_review_dataset(ROOT / "tests/fixtures/reports/sampleproject.json")
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _local_base_url(raw):
    url = httpx.URL(raw)
    if (
        url.scheme not in ("http", "https")
        or url.userinfo
        or url.path != "/"
        or url.query
        or url.fragment
    ):
        raise ValueError("Use a local API origin without credentials, path or query")
    if url.host != "localhost":
        try:
            local = ipaddress.ip_address(url.host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise ValueError("Deployed acceptance targets a loopback API only")
    return str(url)


async def _exercise_api(
    client, case: ReportReviewCase, trace, *, timeout_seconds=600.0, poll_interval=1.0
):
    """Exercise the deployed public protocol; never create internal test rows."""
    started = time.monotonic()

    async def request(method, path, *, response_model=None, **kwargs):
        response = await client.request(method, path, **kwargs)
        entry = {"method": method, "path": path, "status": response.status_code}
        trace.setdefault("http", []).append(entry)
        assert response.status_code in (200, 202), (
            f"{method} {path}: HTTP {response.status_code}"
        )
        payload = response.json()
        entry["payload"] = payload
        # JSON has string timestamps/UUIDs by design. Keep JSON-mode validation
        # for strict nested models instead of treating decoded data as Python.
        return (
            response_model.model_validate_json(response.content)
            if response_model is not None
            else payload
        )

    async with asyncio.timeout(timeout_seconds):
        assert await request("GET", "/health/live") == {"status": "ok"}
        submission = {"issue_url": case.issue_url, "knowledge_budget_profile": "small"}
        initial = await request(
            "POST",
            "/investigations/from-url",
            json=submission,
            response_model=ResolveInvestigationResponse,
        )
        trace["task_id"] = initial.task_id
        if initial.status in ("pending", "running"):
            duplicate = await request(
                "POST",
                "/investigations/from-url",
                json=submission,
                response_model=ResolveInvestigationResponse,
            )
            assert duplicate.task_id == initial.task_id, (
                "Inputs changed during active URL check; inspect both task IDs in trace"
            )
        while True:
            detail = await request(
                "GET",
                f"/investigations/{initial.task_id}",
                response_model=InvestigationDetailResponse,
            )
            assert (detail.task_id, detail.repository, detail.issue_number) == (
                initial.task_id,
                case.repository,
                case.issue_number,
            )
            if detail.status == "succeeded":
                break
            assert detail.result is None and detail.report_markdown is None
            if detail.status in ("failed", "waiting_review"):
                if detail.status == "waiting_review":
                    await request("GET", f"/investigations/{initial.task_id}/reviews")
                code = detail.last_error.code if detail.last_error else "unavailable"
                raise AssertionError(
                    f"Task {detail.task_id}: {detail.status}; reason={code}; no automatic approval or retry"
                )
            await asyncio.sleep(poll_interval)
        assert (
            detail.auto_prepare_knowledge
            and detail.prepared_source_index_id is not None
        )
        assert detail.result is not None and detail.report_markdown
        result = detail.result
        assert (result.repository, result.issue_number, result.source_index_id) == (
            case.repository,
            case.issue_number,
            detail.prepared_source_index_id,
        )
        observations = inspect_report_evidence(result)
        assert render_investigation_report(result) in detail.report_markdown
        assert detail.issue is not None
        state = detail.freshness.observed_issue_state or detail.issue.state
        labels = {
            "zh": {"open": "打开中", "closed": "已关闭"},
            "en": {"open": "Open", "closed": "Closed"},
        }
        assert (
            f"**{labels[result.response_language][state]}**" in detail.report_markdown
        )
        await request(
            "GET",
            f"/investigations/{initial.task_id}/events?limit=100",
            response_model=InvestigationEventPage,
        )
        cached = await request(
            "POST",
            "/investigations/from-url",
            json=submission,
            response_model=ResolveInvestigationResponse,
        )
        assert cached.task_id == cached.report_task_id == initial.task_id, (
            "Freshness check selected different work; inspect trace before retrying"
        )
        assert cached.decision == "reused" and cached.freshness.status == "current", (
            "Latest metadata was not confirmed current; inspect decision/freshness in trace"
        )
        assert (
            cached.report_markdown
            and render_investigation_report(result) in cached.report_markdown
        )
        trace.update(
            initial_decision=initial.decision,
            elapsed_http_seconds=round(time.monotonic() - started, 3),
            observations=asdict(observations),
            semantic_review="pending",
            provider_usage="not exposed by the public API; consult provider usage separately",
        )
        return detail


def _save_artifacts(folder, case, trace, detail=None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "trace.json").write_text(
        json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"# Report review: {case.id}",
        "",
        case.issue_url,
        "",
        "Status: pending human review.",
        "",
        "## Review focus",
        *[f"- {item}" for item in case.review_focus],
        "",
        "## Claims requiring particular scrutiny",
        *[f"- {item}" for item in case.unsupported_claims],
        "",
        "## Human assessment",
        "",
        "For each finding record: supported / unsupported / uncertain; cite the exact excerpt and explain the judgment.",
        "",
        "Check relevance, version applicability, declared gaps and a useful next action. Record latency and provider-measured cost separately.",
        "",
        "Final decision: pending. Do not equate pytest success with semantic correctness.",
        "Deployment LLM/Embedding model names (no keys): pending.",
        "Provider-measured token usage and cost: pending.",
    ]
    (folder / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if detail is not None:
        (folder / "report.md").write_text(detail.report_markdown, encoding="utf-8")


@pytest.mark.parametrize("case", DATASET.cases, ids=lambda case: case.id)
async def test_deployed_url_to_report_quality(case):
    if os.getenv("RUN_DEPLOYED_REPORT_QUALITY") != "1":
        pytest.skip("HTTP deployment acceptance is disabled; may incur provider costs")
    selected = set(os.getenv("REPORT_QUALITY_CASES", "license-metadata").split(","))
    assert selected <= {item.id for item in DATASET.cases}, (
        "Unknown REPORT_QUALITY_CASES ID"
    )
    if case.id not in selected:
        pytest.skip("Case not selected for this paid/deployed acceptance run")
    base = _local_base_url(
        os.getenv("REPORT_QUALITY_BASE_URL", "http://127.0.0.1:8000")
    )
    folder = ROOT / ".local/report-quality" / uuid4().hex / case.id
    trace = {
        "case": case.model_dump(mode="json"),
        "dataset_observed_on": DATASET.observed_on.isoformat(),
        "started_at": datetime.now(UTC).isoformat(),
        "api_origin": base,
        "semantic_review": "pending",
    }
    detail = None
    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=20.0, follow_redirects=False, trust_env=False
        ) as client:
            detail = await _exercise_api(client, case, trace)
    except BaseException as error:
        trace["error_type"] = type(error).__name__
        raise
    finally:
        _save_artifacts(folder, case, trace, detail)
        print(
            json.dumps(
                {
                    "case": case.id,
                    "task_id": trace.get("task_id"),
                    "artifacts": str(folder),
                    "elapsed_http_seconds": trace.get("elapsed_http_seconds"),
                    "semantic_review": "pending",
                },
                ensure_ascii=False,
            )
        )


def _fake_detail(case, *, status="succeeded", code=None):
    index = "3b111111-1111-4111-8111-111111111111"
    result = InvestigationResult(
        repository=case.repository,
        issue_number=case.issue_number,
        source_index_id=index,
        issue_title="Review example",
        issue_body=None,
        response_language="en",
        next_step="needs_more_evidence",
        preliminary_analysis={
            "summary": "No evidence.",
            "findings": [],
            "hypotheses": [],
            "unanswered_questions": ["What can be checked?"],
        },
        history_evidence=[],
        source_evidence=[],
        analysis_evidence=[],
        search_round=1,
        routing_reason="No indexed evidence.",
    )
    return {
        "task_id": "offline-task",
        "repository": case.repository,
        "issue_number": case.issue_number,
        "auto_prepare_knowledge": True,
        "prepared_source_index_id": index,
        "knowledge_budget": make_knowledge_preparation_budget("small").model_dump(
            mode="json"
        ),
        "status": status,
        "issue": {
            "title": result.issue_title,
            "body": None,
            "state": "closed",
            "source_url": case.issue_url,
            "source_created_at": "2026-10-29T00:00:00Z",
            "source_updated_at": "2026-10-29T00:00:00Z",
            "fetched_at": "2026-10-29T00:00:00Z",
        },
        "result": result.model_dump(mode="json") if status == "succeeded" else None,
        "report_markdown": "Issue: **Closed**\n\n" + render_investigation_report(result)
        if status == "succeeded"
        else None,
        "progress": {
            "event_id": 1,
            "generation": 0,
            "kind": "completed" if status == "succeeded" else "progress",
            "phase": "completed" if status == "succeeded" else "analyzing",
            "search_round": 1,
            "created_at": "2026-10-29T00:00:00Z",
        },
        "freshness": {"status": "not_checked", "checked_at": "2026-10-29T00:00:00Z"},
        "last_error": {
            "code": code,
            "message": "Safe failure.",
            "retryable": False,
            "not_before": "2026-10-29T00:00:00Z",
        }
        if code
        else None,
    }


def _resolution(
    *,
    decision="reused",
    status="succeeded",
    freshness="current",
    task_id="offline-task",
):
    return {
        "task_id": task_id,
        "status": status,
        "decision": decision,
        "generation": 0,
        "report_task_id": task_id if status == "succeeded" else None,
        "report_markdown": "Saved report." if status == "succeeded" else None,
        "freshness": {"status": freshness, "checked_at": "2026-10-29T00:00:00Z"},
    }


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com",
        "http://user:key@localhost:8000",
        "http://localhost:8000/prefix",
        "http://localhost:8000?x=1",
    ],
)
def test_deployed_acceptance_rejects_nonlocal_or_ambiguous_target_before_requests(
    origin,
):
    with pytest.raises(ValueError):
        _local_base_url(origin)


@pytest.mark.parametrize("state", ["failed", "waiting_review"])
async def test_http_protocol_stops_on_safe_failure_or_review_without_deciding(state):
    case, calls = DATASET.cases[0], []

    def transport(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/health/live":
            payload = {"status": "ok"}
        elif request.url.path == "/investigations/from-url":
            payload = _resolution(
                status=state,
                decision="blocked" if state == "failed" else "awaiting_review",
            )
        elif request.url.path.endswith("/reviews"):
            payload = []
        else:
            payload = _fake_detail(case, status=state, code="analysis_generation_limit")
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(transport)
    ) as client:
        with pytest.raises(AssertionError, match=state):
            await _exercise_api(client, case, {}, poll_interval=0)
    assert sum(method == "POST" for method, _ in calls) == 1
    assert not any(path.endswith("/decision") for _, path in calls)


@pytest.mark.parametrize(
    "change",
    [
        "normal",
        "early-report",
        "unknown-freshness",
        "different-task",
        "invalid-progress-time",
        "invalid-event-id",
        "invalid-event-time",
        "invalid-freshness-time",
    ],
)
async def test_http_protocol_polls_real_shapes_and_distinguishes_reuse_failures(change):
    case = DATASET.cases[0]
    posts, reads = 0, 0
    saved = _fake_detail(case)
    if change == "invalid-progress-time":
        saved["progress"]["created_at"] = "not-a-timestamp"
    if change == "invalid-event-id":
        saved["progress"]["event_id"] = "1"

    def transport(request):
        nonlocal posts, reads
        if request.url.path == "/health/live":
            payload = {"status": "ok"}
        elif request.method == "POST":
            posts += 1
            if posts < 3:
                payload = _resolution(
                    decision="created" if posts == 1 else "active", status="pending"
                )
            else:
                payload = _resolution(
                    decision="unverified"
                    if change == "unknown-freshness"
                    else "reused",
                    freshness="unknown" if change == "unknown-freshness" else "current",
                    task_id="different"
                    if change == "different-task"
                    else "offline-task",
                )
                payload["report_markdown"] = saved["report_markdown"]
                if change == "invalid-freshness-time":
                    payload["freshness"]["checked_at"] = "not-a-timestamp"
        elif request.url.path.endswith("/events"):
            event = {**saved["progress"]}
            if change == "invalid-event-time":
                event["created_at"] = "not-a-timestamp"
            payload = {"events": [event], "next_after": None}
        else:
            reads += 1
            payload = saved if reads > 1 else _fake_detail(case, status="running")
            if change == "early-report" and reads == 1:
                payload["report_markdown"] = "Premature report."
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(transport)
    ) as client:
        trace = {}
        if change == "normal":
            detail = await _exercise_api(client, case, trace, poll_interval=0)
            assert detail.result.next_step == "needs_more_evidence"
            assert trace["observations"]["finding_count"] == 0
            assert trace["semantic_review"] == "pending" and posts == 3
            assert detail.progress.created_at == datetime(2026, 10, 29, tzinfo=UTC)
            assert detail.freshness.checked_at == datetime(2026, 10, 29, tzinfo=UTC)
            assert any(
                entry["payload"].get("events")
                for entry in trace["http"]
                if isinstance(entry["payload"], dict)
            )
        else:
            expected = (
                ValidationError if change.startswith("invalid-") else AssertionError
            )
            with pytest.raises(expected):
                await _exercise_api(client, case, trace, poll_interval=0)


async def test_http_wait_deadline_does_not_cancel_or_delete_server_task():
    case, methods = DATASET.cases[0], []

    def transport(request):
        methods.append(request.method)
        if request.url.path == "/health/live":
            payload = {"status": "ok"}
        elif request.method == "POST":
            payload = _resolution(status="pending", decision="active")
        else:
            payload = _fake_detail(case, status="running")
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(transport)
    ) as client:
        trace = {}
        with pytest.raises(TimeoutError):
            await _exercise_api(
                client, case, trace, timeout_seconds=1, poll_interval=60
            )
    assert trace["task_id"] == "offline-task"
    assert "DELETE" not in methods and methods.count("POST") == 2


def test_artifacts_preserve_human_review_as_pending_and_export_report(tmp_path):
    case = DATASET.cases[0]
    detail = InvestigationDetailResponse.model_validate_json(
        json.dumps(_fake_detail(case))
    )
    _save_artifacts(tmp_path, case, {"task_id": detail.task_id}, detail)
    assert (tmp_path / "report.md").read_text() == detail.report_markdown
    assert "pending human review" in (tmp_path / "review.md").read_text()
    assert (
        json.loads((tmp_path / "trace.json").read_text())["task_id"] == detail.task_id
    )

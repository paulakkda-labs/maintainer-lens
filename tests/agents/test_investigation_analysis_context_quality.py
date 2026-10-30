"""Real Graph context and provider-payload regressions without paid requests."""

import json
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from maintainer_lens.agents.investigation_analysis import (
    InvestigationAnalysis,
    create_llm_analyzer,
)
from maintainer_lens.db.investigation_checkpoints import (
    InvalidInvestigationCheckpointError,
)
from maintainer_lens.schemas.investigation_checkpoint import GraphCheckpointConfig
from maintainer_lens.schemas.investigation_contracts import InvestigationInput
from maintainer_lens.services.investigation_checkpoint_execution import (
    validate_execution_compatibility,
)
from maintainer_lens.services.investigation_execution import execute_graph_investigation
from maintainer_lens.services.repository_hybrid_search import HybridSearchResult

pytestmark = pytest.mark.anyio
INDEX = UUID("2c111111-1111-4111-8111-111111111111")
REPOSITORY = "context-tests/repo"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _hit(content, number=1, *, file_path=None):
    file_path = file_path or f"config-{number}.toml"
    return HybridSearchResult(
        index_id=INDEX,
        chunk_key=f"chunk-{number}",
        repository=REPOSITORY,
        commit_sha="a" * 40,
        blob_sha="b" * 40,
        file_path=file_path,
        language={"py": "python", "rst": "rst"}.get(
            file_path.rsplit(".", 1)[-1], "toml"
        ),
        kind="text",
        symbol=None,
        start_line=1,
        end_line=len(content.splitlines()),
        content=content,
        rrf_score=0.03,
        vector_rank=number,
        keyword_rank=None,
        cosine_distance=0.1,
        matched_in=None,
    )


def _analysis(*, gaps=False):
    return InvestigationAnalysis(
        summary="The configuration needs an execution check.",
        findings=[
            {
                "statement": "The retrieved configuration declares a license classifier.",
                "evidence_ids": [f"source:{INDEX}:chunk-1"],
            }
        ],
        hypotheses=[],
        unanswered_questions=["Which build environment reproduces the warning?"]
        if gaps
        else [],
    )


async def _execute(
    source,
    analyzer,
    *,
    language="en",
    rounds=1,
    issue_title="License metadata warning",
    issue_body="The reporter observed a warning during a build.",
):
    return await execute_graph_investigation(
        InvestigationInput(
            repository=REPOSITORY,
            issue_number=123,
            issue_title=issue_title,
            issue_body=issue_body,
            response_language=language,
            source_index_id=INDEX,
        ),
        history_searcher=AsyncMock(return_value=()),
        source_searcher=source,
        analyzer=analyzer,
        timeout_seconds=5.0,
        max_search_rounds=rounds,
    )


def _late_classifier():
    # Under the existing 60-line/4000-character automatic chunk boundaries.
    return (
        "# " + "background " * 8 + "\n"
    ) * 25 + '"License :: OSI Approved :: MIT License"\n'


@pytest.mark.parametrize("language", ["en", "zh"])
async def test_full_bounded_chunk_and_grounding_rules_reach_the_provider(language):
    content = _late_classifier()
    assert 1600 < content.index("License ::") < len(content) < 4000
    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        prompt = payload["messages"][0]["content"]
        assert "do not prove that a job ran" in prompt
        assert "hypotheses or unanswered questions" in prompt
        assert "Attribute warnings and deadlines" in prompt
        assert "A truncated excerpt does not establish absence" in prompt
        assert payload["max_tokens"] == 4096
        context = json.loads(payload["messages"][1]["content"])["evidence"]
        assert context[0]["content"] == f"config-1.toml:1-26\n{content}"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": _analysis().model_dump_json(),
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        analyzer = create_llm_analyzer(
            client,
            base_url="https://model.invalid/v1",
            api_key="test-key",
            model_name="user-configured-model",
        )
        result = await _execute(
            AsyncMock(return_value=[_hit(content)]), analyzer, language=language
        )
    assert len(requests) == 1
    assert result.analysis_evidence[0]["content"].endswith(content)
    assert result.source_evidence[0]["content"] == content
    assert result.next_step == "draft_preliminary_report"
    # Mocked provider output proves data/instruction delivery, not LLM obedience.


async def test_oversized_external_chunk_is_bounded_and_explicitly_marked():
    content = "x" * 4000 + "UNSUPPLIED_TAIL"
    analyzer = AsyncMock(return_value=_analysis(gaps=True))
    result = await _execute(AsyncMock(return_value=[_hit(content)]), analyzer)
    supplied = analyzer.await_args.args[0]["evidence"][0]["content"]
    assert supplied.startswith("config-1.toml:1-1\n" + "x" * 4000)
    assert supplied.endswith("remaining content was not supplied to this analysis.]")
    assert "UNSUPPLIED_TAIL" not in supplied
    assert result.analysis_evidence[0]["content"] == supplied
    assert result.source_evidence[0]["content"] == content


async def test_top_five_bound_and_last_context_survive_no_novel_follow_up():
    content = _late_classifier()
    source = AsyncMock(return_value=[_hit(content, number) for number in range(1, 7)])
    analyzer = AsyncMock(return_value=_analysis(gaps=True))
    result = await _execute(source, analyzer, rounds=3)
    assert len(result.source_evidence) == 6 and len(result.analysis_evidence) == 5
    assert all(item["content"].endswith(content) for item in result.analysis_evidence)
    assert result.analysis_evidence == analyzer.await_args.args[0]["evidence"]
    assert source.await_count == result.search_round == 2
    analyzer.assert_awaited_once()
    assert result.next_step == "needs_more_evidence"


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize(
    "scenario", ["reporter-attribution", "keyword-scope", "config-semantics"]
)
async def test_grounding_constraints_and_original_excerpts_reach_provider(
    language, scenario
):
    if scenario == "reporter-attribution":
        content = (
            "Serializer provides dumps and loads.\n"
            "For signature age validation, see the timed serializer documentation.\n"
        )
        file_path = "docs/serializer.rst"
        body = "The reporter says loads(token, max_age=-1) silently ignores max_age."
        statement = None
        expected_rules = [
            "Issue input is reporter context, not independent corroboration",
            "General documentation about the same API does not establish",
            "Every factual clause in a finding",
        ]
        question = "Which implementation or execution evidence corroborates the reported max_age behavior?"
    elif scenario == "keyword-scope":
        content = (
            "def iter_unsigners(salt=None):\n"
            "    kwargs = {'digest_method': 'sha256'}\n"
            "    return choose_signer(salt, **kwargs)\n\n"
            "def loads(payload, salt=None, **kwargs):\n"
            "    signer = iter_unsigners(salt)\n"
            "    return load_payload(signer.unsign(payload))\n"
        )
        file_path = "serializer.py"
        body = "Does loads forward its keyword arguments when deserializing?"
        statement = "The shown loads passes salt to iter_unsigners without forwarding its kwargs."
        expected_rules = [
            "Trace arguments within the exact callable and scope shown",
            "Receiving an\n    argument does not mean using or forwarding it",
            "a local kwargs in another function",
        ]
        question = "Does this implementation reproduce the caller's reported failure?"
    else:
        content = (
            "[project]\n"
            'license = {file = "LICENSE.txt"}\n'
            'classifiers = ["License :: OSI Approved :: MIT License"]\n'
        )
        file_path = "pyproject.toml"
        body = "The reporter observed license metadata deprecation warnings."
        statement = "The supplied project configuration declares a license classifier."
        expected_rules = [
            "Configuration fields have distinct meanings and value types",
            "file paths and glob patterns are not interchangeable",
            "Apply these factual checks to hypotheses and suggested next actions too",
            "ask\n    for its schema or documentation instead of inventing a concrete migration",
        ]
        question = "Which documented field definitions and backend version support a replacement configuration?"

    analysis = InvestigationAnalysis(
        summary="The supplied evidence supports only the observations below.",
        findings=[{"statement": statement, "evidence_ids": [f"source:{INDEX}:chunk-1"]}]
        if statement
        else [],
        hypotheses=[],
        unanswered_questions=[question],
    )
    payloads = []

    def transport(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        prompt = payload["messages"][0]["content"]
        for rule in expected_rules:
            assert rule in prompt
        assert ("in Simplified Chinese" if language == "zh" else "in English") in prompt
        assert (
            "Distinguish selected line ranges from explicitly truncated content"
            in prompt
        )
        user = json.loads(payload["messages"][1]["content"])
        assert user["issue_body"] == body
        assert (
            user["evidence"][0]["content"]
            == f"{file_path}:1-{len(content.splitlines())}\n{content}"
        )
        assert payload["max_tokens"] == 4096
        assert payload["response_format"] == {"type": "json_object"}
        if scenario == "reporter-attribution":
            assert "max_age" not in user["evidence"][0]["content"]
        if scenario == "config-semantics":
            assert "license-files" not in user["evidence"][0]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": analysis.model_dump_json()},
                    }
                ]
            },
        )

    source = AsyncMock(return_value=[_hit(content, file_path=file_path)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        analyzer = create_llm_analyzer(
            client,
            base_url="https://model.invalid/v1",
            api_key="test-key",
            model_name="deployment-configured-model",
        )
        result = await _execute(
            source,
            analyzer,
            language=language,
            issue_title=scenario,
            issue_body=body,
        )
    assert len(payloads) == source.await_count == 1
    assert result.preliminary_analysis == analysis
    assert result.next_step == "needs_more_evidence"
    assert (
        result.analysis_evidence
        == json.loads(payloads[0]["messages"][1]["content"])["evidence"]
    )
    assert result.source_evidence[0]["content"] == content
    # This verifies contract/context delivery through a real Graph, not semantic model obedience.


@pytest.mark.parametrize(
    "previous_version",
    [
        "investigation-v1",
        "investigation-v2",
        "investigation-v3",
        "investigation-v4",
        "investigation-v5",
    ],
)
def test_changed_analysis_semantics_reject_previous_checkpoint_execution(
    previous_version,
):
    current = GraphCheckpointConfig(
        response_language="en",
        max_search_rounds=1,
        timeout_seconds=5.0,
        search_strategy="deterministic",
        source_enabled=True,
        history_top_k=5,
        source_top_k=5,
        embedding_provider="test",
        embedding_model="embed",
        embedding_dimensions=3,
        embedding_endpoint_fingerprint="1" * 64,
        llm_model="configured-model",
        llm_endpoint_fingerprint="2" * 64,
        llm_max_output_tokens=4096,
        thinking_enabled=False,
    )
    assert current.graph_version == "investigation-v6"
    assert validate_execution_compatibility(current) == current
    with pytest.raises(InvalidInvestigationCheckpointError, match="incompatible"):
        validate_execution_compatibility(
            current.model_copy(update={"graph_version": previous_version})
        )


@pytest.mark.parametrize("language", ["en", "zh"])
async def test_unrelated_evidence_can_produce_a_valid_report_without_findings(language):
    response_body = {
        "summary": "The reported exception concerns a dependency not established by these excerpts.",
        "findings": [],
        "hypotheses": [],
        "unanswered_questions": [
            "Which dependency repository and version reproduce the error?"
        ],
    }
    calls = []

    def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        prompt = payload["messages"][0]["content"]
        assert "An empty evidence_ids array is invalid" in prompt
        assert "rather than creating an uncited finding" in prompt
        schema_text = prompt.split("Required output JSON Schema:\n", 1)[1].split(
            "\n\n", 1
        )[0]
        schema = json.loads(schema_text)
        citation_schema = schema["$defs"]["AnalysisFinding"]["properties"][
            "evidence_ids"
        ]
        assert citation_schema["type"] == "array" and citation_schema["minItems"] == 1
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == 4096
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(response_body),
                        },
                    }
                ]
            },
        )

    source = AsyncMock(return_value=[_hit("sample = 'template'\n")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        analyzer = create_llm_analyzer(
            client,
            base_url="https://model.invalid/v1",
            api_key="test-key",
            model_name="user-configured-model",
        )
        result = await _execute(
            source,
            analyzer,
            language=language,
            issue_title="TypeError in an external dependency",
            issue_body="The reporter's traceback names a module not shown in the indexed repository.",
        )
    assert len(calls) == source.await_count == 1
    assert result.preliminary_analysis.findings == []
    assert result.preliminary_analysis.unanswered_questions
    assert result.next_step == "needs_more_evidence"
    assert len(result.source_evidence) == len(result.analysis_evidence) == 1


async def test_empty_finding_citations_still_fail_after_one_corrective_request():
    calls = []
    invalid = {
        "summary": "The supplied evidence is insufficient.",
        "findings": [
            {
                "statement": "No matching implementation is established.",
                "evidence_ids": [],
            }
        ],
        "hypotheses": [],
        "unanswered_questions": ["Where is the dependency implementation?"],
    }

    def transport(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(invalid),
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        analyzer = create_llm_analyzer(
            client,
            base_url="https://model.invalid/v1",
            api_key="test-key",
            model_name="user-configured-model",
        )
        with pytest.raises(ValidationError) as caught:
            await _execute(
                AsyncMock(return_value=[_hit("sample = 'template'\n")]), analyzer
            )
    errors = caught.value.errors(include_input=False, include_url=False)
    assert len(errors) == 1
    assert errors[0]["loc"] == ("findings", 0, "evidence_ids")
    assert errors[0]["type"] == "too_short"
    assert len(calls) == 2
    assert calls[1]["messages"][:2] == calls[0]["messages"]
    assert "Validation issues (data)" in calls[1]["messages"][-1]["content"]

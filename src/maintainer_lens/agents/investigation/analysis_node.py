"""LangGraph node for evidence-grounded LLM analysis."""

from maintainer_lens.agents.investigation.evidence import history_evidence_id
from maintainer_lens.agents.investigation.state import InvestigationState
from maintainer_lens.agents.investigation_analysis import (
    AnalysisEvidence,
    AnalysisFindingState,
    InvestigationAnalyzer,
)


def make_analysis_node(analyzer: InvestigationAnalyzer):
    """Create the LLM analysis node with an injected analyzer."""

    async def analyze_evidence(state: InvestigationState) -> dict:
        if (
            state["search_round"] > 1
            and state["new_history_evidence"] == 0
            and state["new_source_evidence"] == 0
        ):
            return {"no_new_evidence": True}

        evidence: list[AnalysisEvidence] = []

        for hit in state.get("history_evidence", [])[:5]:
            if hit["repository"] != state["repository"]:
                raise ValueError("History evidence belongs to another repository")
            if hit["number"] == state["issue_number"]:
                continue
            evidence_id = history_evidence_id(hit)

            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "category": "history",
                    "source_url": hit["source_url"],
                    "content": (f"{hit['title']}\n{hit['content'][:1600]}"),
                }
            )

        for hit in state.get("source_evidence", [])[:5]:
            evidence_id = f"source:{hit['index_id']}:{hit['chunk_key']}"

            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "category": "source",
                    "source_url": hit["source_url"],
                    "content": (
                        f"{hit['file_path']}:"
                        f"{hit['start_line']}-{hit['end_line']}\n"
                        f"{hit['content'][:1600]}"
                    ),
                }
            )

        allowed_ids = {item["evidence_id"] for item in evidence}

        if len(allowed_ids) != len(evidence):
            raise ValueError("Duplicate evidence IDs in analysis input")

        if not evidence:
            if state["response_language"] == "zh":
                summary = "当前没有可用于初步调查的索引证据。"
                question = "还可以检索哪些相关证据？"
            else:
                summary = (
                    "No indexed evidence is available for "
                    "a grounded preliminary analysis."
                )
                question = "What additional relevant evidence can be retrieved?"

            return {
                "preliminary_analysis": {
                    "summary": summary,
                    "findings": [],
                    "hypotheses": [],
                    "unanswered_questions": [question],
                }
            }

        analysis = await analyzer(
            {
                "repository": state["repository"],
                "issue_number": state["issue_number"],
                "issue_title": state["issue_title"],
                "issue_body": state["issue_body"],
                "response_language": state["response_language"],
                "evidence": evidence,
            }
        )

        findings: list[AnalysisFindingState] = []

        for finding in analysis.findings:
            if not set(finding.evidence_ids).issubset(allowed_ids):
                raise ValueError("LLM cited evidence not supplied to this analysis")

            findings.append(
                {
                    "statement": finding.statement,
                    "evidence_ids": finding.evidence_ids,
                }
            )

        return {
            "preliminary_analysis": {
                "summary": analysis.summary,
                "findings": findings,
                "hypotheses": analysis.hypotheses,
                "unanswered_questions": analysis.unanswered_questions,
            }
        }

    return analyze_evidence

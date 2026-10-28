"""Render a deterministic Markdown report from a validated investigation."""

import html
import re

from maintainer_lens.agents.investigation.routing import assess_next_step
from maintainer_lens.agents.investigation.state import InvalidInvestigationResultError
from maintainer_lens.schemas.investigation_contracts import InvestigationResult
from maintainer_lens.services.investigation_execution import (
    validate_investigation_evidence,
)


def _text(value: str) -> str:
    """Keep model/repository text literal rather than active Markdown or HTML."""
    escaped = html.escape(value, quote=False).replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", escaped)


def render_investigation_report(result: InvestigationResult) -> str:
    """Format saved analysis and its actual citations without I/O or new inference.

    Revalidate mutable data and provenance before creating links. The report is
    derived on read, so version-1 results need no migration or extra LLM request.
    Valid citations do not establish the semantic truth of a model's statements.
    """
    try:
        result = InvestigationResult.model_validate(result.model_dump(), strict=True)
        validate_investigation_evidence(result)
        if (
            result.next_step == "draft_preliminary_report"
            and assess_next_step(result.model_dump())[0] != "ready"
        ):
            raise ValueError("Report readiness disagrees with the structured analysis")
    except ValueError as exc:
        raise InvalidInvestigationResultError(
            "Invalid investigation report input"
        ) from exc
    zh = result.response_language == "zh"
    labels = (
        {
            "title": "调查报告",
            "status": "调查状态",
            "ready": "已有引用支持的发现，未声明待解决问题。",
            "insufficient": "证据不足，仍需进一步调查。",
            "rounds": "检索轮数",
            "index": "Source 索引",
            "summary": "摘要",
            "findings": "有引用支持的发现",
            "hypotheses": "待验证假设",
            "questions": "未解决问题",
            "evidence": "最近一次实际分析使用的证据",
            "reason": "执行说明",
            "empty": "无。",
            "limits": "引用可追溯不代表事实或根因已得到独立验证。",
        }
        if zh
        else {
            "title": "Investigation report",
            "status": "Investigation status",
            "ready": "Cited findings with no declared unresolved gaps.",
            "insufficient": "Insufficient evidence; further investigation is needed.",
            "rounds": "Search rounds",
            "index": "Source index",
            "summary": "Summary",
            "findings": "Findings with citations",
            "hypotheses": "Unverified hypotheses",
            "questions": "Unanswered questions",
            "evidence": "Evidence used by the last actual analysis",
            "reason": "Execution notes",
            "empty": "None.",
            "limits": "Traceable citations do not independently verify facts or a root cause.",
        }
    )
    status = labels[
        "ready" if result.next_step == "draft_preliminary_report" else "insufficient"
    ]
    lines = [
        f"# {labels['title']}: {_text(result.repository)} #{result.issue_number}",
        "",
        _text(result.issue_title),
        "",
        f"{labels['status']}: {status}",
        f"{labels['rounds']}: {result.search_round}",
    ]
    if result.source_index_id is not None:
        lines.append(f"{labels['index']}: {result.source_index_id}")
    analysis = result.preliminary_analysis
    lines.extend(["", f"## {labels['summary']}", "", _text(analysis.summary)])

    # Resolve findings through the saved analysis context, never final top-k ranks.
    context = {item["evidence_id"]: item for item in result.analysis_evidence}

    def citation(evidence_id: str) -> str:
        url = context[evidence_id]["source_url"]
        # The shared provenance validator above only accepts matching GitHub URLs.
        url = url.replace("(", "%28").replace(")", "%29")
        return f"[{_text(evidence_id)}]({url})"

    lines.extend(["", f"## {labels['findings']}", ""])
    for finding in analysis.findings:
        references = ", ".join(citation(key) for key in finding.evidence_ids)
        lines.append(f"- {_text(finding.statement)} ({references})")
    if not analysis.findings:
        lines.append(labels["empty"])
    for heading, items in (
        ("hypotheses", analysis.hypotheses),
        ("questions", analysis.unanswered_questions),
    ):
        lines.extend(["", f"## {labels[heading]}", ""])
        lines.extend(f"- {_text(item)}" for item in items)
        if not items:
            lines.append(labels["empty"])

    lines.extend(["", f"## {labels['evidence']}", ""])
    for item in result.analysis_evidence:
        lines.extend([citation(item["evidence_id"]), ""])
        lines.extend(f"> {line}" for line in _text(item["content"]).split("\n"))
        lines.append("")
    if not result.analysis_evidence:
        lines.append(labels["empty"])
    lines.extend(
        [
            "",
            f"## {labels['reason']}",
            "",
            _text(result.routing_reason),
            "",
            labels["limits"],
        ]
    )
    return "\n".join(lines).rstrip() + "\n"

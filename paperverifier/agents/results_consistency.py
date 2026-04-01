"""Results consistency verification agent.

Verifies internal consistency between methodology, results, and conclusions
sections of a research paper.  Cross-references numerical data, statistical
tests, and claims across sections.
"""

from __future__ import annotations

from typing import Any

import structlog

from paperverifier.agents.base import BaseAgent
from paperverifier.llm.client import Message, UnifiedLLMClient
from paperverifier.llm.roles import AgentRole, RoleAssignment
from paperverifier.models.document import ParsedDocument, Section
from paperverifier.models.findings import Finding
from paperverifier.utils.chunking import get_context_window
from paperverifier.utils.prompts import get_prompts
from paperverifier.utils.text import count_tokens_estimate, truncate_to_token_limit

logger = structlog.get_logger(__name__)

# Keywords used to identify methodology, results, and conclusion sections
_METHODOLOGY_KEYWORDS = {
    "method",
    "methods",
    "methodology",
    "approach",
    "experimental setup",
    "experimental design",
    "materials and methods",
    "data collection",
    "procedure",
    "implementation",
}
_RESULTS_KEYWORDS = {
    "result",
    "results",
    "experiment",
    "experiments",
    "evaluation",
    "findings",
    "analysis",
    "performance",
}
_CONCLUSION_KEYWORDS = {
    "conclusion",
    "conclusions",
    "discussion",
    "discussion and conclusion",
    "summary",
    "summary and conclusion",
    "concluding remarks",
    "future work",
}


def _find_sections_by_keywords(
    document: ParsedDocument,
    keywords: set[str],
) -> list[Section]:
    """Find sections whose title matches any of the given keywords."""
    matches: list[Section] = []
    _search_sections(document.sections, keywords, matches)
    return matches


def _search_sections(
    sections: list[Section],
    keywords: set[str],
    matches: list[Section],
) -> None:
    """Recursively search for sections matching keyword set."""
    for sec in sections:
        title_lower = sec.title.lower().strip()
        for keyword in keywords:
            if keyword in title_lower:
                matches.append(sec)
                break
        _search_sections(sec.subsections, keywords, matches)


def _extract_single_section_text(section: Section) -> str:
    """Recursively extract text from a single section and all its subsections."""
    body_parts: list[str] = []
    for para in section.paragraphs:
        body_parts.append(para.raw_text)
    for sub in section.subsections:
        body_parts.append(f"\n### [{sub.id}] {sub.title}")
        body_parts.append(_extract_single_section_text(sub))
    return "\n\n".join(body_parts)


def _extract_section_text(
    document: ParsedDocument,
    sections: list[Section],
) -> str:
    """Extract combined text from a list of sections."""
    if not sections:
        return "(Section not found in document)"

    parts: list[str] = []
    for sec in sections:
        header = f"[{sec.id}] {sec.title}"
        body = _extract_single_section_text(sec) or "(Empty section)"
        parts.append(f"## {header}\n\n{body}")

    return "\n\n---\n\n".join(parts)


class ResultsConsistencyAgent(BaseAgent):
    """Verifies consistency between methodology, results, and conclusions.

    Extracts the relevant sections from the document and cross-references
    them in a single prompt to detect inconsistencies, unsupported
    conclusions, and methodological concerns.
    """

    def __init__(
        self,
        client: UnifiedLLMClient,
        assignment: RoleAssignment,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            role=AgentRole.RESULTS_CONSISTENCY,
            client=client,
            assignment=assignment,
            **kwargs,
        )

    async def _run_analysis(
        self,
        document: ParsedDocument,
        **kwargs: Any,
    ) -> list[Finding]:
        """Run results consistency analysis."""
        system_prompt, user_template = get_prompts(self.role.value)

        # Extract methodology, results, and conclusion sections
        method_sections = _find_sections_by_keywords(
            document,
            _METHODOLOGY_KEYWORDS,
        )
        results_sections = _find_sections_by_keywords(
            document,
            _RESULTS_KEYWORDS,
        )
        conclusion_sections = _find_sections_by_keywords(
            document,
            _CONCLUSION_KEYWORDS,
        )

        methodology_text = _extract_section_text(document, method_sections)
        results_text = _extract_section_text(document, results_sections)
        conclusion_text = _extract_section_text(document, conclusion_sections)

        self._logger.info(
            "sections_extracted",
            methodology_sections=len(method_sections),
            results_sections=len(results_sections),
            conclusion_sections=len(conclusion_sections),
        )

        # Guard against context overflow: if the combined section text
        # is too large for the model, truncate each section proportionally
        # (Codex-2).
        combined_tokens = count_tokens_estimate(methodology_text + results_text + conclusion_text)
        context_window = get_context_window(self._assignment.model)
        # Reserve tokens for system prompt, user template, and response.
        max_section_tokens = (context_window - 4_000) // 3
        if combined_tokens > context_window - 4_000:
            self._logger.warning(
                "results_consistency_truncating",
                combined_tokens=combined_tokens,
                max_per_section=max_section_tokens,
            )
            methodology_text = truncate_to_token_limit(
                methodology_text,
                max_section_tokens,
            )
            results_text = truncate_to_token_limit(
                results_text,
                max_section_tokens,
            )
            conclusion_text = truncate_to_token_limit(
                conclusion_text,
                max_section_tokens,
            )

        # Escape curly braces to avoid crashes on LaTeX/code (CRIT-1).
        user_msg = user_template.format(
            methodology_text=methodology_text.replace("{", "{{").replace("}", "}}"),
            results_text=results_text.replace("{", "{{").replace("}", "}}"),
            conclusion_text=conclusion_text.replace("{", "{{").replace("}", "}}"),
        )

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_msg),
        ]
        response = await self._call_llm(messages)
        return self._parse_findings(response)

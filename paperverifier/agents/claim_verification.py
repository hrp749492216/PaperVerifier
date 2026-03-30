"""Claim verification agent.

Examines every factual claim, assertion, and statement of fact in a research
paper to determine whether each is adequately supported by evidence,
citations, or logical reasoning within the paper.
"""

from __future__ import annotations

from typing import Any

import structlog

from paperverifier.llm.client import Message, UnifiedLLMClient
from paperverifier.llm.roles import AgentRole, RoleAssignment
from paperverifier.models.document import ParsedDocument
from paperverifier.models.findings import Finding
from paperverifier.utils.chunking import (
    chunk_document,
    create_document_summary,
)
from paperverifier.utils.prompts import get_prompts

from paperverifier.agents.base import BaseAgent

logger = structlog.get_logger(__name__)


def _format_references_list(document: ParsedDocument) -> str:
    """Format the reference list from a parsed document for the prompt."""
    if not document.references:
        return "(No references found in the document)"

    lines: list[str] = []
    for ref in document.references:
        parts: list[str] = []
        if ref.citation_key:
            parts.append(f"[{ref.citation_key}]")
        if ref.title:
            parts.append(ref.title)
        if ref.authors:
            parts.append(f"by {', '.join(ref.authors[:3])}")
            if len(ref.authors) > 3:
                parts[-1] += " et al."
        if ref.year:
            parts.append(f"({ref.year})")
        if ref.doi:
            parts.append(f"DOI: {ref.doi}")

        line = " ".join(parts) if parts else ref.raw_text
        lines.append(f"  - {line}")

    return "\n".join(lines)


class ClaimVerificationAgent(BaseAgent):
    """Verifies that every claim in the paper is properly supported.

    Provides ``document_text`` and ``references_list`` to the prompt.
    Chunks by section, prioritising sections that make claims
    (Introduction, Results, Discussion, etc.).
    """

    def __init__(
        self,
        client: UnifiedLLMClient,
        assignment: RoleAssignment,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            role=AgentRole.CLAIM_VERIFICATION,
            client=client,
            assignment=assignment,
            **kwargs,
        )

    async def _run_analysis(
        self,
        document: ParsedDocument,
        **kwargs: Any,
    ) -> list[Finding]:
        """Run claim verification, focusing on claim-bearing sections."""
        system_prompt, user_template = get_prompts(self.role.value)
        references_list = _format_references_list(document)
        summary = create_document_summary(document)
        chunks = chunk_document(document, self._assignment.model)

        all_findings: list[Finding] = []
        for chunk in chunks:
            # Add chunk context for partial documents so the LLM knows
            # it is analysing a portion of the full paper.
            if chunk.is_complete:
                document_text = chunk.text
            else:
                document_text = (
                    f"=== DOCUMENT SUMMARY (for global context) ===\n"
                    f"{summary}\n\n"
                    f"=== DOCUMENT CHUNK {chunk.chunk_index + 1}/{chunk.total_chunks} ===\n"
                    f"NOTE: You are analysing chunk {chunk.chunk_index + 1} of "
                    f"{chunk.total_chunks}. This is a partial view of the document. "
                    f"Focus your analysis on this portion but use the summary above "
                    f"for overall context.\n\n"
                    f"{chunk.text}"
                )

            # Escape curly braces to avoid crashes on LaTeX/code (CRIT-1).
            safe_text = document_text.replace("{", "{{").replace("}", "}}")
            safe_refs = references_list.replace("{", "{{").replace("}", "}}")
            user_msg = user_template.format(
                document_text=safe_text,
                references_list=safe_refs,
            )
            messages = [
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_msg),
            ]
            response = await self._call_llm(messages)
            findings = self._parse_findings(response)
            all_findings.extend(findings)

        return all_findings

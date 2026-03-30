"""Claim verification agent.

Examines every factual claim, assertion, and statement of fact in a research
paper to determine whether each is adequately supported by evidence,
citations, or logical reasoning within the paper.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from paperverifier.llm.client import Message, UnifiedLLMClient
from paperverifier.llm.roles import AgentRole, RoleAssignment
from paperverifier.models.document import ParsedDocument, Section
from paperverifier.models.findings import Finding
from paperverifier.utils.chunking import (
    DocumentChunk,
    chunk_document,
    create_document_summary,
)
from paperverifier.utils.prompts import get_prompts

from paperverifier.agents.base import BaseAgent

logger = structlog.get_logger(__name__)

# Sections that typically contain verifiable claims
_CLAIM_BEARING_TITLES = {
    "introduction",
    "abstract",
    "results",
    "discussion",
    "experiments",
    "evaluation",
    "analysis",
    "conclusion",
    "conclusions",
    "findings",
}


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


def _is_claim_bearing_section(section: Section) -> bool:
    """Check whether a section is likely to contain verifiable claims."""
    title_lower = section.title.lower().strip()
    for keyword in _CLAIM_BEARING_TITLES:
        if keyword in title_lower:
            return True
    return False


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
        chunks = chunk_document(document, self._assignment.model)

        all_findings: list[Finding] = []
        for chunk in chunks:
            user_msg = user_template.format(
                document_text=chunk.text,
                references_list=references_list,
            )
            messages = [
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_msg),
            ]
            response = await self._call_llm(messages)
            findings = self._parse_findings(response)
            all_findings.extend(findings)

        return all_findings

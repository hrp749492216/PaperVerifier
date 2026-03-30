"""Section structure verification agent.

Analyses the structural integrity of a research paper: required sections,
section ordering, heading hierarchy, paragraph structure, abstract
completeness, section balance, and missing cross-references.
"""

from __future__ import annotations

from typing import Any

import structlog

from paperverifier.llm.client import Message, UnifiedLLMClient
from paperverifier.llm.roles import AgentRole, RoleAssignment
from paperverifier.models.document import ParsedDocument, Section
from paperverifier.models.findings import Finding
from paperverifier.utils.chunking import DocumentChunk, create_document_summary
from paperverifier.utils.prompts import get_prompts

from paperverifier.agents.base import BaseAgent

logger = structlog.get_logger(__name__)


def _build_sections_summary(document: ParsedDocument) -> str:
    """Build a concise summary of section titles with paragraph counts.

    Produces a hierarchical outline like::

        1. [sec-1] Introduction (3 paragraphs, 12 sentences)
           1.1. [sec-1.sub-1] Background (2 paragraphs, 8 sentences)
        2. [sec-2] Methodology (5 paragraphs, 24 sentences)
    """
    lines: list[str] = []
    _format_section_line(document.sections, lines, depth=0, prefix="")
    return "\n".join(lines) if lines else "(No sections found)"


def _format_section_line(
    sections: list[Section],
    lines: list[str],
    depth: int,
    prefix: str,
) -> None:
    """Recursively format section lines."""
    indent = "  " * depth
    for i, sec in enumerate(sections, start=1):
        num = f"{prefix}{i}" if prefix else str(i)
        para_count = len(sec.paragraphs)
        sent_count = sum(len(p.sentences) for p in sec.paragraphs)
        word_count = sum(
            len(p.raw_text.split()) for p in sec.paragraphs
        )
        lines.append(
            f"{indent}{num}. [{sec.id}] {sec.title} "
            f"({para_count} paragraphs, {sent_count} sentences, ~{word_count} words)"
        )
        if sec.subsections:
            _format_section_line(
                sec.subsections, lines, depth + 1, prefix=f"{num}.",
            )


class SectionStructureAgent(BaseAgent):
    """Verifies the structural integrity of a research paper.

    Provides a ``sections_summary`` variable to the prompt containing
    section titles with paragraph/sentence/word counts.  Uses the full
    document text (structure analysis typically fits in a single chunk
    on modern large-context models).
    """

    def __init__(
        self,
        client: UnifiedLLMClient,
        assignment: RoleAssignment,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            role=AgentRole.SECTION_STRUCTURE,
            client=client,
            assignment=assignment,
            **kwargs,
        )

    async def _run_analysis(
        self,
        document: ParsedDocument,
        **kwargs: Any,
    ) -> list[Finding]:
        """Run structure analysis using the full document."""
        system_prompt, user_template = get_prompts(self.role.value)
        sections_summary = _build_sections_summary(document)
        summary = create_document_summary(document)

        # Structure analysis works best with the complete document.
        # Use full_text directly; the model's context window is typically
        # large enough for structure verification.
        # Escape curly braces to avoid crashes on LaTeX/code content (CRIT-1).
        safe_text = document.full_text.replace("{", "{{").replace("}", "}}")
        safe_sections = sections_summary.replace("{", "{{").replace("}", "}}")
        user_msg = user_template.format(
            document_text=safe_text,
            sections_summary=safe_sections,
        )

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_msg),
        ]
        response = await self._call_llm(messages)
        return self._parse_findings(response)

"""Language and flow verification agent.

Evaluates language quality, readability, and rhetorical flow section by
section.  Focuses on grammar, clarity, transitions between sections,
tense consistency, and terminology usage.
"""

from __future__ import annotations

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


def _extract_section_texts(document: ParsedDocument) -> list[tuple[str, str]]:
    """Extract text for each section, including subsections.

    Returns a list of (section_header, section_text) tuples.  If the
    document has no sections, the full text is returned as a single entry.
    """
    if not document.sections:
        return [("Full Document", document.full_text)]

    entries: list[tuple[str, str]] = []
    for sec in document.sections:
        header = f"[{sec.id}] {sec.title}"
        text = _build_section_text(sec)
        if text.strip():
            entries.append((header, text))

    return entries if entries else [("Full Document", document.full_text)]


def _build_section_text(section: Section) -> str:
    """Build the full text for a section including subsections."""
    parts: list[str] = []
    for para in section.paragraphs:
        parts.append(para.raw_text)
    for sub in section.subsections:
        parts.append(f"\n### [{sub.id}] {sub.title}")
        parts.append(_build_section_text(sub))
    return "\n\n".join(parts)


class LanguageFlowAgent(BaseAgent):
    """Evaluates language quality and writing flow section by section.

    Processes the document section by section to provide granular feedback
    on grammar, transitions, tense consistency, and terminology usage.
    """

    def __init__(
        self,
        client: UnifiedLLMClient,
        assignment: RoleAssignment,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            role=AgentRole.LANGUAGE_FLOW,
            client=client,
            assignment=assignment,
            **kwargs,
        )

    async def _run_analysis(
        self,
        document: ParsedDocument,
        **kwargs: Any,
    ) -> list[Finding]:
        """Run language/flow analysis section by section."""
        system_prompt, user_template = get_prompts(self.role.value)
        chunks = chunk_document(document, self._assignment.model)

        self._logger.info(
            "language_analysis_start",
            section_count=len(document.sections),
            chunk_count=len(chunks),
        )

        all_findings: list[Finding] = []
        for chunk in chunks:
            user_msg = user_template.format(document_text=chunk.text)
            messages = [
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_msg),
            ]
            response = await self._call_llm(messages)
            findings = self._parse_findings(response)
            all_findings.extend(findings)

        return all_findings

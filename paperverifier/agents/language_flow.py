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
from paperverifier.models.document import ParsedDocument
from paperverifier.models.findings import Finding
from paperverifier.utils.chunking import (
    chunk_document,
    create_document_summary,
)
from paperverifier.utils.prompts import get_prompts

from paperverifier.agents.base import BaseAgent

logger = structlog.get_logger(__name__)


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
        summary = create_document_summary(document)
        chunks = chunk_document(document, self._assignment.model)

        self._logger.info(
            "language_analysis_start",
            section_count=len(document.sections),
            chunk_count=len(chunks),
        )

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
            user_msg = user_template.format(document_text=safe_text)
            messages = [
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_msg),
            ]
            response = await self._call_llm(messages)
            findings = self._parse_findings(response)
            all_findings.extend(findings)

        return all_findings

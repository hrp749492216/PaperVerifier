"""Hallucination detection agent.

Scans the paper for fabricated statistics, invented references, false
historical claims, implausible results, and unsupported factual statements.
Processes the document with full context to detect cross-section patterns.
"""

from __future__ import annotations

from typing import Any

import structlog

from paperverifier.llm.client import Message, UnifiedLLMClient
from paperverifier.llm.roles import AgentRole, RoleAssignment
from paperverifier.models.document import ParsedDocument
from paperverifier.models.findings import Finding
from paperverifier.utils.chunking import (
    DocumentChunk,
    chunk_document,
    create_document_summary,
)
from paperverifier.utils.prompts import get_prompts

from paperverifier.agents.base import BaseAgent

logger = structlog.get_logger(__name__)


class HallucinationDetectionAgent(BaseAgent):
    """Detects potential hallucinations and fabricated content.

    Processes the document with full context to detect fabricated
    statistics, invented references, false historical claims, implausible
    results, and unsupported factual statements.  When the document must
    be chunked, a document summary is prepended to each chunk so the LLM
    has cross-section awareness.
    """

    def __init__(
        self,
        client: UnifiedLLMClient,
        assignment: RoleAssignment,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            role=AgentRole.HALLUCINATION_DETECTION,
            client=client,
            assignment=assignment,
            **kwargs,
        )

    async def _run_analysis(
        self,
        document: ParsedDocument,
        **kwargs: Any,
    ) -> list[Finding]:
        """Run hallucination detection with full document context."""
        system_prompt, user_template = get_prompts(self.role.value)
        summary = create_document_summary(document)
        chunks = chunk_document(document, self._assignment.model)

        self._logger.info(
            "hallucination_detection_start",
            chunk_count=len(chunks),
            is_complete=len(chunks) == 1 and chunks[0].is_complete,
        )

        all_findings: list[Finding] = []
        for chunk in chunks:
            # For multi-chunk documents, prepend the summary so the LLM
            # has global context even when analysing a portion.
            if chunk.is_complete:
                document_text = chunk.text
            else:
                document_text = (
                    f"=== DOCUMENT SUMMARY (for global context) ===\n"
                    f"{summary}\n\n"
                    f"=== DOCUMENT CHUNK {chunk.chunk_index + 1}/{chunk.total_chunks} ===\n"
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

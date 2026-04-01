"""Writer agent for generating fixes for identified findings.

Unlike other agents, the WriterAgent does not produce new findings.
Instead, it takes an existing :class:`Finding` and produces rewritten
text that resolves the identified issue while preserving the paper's
voice and technical accuracy.
"""

from __future__ import annotations

from typing import Any

import structlog

from paperverifier.agents.base import BaseAgent
from paperverifier.llm.client import Message, UnifiedLLMClient
from paperverifier.llm.roles import AgentRole, RoleAssignment
from paperverifier.models.document import ParsedDocument
from paperverifier.models.findings import Finding
from paperverifier.utils.json_parser import JSONParseError, parse_llm_json
from paperverifier.utils.prompts import get_prompts

logger = structlog.get_logger(__name__)

# Context window (in characters) to extract around a finding
_CONTEXT_CHARS = 2000


class WriterAgent(BaseAgent):
    """Generates fixes for identified findings.

    Unlike verification agents, the WriterAgent takes a single
    :class:`Finding` and the source document, then produces rewritten
    text that resolves the issue.

    The primary public method is :meth:`generate_fix`, not
    :meth:`analyze`.
    """

    def __init__(
        self,
        client: UnifiedLLMClient,
        assignment: RoleAssignment,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            role=AgentRole.WRITER,
            client=client,
            assignment=assignment,
            **kwargs,
        )

    async def generate_fix(
        self,
        finding: Finding,
        document: ParsedDocument,
    ) -> str:
        """Generate a rewritten text that fixes the given finding.

        Parameters
        ----------
        finding:
            The finding to fix.  Must have at least a ``description``
            and ideally a ``segment_id`` and/or ``segment_text``.
        document:
            The parsed document containing the text to be rewritten.

        Returns
        -------
        str
            The rewritten text that resolves the issue.  If the LLM
            cannot produce a fix, returns a descriptive error message.
        """
        system_prompt, user_template = get_prompts(self.role.value)

        # Build the finding description for the prompt
        finding_text = self._format_finding(finding)

        # Extract surrounding context
        context_text = self._extract_context(finding, document)

        # Build instruction
        instruction = self._build_instruction(finding)

        # Escape curly braces to avoid crashes on LaTeX/code (CRIT-1).
        user_msg = user_template.format(
            finding=finding_text.replace("{", "{{").replace("}", "}}"),
            context_text=context_text.replace("{", "{{").replace("}", "}}"),
            instruction=instruction.replace("{", "{{").replace("}", "}}"),
        )

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_msg),
        ]

        try:
            response = await self._call_llm(messages)
        except Exception as exc:
            self._logger.error(
                "fix_generation_failed",
                finding_id=finding.id,
                error=str(exc),
                exc_info=True,
            )
            return "[Fix generation failed. Please try again later.]"

        # Parse the response to extract the suggestion
        return self._extract_fix(response.content)

    def _format_finding(self, finding: Finding) -> str:
        """Format a Finding into a human-readable string for the prompt."""
        parts: list[str] = [
            f"Category: {finding.category.value}",
            f"Severity: {finding.severity.value}",
            f"Title: {finding.title}",
            f"Description: {finding.description}",
        ]
        if finding.segment_id:
            parts.append(f"Location: {finding.segment_id}")
        if finding.segment_text:
            parts.append(f'Flagged text: "{finding.segment_text}"')
        if finding.suggestion:
            parts.append(f"Original suggestion: {finding.suggestion}")
        if finding.evidence:
            parts.append(f"Evidence: {'; '.join(finding.evidence)}")
        return "\n".join(parts)

    def _extract_context(
        self,
        finding: Finding,
        document: ParsedDocument,
    ) -> str:
        """Extract surrounding context for the finding from the document."""
        # Try to locate the segment in the document tree
        if finding.segment_id:
            segment = document.get_segment(finding.segment_id)
            if segment is not None:
                # Get the segment's text and surrounding context
                if hasattr(segment, "start_char") and hasattr(segment, "end_char"):
                    start = max(0, segment.start_char - _CONTEXT_CHARS // 2)
                    end = min(
                        len(document.full_text),
                        segment.end_char + _CONTEXT_CHARS // 2,
                    )
                    return document.full_text[start:end]

        # Fall back: try to find the flagged text in full_text
        if finding.segment_text and finding.segment_text in document.full_text:
            idx = document.full_text.index(finding.segment_text)
            start = max(0, idx - _CONTEXT_CHARS // 2)
            end = min(
                len(document.full_text),
                idx + len(finding.segment_text) + _CONTEXT_CHARS // 2,
            )
            return document.full_text[start:end]

        # Last resort: return the first portion of the document
        return document.full_text[:_CONTEXT_CHARS]

    def _build_instruction(self, finding: Finding) -> str:
        """Build a clear instruction for the writer based on the finding."""
        instruction = (
            f"Fix the {finding.severity.value} {finding.category.value} issue: {finding.title}. "
        )
        if finding.suggestion:
            instruction += f"The suggested approach is: {finding.suggestion}. "
        instruction += (
            "Produce a rewrite that resolves this issue while preserving "
            "the paper's voice, style, and technical accuracy. "
            "Change only what is necessary."
        )
        return instruction

    def _extract_fix(self, response_content: str) -> str:
        """Extract the fix text from the LLM response.

        Attempts to parse JSON and extract the ``suggestion`` field.
        Falls back to returning the raw response if parsing fails.
        """
        try:
            parsed = parse_llm_json(response_content, expect_array=True)
            if isinstance(parsed, list) and parsed:
                first = parsed[0]
                if isinstance(first, dict):
                    suggestion = first.get("suggestion")
                    if suggestion:
                        return str(suggestion)
                    # Try description as fallback
                    description = first.get("description")
                    if description:
                        return str(description)
        except JSONParseError:
            self._logger.debug(
                "writer_json_parse_fallback",
                hint="Using raw response as fix text",
            )

        # Return the raw response text as the fix
        return response_content.strip()

    async def _run_analysis(
        self,
        document: ParsedDocument,
        **kwargs: Any,
    ) -> list[Finding]:
        """Not supported for the WriterAgent.

        The WriterAgent is designed to be called via :meth:`generate_fix`,
        not through the ``analyze()`` pipeline.
        """
        raise NotImplementedError(
            "WriterAgent does not support analyze(). Use generate_fix() instead."
        )

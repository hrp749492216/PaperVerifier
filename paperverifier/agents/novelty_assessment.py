"""Novelty assessment agent.

Evaluates the novelty and originality of a paper's claimed contributions
by comparing them against related works retrieved from academic search APIs.
Frames output as "related work overlap analysis" rather than a numeric
novelty score.
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

# Caveat injected into every novelty assessment prompt
_KNOWLEDGE_CUTOFF_CAVEAT = (
    "\n\nIMPORTANT CAVEAT: The LLM performing this analysis has a knowledge "
    "cutoff date and may not be aware of very recent publications.  The "
    "related works list from API search is the primary source of truth for "
    "overlap analysis.  Any novelty assessment based on the LLM's own "
    "knowledge should be clearly marked as tentative and subject to this "
    "limitation.  Frame all findings as 'related work overlap analysis' "
    "rather than definitive novelty scores."
)


def _format_related_works(related_works: Any) -> str:
    """Format related works from external APIs for the prompt.

    Parameters
    ----------
    related_works:
        Either a list of dicts with paper metadata, a pre-formatted string,
        or None.
    """
    if not related_works:
        return (
            "(No related works retrieved from APIs.  Novelty assessment "
            "will be based solely on the paper's own claims and citations.)"
        )

    if isinstance(related_works, str):
        return related_works

    if not isinstance(related_works, list):
        return str(related_works)

    lines: list[str] = []
    for i, work in enumerate(related_works, start=1):
        if not isinstance(work, dict):
            lines.append(f"  {i}. {work}")
            continue

        parts: list[str] = []
        if work.get("title"):
            parts.append(f'"{work["title"]}"')
        if work.get("authors"):
            authors = work["authors"]
            if isinstance(authors, list):
                author_str = ", ".join(str(a) for a in authors[:3])
                if len(authors) > 3:
                    author_str += " et al."
            else:
                author_str = str(authors)
            parts.append(f"by {author_str}")
        if work.get("year"):
            parts.append(f"({work['year']})")
        if work.get("venue"):
            parts.append(f"in {work['venue']}")
        if work.get("abstract"):
            abstract = str(work["abstract"])[:300]
            parts.append(f"Abstract: {abstract}...")
        if work.get("doi"):
            parts.append(f"DOI: {work['doi']}")
        if work.get("citation_count") is not None:
            parts.append(f"Citations: {work['citation_count']}")
        if work.get("similarity_score") is not None:
            parts.append(f"Relevance: {work['similarity_score']:.2f}")

        line = " | ".join(parts) if parts else str(work)
        lines.append(f"  {i}. {line}")

    return "\n".join(lines)


class NoveltyAssessmentAgent(BaseAgent):
    """Assesses novelty by comparing claimed contributions against related works.

    Takes ``related_works`` from external APIs as kwargs.  Frames output
    as "related work overlap analysis" and includes an LLM knowledge
    cutoff caveat.
    """

    def __init__(
        self,
        client: UnifiedLLMClient,
        assignment: RoleAssignment,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            role=AgentRole.NOVELTY_ASSESSMENT,
            client=client,
            assignment=assignment,
            **kwargs,
        )

    async def _run_analysis(
        self,
        document: ParsedDocument,
        **kwargs: Any,
    ) -> list[Finding]:
        """Run novelty assessment with related works context."""
        related_works = kwargs.get("related_works")

        system_prompt, user_template = get_prompts(self.role.value)

        # Append knowledge cutoff caveat to system prompt
        system_prompt_with_caveat = system_prompt + _KNOWLEDGE_CUTOFF_CAVEAT

        related_works_text = _format_related_works(related_works)
        summary = create_document_summary(document)
        chunks = chunk_document(document, self._assignment.model)

        self._logger.info(
            "novelty_analysis_start",
            has_related_works=bool(related_works),
            related_works_count=(
                len(related_works) if isinstance(related_works, list) else 0
            ),
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
            safe_works = related_works_text.replace("{", "{{").replace("}", "}}")
            user_msg = user_template.format(
                document_text=safe_text,
                related_works=safe_works,
            )
            messages = [
                Message(role="system", content=system_prompt_with_caveat),
                Message(role="user", content=user_msg),
            ]
            response = await self._call_llm(messages)
            findings = self._parse_findings(response)
            all_findings.extend(findings)

        return all_findings

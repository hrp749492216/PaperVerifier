"""Reference verification agent.

Cross-checks bibliographic references against external API results from
OpenAlex, CrossRef, and Semantic Scholar.  Each reference is classified
into a confidence tier: verified, likely_valid, unverified, or suspicious.
"""

from __future__ import annotations

from typing import Any

import structlog

from paperverifier.agents.base import BaseAgent
from paperverifier.llm.client import Message, UnifiedLLMClient
from paperverifier.llm.roles import AgentRole, RoleAssignment
from paperverifier.models.document import ParsedDocument
from paperverifier.models.findings import ConfidenceLevel, Finding
from paperverifier.utils.prompts import get_prompts

logger = structlog.get_logger(__name__)


def _format_references_with_status(
    document: ParsedDocument,
    api_results: dict[str, Any] | None,
) -> tuple[str, str]:
    """Format the reference list and API lookup results for the prompt.

    Returns
    -------
    tuple[str, str]
        A 2-tuple of (references_list_text, api_results_text).
    """
    api_results = api_results or {}

    ref_lines: list[str] = []
    api_lines: list[str] = []

    for ref in document.references:
        # Build reference summary
        ref_parts: list[str] = []
        ref_key = ref.citation_key or ref.id
        ref_parts.append(f"[{ref_key}]")
        if ref.title:
            ref_parts.append(f'Title: "{ref.title}"')
        if ref.authors:
            author_str = ", ".join(ref.authors[:4])
            if len(ref.authors) > 4:
                author_str += " et al."
            ref_parts.append(f"Authors: {author_str}")
        if ref.year:
            ref_parts.append(f"Year: {ref.year}")
        if ref.doi:
            ref_parts.append(f"DOI: {ref.doi}")
        if ref.in_text_locations:
            ref_parts.append(f"Cited at: {', '.join(ref.in_text_locations[:5])}")

        ref_lines.append("  " + " | ".join(ref_parts))

        # Build API result summary for this reference
        lookup = api_results.get(ref_key) or api_results.get(ref.id)
        if lookup:
            status = _classify_lookup(lookup)
            api_entry = f"  [{ref_key}] Status: {status}"
            if isinstance(lookup, dict):
                if lookup.get("matched_title"):
                    api_entry += f' | Matched title: "{lookup["matched_title"]}"'
                if lookup.get("matched_year"):
                    api_entry += f" | Matched year: {lookup['matched_year']}"
                if lookup.get("matched_doi"):
                    api_entry += f" | Matched DOI: {lookup['matched_doi']}"
                if lookup.get("retracted"):
                    api_entry += " | WARNING: RETRACTED"
                if lookup.get("source"):
                    api_entry += f" | Source: {lookup['source']}"
                if lookup.get("citation_count") is not None:
                    api_entry += f" | Citations: {lookup['citation_count']}"
            api_lines.append(api_entry)
        else:
            api_lines.append(f"  [{ref_key}] Status: NOT_FOUND (no API match)")

    references_text = "\n".join(ref_lines) if ref_lines else "(No references found)"
    api_text = "\n".join(api_lines) if api_lines else "(No API results available)"

    return references_text, api_text


def _classify_lookup(lookup: Any) -> str:
    """Classify an API lookup result into a confidence tier.

    Returns one of: VERIFIED, LIKELY_VALID, UNVERIFIED, SUSPICIOUS.
    """
    if not isinstance(lookup, dict):
        return ConfidenceLevel.UNVERIFIED.value

    # If the API explicitly flagged it as retracted
    if lookup.get("retracted"):
        return ConfidenceLevel.SUSPICIOUS.value

    confidence = lookup.get("confidence", 0.0)
    if isinstance(confidence, str):
        # Some APIs return string labels
        label = confidence.lower()
        if label in ("high", "exact"):
            return ConfidenceLevel.VERIFIED.value
        if label in ("medium", "partial"):
            return ConfidenceLevel.LIKELY_VALID.value
        if label in ("low", "none"):
            return ConfidenceLevel.UNVERIFIED.value
        return ConfidenceLevel.UNVERIFIED.value

    try:
        score = float(confidence)
    except (ValueError, TypeError):
        return ConfidenceLevel.UNVERIFIED.value

    if score >= 0.85:
        return ConfidenceLevel.VERIFIED.value
    if score >= 0.5:
        return ConfidenceLevel.LIKELY_VALID.value
    if score >= 0.2:
        return ConfidenceLevel.UNVERIFIED.value
    return ConfidenceLevel.SUSPICIOUS.value


class ReferenceVerificationAgent(BaseAgent):
    """Cross-checks references against external API results.

    Expects ``api_results`` in kwargs -- a dict mapping reference keys to
    API lookup result dicts from OpenAlex, CrossRef, or Semantic Scholar.
    Uses confidence tiers: verified, likely_valid, unverified, suspicious.
    """

    def __init__(
        self,
        client: UnifiedLLMClient,
        assignment: RoleAssignment,
        **kwargs: Any,
    ) -> None:
        # The RESEARCH role is used for reference verification in the
        # role assignment table.
        super().__init__(
            role=AgentRole.RESEARCH,
            client=client,
            assignment=assignment,
            **kwargs,
        )

    async def _run_analysis(
        self,
        document: ParsedDocument,
        **kwargs: Any,
    ) -> list[Finding]:
        """Run reference verification using external API results."""
        api_results = kwargs.get("api_results", {})

        # Use the reference_verification prompt key (not 'research')
        system_prompt, user_template = get_prompts("reference_verification")

        references_text, api_text = _format_references_with_status(
            document,
            api_results,
        )

        # Escape curly braces to avoid crashes on LaTeX/code (CRIT-1).
        user_msg = user_template.format(
            references_list=references_text.replace("{", "{{").replace("}", "}}"),
            api_results=api_text.replace("{", "{{").replace("}", "}}"),
        )

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_msg),
        ]
        response = await self._call_llm(messages)
        return self._parse_findings(response)

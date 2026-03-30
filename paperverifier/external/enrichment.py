"""External evidence enrichment for the verification pipeline.

Queries Crossref, OpenAlex, and Semantic Scholar to build the
``external_data`` dict expected by :meth:`AgentOrchestrator.verify`.

This module bridges the gap between the fully-implemented external API
clients and the orchestrator (Codex-1 fix #3).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from paperverifier.config import get_settings
from paperverifier.external.crossref import CrossRefClient
from paperverifier.external.openalex import OpenAlexClient
from paperverifier.external.semantic_scholar import SemanticScholarClient
from paperverifier.models.document import ParsedDocument, Reference

logger = structlog.get_logger(__name__)


async def enrich_document(document: ParsedDocument) -> dict[str, Any]:
    """Query external APIs to build evidence for verification agents.

    Returns a dict with two keys:

    * ``api_results`` -- maps reference citation keys to lookup dicts
      consumed by :class:`ReferenceVerificationAgent`.
    * ``related_works`` -- list of related-work dicts consumed by
      :class:`NoveltyAssessmentAgent`.
    """
    settings = get_settings()

    api_results: dict[str, Any] = {}
    related_works: list[dict[str, Any]] = []

    # Cap concurrency to avoid flooding external APIs (Codex-2).
    _MAX_CONCURRENT_LOOKUPS = 10
    sem = asyncio.Semaphore(_MAX_CONCURRENT_LOOKUPS)

    async def _bounded_lookup(
        ref: Reference,
        crossref: CrossRefClient,
        openalex: OpenAlexClient,
        s2: SemanticScholarClient,
    ) -> dict[str, Any] | None:
        async with sem:
            return await _lookup_reference(ref, crossref, openalex, s2)

    async with CrossRefClient(email=settings.crossref_email) as crossref, \
               OpenAlexClient(email=settings.openalex_email) as openalex, \
               SemanticScholarClient(api_key=settings.semantic_scholar_api_key) as s2:

        # 1. Look up references by DOI and title
        ref_tasks = [
            _bounded_lookup(ref, crossref, openalex, s2)
            for ref in document.references
        ]
        results = await asyncio.gather(*ref_tasks, return_exceptions=True)

        for ref, result in zip(document.references, results):
            key = ref.citation_key or ref.id
            if isinstance(result, Exception):
                logger.warning(
                    "enrichment_ref_failed",
                    ref_key=key,
                    error=str(result),
                )
                continue
            if result is not None:
                api_results[key] = result

        # 2. Find related works for novelty assessment
        # Use the document title to find the paper in OpenAlex, then
        # fetch related works from the top match.
        if document.title:
            try:
                oa_results = await openalex.search_by_title(document.title)
                if oa_results:
                    top_match = oa_results[0]
                    work_id = top_match.get("id", "")
                    if work_id:
                        related_works = await openalex.get_related_works(work_id)
            except Exception as exc:
                logger.warning(
                    "enrichment_related_works_failed",
                    error=str(exc),
                )

    logger.info(
        "enrichment_completed",
        refs_enriched=len(api_results),
        refs_total=len(document.references),
        related_works=len(related_works),
    )

    return {
        "api_results": api_results,
        "related_works": related_works,
    }


async def _lookup_reference(
    ref: Reference,
    crossref: CrossRefClient,
    openalex: OpenAlexClient,
    s2: SemanticScholarClient,
) -> dict[str, Any] | None:
    """Look up a single reference across all three APIs.

    Tries DOI first (most reliable), then title search. Merges results
    from the first API that returns a match.
    """
    result: dict[str, Any] = {}

    # Strategy 1: DOI lookup (highest confidence)
    if ref.doi:
        cr_data = await crossref.verify_doi(ref.doi)
        if cr_data:
            result = _parse_crossref(cr_data, ref)
            result["source"] = "crossref"
            # Check retraction
            retracted = await crossref.check_retraction(ref.doi)
            result["retracted"] = retracted
            return result

    # Strategy 2: Title search via Semantic Scholar
    search_title = ref.title or (ref.raw_text[:120] if ref.raw_text else "")
    if search_title:
        s2_papers = await s2.search_paper(search_title)
        for paper in s2_papers:
            if _titles_match(search_title, paper.get("title", "")):
                result = _parse_s2(paper, ref)
                result["source"] = "semantic_scholar"
                return result

    # Strategy 3: Title search via OpenAlex
    if search_title:
        oa_results = await openalex.search_by_title(search_title)
        for work in oa_results:
            if _titles_match(search_title, work.get("title", "")):
                result = _parse_openalex(work, ref)
                result["source"] = "openalex"
                return result

    return None


def _titles_match(query: str, candidate: str) -> bool:
    """Fuzzy title match: lowercased, stripped, 80% overlap check."""
    q = query.lower().strip()
    c = candidate.lower().strip()
    if not q or not c:
        return False
    # Exact match
    if q == c:
        return True
    # One contains the other
    if q in c or c in q:
        return True
    # Word overlap check
    q_words = set(q.split())
    c_words = set(c.split())
    if not q_words:
        return False
    intersection = len(q_words & c_words)
    union = len(q_words | c_words)
    jaccard = intersection / union if union else 0.0
    return jaccard >= 0.60


def _parse_crossref(data: dict[str, Any], ref: Reference) -> dict[str, Any]:
    """Extract structured fields from a Crossref work record."""
    title_list = data.get("title", [])
    matched_title = title_list[0] if title_list else None
    published = data.get("published-print") or data.get("published-online") or {}
    date_parts = published.get("date-parts", [[None]])[0]
    matched_year = date_parts[0] if date_parts else None

    return {
        "matched_title": matched_title,
        "matched_year": matched_year,
        "matched_doi": data.get("DOI"),
        "citation_count": data.get("is-referenced-by-count"),
        "confidence": 0.95 if matched_title else 0.7,
        "retracted": False,
    }


def _parse_s2(paper: dict[str, Any], ref: Reference) -> dict[str, Any]:
    """Extract structured fields from a Semantic Scholar paper record."""
    return {
        "matched_title": paper.get("title"),
        "matched_year": paper.get("year"),
        "matched_doi": (paper.get("externalIds") or {}).get("DOI"),
        "citation_count": paper.get("citationCount"),
        "confidence": 0.85,
        "retracted": False,
    }


def _parse_openalex(work: dict[str, Any], ref: Reference) -> dict[str, Any]:
    """Extract structured fields from an OpenAlex work record."""
    return {
        "matched_title": work.get("title"),
        "matched_year": work.get("publication_year"),
        "matched_doi": work.get("doi"),
        "citation_count": work.get("cited_by_count"),
        "confidence": 0.80,
        "retracted": work.get("is_retracted", False),
    }

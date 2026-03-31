"""Integration tests for the external enrichment pipeline.

Mocks all three API clients (CrossRef, OpenAlex, Semantic Scholar) so
no network calls are made, but exercises the ``enrich_document`` function
end-to-end, including error-handling paths.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paperverifier.external.enrichment import enrich_document
from paperverifier.models.document import ParsedDocument, Reference


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_document(
    title: str = "Test Paper",
    references: list[Reference] | None = None,
) -> ParsedDocument:
    """Build a minimal ParsedDocument for enrichment tests."""
    return ParsedDocument(
        title=title,
        full_text="This is a test paper about testing.",
        references=references or [],
    )


def _mock_client_class(instance: AsyncMock) -> MagicMock:
    """Create a mock class whose async-context-manager yields *instance*.

    The mock class is callable (like a real class constructor) and
    supports ``async with Cls(...) as obj:``.
    """
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=instance)
    ctx.__aexit__ = AsyncMock(return_value=False)

    cls = MagicMock(return_value=ctx)
    return cls


def _default_crossref() -> AsyncMock:
    client = AsyncMock()
    client.verify_doi = AsyncMock(return_value=None)
    client.search_by_title = AsyncMock(return_value=[])
    client.check_retraction = AsyncMock(return_value=False)
    return client


def _default_openalex() -> AsyncMock:
    client = AsyncMock()
    client.search_by_title = AsyncMock(return_value=[])
    client.get_related_works = AsyncMock(return_value=[])
    return client


def _default_s2() -> AsyncMock:
    client = AsyncMock()
    client.search_paper = AsyncMock(return_value=[])
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enrichment_returns_expected_keys():
    """With no references, enrich_document should return both expected keys."""
    document = _make_document(references=[])

    crossref_inst = _default_crossref()
    openalex_inst = _default_openalex()
    s2_inst = _default_s2()

    with (
        patch(
            "paperverifier.external.enrichment.CrossRefClient",
            _mock_client_class(crossref_inst),
        ),
        patch(
            "paperverifier.external.enrichment.OpenAlexClient",
            _mock_client_class(openalex_inst),
        ),
        patch(
            "paperverifier.external.enrichment.SemanticScholarClient",
            _mock_client_class(s2_inst),
        ),
        patch("paperverifier.external.enrichment.get_settings", MagicMock()),
    ):
        result = await enrich_document(document)

    assert isinstance(result, dict)
    assert "api_results" in result
    assert "related_works" in result
    assert isinstance(result["api_results"], dict)
    assert isinstance(result["related_works"], list)


@pytest.mark.asyncio
async def test_enrichment_handles_api_failure_gracefully():
    """When one API raises an exception, enrichment should still return
    partial results rather than propagating the error."""

    ref = Reference(
        raw_text="Smith et al. 2020. A great paper.",
        title="A great paper",
        doi="10.1234/fake",
        citation_key="smith2020",
    )
    document = _make_document(references=[ref])

    # CrossRef will raise an exception
    crossref_inst = _default_crossref()
    crossref_inst.verify_doi = AsyncMock(
        side_effect=RuntimeError("CrossRef API unavailable"),
    )

    # OpenAlex works fine but returns nothing
    openalex_inst = _default_openalex()

    # Semantic Scholar works fine but returns nothing
    s2_inst = _default_s2()

    with (
        patch(
            "paperverifier.external.enrichment.CrossRefClient",
            _mock_client_class(crossref_inst),
        ),
        patch(
            "paperverifier.external.enrichment.OpenAlexClient",
            _mock_client_class(openalex_inst),
        ),
        patch(
            "paperverifier.external.enrichment.SemanticScholarClient",
            _mock_client_class(s2_inst),
        ),
        patch("paperverifier.external.enrichment.get_settings", MagicMock()),
    ):
        result = await enrich_document(document)

    # Should still return the expected structure, not crash
    assert isinstance(result, dict)
    assert "api_results" in result
    assert "related_works" in result

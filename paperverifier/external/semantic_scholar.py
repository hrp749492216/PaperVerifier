"""Async client for the `Semantic Scholar <https://www.semanticscholar.org/>`_ Academic Graph API.

Semantic Scholar provides rich citation-graph data, paper embeddings, and
TLDR summaries.  This client wraps the ``/graph/v1`` endpoints with rate
limiting, circuit breaking, and graceful degradation.

Usage::

    client = SemanticScholarClient(api_key="optional-key")
    papers = await client.search_paper("attention is all you need")
    await client.close()
"""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any

import aiohttp
import structlog

from paperverifier.external.rate_limiter import AsyncRateLimiter, CircuitBreaker

logger = structlog.get_logger(__name__)

S2_BASE_URL: str = "https://api.semanticscholar.org/graph/v1"

# Default request timeout in seconds.
_DEFAULT_TIMEOUT: float = 30.0

# Default fields to request from the papers endpoint.
_DEFAULT_PAPER_FIELDS: str = (
    "paperId,externalIds,title,abstract,year,referenceCount,"
    "citationCount,influentialCitationCount,isOpenAccess,fieldsOfStudy,"
    "authors,venue,publicationDate,tldr"
)

_DEFAULT_CITATION_FIELDS: str = (
    "paperId,title,year,authors,venue,citationCount,externalIds"
)


class SemanticScholarClient:
    """Async client for the Semantic Scholar Academic Graph API.

    Parameters
    ----------
    api_key:
        Optional API key.  When provided, requests include an
        ``x-api-key`` header which grants higher rate limits
        (100 req/s vs. 10 req/s for unauthenticated).
    rate_limiter:
        Shared :class:`AsyncRateLimiter` instance.  When omitted, a
        conservative default is created based on whether an API key is
        present.
    timeout:
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str = "",
        rate_limiter: AsyncRateLimiter | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key

        # Authenticated users get 100 req/s; unauthenticated get ~10.
        if rate_limiter is not None:
            self._rate_limiter = rate_limiter
        elif api_key:
            self._rate_limiter = AsyncRateLimiter(
                max_concurrent=10, requests_per_second=50.0,
            )
        else:
            self._rate_limiter = AsyncRateLimiter(
                max_concurrent=3, requests_per_second=5.0,
            )

        self._circuit_breaker = CircuitBreaker(
            failure_threshold=3, recovery_timeout=60.0, name="semantic_scholar",
        )
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    # -- Destructor safety net --------------------------------------------

    def __del__(self) -> None:
        """Warn and attempt cleanup if the client was not properly closed."""
        if self._session is not None and not self._session.closed:
            logger.warning(
                "s2_client_not_closed",
                hint="SemanticScholarClient was garbage-collected without "
                     "calling close(). Use 'async with' to avoid connection leaks.",
            )
            # Cannot await in __del__; schedule close on the running loop.
            try:
                import asyncio as _asyncio

                loop = _asyncio.get_running_loop()
                loop.create_task(self._session.close())
            except RuntimeError:
                pass  # No running loop; session will be collected.

    # -- Async context manager ---------------------------------------------

    async def __aenter__(self) -> SemanticScholarClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- Session management ------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return (or lazily create) the shared :class:`aiohttp.ClientSession`."""
        async with self._session_lock:
            if self._session is None or self._session.closed:
                headers: dict[str, str] = {
                    "User-Agent": "PaperVerifier/0.1",
                }
                if self._api_key:
                    headers["x-api-key"] = self._api_key
                self._session = aiohttp.ClientSession(
                    headers=headers, timeout=self._timeout,
                )
            return self._session

    # -- Low-level request -------------------------------------------------

    async def _request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Make a rate-limited, circuit-broken GET request.

        Returns the parsed JSON response on success, or ``None`` on any
        failure (network, HTTP error, circuit open).
        """
        if not await self._circuit_breaker.can_execute():
            logger.warning("s2_circuit_open", endpoint=endpoint)
            return None

        async with self._rate_limiter:
            try:
                session = await self._get_session()
                url = f"{S2_BASE_URL}{endpoint}"
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        logger.warning("s2_rate_limited", endpoint=endpoint)
                        await self._circuit_breaker.record_failure()
                        return None
                    if resp.status == 404:
                        logger.debug("s2_not_found", endpoint=endpoint)
                        # A 404 is a valid response, not a service failure.
                        await self._circuit_breaker.record_success()
                        return None
                    if resp.status >= 400:
                        body = await resp.text()
                        logger.warning(
                            "s2_http_error",
                            endpoint=endpoint,
                            status=resp.status,
                            body=body[:200],
                        )
                        await self._circuit_breaker.record_failure()
                        return None
                    data: dict[str, Any] = await resp.json(content_type=None)
                    await self._circuit_breaker.record_success()
                    return data
            except asyncio.TimeoutError:
                logger.warning("s2_timeout", endpoint=endpoint)
                await self._circuit_breaker.record_failure()
                return None
            except aiohttp.ClientError as exc:
                logger.warning(
                    "s2_connection_error",
                    endpoint=endpoint,
                    error=str(exc),
                )
                await self._circuit_breaker.record_failure()
                return None
            except Exception:  # noqa: BLE001
                logger.exception("s2_unexpected_error", endpoint=endpoint)
                await self._circuit_breaker.record_failure()
                return None

    # -- Public API --------------------------------------------------------

    async def search_paper(self, query: str) -> list[dict[str, Any]]:
        """Search for papers by query string.

        Parameters
        ----------
        query:
            Free-text search query.

        Returns a list of paper objects, or an empty list on failure.
        """
        data = await self._request(
            "/paper/search",
            params={
                "query": query,
                "limit": "10",
                "fields": _DEFAULT_PAPER_FIELDS,
            },
        )
        if data is None:
            return []
        papers: list[dict[str, Any]] = data.get("data", [])
        logger.debug("s2_search_results", query=query, count=len(papers))
        return papers

    async def get_paper(self, paper_id: str) -> dict[str, Any] | None:
        """Get paper metadata by Semantic Scholar ID, DOI, or ArXiv ID.

        Parameters
        ----------
        paper_id:
            Any of the following formats:
            - Semantic Scholar ID (e.g. ``"649def34f8be52c8b66281af98ae884c09aef38b"``)
            - DOI (e.g. ``"DOI:10.1038/s41586-020-2649-2"``)
            - ArXiv ID (e.g. ``"ARXIV:2106.09685"``)
            - Corpus ID (e.g. ``"CorpusID:215416146"``)

        Returns the paper metadata dict, or ``None`` on failure / not found.
        """
        return await self._request(
            f"/paper/{urllib.parse.quote(paper_id, safe='')}",
            params={"fields": _DEFAULT_PAPER_FIELDS},
        )

    async def get_citations(
        self,
        paper_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get papers that cite the given paper.

        Parameters
        ----------
        paper_id:
            Paper identifier (any format accepted by :meth:`get_paper`).
        limit:
            Maximum number of citing papers to return (max 1000).

        Returns a list of citing-paper objects, or an empty list on failure.
        """
        limit = min(limit, 1000)
        data = await self._request(
            f"/paper/{urllib.parse.quote(paper_id, safe='')}/citations",
            params={
                "limit": str(limit),
                "fields": _DEFAULT_CITATION_FIELDS,
            },
        )
        if data is None:
            return []
        # Each entry has {"citingPaper": {...}, "contexts": [...], ...}.
        raw: list[dict[str, Any]] = data.get("data", [])
        citations = [
            entry["citingPaper"]
            for entry in raw
            if "citingPaper" in entry and entry["citingPaper"].get("paperId")
        ]
        logger.debug(
            "s2_citations", paper_id=paper_id, count=len(citations),
        )
        return citations

    async def get_references(
        self,
        paper_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get papers referenced by the given paper.

        Parameters
        ----------
        paper_id:
            Paper identifier (any format accepted by :meth:`get_paper`).
        limit:
            Maximum number of referenced papers to return (max 1000).

        Returns a list of referenced-paper objects, or an empty list on
        failure.
        """
        limit = min(limit, 1000)
        data = await self._request(
            f"/paper/{urllib.parse.quote(paper_id, safe='')}/references",
            params={
                "limit": str(limit),
                "fields": _DEFAULT_CITATION_FIELDS,
            },
        )
        if data is None:
            return []
        # Each entry has {"citedPaper": {...}, "contexts": [...], ...}.
        raw: list[dict[str, Any]] = data.get("data", [])
        references = [
            entry["citedPaper"]
            for entry in raw
            if "citedPaper" in entry and entry["citedPaper"].get("paperId")
        ]
        logger.debug(
            "s2_references", paper_id=paper_id, count=len(references),
        )
        return references

    # -- Lifecycle ---------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

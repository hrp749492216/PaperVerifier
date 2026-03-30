"""Async client for the `OpenAlex <https://openalex.org/>`_ REST API.

OpenAlex is a free, open catalogue of the global research system.  This
client provides methods for title search, DOI lookup, retraction checking,
and related-work discovery -- all wrapped with rate limiting, circuit
breaking, and graceful degradation.

Usage::

    client = OpenAlexClient(email="you@example.com")
    works = await client.search_by_title("attention is all you need")
    await client.close()
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import structlog

from paperverifier.external.rate_limiter import AsyncRateLimiter, CircuitBreaker

logger = structlog.get_logger(__name__)

OPENALEX_BASE_URL: str = "https://api.openalex.org"

# Default request timeout in seconds.
_DEFAULT_TIMEOUT: float = 30.0


class OpenAlexClient:
    """Async client for the OpenAlex API with built-in resilience.

    Parameters
    ----------
    email:
        Optional contact email.  When provided, requests are routed to the
        OpenAlex *polite pool* which offers higher rate limits.
    rate_limiter:
        Shared :class:`AsyncRateLimiter` instance.  A conservative default
        (3 concurrent / 5 req/s) is used when omitted.
    timeout:
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        email: str = "",
        rate_limiter: AsyncRateLimiter | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._email = email
        self._rate_limiter = rate_limiter or AsyncRateLimiter(
            max_concurrent=3, requests_per_second=5.0,
        )
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=3, recovery_timeout=60.0, name="openalex",
        )
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    # -- Session management ------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return (or lazily create) the shared :class:`aiohttp.ClientSession`."""
        if self._session is None or self._session.closed:
            headers: dict[str, str] = {}
            if self._email:
                headers["User-Agent"] = f"PaperVerifier/0.1 (mailto:{self._email})"
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
        if not self._circuit_breaker.can_execute():
            logger.warning("openalex_circuit_open", endpoint=endpoint)
            return None

        async with self._rate_limiter:
            try:
                session = await self._get_session()
                url = f"{OPENALEX_BASE_URL}{endpoint}"
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        logger.warning("openalex_rate_limited", endpoint=endpoint)
                        self._circuit_breaker.record_failure()
                        return None
                    if resp.status >= 400:
                        body = await resp.text()
                        logger.warning(
                            "openalex_http_error",
                            endpoint=endpoint,
                            status=resp.status,
                            body=body[:200],
                        )
                        self._circuit_breaker.record_failure()
                        return None
                    data: dict[str, Any] = await resp.json(content_type=None)
                    self._circuit_breaker.record_success()
                    return data
            except asyncio.TimeoutError:
                logger.warning("openalex_timeout", endpoint=endpoint)
                self._circuit_breaker.record_failure()
                return None
            except aiohttp.ClientError as exc:
                logger.warning(
                    "openalex_connection_error",
                    endpoint=endpoint,
                    error=str(exc),
                )
                self._circuit_breaker.record_failure()
                return None
            except Exception:  # noqa: BLE001
                logger.exception("openalex_unexpected_error", endpoint=endpoint)
                self._circuit_breaker.record_failure()
                return None

    # -- Public API --------------------------------------------------------

    async def search_by_title(self, title: str) -> list[dict[str, Any]]:
        """Search for works by title.

        Returns a list of work objects, or an empty list on failure.

        Parameters
        ----------
        title:
            The paper title (or partial title) to search for.
        """
        data = await self._request(
            "/works",
            params={"filter": f"title.search:{title}", "per_page": "10"},
        )
        if data is None:
            return []
        results: list[dict[str, Any]] = data.get("results", [])
        logger.debug("openalex_search_results", title=title, count=len(results))
        return results

    async def get_by_doi(self, doi: str) -> dict[str, Any] | None:
        """Look up a specific work by DOI.

        Parameters
        ----------
        doi:
            The DOI string (e.g. ``"10.1234/example"``).  A ``https://doi.org/``
            prefix is added automatically if missing.
        """
        if not doi.startswith("https://doi.org/"):
            doi = f"https://doi.org/{doi}"
        return await self._request(f"/works/{doi}")

    async def check_retraction(self, work_id: str) -> bool:
        """Check whether a work has been retracted.

        Parameters
        ----------
        work_id:
            An OpenAlex work ID (e.g. ``"W1234567890"``).

        Returns ``True`` if the work is marked as retracted, ``False``
        otherwise (including on lookup failure).
        """
        data = await self._request(f"/works/{work_id}")
        if data is None:
            return False
        return bool(data.get("is_retracted", False))

    async def get_related_works(
        self,
        work_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get related works for novelty assessment.

        Uses the ``related_works`` field from the work record to fetch full
        metadata for up to *limit* related entries.

        Parameters
        ----------
        work_id:
            An OpenAlex work ID.
        limit:
            Maximum number of related works to return.
        """
        data = await self._request(f"/works/{work_id}")
        if data is None:
            return []

        related_ids: list[str] = data.get("related_works", [])[:limit]
        if not related_ids:
            return []

        # Batch-fetch related works via the pipe-separated filter.
        pipe_ids = "|".join(related_ids)
        batch = await self._request(
            "/works",
            params={"filter": f"openalex_id:{pipe_ids}", "per_page": str(limit)},
        )
        if batch is None:
            return []
        results: list[dict[str, Any]] = batch.get("results", [])
        logger.debug("openalex_related_works", work_id=work_id, count=len(results))
        return results

    # -- Lifecycle ---------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

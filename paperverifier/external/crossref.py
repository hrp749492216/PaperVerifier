"""Async client for the `Crossref <https://www.crossref.org/>`_ REST API.

Crossref is the authoritative registry for DOI metadata.  This client
provides DOI verification, title search, and retraction checking -- all
wrapped with rate limiting, circuit breaking, and graceful degradation.

Usage::

    client = CrossRefClient(email="you@example.com")
    meta = await client.verify_doi("10.1038/s41586-020-2649-2")
    await client.close()
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote as _url_quote

import aiohttp
import structlog

from paperverifier.external.rate_limiter import AsyncRateLimiter, CircuitBreaker

logger = structlog.get_logger(__name__)

CROSSREF_BASE_URL: str = "https://api.crossref.org"

# Default request timeout in seconds.
_DEFAULT_TIMEOUT: float = 30.0


class CrossRefClient:
    """Async client for the Crossref API with built-in resilience.

    Parameters
    ----------
    email:
        Optional contact email.  When provided, requests are routed to the
        Crossref *polite pool* (via the ``mailto`` query parameter) which
        offers higher rate limits and priority service.
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
            failure_threshold=3, recovery_timeout=60.0, name="crossref",
        )
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    # -- Async context manager ---------------------------------------------

    async def __aenter__(self) -> CrossRefClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- Session management ------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return (or lazily create) the shared :class:`aiohttp.ClientSession`."""
        async with self._session_lock:
            if self._session is None or self._session.closed:
                headers: dict[str, str] = {
                    "User-Agent": f"PaperVerifier/0.1 (mailto:{self._email})"
                    if self._email
                    else "PaperVerifier/0.1",
                }
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
            logger.warning("crossref_circuit_open", endpoint=endpoint)
            return None

        # Inject polite-pool email when configured.
        if self._email:
            params = dict(params) if params else {}
            params.setdefault("mailto", self._email)

        async with self._rate_limiter:
            try:
                session = await self._get_session()
                url = f"{CROSSREF_BASE_URL}{endpoint}"
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        logger.warning("crossref_rate_limited", endpoint=endpoint)
                        await self._circuit_breaker.record_failure()
                        return None
                    if resp.status == 404:
                        logger.debug("crossref_not_found", endpoint=endpoint)
                        # A 404 is a valid response (DOI does not exist),
                        # not a service failure.
                        await self._circuit_breaker.record_success()
                        return None
                    if resp.status >= 400:
                        body = await resp.text()
                        logger.warning(
                            "crossref_http_error",
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
                logger.warning("crossref_timeout", endpoint=endpoint)
                await self._circuit_breaker.record_failure()
                return None
            except aiohttp.ClientError as exc:
                logger.warning(
                    "crossref_connection_error",
                    endpoint=endpoint,
                    error=str(exc),
                )
                await self._circuit_breaker.record_failure()
                return None
            except Exception:  # noqa: BLE001
                logger.exception("crossref_unexpected_error", endpoint=endpoint)
                await self._circuit_breaker.record_failure()
                return None

    # -- Public API --------------------------------------------------------

    async def verify_doi(self, doi: str) -> dict[str, Any] | None:
        """Verify a DOI exists and retrieve its metadata.

        Parameters
        ----------
        doi:
            The DOI to look up (e.g. ``"10.1038/s41586-020-2649-2"``).

        Returns the Crossref work message, or ``None`` if the DOI does not
        exist or the request fails.
        """
        data = await self._request(f"/works/{_url_quote(doi, safe='')}")
        if data is None:
            return None
        # Crossref wraps results in {"status": "ok", "message": {...}}.
        message: dict[str, Any] | None = data.get("message")
        if message:
            logger.debug("crossref_doi_verified", doi=doi)
        return message

    async def search_by_title(self, title: str) -> list[dict[str, Any]]:
        """Search works by title for fuzzy matching.

        Parameters
        ----------
        title:
            The paper title (or partial title) to search for.

        Returns a list of work objects, or an empty list on failure.
        """
        data = await self._request(
            "/works",
            params={"query.title": title, "rows": "10"},
        )
        if data is None:
            return []
        message = data.get("message", {})
        items: list[dict[str, Any]] = message.get("items", [])
        logger.debug("crossref_search_results", title=title, count=len(items))
        return items

    async def check_retraction(self, doi: str) -> bool:
        """Check if a DOI has been retracted.

        Crossref marks retractions via the ``update-to`` field on the
        original work record.  If any update has type ``"retraction"``,
        this method returns ``True``.

        Parameters
        ----------
        doi:
            The DOI to check.

        Returns ``True`` if retracted, ``False`` otherwise (including on
        lookup failure).
        """
        data = await self._request(f"/works/{_url_quote(doi, safe='')}")
        if data is None:
            return False
        message: dict[str, Any] = data.get("message", {})
        return self.is_retracted(message)

    @staticmethod
    def is_retracted(work_message: dict[str, Any]) -> bool:
        """Check retraction status from an already-fetched Crossref work message.

        This avoids a second HTTP request when the work data was already
        fetched by :meth:`verify_doi`.
        """
        # Check the ``update-to`` list for retraction entries.
        updates: list[dict[str, Any]] = work_message.get("update-to", [])
        for update in updates:
            if update.get("type", "").lower() == "retraction":
                logger.info("crossref_retraction_found")
                return True

        # Also check the ``relation.is-retracted-by`` field.
        relation: dict[str, Any] = work_message.get("relation", {})
        if relation.get("is-retracted-by"):
            logger.info("crossref_retraction_found_via_relation")
            return True

        return False

    # -- Lifecycle ---------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

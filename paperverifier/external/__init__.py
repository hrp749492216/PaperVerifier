"""External academic API clients with built-in resilience.

Provides async clients for OpenAlex, Crossref, and Semantic Scholar, each
wrapped with :class:`AsyncRateLimiter` (sliding-window rate limiting) and
:class:`CircuitBreaker` (three-state failure isolation).  All clients degrade
gracefully -- returning ``None`` or empty lists on failure instead of raising.
"""

from __future__ import annotations

from paperverifier.external.crossref import CrossRefClient
from paperverifier.external.openalex import OpenAlexClient
from paperverifier.external.rate_limiter import AsyncRateLimiter, CircuitBreaker
from paperverifier.external.semantic_scholar import SemanticScholarClient

__all__ = [
    # Resilience primitives
    "AsyncRateLimiter",
    "CircuitBreaker",
    # API clients
    "CrossRefClient",
    "OpenAlexClient",
    "SemanticScholarClient",
]

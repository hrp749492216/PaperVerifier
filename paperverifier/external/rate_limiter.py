"""Rate limiting and circuit breaker primitives for external API clients.

Provides :class:`AsyncRateLimiter` (sliding-window + concurrency limiting) and
:class:`CircuitBreaker` (three-state failure isolation) to protect both the
application and upstream services from overload or cascading failures.

Usage::

    limiter = AsyncRateLimiter(max_concurrent=3, requests_per_second=5.0)
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0, name="openalex")

    if await breaker.can_execute():
        async with limiter:
            try:
                result = await do_request()
                await breaker.record_success()
            except Exception:
                await breaker.record_failure()
"""

from __future__ import annotations

import asyncio
import time

import structlog

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Async rate limiter
# ---------------------------------------------------------------------------


class AsyncRateLimiter:
    """Async rate limiter with sliding window and concurrency cap.

    Combines an :class:`asyncio.Semaphore` for bounding concurrent in-flight
    requests with a sliding-window counter that enforces a maximum
    *requests_per_second* throughput.  Both constraints must be satisfied
    before :meth:`acquire` returns.

    The limiter is designed to be used as an async context manager::

        async with limiter:
            await make_request()

    Parameters
    ----------
    max_concurrent:
        Maximum number of requests allowed in-flight simultaneously.
    requests_per_second:
        Upper bound on throughput measured over a 1-second sliding window.
    """

    def __init__(
        self,
        max_concurrent: int = 5,
        requests_per_second: float = 10.0,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._rate = requests_per_second
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until both concurrency and rate constraints are met."""
        await self._semaphore.acquire()
        while True:
            async with self._lock:
                now = time.monotonic()
                # Discard timestamps outside the 1-second sliding window.
                self._timestamps = [t for t in self._timestamps if now - t < 1.0]
                if len(self._timestamps) < self._rate:
                    self._timestamps.append(time.monotonic())
                    return
                sleep_time = 1.0 - (now - self._timestamps[0])
            # Sleep OUTSIDE the lock so other coroutines aren't blocked.
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    def release(self) -> None:
        """Release the concurrency semaphore."""
        self._semaphore.release()

    async def __aenter__(self) -> AsyncRateLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, *args: object) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Three-state circuit breaker for external service calls.

    +---------+    failure_threshold    +------+    recovery_timeout    +-----------+
    | CLOSED  | ---------------------->| OPEN | ---------------------->| HALF_OPEN |
    +---------+                        +------+                        +-----------+
         ^                                  ^                               |
         |           success                |          failure              |
         +----------------------------------+-------------------------------+

    Parameters
    ----------
    failure_threshold:
        Number of consecutive failures required to trip the breaker.
    recovery_timeout:
        Seconds to wait in OPEN state before transitioning to HALF_OPEN.
    name:
        Human-readable name for log messages.
    """

    CLOSED: str = "closed"
    OPEN: str = "open"
    HALF_OPEN: str = "half_open"

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 300.0,
        name: str = "",
    ) -> None:
        self.state: str = self.CLOSED
        self.failure_count: int = 0
        self.failure_threshold: int = failure_threshold
        self.recovery_timeout: float = recovery_timeout
        self.last_failure_time: float = 0.0
        self.name: str = name
        self._lock = asyncio.Lock()

    # -- Query -------------------------------------------------------------

    async def can_execute(self) -> bool:
        """Return ``True`` if a request is allowed in the current state.

        * **CLOSED** -- always allowed.
        * **OPEN** -- allowed only after :pyattr:`recovery_timeout` has
          elapsed, at which point the state transitions to HALF_OPEN.
        * **HALF_OPEN** -- one probe request is allowed.
        """
        async with self._lock:
            if self.state == self.CLOSED:
                return True

            if self.state == self.OPEN:
                elapsed = time.monotonic() - self.last_failure_time
                if elapsed >= self.recovery_timeout:
                    logger.info(
                        "circuit_breaker_half_open",
                        name=self.name,
                        elapsed=round(elapsed, 1),
                    )
                    self.state = self.HALF_OPEN
                    return True
                return False

            # HALF_OPEN -- allow the single probe request.
            return True

    # -- Feedback ----------------------------------------------------------

    async def record_success(self) -> None:
        """Record a successful request.  Resets the breaker to CLOSED."""
        async with self._lock:
            if self.state != self.CLOSED:
                logger.info("circuit_breaker_closed", name=self.name)
            self.state = self.CLOSED
            self.failure_count = 0

    async def record_failure(self) -> None:
        """Record a failed request.

        In CLOSED state the failure counter increments; when it reaches
        :pyattr:`failure_threshold` the breaker trips to OPEN.  In
        HALF_OPEN state, any failure immediately re-opens the breaker.
        """
        async with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.monotonic()

            if self.state == self.HALF_OPEN:
                self.state = self.OPEN
                logger.warning(
                    "circuit_breaker_reopened",
                    name=self.name,
                    failure_count=self.failure_count,
                )
            elif self.failure_count >= self.failure_threshold:
                self.state = self.OPEN
                logger.warning(
                    "circuit_breaker_opened",
                    name=self.name,
                    failure_count=self.failure_count,
                    recovery_timeout=self.recovery_timeout,
                )

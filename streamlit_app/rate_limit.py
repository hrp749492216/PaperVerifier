"""Per-session rate limiting for the Streamlit app.

Prevents abuse by limiting how many verification requests a single
session can submit within a rolling time window.
"""

from __future__ import annotations

import threading
import time


class SessionRateLimiter:
    """Sliding-window rate limiter keyed by session ID.

    Parameters
    ----------
    max_requests:
        Maximum number of requests allowed per *window_seconds*.
    window_seconds:
        Length of the sliding window in seconds.
    """

    def __init__(self, max_requests: int = 10, window_seconds: float = 3600.0) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, session_id: str) -> list[float]:
        """Remove expired timestamps and return the active list."""
        now = time.monotonic()
        timestamps = self._timestamps.get(session_id, [])
        active = [t for t in timestamps if now - t < self.window_seconds]
        self._timestamps[session_id] = active
        return active

    def check(self, session_id: str) -> bool:
        """Return True if the session is allowed to make another request."""
        with self._lock:
            active = self._prune(session_id)
            return len(active) < self.max_requests

    def record(self, session_id: str) -> None:
        """Record a request for the session."""
        with self._lock:
            self._prune(session_id)
            if session_id not in self._timestamps:
                self._timestamps[session_id] = []
            self._timestamps[session_id].append(time.monotonic())

    def remaining_wait(self, session_id: str) -> float:
        """Return seconds until the oldest request in the window expires."""
        with self._lock:
            active = self._prune(session_id)
            if len(active) < self.max_requests:
                return 0.0
            oldest = active[0]
            return max(0.0, self.window_seconds - (time.monotonic() - oldest))

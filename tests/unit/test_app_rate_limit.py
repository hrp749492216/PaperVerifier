"""Tests for app-level rate limiting."""

from __future__ import annotations

import time

from streamlit_app.rate_limit import SessionRateLimiter


class TestSessionRateLimiter:
    """Tests for per-session rate limiting of verification requests."""

    def test_allows_under_limit(self) -> None:
        limiter = SessionRateLimiter(max_requests=3, window_seconds=60)
        assert limiter.check("session-1") is True
        limiter.record("session-1")
        assert limiter.check("session-1") is True

    def test_blocks_over_limit(self) -> None:
        limiter = SessionRateLimiter(max_requests=2, window_seconds=60)
        limiter.record("session-1")
        limiter.record("session-1")
        assert limiter.check("session-1") is False

    def test_window_expires(self) -> None:
        limiter = SessionRateLimiter(max_requests=1, window_seconds=1)
        limiter.record("session-1")
        assert limiter.check("session-1") is False
        time.sleep(1.1)
        assert limiter.check("session-1") is True

    def test_independent_sessions(self) -> None:
        limiter = SessionRateLimiter(max_requests=1, window_seconds=60)
        limiter.record("session-1")
        assert limiter.check("session-1") is False
        assert limiter.check("session-2") is True

    def test_remaining_wait(self) -> None:
        limiter = SessionRateLimiter(max_requests=1, window_seconds=60)
        limiter.record("session-1")
        wait = limiter.remaining_wait("session-1")
        assert 55 < wait <= 60

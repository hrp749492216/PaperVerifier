"""Unit tests for streamlit_app.auth brute-force protection."""

from __future__ import annotations

import time

from streamlit_app.auth import _LoginThrottler


class TestLoginThrottler:
    """Tests for brute-force login protection."""

    def test_allows_first_attempt(self) -> None:
        throttler = _LoginThrottler(max_attempts=3, lockout_seconds=60)
        assert throttler.is_locked("session-1") is False

    def test_locks_after_max_attempts(self) -> None:
        throttler = _LoginThrottler(max_attempts=3, lockout_seconds=60)
        for _ in range(3):
            throttler.record_failure("session-1")
        assert throttler.is_locked("session-1") is True

    def test_does_not_lock_under_threshold(self) -> None:
        throttler = _LoginThrottler(max_attempts=3, lockout_seconds=60)
        for _ in range(2):
            throttler.record_failure("session-1")
        assert throttler.is_locked("session-1") is False

    def test_lockout_expires(self) -> None:
        throttler = _LoginThrottler(max_attempts=3, lockout_seconds=1)
        for _ in range(3):
            throttler.record_failure("session-1")
        assert throttler.is_locked("session-1") is True
        time.sleep(1.1)
        assert throttler.is_locked("session-1") is False

    def test_reset_clears_failures(self) -> None:
        throttler = _LoginThrottler(max_attempts=3, lockout_seconds=60)
        for _ in range(3):
            throttler.record_failure("session-1")
        throttler.reset("session-1")
        assert throttler.is_locked("session-1") is False

    def test_independent_sessions(self) -> None:
        throttler = _LoginThrottler(max_attempts=3, lockout_seconds=60)
        for _ in range(3):
            throttler.record_failure("session-1")
        assert throttler.is_locked("session-1") is True
        assert throttler.is_locked("session-2") is False

    def test_remaining_seconds(self) -> None:
        throttler = _LoginThrottler(max_attempts=3, lockout_seconds=60)
        for _ in range(3):
            throttler.record_failure("session-1")
        remaining = throttler.remaining_lockout("session-1")
        assert 55 < remaining <= 60

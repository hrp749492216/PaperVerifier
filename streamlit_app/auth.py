"""Simple password-based authentication gate for the Streamlit app.

Prevents unauthenticated access to expensive LLM endpoints.  The password
is read from the ``PV_APP_PASSWORD`` environment variable.  When the
variable is unset the app **fails closed** -- access is denied unless the
``PV_ALLOW_INSECURE_LOCAL`` environment variable is explicitly set to
``"1"``, which enables insecure local development mode with a warning banner.

Includes brute-force protection via :class:`_LoginThrottler` which locks
out a session after repeated failed login attempts.

Usage (in ``app.py``, before any other Streamlit calls except
``set_page_config``)::

    from streamlit_app.auth import require_auth
    require_auth()
"""

from __future__ import annotations

import hashlib
import hmac
import math
import os
import threading
import time
import uuid
from collections import defaultdict

import streamlit as st

# ---------------------------------------------------------------------------
# Brute-force login throttler
# ---------------------------------------------------------------------------

class _LoginThrottler:
    """Thread-safe in-memory brute-force throttler keyed by session ID.

    After *max_attempts* failed login attempts within *lockout_seconds* the
    session is locked out until the lockout window expires.
    """

    def __init__(self, max_attempts: int = 5, lockout_seconds: float = 300.0) -> None:
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    # -- public API --------------------------------------------------------

    def is_locked(self, session_id: str) -> bool:
        """Return ``True`` if *session_id* has exhausted its login attempts."""
        with self._lock:
            timestamps = self._failures.get(session_id)
            if not timestamps or len(timestamps) < self.max_attempts:
                return False
            # The oldest relevant failure must be within the lockout window.
            cutoff = time.monotonic() - self.lockout_seconds
            return timestamps[-self.max_attempts] >= cutoff

    def record_failure(self, session_id: str) -> None:
        """Record a failed login attempt for *session_id*."""
        with self._lock:
            entries = self._failures[session_id]
            entries.append(time.monotonic())
            # Keep only the last *max_attempts* entries to bound memory.
            if len(entries) > self.max_attempts:
                self._failures[session_id] = entries[-self.max_attempts:]

    def reset(self, session_id: str) -> None:
        """Clear all failure history for *session_id* (e.g. on successful login)."""
        with self._lock:
            self._failures.pop(session_id, None)

    def remaining_lockout(self, session_id: str) -> float:
        """Return seconds remaining in the lockout period, or ``0.0``."""
        with self._lock:
            timestamps = self._failures.get(session_id)
            if not timestamps or len(timestamps) < self.max_attempts:
                return 0.0
            oldest_relevant = timestamps[-self.max_attempts]
            elapsed = time.monotonic() - oldest_relevant
            remaining = self.lockout_seconds - elapsed
            return max(remaining, 0.0)


# Module-level throttler instance shared across reruns.
_throttler = _LoginThrottler(max_attempts=5, lockout_seconds=300.0)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _get_session_id() -> str:
    """Return a stable per-session identifier, creating one if needed."""
    if "_pv_session_id" not in st.session_state:
        st.session_state["_pv_session_id"] = str(uuid.uuid4())
    return st.session_state["_pv_session_id"]


# ---------------------------------------------------------------------------
# Core auth helpers
# ---------------------------------------------------------------------------

def _check_password(password: str, expected_hash: str) -> bool:
    """Constant-time comparison of SHA-256 hash of *password* against *expected_hash*."""
    actual = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(actual, expected_hash)


def require_auth() -> None:
    """Block the Streamlit app behind a password prompt.

    Reads the expected password from ``PV_APP_PASSWORD``.  If the env var
    is not set, access is denied (fail-closed) unless
    ``PV_ALLOW_INSECURE_LOCAL=1`` is set, which enables an insecure local
    development mode with a sidebar warning.

    The password is stored as a SHA-256 hash in session state after first
    successful login so the raw value is not kept in memory.

    Brute-force protection: after 5 consecutive failed attempts the session
    is locked out for 5 minutes.
    """
    expected_password = os.environ.get("PV_APP_PASSWORD", "").strip()

    if not expected_password:
        # Fail closed unless explicitly running in insecure local dev mode.
        if os.environ.get("PV_ALLOW_INSECURE_LOCAL") == "1":
            st.sidebar.warning(
                "Running in insecure local mode (no authentication). "
                "Set `PV_APP_PASSWORD` to enable login.",
                icon="\u26a0\ufe0f",
            )
            return
        st.error(
            "`PV_APP_PASSWORD` must be set. For local development, "
            "set `PV_ALLOW_INSECURE_LOCAL=1` to bypass authentication."
        )
        st.stop()

    # Already authenticated this session.
    if st.session_state.get("_pv_authenticated"):
        return

    session_id = _get_session_id()

    # Check brute-force lockout *before* rendering the login form.
    if _throttler.is_locked(session_id):
        remaining = math.ceil(_throttler.remaining_lockout(session_id))
        st.error(
            f"Too many failed login attempts. "
            f"Please try again in {remaining} seconds."
        )
        st.stop()

    # Compute expected hash once.
    expected_hash = hashlib.sha256(expected_password.encode()).hexdigest()

    st.title("PaperVerifier — Login")
    st.markdown("This application requires authentication.")

    with st.form("login_form"):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")

    if submitted:
        if _check_password(password, expected_hash):
            _throttler.reset(session_id)
            st.session_state["_pv_authenticated"] = True
            st.rerun()
        else:
            _throttler.record_failure(session_id)
            if _throttler.is_locked(session_id):
                remaining = math.ceil(_throttler.remaining_lockout(session_id))
                st.error(
                    f"Too many failed login attempts. "
                    f"Please try again in {remaining} seconds."
                )
            else:
                st.error("Incorrect password.")

    # Block the rest of the app until authenticated.
    st.stop()

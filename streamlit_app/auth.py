"""Simple password-based authentication gate for the Streamlit app.

Prevents unauthenticated access to expensive LLM endpoints.  The password
is read from the ``PV_APP_PASSWORD`` environment variable.  When the
variable is unset the app starts in *open mode* with a warning banner so
development / local usage is not blocked.

Usage (in ``app.py``, before any other Streamlit calls except
``set_page_config``)::

    from streamlit_app.auth import require_auth
    require_auth()
"""

from __future__ import annotations

import hashlib
import hmac
import os

import streamlit as st


def _check_password(password: str, expected_hash: str) -> bool:
    """Constant-time comparison of SHA-256 hash of *password* against *expected_hash*."""
    actual = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(actual, expected_hash)


def require_auth() -> None:
    """Block the Streamlit app behind a password prompt.

    Reads the expected password from ``PV_APP_PASSWORD``.  If the env var
    is not set, logs a warning and allows access (dev/local mode).

    The password is stored as a SHA-256 hash in session state after first
    successful login so the raw value is not kept in memory.
    """
    expected_password = os.environ.get("PV_APP_PASSWORD", "").strip()

    if not expected_password:
        # No password configured — warn but allow access for local dev.
        st.sidebar.warning(
            "No authentication configured. Set the `PV_APP_PASSWORD` "
            "environment variable to enable login.",
            icon="\u26a0\ufe0f",
        )
        return

    # Already authenticated this session.
    if st.session_state.get("_pv_authenticated"):
        return

    # Compute expected hash once.
    expected_hash = hashlib.sha256(expected_password.encode()).hexdigest()

    st.title("PaperVerifier — Login")
    st.markdown("This application requires authentication.")

    with st.form("login_form"):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")

    if submitted:
        if _check_password(password, expected_hash):
            st.session_state["_pv_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")

    # Block the rest of the app until authenticated.
    st.stop()

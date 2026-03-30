"""Shared utilities for the Streamlit application.

Centralises helpers that are used across multiple pages to avoid
code duplication (HIGH-Q2).
"""

from __future__ import annotations

import asyncio
from typing import Any

import nest_asyncio

nest_asyncio.apply()


def run_async(coro: Any) -> Any:
    """Run an async coroutine from synchronous Streamlit code.

    Uses ``nest_asyncio`` so that an already-running event loop (common
    in notebook / Streamlit environments) can be re-entered.
    """
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)

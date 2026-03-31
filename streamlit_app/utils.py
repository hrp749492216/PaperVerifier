"""Shared utilities for the Streamlit application.

Centralises helpers that are used across multiple pages to avoid
code duplication (HIGH-Q2).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


def run_async(coro: Any) -> Any:
    """Run an async coroutine from synchronous Streamlit code.

    Creates a dedicated event loop in a background thread to avoid
    interfering with Streamlit's own event loop.  This replaces the
    previous ``nest_asyncio.apply()`` approach which corrupted asyncio
    semantics system-wide.
    """
    result_holder: dict[str, Any] = {}

    def _run() -> None:
        loop = asyncio.new_event_loop()
        try:
            result_holder["value"] = loop.run_until_complete(coro)
        except BaseException as exc:
            result_holder["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join()

    if "error" in result_holder:
        raise result_holder["error"]
    return result_holder["value"]

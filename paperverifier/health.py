"""Readiness and liveness health checks.

Provides a meaningful health check endpoint that verifies critical
dependencies are available, beyond just "the process is running."
"""

from __future__ import annotations

import shutil

import structlog

from paperverifier.config import get_settings

logger = structlog.get_logger(__name__)


def check_health() -> dict[str, object]:
    """Run readiness checks and return a status dict.

    Checks:
    - Temp directory is writable.
    - ``pandoc`` is available on PATH.
    - At least one LLM API key env var is set.

    Returns a dict with ``"healthy"`` (bool) and ``"checks"`` (detail dict).
    """
    checks: dict[str, object] = {}
    healthy = True

    # 1. Temp directory writable
    try:
        temp_dir = get_settings().temp_dir
        temp_dir.mkdir(parents=True, exist_ok=True)
        probe = temp_dir / ".healthcheck"
        probe.write_text("ok")
        probe.unlink()
        checks["temp_dir"] = "ok"
    except OSError as exc:
        checks["temp_dir"] = f"FAIL: {exc}"
        healthy = False

    # 2. pandoc available
    if shutil.which("pandoc"):
        checks["pandoc"] = "ok"
    else:
        checks["pandoc"] = "FAIL: pandoc not found on PATH"
        healthy = False

    # 3. At least one API key configured
    import os

    api_key_vars = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]
    has_key = any(os.environ.get(v) for v in api_key_vars)
    checks["api_keys"] = "ok" if has_key else "WARN: no API keys set"

    return {"healthy": healthy, "checks": checks}

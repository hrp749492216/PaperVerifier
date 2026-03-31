"""Application configuration and structured logging setup.

Provides :class:`AppSettings` (backed by Pydantic v2 ``BaseSettings``) for all
non-LLM runtime knobs, and :func:`setup_logging` for structured log output via
*structlog*.  Configuration values are read from environment variables prefixed
with ``PAPERVERIFIER_`` or from a ``.env`` file at the project root.

Usage::

    from paperverifier.config import get_settings, setup_logging

    settings = get_settings()
    setup_logging(level=settings.log_level, fmt=settings.log_format)
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Literal

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
"""Absolute path to the repository root (one level above the package)."""

CONFIG_DIR: Path = PROJECT_ROOT / "config"
"""Default directory for YAML / JSON configuration files."""

TEMP_DIR: Path = PROJECT_ROOT / "temp_uploads"
"""Default directory for temporary uploaded documents."""

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class AppSettings(BaseSettings):
    """Non-LLM application settings, loaded from env vars or ``.env`` file.

    Every field can be overridden by setting an environment variable with the
    ``PAPERVERIFIER_`` prefix.  For example, ``PAPERVERIFIER_LOG_LEVEL=DEBUG``
    sets :pyattr:`log_level` to ``"DEBUG"``.
    """

    # -- Concurrency --------------------------------------------------------
    max_concurrent_agents: int = Field(
        default=9,
        ge=1,
        le=20,
        description="Maximum number of verification agents running in parallel.",
    )
    max_concurrent_llm_calls: int = Field(
        default=20,
        ge=1,
        le=50,
        description="Maximum number of concurrent outbound LLM API calls.",
    )

    # -- Timeouts (seconds) -------------------------------------------------
    llm_call_timeout: float = Field(
        default=120.0,
        ge=10,
        le=600,
        description="Per-call timeout for LLM requests in seconds.",
    )
    external_api_timeout: float = Field(
        default=30.0,
        ge=5,
        le=120,
        description="Timeout for external metadata API requests (OpenAlex, Crossref, etc.).",
    )
    git_clone_timeout: float = Field(
        default=120.0,
        ge=30,
        le=300,
        description="Timeout for cloning GitHub repositories.",
    )
    pipeline_timeout: float = Field(
        default=1800.0,
        ge=60,
        le=7200,
        description="Global timeout for the entire verification pipeline in seconds.",
    )

    # -- Document limits ----------------------------------------------------
    max_document_size_mb: int = Field(
        default=100,
        ge=1,
        le=500,
        description="Maximum allowed document size in megabytes.",
    )
    max_document_pages: int = Field(
        default=500,
        ge=1,
        le=2000,
        description="Maximum number of pages to process from a single document.",
    )

    # -- External API credentials -------------------------------------------
    openalex_email: str = Field(
        default="",
        description="Polite-pool email for OpenAlex API (higher rate limits).",
    )
    crossref_email: str = Field(
        default="",
        description="Polite-pool email for Crossref API.",
    )
    semantic_scholar_api_key: str = Field(
        default="",
        description="API key for Semantic Scholar (optional, enables higher rate limits).",
    )

    # -- Logging ------------------------------------------------------------
    log_level: str = Field(
        default="INFO",
        description="Root log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )
    log_format: Literal["json", "console"] = Field(
        default="json",
        description="Log output format: 'json' for production, 'console' for development.",
    )

    # -- Paths --------------------------------------------------------------
    temp_dir: Path = Field(
        default=TEMP_DIR,
        description="Directory for temporary file uploads.",
    )
    config_dir: Path = Field(
        default=CONFIG_DIR,
        description="Directory containing YAML/JSON configuration files.",
    )

    model_config = {
        "env_prefix": "PAPERVERIFIER_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_settings: AppSettings | None = None
_settings_lock = threading.Lock()


def get_settings() -> AppSettings:
    """Return the global :class:`AppSettings` singleton (thread-safe).

    The instance is created lazily on first call and reused thereafter.  To
    force a reload (e.g. in tests), assign ``None`` to the module-level
    ``_settings`` variable before calling this function again.
    """
    global _settings  # noqa: PLW0603
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = AppSettings()
    return _settings


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

#: Keys that must never appear in log output (redacted at the processor level).
_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "api_key",
    "api_secret",
    "password",
    "token",
    "secret",
    "authorization",
    "semantic_scholar_api_key",
    "paper_content",
    "document_text",
})


def _redact_sensitive_keys(
    _logger: object,
    _method: str,
    event_dict: dict[str, object],
) -> dict[str, object]:
    """Structlog processor that replaces sensitive values with ``'[REDACTED]'``.

    Matches any key whose lowercased form is in :data:`_SENSITIVE_KEYS` or
    contains common secret-related substrings.
    """
    for key in list(event_dict):
        key_lower = key.lower()
        if key_lower in _SENSITIVE_KEYS or any(
            fragment in key_lower
            for fragment in ("secret", "password", "api_key", "authorization", "access_token", "refresh_token", "auth_token", "bearer_token")
        ):
            # Do NOT match "token" as a substring -- it would redact
            # "input_tokens" and "output_tokens" metrics (HIGH-I4).
            event_dict[key] = "[REDACTED]"
    return event_dict


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure *structlog* with either JSON or coloured console output.

    Parameters
    ----------
    level:
        Minimum severity to emit (e.g. ``"DEBUG"``, ``"INFO"``).
    fmt:
        ``"json"`` for machine-readable newline-delimited JSON (production),
        or ``"console"`` for human-readable coloured output (development).

    The processor pipeline always includes:

    * context-variable merging (``structlog.contextvars``)
    * log-level annotation
    * stack-info rendering
    * automatic ``exc_info`` attachment
    * ISO-8601 timestamps
    * sensitive-key redaction
    * final rendering (JSON **or** console)
    """
    # Build the shared processor chain; the renderer goes last.
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_sensitive_keys,
    ]

    if fmt == "console":
        shared_processors.append(structlog.dev.ConsoleRenderer())
    else:
        shared_processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=shared_processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level.upper()),
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# Correlation / Request ID
# ---------------------------------------------------------------------------


def bind_request_id(request_id: str | None = None) -> str:
    """Bind a correlation ID to the current context for structured logging.

    If *request_id* is not provided, a short UUID is generated.  The ID
    is bound via ``structlog.contextvars`` so all subsequent log entries
    in the same async/thread context automatically include it.

    Returns the bound request ID.
    """
    import uuid as _uuid

    rid = request_id or _uuid.uuid4().hex[:8]
    structlog.contextvars.bind_contextvars(request_id=rid)
    return rid


def get_request_id() -> str | None:
    """Return the currently bound request ID, or None if unbound."""
    ctx = structlog.contextvars.get_contextvars()
    return ctx.get("request_id")


# ---------------------------------------------------------------------------
# Deferred logging setup -- call setup_logging() explicitly rather than
# at import time so that it does not override user configuration (MED-I9).
# ---------------------------------------------------------------------------

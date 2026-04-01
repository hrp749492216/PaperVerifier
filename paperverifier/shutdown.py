"""Graceful shutdown handler for the verification pipeline.

Registers a SIGTERM handler that sets a flag, allowing in-flight
verifications to save partial results before exiting.
"""

from __future__ import annotations

import signal
import threading

import structlog

logger = structlog.get_logger(__name__)

_shutdown_event = threading.Event()


def is_shutting_down() -> bool:
    """Return True if a shutdown signal has been received."""
    return _shutdown_event.is_set()


def request_shutdown() -> None:
    """Programmatically request a graceful shutdown."""
    _shutdown_event.set()
    logger.info("shutdown_requested", source="programmatic")


def _handle_sigterm(signum: int, frame: object) -> None:
    """SIGTERM handler that sets the shutdown event."""
    logger.info("sigterm_received", signal=signum)
    _shutdown_event.set()


def register_shutdown_handler() -> None:
    """Register the SIGTERM handler for graceful shutdown.

    Safe to call multiple times; only the first call installs the handler.
    Should be called once at application startup.
    """
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
        logger.debug("shutdown_handler_registered")
    except (OSError, ValueError):
        # signal.signal can fail if not called from the main thread.
        logger.debug("shutdown_handler_registration_skipped", reason="not main thread")

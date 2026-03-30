"""Audit logging for PaperVerifier.

Provides structured audit log functions for security-relevant and
compliance-relevant events.  All audit log entries are emitted via
*structlog* with an ``audit=True`` flag so they can be filtered and
routed independently of application-level logs.

Usage::

    from paperverifier.audit import log_verification_start

    log_verification_start("My Paper Title", "sha256-abc123", user_id="user@example.com")

Audit events are designed to answer: *who* did *what*, *when*, and on
*which resource*.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger("paperverifier.audit")


# ---------------------------------------------------------------------------
# Verification lifecycle
# ---------------------------------------------------------------------------


def log_verification_start(
    document_title: str,
    document_hash: str,
    user_id: str | None = None,
) -> None:
    """Log the start of a document verification run.

    Parameters
    ----------
    document_title:
        Human-readable title of the document being verified.
    document_hash:
        Content hash (SHA-256) of the document for integrity tracking.
    user_id:
        Optional identifier for the user who initiated the verification.
    """
    logger.info(
        "verification_started",
        audit=True,
        document_title=document_title,
        document_hash=document_hash,
        user_id=user_id,
    )


def log_verification_complete(
    report_id: str,
    findings_count: int,
    duration: float,
) -> None:
    """Log the completion of a verification run.

    Parameters
    ----------
    report_id:
        Unique identifier for the generated verification report.
    findings_count:
        Total number of findings produced.
    duration:
        Wall-clock duration of the verification in seconds.
    """
    logger.info(
        "verification_completed",
        audit=True,
        report_id=report_id,
        findings_count=findings_count,
        duration_seconds=duration,
    )


# ---------------------------------------------------------------------------
# Feedback application
# ---------------------------------------------------------------------------


def log_feedback_applied(
    report_id: str,
    items_applied: int,
    items_skipped: int,
) -> None:
    """Log a feedback application event.

    Parameters
    ----------
    report_id:
        Identifier of the verification report whose feedback was applied.
    items_applied:
        Number of feedback items successfully applied.
    items_skipped:
        Number of feedback items skipped (e.g., due to conflicts).
    """
    logger.info(
        "feedback_applied",
        audit=True,
        report_id=report_id,
        items_applied=items_applied,
        items_skipped=items_skipped,
    )


# ---------------------------------------------------------------------------
# API key access
# ---------------------------------------------------------------------------


def log_api_key_access(
    provider: str,
    action: str,
) -> None:
    """Log access to an API key (read, write, or delete from keyring).

    Parameters
    ----------
    provider:
        LLM provider name (e.g., ``"anthropic"``, ``"openai"``).
    action:
        The action performed: ``"read"``, ``"write"``, or ``"delete"``.
    """
    logger.info(
        "api_key_access",
        audit=True,
        provider=provider,
        action=action,
    )

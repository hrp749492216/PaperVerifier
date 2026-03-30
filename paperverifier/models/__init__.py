"""PaperVerifier data models."""

from __future__ import annotations

from paperverifier.models.document import (
    FigureTableRef,
    Paragraph,
    ParsedDocument,
    Reference,
    Section,
    Sentence,
)
from paperverifier.models.findings import (
    ConfidenceLevel,
    Finding,
    FindingCategory,
    Severity,
)
from paperverifier.models.report import (
    AgentReport,
    FeedbackItem,
    VerificationReport,
)

__all__ = [
    # document
    "FigureTableRef",
    "Paragraph",
    "ParsedDocument",
    "Reference",
    "Section",
    "Sentence",
    # findings
    "ConfidenceLevel",
    "Finding",
    "FindingCategory",
    "Severity",
    # report
    "AgentReport",
    "FeedbackItem",
    "VerificationReport",
]

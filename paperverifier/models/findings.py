"""Finding and classification models for verification results."""

from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    """How severe a finding is, from informational to critical."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class FindingCategory(str, Enum):
    """Broad category that a finding belongs to."""

    STRUCTURE = "structure"
    CLAIM = "claim"
    REFERENCE = "reference"
    RESULTS = "results"
    NOVELTY = "novelty"
    LANGUAGE = "language"
    HALLUCINATION = "hallucination"
    CONSISTENCY = "consistency"
    CROSS_REFERENCE = "cross_reference"
    GENERAL = "general"


class ConfidenceLevel(str, Enum):
    """Confidence in a reference verification outcome."""

    VERIFIED = "verified"
    LIKELY_VALID = "likely_valid"
    UNVERIFIED = "unverified"
    SUSPICIOUS = "suspicious"


class Finding(BaseModel):
    """A single verification finding produced by an agent.

    ``segment_id`` links the finding to a stable location in the document tree
    (e.g. ``sec-2.para-3.sent-1``), enabling conflict detection and targeted
    feedback application.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    agent_role: str
    category: FindingCategory
    severity: Severity
    title: str
    description: str
    segment_id: str | None = None
    segment_text: str | None = None
    suggestion: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    related_findings: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)

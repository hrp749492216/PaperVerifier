"""Verification report models that aggregate agent findings."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from paperverifier.models.findings import (
    Finding,
    FindingCategory,
    Severity,
)


class AgentReport(BaseModel):
    """Results produced by a single verification agent."""

    agent_role: str
    status: str = "completed"
    findings: list[Finding] = Field(default_factory=list)
    error_message: str | None = None
    duration_seconds: float = 0.0
    tokens_used: dict[str, int] = Field(default_factory=dict)
    provider: str = ""
    model: str = ""


class FeedbackItem(BaseModel):
    """A user-facing feedback entry derived from a Finding.

    ``number`` is a sequential index so the user can select items by number.
    ``conflicts_with`` lists the numbers of other feedback items that target
    overlapping text regions and therefore cannot both be applied.
    """

    number: int
    finding: Finding
    applicable: bool = True
    conflicts_with: list[int] = Field(default_factory=list)
    applied: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class VerificationReport(BaseModel):
    """Top-level report aggregating all agent results and feedback items."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    document_title: str | None = None
    document_hash: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    agent_reports: list[AgentReport] = Field(default_factory=list)
    consolidated_findings: list[Finding] | None = Field(
        default=None, exclude=True,
    )
    feedback_items: list[FeedbackItem] = Field(default_factory=list)
    overall_score: float | None = None
    summary: str = ""
    total_findings: int = 0
    severity_counts: dict[str, int] = Field(default_factory=dict)
    agents_completed: int = 0
    agents_total: int = 0
    duration_seconds: float = 0.0
    total_tokens: dict[str, int] = Field(default_factory=dict)
    estimated_cost_usd: float | None = None

    # ------------------------------------------------------------------
    # Aggregation helpers
    # ------------------------------------------------------------------

    def _all_findings(self) -> list[Finding]:
        """Return the authoritative set of findings for counting/feedback.

        When consolidated findings are available (set by the orchestrator
        after synthesis), use only those to avoid double-counting the same
        findings from both per-agent reports and the orchestrator summary.
        Individual agent findings remain accessible via ``agent_reports``.
        """
        if self.consolidated_findings is not None:
            return list(self.consolidated_findings)
        return [f for report in self.agent_reports for f in report.findings]

    def compute_severity_counts(self) -> None:
        """Count findings by severity across all agent reports and update fields."""
        counts: dict[str, int] = defaultdict(int)
        findings = self._all_findings()
        for finding in findings:
            counts[finding.severity.value] += 1
        self.severity_counts = dict(counts)
        self.total_findings = len(findings)

    def compute_estimated_cost(self) -> None:
        """Estimate the USD cost based on aggregated token usage.

        Uses approximate per-1K-token pricing:
        - Input tokens:  $0.003 / 1K tokens (roughly GPT-4o-mini rate)
        - Output tokens: $0.015 / 1K tokens

        .. note::
            This is a **rough estimate** only. Actual costs vary significantly
            by provider, model, and pricing tier.  Override or replace this
            method when accurate billing data is available.
        """
        input_tokens = self.total_tokens.get("input_tokens", 0)
        output_tokens = self.total_tokens.get("output_tokens", 0)
        # Rough estimate using mid-range pricing. Actual costs vary by provider/model.
        cost = (input_tokens / 1000.0) * 0.003 + (output_tokens / 1000.0) * 0.015
        self.estimated_cost_usd = round(cost, 6)

    def get_findings_by_severity(self, severity: Severity) -> list[Finding]:
        """Return all findings matching *severity*."""
        return [f for f in self._all_findings() if f.severity == severity]

    def get_findings_by_category(self, category: FindingCategory) -> list[Finding]:
        """Return all findings matching *category*."""
        return [f for f in self._all_findings() if f.category == category]

    # ------------------------------------------------------------------
    # Feedback generation
    # ------------------------------------------------------------------

    def generate_feedback_items(self) -> None:
        """Convert all findings into numbered ``FeedbackItem`` instances.

        After numbering, a simple overlap-detection pass marks items that
        target the same ``segment_id`` as conflicting with each other.
        """
        findings = self._all_findings()
        items: list[FeedbackItem] = []
        for idx, finding in enumerate(findings, start=1):
            items.append(
                FeedbackItem(
                    number=idx,
                    finding=finding,
                    applicable=finding.suggestion is not None,
                )
            )

        # Detect conflicts: items targeting the same segment_id.
        segment_groups: dict[str, list[int]] = defaultdict(list)
        for item in items:
            sid = item.finding.segment_id
            if sid is not None:
                segment_groups[sid].append(item.number)

        for group_numbers in segment_groups.values():
            if len(group_numbers) > 1:
                for num in group_numbers:
                    item = items[num - 1]  # numbers are 1-based
                    item.conflicts_with = [n for n in group_numbers if n != num]

        self.feedback_items = items

    # ------------------------------------------------------------------
    # Serialisation shortcuts
    # ------------------------------------------------------------------

    def to_json(self) -> str:
        """Serialise the report to a pretty-printed JSON string."""
        return self.model_dump_json(indent=2)

    def to_dict(self) -> dict[str, object]:
        """Serialise the report to a plain dictionary."""
        return self.model_dump()

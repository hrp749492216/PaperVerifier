"""Unit tests for paperverifier.models (document, findings, report)."""

from __future__ import annotations

from paperverifier.models import (
    AgentReport,
    Finding,
    FindingCategory,
    ParsedDocument,
    Severity,
    VerificationReport,
)

# ---------------------------------------------------------------------------
# ParsedDocument
# ---------------------------------------------------------------------------


class TestParsedDocument:
    """Tests for ParsedDocument creation and hashing."""

    def test_creation_with_defaults(self) -> None:
        doc = ParsedDocument()
        assert doc.title is None
        assert doc.authors == []
        assert doc.sections == []
        assert doc.full_text == ""
        assert doc.content_hash == ""

    def test_content_hash_computed_on_creation(self) -> None:
        doc = ParsedDocument(full_text="Hello, world!")
        assert doc.content_hash != ""
        assert len(doc.content_hash) == 64  # SHA-256 hex digest

    def test_content_hash_changes_with_different_text(self) -> None:
        doc1 = ParsedDocument(full_text="Text A")
        doc2 = ParsedDocument(full_text="Text B")
        assert doc1.content_hash != doc2.content_hash

    def test_creation_with_metadata(self) -> None:
        doc = ParsedDocument(
            title="My Paper",
            authors=["Author One", "Author Two"],
            abstract="This is the abstract.",
            source_type="pdf",
        )
        assert doc.title == "My Paper"
        assert len(doc.authors) == 2
        assert doc.source_type == "pdf"


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


class TestFinding:
    """Tests for Finding creation."""

    def test_finding_creation(self) -> None:
        finding = Finding(
            agent_role="claim_verification",
            category=FindingCategory.CLAIM,
            severity=Severity.MAJOR,
            title="Unsupported claim",
            description="Claim in sec-2 has no supporting reference.",
            segment_id="sec-2.para-1.sent-3",
            suggestion="Add a citation to support this claim.",
        )
        assert finding.agent_role == "claim_verification"
        assert finding.severity == Severity.MAJOR
        assert finding.category == FindingCategory.CLAIM
        assert finding.segment_id == "sec-2.para-1.sent-3"
        assert finding.suggestion is not None
        assert finding.confidence == 1.0

    def test_finding_default_id_is_generated(self) -> None:
        f1 = Finding(
            agent_role="test",
            category=FindingCategory.GENERAL,
            severity=Severity.INFO,
            title="Test",
            description="Test description",
        )
        f2 = Finding(
            agent_role="test",
            category=FindingCategory.GENERAL,
            severity=Severity.INFO,
            title="Test",
            description="Test description",
        )
        assert f1.id != f2.id  # UUIDs should differ

    def test_finding_optional_fields_default_to_none(self) -> None:
        finding = Finding(
            agent_role="test",
            category=FindingCategory.GENERAL,
            severity=Severity.INFO,
            title="Test",
            description="Test description",
        )
        assert finding.segment_id is None
        assert finding.segment_text is None
        assert finding.suggestion is None


# ---------------------------------------------------------------------------
# VerificationReport.compute_severity_counts
# ---------------------------------------------------------------------------


class TestVerificationReportSeverityCounts:
    """Tests for VerificationReport.compute_severity_counts()."""

    def _make_finding(self, severity: Severity) -> Finding:
        return Finding(
            agent_role="test",
            category=FindingCategory.GENERAL,
            severity=severity,
            title=f"{severity.value} finding",
            description="Test",
        )

    def test_compute_severity_counts(self) -> None:
        report = VerificationReport(
            agent_reports=[
                AgentReport(
                    agent_role="agent_a",
                    findings=[
                        self._make_finding(Severity.CRITICAL),
                        self._make_finding(Severity.MAJOR),
                        self._make_finding(Severity.MAJOR),
                    ],
                ),
                AgentReport(
                    agent_role="agent_b",
                    findings=[
                        self._make_finding(Severity.MINOR),
                        self._make_finding(Severity.INFO),
                    ],
                ),
            ]
        )
        report.compute_severity_counts()

        assert report.total_findings == 5
        assert report.severity_counts["critical"] == 1
        assert report.severity_counts["major"] == 2
        assert report.severity_counts["minor"] == 1
        assert report.severity_counts["info"] == 1

    def test_compute_severity_counts_empty(self) -> None:
        report = VerificationReport()
        report.compute_severity_counts()
        assert report.total_findings == 0
        assert report.severity_counts == {}


# ---------------------------------------------------------------------------
# VerificationReport.generate_feedback_items
# ---------------------------------------------------------------------------


class TestVerificationReportFeedback:
    """Tests for VerificationReport.generate_feedback_items()."""

    def test_generate_feedback_items(self) -> None:
        findings = [
            Finding(
                agent_role="agent_a",
                category=FindingCategory.CLAIM,
                severity=Severity.MAJOR,
                title="Issue 1",
                description="Description 1",
                suggestion="Fix 1",
            ),
            Finding(
                agent_role="agent_b",
                category=FindingCategory.LANGUAGE,
                severity=Severity.MINOR,
                title="Issue 2",
                description="Description 2",
            ),
        ]
        report = VerificationReport(
            agent_reports=[
                AgentReport(agent_role="agent_a", findings=[findings[0]]),
                AgentReport(agent_role="agent_b", findings=[findings[1]]),
            ]
        )
        report.generate_feedback_items()

        assert len(report.feedback_items) == 2
        assert report.feedback_items[0].number == 1
        assert report.feedback_items[1].number == 2
        # First finding has a suggestion so it should be applicable
        assert report.feedback_items[0].applicable is True
        # Second finding has no suggestion so it should not be applicable
        assert report.feedback_items[1].applicable is False

    def test_conflict_detection(self) -> None:
        """Findings targeting the same segment_id should be marked as conflicting."""
        shared_segment = "sec-1.para-2.sent-1"
        findings = [
            Finding(
                agent_role="agent_a",
                category=FindingCategory.CLAIM,
                severity=Severity.MAJOR,
                title="Issue A",
                description="Desc A",
                segment_id=shared_segment,
                suggestion="Fix A",
            ),
            Finding(
                agent_role="agent_b",
                category=FindingCategory.LANGUAGE,
                severity=Severity.MINOR,
                title="Issue B",
                description="Desc B",
                segment_id=shared_segment,
                suggestion="Fix B",
            ),
            Finding(
                agent_role="agent_c",
                category=FindingCategory.STRUCTURE,
                severity=Severity.INFO,
                title="Issue C",
                description="Desc C",
                segment_id="sec-3.para-1.sent-1",  # different segment
                suggestion="Fix C",
            ),
        ]
        report = VerificationReport(
            agent_reports=[
                AgentReport(agent_role="mixed", findings=findings),
            ]
        )
        report.generate_feedback_items()

        item_a = report.feedback_items[0]
        item_b = report.feedback_items[1]
        item_c = report.feedback_items[2]

        # A and B conflict with each other
        assert item_b.number in item_a.conflicts_with
        assert item_a.number in item_b.conflicts_with
        # C has no conflicts
        assert item_c.conflicts_with == []

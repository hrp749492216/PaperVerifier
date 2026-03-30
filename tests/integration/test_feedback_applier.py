"""Integration tests for FeedbackApplier offset logic and conflict detection.

Verifies that text replacements are applied correctly, position mismatches
are handled, ambiguous matches are rejected, and conflict detection works.
"""

from __future__ import annotations

import pytest

from paperverifier.feedback.applier import (
    AppliedFeedback,
    FeedbackApplier,
    FeedbackChange,
    FeedbackConflictError,
)
from paperverifier.models.document import (
    ParsedDocument,
    Paragraph,
    Section,
    Sentence,
)
from paperverifier.models.findings import Finding, FindingCategory, Severity
from paperverifier.models.report import (
    AgentReport,
    FeedbackItem,
    VerificationReport,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_document(full_text: str) -> ParsedDocument:
    """Build a minimal ParsedDocument with the given full_text."""
    sent = Sentence(
        id="sec-1.para-1.sent-1",
        text=full_text,
        start_char=0,
        end_char=len(full_text),
    )
    para = Paragraph(
        id="sec-1.para-1",
        sentences=[sent],
        raw_text=full_text,
        start_char=0,
        end_char=len(full_text),
    )
    section = Section(
        id="sec-1",
        title="Body",
        level=1,
        paragraphs=[para],
        start_char=0,
        end_char=len(full_text),
    )
    return ParsedDocument(
        title="Test",
        full_text=full_text,
        sections=[section],
        references=[],
    )


def _make_finding(
    segment_id: str,
    segment_text: str,
    suggestion: str,
    start_char: int = 0,
    end_char: int = 0,
) -> Finding:
    return Finding(
        agent_role="test",
        category=FindingCategory.LANGUAGE,
        severity=Severity.MINOR,
        title="Test finding",
        description="Test",
        segment_id=segment_id,
        segment_text=segment_text,
        suggestion=suggestion,
        metadata={"start_char": start_char, "end_char": end_char},
    )


def _make_report(items: list[FeedbackItem]) -> VerificationReport:
    report = VerificationReport(
        document_title="Test",
        agent_reports=[],
    )
    report.feedback_items = items
    return report


# ---------------------------------------------------------------------------
# Tests: _apply_change (text replacement engine)
# ---------------------------------------------------------------------------


class TestApplyChange:
    """Test the core _apply_change logic."""

    def test_exact_position_match(self):
        applier = FeedbackApplier()
        text = "The quick brown fox jumps over the lazy dog."
        change = FeedbackChange(
            item_number=1,
            original_text="brown",
            replacement_text="red",
            start_char=10,
            end_char=15,
        )
        result = applier._apply_change(text, change)
        assert result == "The quick red fox jumps over the lazy dog."

    def test_position_mismatch_falls_back_to_window_search(self):
        applier = FeedbackApplier()
        text = "The quick brown fox jumps over the lazy dog."
        change = FeedbackChange(
            item_number=1,
            original_text="brown",
            replacement_text="red",
            start_char=5,  # Wrong position
            end_char=10,
        )
        result = applier._apply_change(text, change)
        assert result == "The quick red fox jumps over the lazy dog."

    def test_ambiguous_match_in_window_returns_none(self):
        """When the same text appears multiple times nearby, reject it."""
        applier = FeedbackApplier()
        text = "the model and the model are both significant."
        change = FeedbackChange(
            item_number=1,
            original_text="the model",
            replacement_text="our model",
            start_char=999,  # Wrong position forces fallback
            end_char=1008,
        )
        result = applier._apply_change(text, change)
        assert result is None

    def test_text_not_found_returns_none(self):
        applier = FeedbackApplier()
        text = "Hello world."
        change = FeedbackChange(
            item_number=1,
            original_text="nonexistent",
            replacement_text="replacement",
            start_char=0,
            end_char=11,
        )
        result = applier._apply_change(text, change)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: conflict detection
# ---------------------------------------------------------------------------


class TestConflictDetection:
    """Test the detect_conflicts logic."""

    def test_no_conflicts(self):
        applier = FeedbackApplier()
        items = [
            FeedbackItem(
                number=1,
                finding=_make_finding("sec-1.para-1.sent-1", "text a", "fix a"),
            ),
            FeedbackItem(
                number=2,
                finding=_make_finding("sec-1.para-2.sent-1", "text b", "fix b"),
            ),
        ]
        conflicts = applier.detect_conflicts(items)
        assert conflicts == []

    def test_same_segment_conflict(self):
        applier = FeedbackApplier()
        items = [
            FeedbackItem(
                number=1,
                finding=_make_finding("sec-1.para-1.sent-1", "text a", "fix a"),
            ),
            FeedbackItem(
                number=2,
                finding=_make_finding("sec-1.para-1.sent-1", "text a", "fix b"),
            ),
        ]
        conflicts = applier.detect_conflicts(items)
        assert len(conflicts) == 1
        assert (1, 2) in conflicts

    def test_overlapping_char_ranges_conflict(self):
        applier = FeedbackApplier()
        items = [
            FeedbackItem(
                number=1,
                finding=_make_finding(
                    "seg-a", "overlap", "fix",
                    start_char=10, end_char=30,
                ),
            ),
            FeedbackItem(
                number=2,
                finding=_make_finding(
                    "seg-b", "overlap", "fix",
                    start_char=20, end_char=40,
                ),
            ),
        ]
        conflicts = applier.detect_conflicts(items)
        assert len(conflicts) == 1


# ---------------------------------------------------------------------------
# Tests: full apply workflow
# ---------------------------------------------------------------------------


class TestApplyWorkflow:
    """Test the full apply() pipeline."""

    @pytest.mark.asyncio
    async def test_apply_single_item(self):
        text = "The quick brown fox jumps over the lazy dog."
        doc = _make_document(text)
        finding = _make_finding(
            "sec-1.para-1.sent-1",
            text,
            "The quick red fox jumps over the lazy dog.",
        )
        item = FeedbackItem(number=1, finding=finding)
        report = _make_report([item])

        applier = FeedbackApplier()
        result = await applier.apply(doc, report, [1])

        assert result.original_text == text
        assert "red" in result.modified_text
        assert 1 in result.applied_items

    @pytest.mark.asyncio
    async def test_apply_nonexistent_item_skipped(self):
        text = "Hello world."
        doc = _make_document(text)
        report = _make_report([])

        applier = FeedbackApplier()
        result = await applier.apply(doc, report, [99])

        assert 99 in result.skipped_items
        assert result.modified_text == text

    @pytest.mark.asyncio
    async def test_apply_conflict_raises(self):
        text = "The quick brown fox."
        doc = _make_document(text)
        finding1 = _make_finding("sec-1.para-1.sent-1", text, "fix1")
        finding2 = _make_finding("sec-1.para-1.sent-1", text, "fix2")
        item1 = FeedbackItem(number=1, finding=finding1)
        item2 = FeedbackItem(number=2, finding=finding2)
        report = _make_report([item1, item2])

        applier = FeedbackApplier()
        with pytest.raises(FeedbackConflictError) as exc_info:
            await applier.apply(doc, report, [1, 2])
        assert len(exc_info.value.conflicts) == 1

    @pytest.mark.asyncio
    async def test_apply_conflict_force_mode(self):
        """In force mode, conflicts are skipped instead of raising."""
        text = "The quick brown fox."
        doc = _make_document(text)
        finding1 = _make_finding("sec-1.para-1.sent-1", text, "fix1")
        finding2 = _make_finding("sec-1.para-1.sent-1", text, "fix2")
        item1 = FeedbackItem(number=1, finding=finding1)
        item2 = FeedbackItem(number=2, finding=finding2)
        report = _make_report([item1, item2])

        applier = FeedbackApplier()
        result = await applier.apply(doc, report, [1, 2], force=True)

        # One should be applied, the other skipped
        assert len(result.applied_items) + len(result.skipped_items) == 2

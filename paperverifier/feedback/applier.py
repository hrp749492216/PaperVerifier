"""Feedback applier with conflict detection and post-application validation.

Applies selected :class:`FeedbackItem` entries to a :class:`ParsedDocument`,
optionally using a :class:`WriterAgent` to generate LLM-powered fixes.  When
no writer is available, the raw ``Finding.suggestion`` text is used as the
replacement.

Text replacements are applied in reverse character-offset order (bottom-up) so
that earlier offsets remain valid after later regions are modified.
"""

from __future__ import annotations

from collections import defaultdict

import structlog
from pydantic import BaseModel, Field

from paperverifier.agents.writer import WriterAgent
from paperverifier.llm.client import UnifiedLLMClient
from paperverifier.llm.roles import RoleAssignment
from paperverifier.models.document import ParsedDocument, Sentence, Paragraph, Section
from paperverifier.models.findings import Finding
from paperverifier.models.report import FeedbackItem, VerificationReport

logger = structlog.get_logger(__name__)


# ------------------------------------------------------------------
# Exceptions
# ------------------------------------------------------------------


class FeedbackConflictError(Exception):
    """Raised when conflicting feedback items are selected.

    Attributes
    ----------
    conflicts:
        Pairs of conflicting item numbers, e.g. ``[(1, 3), (2, 5)]``.
    """

    def __init__(self, conflicts: list[tuple[int, int]]) -> None:
        self.conflicts = conflicts
        conflict_strs = [f"#{a} <-> #{b}" for a, b in conflicts]
        super().__init__(
            f"Conflicting feedback items detected: {', '.join(conflict_strs)}"
        )


# ------------------------------------------------------------------
# Data models
# ------------------------------------------------------------------


class FeedbackChange(BaseModel):
    """Record of a single text replacement applied to the document."""

    item_number: int
    segment_id: str | None = None
    original_text: str
    replacement_text: str
    start_char: int
    end_char: int


class AppliedFeedback(BaseModel):
    """Result of applying feedback to a document."""

    original_text: str
    modified_text: str
    applied_items: list[int] = Field(default_factory=list)
    skipped_items: list[int] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    changes: list[FeedbackChange] = Field(default_factory=list)


# ------------------------------------------------------------------
# Applier
# ------------------------------------------------------------------


class FeedbackApplier:
    """Applies selected feedback items to a parsed document.

    Can optionally use a :class:`WriterAgent` to produce LLM-generated
    rewrites.  When no writer is provided, the ``Finding.suggestion``
    field is used directly as the replacement text.

    Parameters
    ----------
    writer_agent:
        An already-initialised writer agent.  If supplied, *client* and
        *writer_assignment* are ignored.
    client:
        A :class:`UnifiedLLMClient` used to construct a :class:`WriterAgent`
        on the fly when *writer_agent* is ``None``.
    writer_assignment:
        The :class:`RoleAssignment` for the writer role, required when
        constructing a writer from *client*.
    """

    def __init__(
        self,
        writer_agent: WriterAgent | None = None,
        client: UnifiedLLMClient | None = None,
        writer_assignment: RoleAssignment | None = None,
    ) -> None:
        if writer_agent is not None:
            self._writer: WriterAgent | None = writer_agent
        elif client is not None and writer_assignment is not None:
            self._writer = WriterAgent(client=client, assignment=writer_assignment)
        else:
            self._writer = None

    # ------------------------------------------------------------------
    # Conflict detection
    # ------------------------------------------------------------------

    def detect_conflicts(
        self, items: list[FeedbackItem]
    ) -> list[tuple[int, int]]:
        """Detect conflicting feedback items.

        Two items conflict when:
        * they share the same non-``None`` ``segment_id``, **or**
        * the character ranges implied by their segment locations overlap.

        Returns
        -------
        list[tuple[int, int]]
            Pairs of conflicting item numbers (always ``(smaller, larger)``).
        """
        conflicts: list[tuple[int, int]] = []
        seen_pairs: set[tuple[int, int]] = set()

        # Group by segment_id for quick same-segment detection.
        segment_groups: dict[str, list[int]] = defaultdict(list)
        for item in items:
            sid = item.finding.segment_id
            if sid is not None:
                segment_groups[sid].append(item.number)

        for group in segment_groups.values():
            if len(group) > 1:
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        pair = (min(group[i], group[j]), max(group[i], group[j]))
                        if pair not in seen_pairs:
                            seen_pairs.add(pair)
                            conflicts.append(pair)

        # Check for overlapping character ranges across different segments.
        items_with_range: list[tuple[int, int, int]] = []
        for item in items:
            seg_text = item.finding.segment_text
            if seg_text is not None:
                start = item.finding.metadata.get("start_char")
                end = item.finding.metadata.get("end_char")
                if isinstance(start, int) and isinstance(end, int):
                    items_with_range.append((item.number, start, end))

        for i in range(len(items_with_range)):
            num_a, start_a, end_a = items_with_range[i]
            for j in range(i + 1, len(items_with_range)):
                num_b, start_b, end_b = items_with_range[j]
                pair = (min(num_a, num_b), max(num_a, num_b))
                if pair in seen_pairs:
                    continue
                # Ranges overlap when one starts before the other ends.
                if start_a < end_b and start_b < end_a:
                    seen_pairs.add(pair)
                    conflicts.append(pair)

        return conflicts

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------

    async def apply(
        self,
        document: ParsedDocument,
        report: VerificationReport,
        selected_items: list[int],
        *,
        force: bool = False,
    ) -> AppliedFeedback:
        """Apply selected feedback items to the document.

        Workflow
        --------
        1. Validate that every number in *selected_items* corresponds to an
           existing :class:`FeedbackItem` in *report*.
        2. Check for conflicts among the selected items.  If any are found
           and *force* is ``False``, raise :class:`FeedbackConflictError`.
        3. Sort the items bottom-up (highest character position first) so
           that applying one replacement does not shift the offsets needed
           by subsequent replacements.
        4. For each item, generate or retrieve the replacement text and
           apply it to the document's ``full_text``.
        5. Return an :class:`AppliedFeedback` with the original and
           modified text together with detailed change records.

        Parameters
        ----------
        document:
            The parsed document whose ``full_text`` will be modified.
        report:
            The verification report containing the feedback items.
        selected_items:
            1-based item numbers to apply.
        force:
            If ``True``, conflicts are logged but do not raise an error.
            Conflicting items are skipped instead.

        Raises
        ------
        FeedbackConflictError
            If conflicts are detected and *force* is ``False``.
        """
        result = AppliedFeedback(
            original_text=document.full_text,
            modified_text=document.full_text,
        )

        # ---- 1. Validate selected items exist ----
        item_map: dict[int, FeedbackItem] = {
            item.number: item for item in report.feedback_items
        }
        valid_items: list[FeedbackItem] = []
        for num in selected_items:
            if num not in item_map:
                result.errors.append(f"Feedback item #{num} does not exist.")
                result.skipped_items.append(num)
            elif not item_map[num].applicable:
                result.errors.append(
                    f"Feedback item #{num} is not applicable (no suggestion)."
                )
                result.skipped_items.append(num)
            else:
                valid_items.append(item_map[num])

        if not valid_items:
            return result

        # ---- 2. Check for conflicts ----
        conflicts = self.detect_conflicts(valid_items)
        if conflicts:
            if not force:
                raise FeedbackConflictError(conflicts)

            # In force mode, keep only the first item from each conflict pair
            # and skip the rest.
            to_skip: set[int] = set()
            for num_a, num_b in conflicts:
                # Skip the later item (higher number).
                to_skip.add(num_b)
                logger.warning(
                    "feedback_conflict_force_skip",
                    skipped=num_b,
                    conflicts_with=num_a,
                )
            valid_items = [it for it in valid_items if it.number not in to_skip]
            for num in to_skip:
                result.skipped_items.append(num)
                result.errors.append(
                    f"Feedback item #{num} skipped due to conflict (force mode)."
                )

        # ---- 3. Sort bottom-up ----
        sorted_items = self._sort_items_for_application(valid_items, document)

        # ---- 4. Apply each item ----
        current_text = document.full_text

        for item in sorted_items:
            try:
                change = await self._prepare_change(item, document, current_text)
                if change is None:
                    result.skipped_items.append(item.number)
                    result.errors.append(
                        f"Feedback item #{item.number}: could not locate text in document."
                    )
                    continue

                new_text = self._apply_change(current_text, change)
                if new_text is None:
                    result.skipped_items.append(item.number)
                    result.errors.append(
                        f"Feedback item #{item.number}: original text mismatch at "
                        f"char [{change.start_char}:{change.end_char}]."
                    )
                    continue

                current_text = new_text
                result.changes.append(change)
                result.applied_items.append(item.number)
                item.applied = True

                logger.info(
                    "feedback_item_applied",
                    item_number=item.number,
                    segment_id=item.finding.segment_id,
                    chars_changed=len(change.replacement_text) - len(change.original_text),
                )

            except Exception as exc:
                result.skipped_items.append(item.number)
                result.errors.append(
                    f"Feedback item #{item.number}: unexpected error: {exc}"
                )
                logger.error(
                    "feedback_item_error",
                    item_number=item.number,
                    error=str(exc),
                )

        result.modified_text = current_text
        return result

    # ------------------------------------------------------------------
    # Sorting
    # ------------------------------------------------------------------

    def _sort_items_for_application(
        self,
        items: list[FeedbackItem],
        document: ParsedDocument,
    ) -> list[FeedbackItem]:
        """Sort feedback items bottom-up (highest character position first).

        This ensures that applying a replacement to a later part of the
        document does not shift offsets for earlier parts that have not yet
        been processed.

        Items whose position cannot be determined are placed at the end
        (applied last, i.e. they have the lowest effective position).
        """

        def _sort_key(item: FeedbackItem) -> int:
            # Try to get the start_char from the segment in the document.
            if item.finding.segment_id:
                segment = document.get_segment(item.finding.segment_id)
                if segment is not None and hasattr(segment, "start_char"):
                    return segment.start_char  # type: ignore[union-attr]

            # Fall back to metadata if available.
            start = item.finding.metadata.get("start_char")
            if isinstance(start, int):
                return start

            # Try to locate the segment text in the document.
            if item.finding.segment_text:
                idx = document.full_text.find(item.finding.segment_text)
                if idx != -1:
                    return idx

            # Unknown position: sort to the front so it is applied last
            # after the list is reversed.
            return -1

        return sorted(items, key=_sort_key, reverse=True)

    # ------------------------------------------------------------------
    # Change preparation
    # ------------------------------------------------------------------

    async def _prepare_change(
        self,
        item: FeedbackItem,
        document: ParsedDocument,
        current_text: str,
    ) -> FeedbackChange | None:
        """Build a :class:`FeedbackChange` for a single feedback item.

        Locates the target text in the document, generates the replacement
        (via WriterAgent or the finding's suggestion), and returns a change
        record.  Returns ``None`` if the target text cannot be located.
        """
        finding = item.finding

        # Resolve the original text and character range.
        start_char, end_char, original_text = self._resolve_location(
            finding, document, current_text
        )
        if original_text is None:
            return None

        # Generate replacement text.
        replacement_text = await self._generate_replacement(finding, document)

        return FeedbackChange(
            item_number=item.number,
            segment_id=finding.segment_id,
            original_text=original_text,
            replacement_text=replacement_text,
            start_char=start_char,
            end_char=end_char,
        )

    def _resolve_location(
        self,
        finding: Finding,
        document: ParsedDocument,
        current_text: str,
    ) -> tuple[int, int, str | None]:
        """Determine the character range and original text for a finding.

        Returns ``(start_char, end_char, original_text)`` or
        ``(-1, -1, None)`` when the location cannot be resolved.
        """
        # Strategy 1: look up the segment by ID in the document tree.
        if finding.segment_id:
            segment = document.get_segment(finding.segment_id)
            if segment is not None and hasattr(segment, "start_char"):
                start: int = segment.start_char  # type: ignore[union-attr]
                end: int = segment.end_char  # type: ignore[union-attr]
                # Use the text attribute for Sentence/Paragraph segments.
                if isinstance(segment, Sentence):
                    original = segment.text
                elif isinstance(segment, Paragraph):
                    original = segment.raw_text
                elif isinstance(segment, Section):
                    original = current_text[start:end]
                else:
                    original = current_text[start:end]
                return start, end, original

        # Strategy 2: use segment_text to search within full_text.
        if finding.segment_text:
            idx = current_text.find(finding.segment_text)
            if idx != -1:
                return idx, idx + len(finding.segment_text), finding.segment_text

        # Strategy 3: use metadata start/end if available.
        start_meta = finding.metadata.get("start_char")
        end_meta = finding.metadata.get("end_char")
        if isinstance(start_meta, int) and isinstance(end_meta, int):
            return start_meta, end_meta, current_text[start_meta:end_meta]

        return -1, -1, None

    async def _generate_replacement(
        self,
        finding: Finding,
        document: ParsedDocument,
    ) -> str:
        """Generate the replacement text for a finding.

        Uses the :class:`WriterAgent` when available; otherwise falls back
        to the ``Finding.suggestion`` field verbatim.
        """
        if self._writer is not None:
            try:
                fix = await self._writer.generate_fix(finding, document)
                return fix
            except Exception as exc:
                logger.warning(
                    "writer_agent_failed_fallback_to_suggestion",
                    finding_id=finding.id,
                    error=str(exc),
                )

        # Fallback: use the suggestion directly.
        if finding.suggestion:
            return finding.suggestion

        # Last resort: return the original description as a comment.
        return f"[TODO: {finding.description}]"

    # ------------------------------------------------------------------
    # Text replacement
    # ------------------------------------------------------------------

    def _apply_change(self, text: str, change: FeedbackChange) -> str | None:
        """Apply a single text change to *text*.

        Verifies that the text at ``[start_char:end_char]`` matches the
        expected ``original_text`` before performing the replacement.
        Returns ``None`` if the pre-condition check fails.
        """
        actual = text[change.start_char : change.end_char]
        if actual != change.original_text:
            logger.error(
                "feedback_text_mismatch",
                item_number=change.item_number,
                expected=change.original_text[:80],
                actual=actual[:80],
            )
            return None

        return text[: change.start_char] + change.replacement_text + text[change.end_char :]

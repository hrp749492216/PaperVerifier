"""Diff generation utilities for reviewing applied feedback changes.

Provides unified diffs for CLI output, HTML diffs for Streamlit, side-by-side
comparisons, and human-readable change summaries.
"""

from __future__ import annotations

import difflib
import html

from paperverifier.feedback.applier import AppliedFeedback


class DiffGenerator:
    """Generate various diff formats from original and modified text."""

    @staticmethod
    def unified_diff(
        original: str,
        modified: str,
        filename: str = "paper",
        context_lines: int = 3,
    ) -> str:
        """Generate a unified diff string suitable for CLI display.

        Parameters
        ----------
        original:
            The original document text.
        modified:
            The modified document text after feedback application.
        filename:
            Base filename used in the diff header (``a/`` and ``b/`` prefixes
            are added automatically).
        context_lines:
            Number of unchanged context lines around each hunk.

        Returns
        -------
        str
            A unified diff string.  Returns an empty string when the texts
            are identical.
        """
        original_lines = original.splitlines(keepends=True)
        modified_lines = modified.splitlines(keepends=True)

        diff = difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            n=context_lines,
        )
        return "".join(diff)

    @staticmethod
    def html_diff(
        original: str,
        modified: str,
        context_lines: int = 3,
    ) -> str:
        """Generate an HTML diff table for Streamlit or web display.

        Uses :class:`difflib.HtmlDiff` with custom inline CSS for clean
        rendering inside a Streamlit ``st.html()`` or ``st.markdown()``
        call with ``unsafe_allow_html=True``.

        Parameters
        ----------
        original:
            The original document text.
        modified:
            The modified document text.
        context_lines:
            Number of unchanged context lines around each change.

        Returns
        -------
        str
            A self-contained HTML string with embedded styles.
        """
        original_lines = [html.escape(line) for line in original.splitlines()]
        modified_lines = [html.escape(line) for line in modified.splitlines()]

        differ = difflib.HtmlDiff(wrapcolumn=80)
        table = differ.make_table(
            original_lines,
            modified_lines,
            fromdesc="Original",
            todesc="Modified",
            context=True,
            numlines=context_lines,
        )

        # Wrap in styled container for consistent rendering.
        styled_output = (
            "<style>\n"
            "  .diff-container { font-family: monospace; font-size: 13px; }\n"
            "  .diff-container table { border-collapse: collapse; width: 100%; }\n"
            "  .diff-container td, .diff-container th {\n"
            "    padding: 2px 6px; border: 1px solid #ddd; vertical-align: top;\n"
            "  }\n"
            "  .diff-container .diff_add { background-color: #d4edda; }\n"
            "  .diff-container .diff_chg { background-color: #fff3cd; }\n"
            "  .diff-container .diff_sub { background-color: #f8d7da; }\n"
            "  .diff-container th { background-color: #f1f1f1; font-weight: bold; }\n"
            "</style>\n"
            '<div class="diff-container">\n'
            f"{table}\n"
            "</div>\n"
        )
        return styled_output

    @staticmethod
    def side_by_side(
        original: str,
        modified: str,
        width: int = 80,
    ) -> str:
        """Generate a side-by-side comparison string for terminal display.

        Each side occupies *width* columns.  Changed lines are marked with
        ``|``, additions with ``>``, and deletions with ``<`` in the gutter.

        Parameters
        ----------
        original:
            The original document text.
        modified:
            The modified document text.
        width:
            Column width for each side (total output width is approximately
            ``2 * width + 5`` to account for the gutter).

        Returns
        -------
        str
            A side-by-side comparison string.
        """
        original_lines = original.splitlines()
        modified_lines = modified.splitlines()

        col_width = max(width, 20)
        header_left = "Original".center(col_width)
        header_right = "Modified".center(col_width)
        separator = "-" * col_width

        output_lines: list[str] = [
            f"{header_left} | {header_right}",
            f"{separator}-+-{separator}",
        ]

        sm = difflib.SequenceMatcher(None, original_lines, modified_lines)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for idx in range(i1, i2):
                    left = _truncate_or_pad(original_lines[idx], col_width)
                    right = _truncate_or_pad(modified_lines[j1 + (idx - i1)], col_width)
                    output_lines.append(f"{left} | {right}")
            elif tag == "replace":
                max_lines = max(i2 - i1, j2 - j1)
                for k in range(max_lines):
                    left_line = original_lines[i1 + k] if (i1 + k) < i2 else ""
                    right_line = modified_lines[j1 + k] if (j1 + k) < j2 else ""
                    left = _truncate_or_pad(left_line, col_width)
                    right = _truncate_or_pad(right_line, col_width)
                    output_lines.append(f"{left} ! {right}")
            elif tag == "delete":
                for idx in range(i1, i2):
                    left = _truncate_or_pad(original_lines[idx], col_width)
                    right = " " * col_width
                    output_lines.append(f"{left} < {right}")
            elif tag == "insert":
                for idx in range(j1, j2):
                    left = " " * col_width
                    right = _truncate_or_pad(modified_lines[idx], col_width)
                    output_lines.append(f"{left} > {right}")

        return "\n".join(output_lines)

    @staticmethod
    def summary(feedback: AppliedFeedback) -> str:
        """Generate a human-readable summary of changes made.

        Parameters
        ----------
        feedback:
            The :class:`AppliedFeedback` result from a feedback application.

        Returns
        -------
        str
            A formatted multi-line summary string.
        """
        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("FEEDBACK APPLICATION SUMMARY")
        lines.append("=" * 60)
        lines.append("")

        total_selected = len(feedback.applied_items) + len(feedback.skipped_items)
        lines.append(f"Items selected:  {total_selected}")
        lines.append(f"Items applied:   {len(feedback.applied_items)}")
        lines.append(f"Items skipped:   {len(feedback.skipped_items)}")
        lines.append("")

        if feedback.applied_items:
            lines.append("Applied changes:")
            lines.append("-" * 40)
            for change in feedback.changes:
                segment_label = change.segment_id or "unknown location"
                orig_preview = _preview(change.original_text, max_len=50)
                repl_preview = _preview(change.replacement_text, max_len=50)
                char_delta = len(change.replacement_text) - len(change.original_text)
                sign = "+" if char_delta >= 0 else ""
                lines.append(f"  #{change.item_number} [{segment_label}]")
                lines.append(f"    - {orig_preview}")
                lines.append(f"    + {repl_preview}")
                lines.append(f"    ({sign}{char_delta} chars)")
                lines.append("")

        if feedback.skipped_items:
            lines.append("Skipped items:")
            lines.append("-" * 40)
            for num in feedback.skipped_items:
                lines.append(f"  #{num}")
            lines.append("")

        if feedback.errors:
            lines.append("Errors:")
            lines.append("-" * 40)
            for error in feedback.errors:
                lines.append(f"  - {error}")
            lines.append("")

        # Text length comparison.
        orig_len = len(feedback.original_text)
        mod_len = len(feedback.modified_text)
        delta = mod_len - orig_len
        sign = "+" if delta >= 0 else ""
        lines.append(f"Original length: {orig_len:,} chars")
        lines.append(f"Modified length: {mod_len:,} chars")
        lines.append(f"Net change:      {sign}{delta:,} chars")
        lines.append("=" * 60)

        return "\n".join(lines)


# ------------------------------------------------------------------
# Private helpers
# ------------------------------------------------------------------


def _truncate_or_pad(text: str, width: int) -> str:
    """Truncate *text* to *width* or pad with spaces to fill *width*."""
    if len(text) > width:
        return text[: width - 1] + "\u2026"
    return text.ljust(width)


def _preview(text: str, max_len: int = 50) -> str:
    """Return a single-line preview of *text*, truncated with ellipsis."""
    single = text.replace("\n", " ").strip()
    if len(single) > max_len:
        return single[: max_len - 3] + "..."
    return single

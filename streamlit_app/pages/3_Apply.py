"""Apply Feedback page -- apply selected fixes and review diffs.

Uses :class:`FeedbackApplier` to apply selected feedback items and
:class:`DiffGenerator` to produce visual diffs for review.
"""

from __future__ import annotations

import streamlit as st

from streamlit_app.auth import require_auth
from streamlit_app.utils import run_async  # noqa: F401 – shared async helper

require_auth()

# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------

st.header("Apply Feedback")

# ---------------------------------------------------------------------------
# Session-state defaults
# ---------------------------------------------------------------------------

for key, default in [
    ("selected_items", []),
    ("applied_feedback", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ---------------------------------------------------------------------------
# Guard: require selections
# ---------------------------------------------------------------------------

selected_items: list[int] = st.session_state.get("selected_items", [])
report = st.session_state.get("verification_report")
doc = st.session_state.get("parsed_document")

if not selected_items or report is None or doc is None:
    st.warning(
        "No findings selected for application. "
        "Please go to the Review page and select items first."
    )
    st.page_link(
        "streamlit_app/pages/2_Review.py",
        label="Go to Review Findings",
        icon="\u27a1\ufe0f",
    )
    st.stop()

# ---------------------------------------------------------------------------
# Selected items summary
# ---------------------------------------------------------------------------

st.subheader("Selected Items")

item_map = {item.number: item for item in report.feedback_items}
valid_selected = [n for n in selected_items if n in item_map]

if not valid_selected:
    st.error("None of the selected item numbers match the report. Please re-select.")
    st.stop()

st.markdown(f"**{len(valid_selected)} item(s)** selected for application:")

# Compact summary table
for num in valid_selected:
    item = item_map[num]
    f = item.finding
    sev_icon = {
        "critical": "\U0001f534",
        "major": "\U0001f7e0",
        "minor": "\U0001f535",
        "info": "\u2139\ufe0f",
    }.get(f.severity.value, "")
    st.markdown(
        f"- {sev_icon} **#{num}** | {f.severity.value.upper()} | "
        f"{f.category.value} | {f.title}"
    )

# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Conflict Check")

# Initialize conflicts before the try block to prevent NameError (CRIT-9).
conflicts: list[tuple[int, int]] = []
force_mode = False

try:
    from paperverifier.feedback.applier import FeedbackApplier, FeedbackConflictError

    applier = FeedbackApplier()
    selected_feedback_items = [item_map[n] for n in valid_selected]
    conflicts = applier.detect_conflicts(selected_feedback_items)

    if conflicts:
        st.warning(
            f"**{len(conflicts)} conflict(s) detected** among selected items. "
            "Conflicting items target overlapping text regions and cannot both be applied."
        )
        for a, b in conflicts:
            st.markdown(f"- Item **#{a}** conflicts with item **#{b}**")

        force_mode = st.checkbox(
            "Force apply (skip conflicting items automatically)",
            value=True,
            key="force_apply",
            help="When enabled, the later item in each conflict pair is skipped.",
        )
    else:
        st.success("No conflicts detected among selected items.")

except Exception as exc:
    import logging
    import uuid as _uuid_mod
    _err_id = _uuid_mod.uuid4().hex[:8]
    logging.getLogger(__name__).error("conflict_detection_failed error_id=%s", _err_id, exc_info=True)
    st.error(f"Error during conflict detection. Error ID: {_err_id}")

# ---------------------------------------------------------------------------
# Apply changes
# ---------------------------------------------------------------------------

st.divider()

if st.button("Apply Changes", type="primary", key="btn_apply"):
    with st.status("Applying feedback...", expanded=True) as status:
        try:
            from paperverifier.feedback.applier import FeedbackApplier
            from paperverifier.llm.client import UnifiedLLMClient
            from paperverifier.llm.config_store import load_role_assignments
            from paperverifier.llm.roles import AgentRole

            # Try to create a writer-backed applier if possible
            client = st.session_state.get("llm_client")
            assignments = st.session_state.get("role_assignments")

            writer_assignment = None
            if assignments:
                writer_assignment = assignments.get(AgentRole.WRITER)

            if client and writer_assignment:
                applier = FeedbackApplier(
                    client=client,
                    writer_assignment=writer_assignment,
                )
                st.write("Using Writer agent for intelligent rewrites...")
            else:
                applier = FeedbackApplier()
                st.write("Using direct suggestion text (no Writer agent)...")

            st.write(f"Applying {len(valid_selected)} item(s)...")

            applied_feedback = run_async(
                applier.apply(
                    document=doc,
                    report=report,
                    selected_items=valid_selected,
                    force=force_mode if conflicts else False,
                )
            )

            st.session_state["applied_feedback"] = applied_feedback

            status.update(
                label="Feedback applied!", state="complete", expanded=False
            )

        except Exception as exc:
            # Log full traceback server-side; show only sanitized
            # message to users (Codex-2).
            import logging
            import traceback
            import uuid

            error_id = uuid.uuid4().hex[:8]
            logging.getLogger(__name__).error(
                "feedback_apply_failed error_id=%s\n%s",
                error_id,
                traceback.format_exc(),
            )
            st.error(
                f"Failed to apply feedback. Error ID: {error_id}\n\n"
                "If this persists, contact support with the error ID above."
            )
            status.update(label="Application failed", state="error")

# ---------------------------------------------------------------------------
# Display results
# ---------------------------------------------------------------------------

applied = st.session_state.get("applied_feedback")

if applied is not None:
    st.divider()
    st.subheader("Application Results")

    # Stats
    stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)

    with stat_col1:
        st.metric("Items Applied", len(applied.applied_items))
    with stat_col2:
        st.metric("Items Skipped", len(applied.skipped_items))
    with stat_col3:
        char_delta = len(applied.modified_text) - len(applied.original_text)
        sign = "+" if char_delta >= 0 else ""
        st.metric("Character Change", f"{sign}{char_delta:,}")
    with stat_col4:
        st.metric("Total Changes", len(applied.changes))

    # Errors/warnings
    if applied.errors:
        with st.expander(f"Warnings & Errors ({len(applied.errors)})", expanded=False):
            for err in applied.errors:
                st.warning(err)

    # Diff display
    st.divider()
    st.subheader("Changes Diff")

    if applied.original_text == applied.modified_text:
        st.info("No changes were made to the document text.")
    else:
        diff_tab_html, diff_tab_unified, diff_tab_side = st.tabs(
            ["Visual Diff", "Unified Diff", "Side-by-Side"]
        )

        from paperverifier.feedback.diff_generator import DiffGenerator

        with diff_tab_html:
            html_diff = DiffGenerator.html_diff(
                applied.original_text,
                applied.modified_text,
                context_lines=3,
            )
            st.components.v1.html(html_diff, height=800, scrolling=True)  # MED-U9: taller

        with diff_tab_unified:
            unified = DiffGenerator.unified_diff(
                applied.original_text,
                applied.modified_text,
                filename=doc.title or "paper",
            )
            if unified:
                st.code(unified, language="diff")
            else:
                st.info("No differences found.")

        with diff_tab_side:
            side_by_side = DiffGenerator.side_by_side(
                applied.original_text,
                applied.modified_text,
                width=60,
            )
            st.code(side_by_side, language=None)

    # Change details
    if applied.changes:
        st.divider()
        st.subheader("Individual Changes")

        for change in applied.changes:
            with st.expander(
                f"Change #{change.item_number} "
                f"(chars {change.start_char}-{change.end_char})"
                + (f" [{change.segment_id}]" if change.segment_id else ""),
                expanded=False,
            ):
                ch_col1, ch_col2 = st.columns(2)
                with ch_col1:
                    st.markdown("**Original:**")
                    st.code(change.original_text[:500], language=None)
                with ch_col2:
                    st.markdown("**Replacement:**")
                    st.code(change.replacement_text[:500], language=None)

    # Download buttons
    st.divider()
    st.subheader("Download Modified Document")

    dl_col1, dl_col2, dl_col3 = st.columns(3)

    with dl_col1:
        st.download_button(
            label="Download Modified Text",
            data=applied.modified_text,
            file_name="paper_modified.txt",
            mime="text/plain",
            key="btn_dl_modified",
        )

    with dl_col2:
        st.download_button(
            label="Download Original Text",
            data=applied.original_text,
            file_name="paper_original.txt",
            mime="text/plain",
            key="btn_dl_original",
        )

    with dl_col3:
        # Summary report
        from paperverifier.feedback.diff_generator import DiffGenerator

        summary_text = DiffGenerator.summary(applied)
        st.download_button(
            label="Download Change Summary",
            data=summary_text,
            file_name="change_summary.txt",
            mime="text/plain",
            key="btn_dl_summary",
        )

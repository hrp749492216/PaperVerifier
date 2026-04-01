"""Review Findings page -- browse, filter, and select verification results.

Displays the :class:`VerificationReport` with filtering by severity,
category, and agent role.  Users select findings to apply and can export
the full report in JSON or Markdown format.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from streamlit_app.auth import require_auth

require_auth()


# ---------------------------------------------------------------------------
# Markdown report builder (must be defined before use)
# ---------------------------------------------------------------------------


def _build_markdown_report(rpt: Any) -> str:
    """Build a Markdown-formatted verification report."""
    lines: list[str] = []
    lines.append("# Verification Report")
    if rpt.document_title:
        lines.append(f"\n**Document:** {rpt.document_title}")
    lines.append(f"\n**Date:** {rpt.created_at.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"\n**Agents:** {rpt.agents_completed}/{rpt.agents_total} completed")
    lines.append(f"**Duration:** {rpt.duration_seconds:.1f}s")
    if rpt.overall_score is not None:
        lines.append(f"**Overall Score:** {rpt.overall_score:.0%}")

    lines.append(f"\n## Summary\n\n{rpt.summary}")

    # Severity breakdown
    lines.append("\n## Severity Breakdown\n")
    for sev in ["critical", "major", "minor", "info"]:
        count = rpt.severity_counts.get(sev, 0)
        lines.append(f"- **{sev.capitalize()}:** {count}")

    # Findings
    lines.append(f"\n## Findings ({rpt.total_findings} total)\n")
    for item in rpt.feedback_items:
        f = item.finding
        lines.append(f"### #{item.number} [{f.severity.value.upper()}] {f.title}\n")
        lines.append(f"- **Category:** {f.category.value}")
        lines.append(f"- **Agent:** {f.agent_role}")
        lines.append(f"- **Confidence:** {f.confidence:.0%}")
        if f.segment_id:
            lines.append(f"- **Location:** `{f.segment_id}`")
        lines.append(f"\n{f.description}\n")
        if f.suggestion:
            lines.append(f"**Suggestion:** {f.suggestion}\n")
        if f.segment_text:
            lines.append(f"```\n{f.segment_text[:300]}\n```\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------

st.header("Review Findings")

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
# Guard: require a report
# ---------------------------------------------------------------------------

report = st.session_state.get("verification_report")

if report is None:
    st.warning("No verification report available. Please upload and verify a document first.")
    st.page_link("streamlit_app/pages/1_Upload.py", label="Go to Upload", icon="\U0001f4e4")
    st.stop()

# ---------------------------------------------------------------------------
# Helper: severity colour
# ---------------------------------------------------------------------------

_SEVERITY_COLORS: dict[str, str] = {
    "critical": "red",
    "major": "orange",
    "minor": "blue",
    "info": "gray",
}

_SEVERITY_ICONS: dict[str, str] = {
    "critical": "\U0001f534",
    "major": "\U0001f7e0",
    "minor": "\U0001f535",
    "info": "\u2139\ufe0f",
}


def _score_color(score: float | None) -> str:
    """Return a CSS-safe colour for an overall score.

    HIGH-U8: Accepts *either* a 0-1 float or a 0-100 percentage and
    normalises to the 0-1 scale before applying thresholds so the
    colour mapping is consistent regardless of which scale upstream
    code uses.
    """
    if score is None:
        return "gray"
    # Normalise: if the value is > 1 assume it is on a 0-100 scale.
    if score > 1:
        score = score / 100.0
    if score >= 0.8:
        return "green"
    if score >= 0.5:
        return "orange"
    return "red"


# ---------------------------------------------------------------------------
# Overall score panel
# ---------------------------------------------------------------------------

st.divider()

score_col, summary_col = st.columns([1, 3])

with score_col:
    score = report.overall_score
    if score is not None:
        # HIGH-S3: Use st.metric instead of unsafe_allow_html to
        # avoid establishing an XSS-vulnerable pattern.
        # Normalise to percentage for display.
        display_score = score if score > 1 else score * 100
        st.metric("Overall Score", f"{display_score:.0f}%")
    else:
        st.metric("Overall Score", "N/A")

with summary_col:
    st.markdown(f"**Summary:** {report.summary}")
    st.markdown(
        f"**Duration:** {report.duration_seconds:.1f}s | "
        f"**Agents:** {report.agents_completed}/{report.agents_total} completed"
    )
    if report.total_tokens:
        input_tok = report.total_tokens.get("input_tokens", 0)
        output_tok = report.total_tokens.get("output_tokens", 0)
        st.caption(f"Tokens used: {input_tok:,} input / {output_tok:,} output")

# ---------------------------------------------------------------------------
# Severity metrics
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Severity Breakdown")

sev_cols = st.columns(4)
severity_order = ["critical", "major", "minor", "info"]

for idx, sev in enumerate(severity_order):
    count = report.severity_counts.get(sev, 0)
    with sev_cols[idx]:
        st.metric(
            label=f"{_SEVERITY_ICONS.get(sev, '')} {sev.capitalize()}",
            value=count,
        )

# ---------------------------------------------------------------------------
# Agent status overview
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Agent Status")

agent_cols = st.columns(min(len(report.agent_reports), 4) or 1)
for idx, ar in enumerate(report.agent_reports):
    with agent_cols[idx % len(agent_cols)]:
        status_icon = (
            "\u2705"
            if ar.status == "completed"
            else "\u274c"
            if ar.status == "failed"
            else "\u26a0\ufe0f"
            if ar.status == "disabled"
            else "\u2753"
        )
        st.markdown(f"{status_icon} **{ar.agent_role}**")
        if ar.status != "completed" and ar.error_message:
            st.caption(ar.error_message[:80])

# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Filter Findings")

all_findings_items = report.feedback_items

# Collect unique values for filter options
all_severities = sorted({item.finding.severity.value for item in all_findings_items})
all_categories = sorted({item.finding.category.value for item in all_findings_items})
all_agents = sorted({item.finding.agent_role for item in all_findings_items})

# MED-U6: Wrap filters in a form so changing a single dropdown doesn't
# trigger an immediate full-page rerun.  The user clicks "Apply Filters"
# once they are happy with the combination.
with st.form("filter_form"):
    filter_col1, filter_col2, filter_col3 = st.columns(3)

    with filter_col1:
        selected_severities = st.multiselect(
            "Severity",
            options=all_severities,
            default=all_severities,
            key="filter_severity",
        )

    with filter_col2:
        selected_categories = st.multiselect(
            "Category",
            options=all_categories,
            default=all_categories,
            key="filter_category",
        )

    with filter_col3:
        selected_agents = st.multiselect(
            "Agent",
            options=all_agents,
            default=all_agents,
            key="filter_agent",
        )

    st.form_submit_button("Apply Filters")

# Apply filters
filtered_items = [
    item
    for item in all_findings_items
    if item.finding.severity.value in selected_severities
    and item.finding.category.value in selected_categories
    and item.finding.agent_role in selected_agents
]

st.caption(f"Showing {len(filtered_items)} of {len(all_findings_items)} findings.")

# ---------------------------------------------------------------------------
# Findings list
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Findings")

if not filtered_items:
    st.info("No findings match the current filters.")
else:
    # HIGH-U3: Scope checkbox keys to report ID so selections don't
    # persist across different reports.
    report_id = report.id

    # Initialize checkbox states
    for item in filtered_items:
        cb_key = f"cb_finding_{report_id}_{item.number}"
        if cb_key not in st.session_state:
            st.session_state[cb_key] = False

    # Select/deselect all controls
    sel_col1, sel_col2, sel_col3 = st.columns([1, 1, 4])
    with sel_col1:
        if st.button("Select All Visible", key="btn_select_all"):
            for item in filtered_items:
                st.session_state[f"cb_finding_{report_id}_{item.number}"] = True
            st.rerun()
    with sel_col2:
        if st.button("Deselect All", key="btn_deselect_all"):
            for item in filtered_items:
                st.session_state[f"cb_finding_{report_id}_{item.number}"] = False
            st.rerun()

    # Render each finding as an expander
    for item in filtered_items:
        finding = item.finding
        sev = finding.severity.value
        icon = _SEVERITY_ICONS.get(sev, "")
        cat = finding.category.value
        applicable_tag = " [applicable]" if item.applicable else " [info only]"

        with st.expander(
            f"{icon} #{item.number} | {sev.upper()} | {cat} | {finding.title}",
            expanded=False,
        ):
            # Checkbox for selection
            cb_key = f"cb_finding_{report_id}_{item.number}"
            st.checkbox(
                "Select for application",
                key=cb_key,
                disabled=not item.applicable,
                help="Only findings with suggestions can be applied."
                if not item.applicable
                else None,
            )

            # Details
            st.markdown(f"**Description:** {finding.description}")

            if finding.suggestion:
                st.markdown(f"**Suggestion:** {finding.suggestion}")

            detail_col1, detail_col2 = st.columns(2)
            with detail_col1:
                st.markdown(f"**Agent:** {finding.agent_role}")
                st.markdown(f"**Confidence:** {finding.confidence:.0%}")
            with detail_col2:
                if finding.segment_id:
                    st.markdown(f"**Location:** `{finding.segment_id}`")
                if item.conflicts_with:
                    st.warning(
                        f"Conflicts with item(s): {', '.join(f'#{n}' for n in item.conflicts_with)}"
                    )

            if finding.segment_text:
                st.markdown("**Quoted text:**")
                st.code(finding.segment_text[:500], language=None)

            if finding.evidence:
                st.markdown("**Evidence:**")
                for ev in finding.evidence:
                    st.markdown(f"- {ev}")


# ---------------------------------------------------------------------------
# Bottom actions
# ---------------------------------------------------------------------------

st.divider()

action_col1, action_col2, action_col3 = st.columns(3)

with action_col1:
    if st.button("Apply Selected", type="primary", key="btn_apply_selected"):
        # Gather selected item numbers (scoped to report ID)
        _report_id = report.id
        selected = [
            item.number
            for item in all_findings_items
            if st.session_state.get(f"cb_finding_{_report_id}_{item.number}", False)
        ]
        if not selected:
            st.warning("No findings selected. Use the checkboxes to select findings to apply.")
        else:
            st.session_state["selected_items"] = selected
            st.toast(f"Selected {len(selected)} item(s). Navigating to Apply Feedback...")
            # MED-U8: Auto-navigate to the Apply page via switch_page
            st.switch_page("streamlit_app/pages/3_Apply.py")

with action_col2:
    # Export JSON
    report_json = report.to_json()
    st.download_button(
        label="Export JSON Report",
        data=report_json,
        file_name="verification_report.json",
        mime="application/json",
        key="btn_export_json",
    )

with action_col3:
    # Export Markdown
    md_lines = _build_markdown_report(report)
    st.download_button(
        label="Export Markdown Report",
        data=md_lines,
        file_name="verification_report.md",
        mime="text/markdown",
        key="btn_export_md",
    )

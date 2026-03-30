"""PaperVerifier -- Streamlit web application entry point.

Main page with sidebar navigation, quick stats, and welcome content.
Run with: ``streamlit run streamlit_app/app.py``
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from streamlit_app.utils import run_async  # noqa: F401 – shared async helper

# ---------------------------------------------------------------------------
# Page configuration (must be the first Streamlit call)
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="PaperVerifier",
    page_icon="\U0001f4dd",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Session-state defaults
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "parsed_document": None,
    "verification_report": None,
    "selected_items": [],
    "applied_feedback": None,
    "llm_client": None,
    "role_assignments": None,
}

for key, value in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = value


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("\U0001f4dd PaperVerifier")
    st.caption("Enterprise-grade research paper verification")
    st.divider()

    # Navigation status
    st.subheader("Workflow Progress")

    doc = st.session_state.get("parsed_document")
    report = st.session_state.get("verification_report")
    applied = st.session_state.get("applied_feedback")

    steps = [
        ("1. Upload & Parse", doc is not None),
        ("2. Review Findings", report is not None),
        ("3. Apply Feedback", applied is not None),
    ]

    for label, done in steps:
        icon = "\u2705" if done else "\u2b1c"
        st.markdown(f"{icon} {label}")

    st.divider()

    # Quick stats when a document is loaded
    if doc is not None:
        st.subheader("Document Info")
        if doc.title:
            st.markdown(f"**Title:** {doc.title}")
        st.markdown(f"**Sections:** {len(doc.sections)}")
        st.markdown(f"**References:** {len(doc.references)}")
        total_sentences = len(doc.get_all_sentences())
        st.markdown(f"**Sentences:** {total_sentences:,}")
        if doc.source_type:
            st.markdown(f"**Source:** {doc.source_type}")
        st.divider()

    # Data-processing notice
    st.subheader("Settings")
    st.page_link("streamlit_app/pages/4_Settings.py", label="LLM Configuration", icon="\u2699\ufe0f")

    st.divider()
    st.info(
        "API keys are stored in your OS keyring, never in plaintext files. "
        "Document content is sent to your configured LLM providers for analysis.",
        icon="\U0001f512",
    )


# ---------------------------------------------------------------------------
# Main page content
# ---------------------------------------------------------------------------

st.title("Welcome to PaperVerifier")
st.markdown(
    "An enterprise-grade tool for automated verification of research papers, "
    "powered by multi-agent LLM analysis."
)

st.divider()

# Quick-start guide
st.header("Quick Start")

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("1. Upload")
    st.markdown(
        "Upload a research paper (PDF, DOCX, Markdown, LaTeX, or plain text), "
        "paste a URL, or point to a GitHub repository."
    )

with col2:
    st.subheader("2. Review")
    st.markdown(
        "Our multi-agent system analyses your paper for structural issues, "
        "claim consistency, reference accuracy, and more. Filter and inspect "
        "each finding."
    )

with col3:
    st.subheader("3. Apply")
    st.markdown(
        "Select the findings you agree with and apply AI-generated fixes. "
        "Review diffs, resolve conflicts, and download the improved document."
    )

st.divider()

# Feature overview
st.header("Verification Agents")

agents_info = [
    ("Section Structure", "Checks heading hierarchy, logical flow, and completeness."),
    ("Claim Verification", "Validates claims against cited evidence and detects unsupported assertions."),
    ("Reference Verification", "Cross-checks references against OpenAlex, Crossref, and Semantic Scholar."),
    ("Hallucination Detection", "Identifies fabricated data, invented citations, and factual inconsistencies."),
    ("Results Consistency", "Verifies that numbers, tables, and figures agree with textual descriptions."),
    ("Novelty Assessment", "Evaluates the paper's contribution relative to existing literature."),
    ("Language & Flow", "Analyses grammar, readability, and overall writing quality."),
]

cols = st.columns(2)
for idx, (name, desc) in enumerate(agents_info):
    with cols[idx % 2]:
        st.markdown(f"**{name}**")
        st.markdown(desc)

st.divider()

# Supported providers
st.header("Supported LLM Providers")
providers_list = [
    "Anthropic (Claude)", "OpenAI (GPT-4o)", "Grok", "OpenRouter",
    "Gemini", "MiniMax", "Kimi", "DeepSeek",
]
st.markdown(" | ".join(f"**{p}**" for p in providers_list))

st.divider()
st.caption("PaperVerifier -- built for rigorous, transparent research verification.")

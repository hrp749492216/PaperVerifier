"""Upload & Parse page -- file upload, URL input, and GitHub import.

Handles document parsing via :class:`InputRouter`, displays a parsed-document
preview, and launches the verification pipeline via :class:`AgentOrchestrator`.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import uuid as _uuid
from pathlib import Path

import streamlit as st

from streamlit_app.auth import require_auth
from streamlit_app.rate_limit import SessionRateLimiter
from streamlit_app.utils import run_async  # noqa: F401 – shared async helper

require_auth()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_UPLOAD_SIZE_MB = 50
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024

# Per-session verification rate limiter (10 requests per hour).
_verification_limiter = SessionRateLimiter(max_requests=10, window_seconds=3600.0)


# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------

st.header("Upload & Parse")
st.markdown(
    "Upload a research paper, provide a URL, or point to a GitHub repository. "
    "The document will be parsed into a structured representation for analysis."
)

# ---------------------------------------------------------------------------
# Session-state defaults
# ---------------------------------------------------------------------------

for key, default in [
    ("parsed_document", None),
    ("verification_report", None),
    ("selected_items", []),
    ("applied_feedback", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default


# ---------------------------------------------------------------------------
# Input methods (tabs)
# ---------------------------------------------------------------------------

tab_upload, tab_url, tab_github = st.tabs(
    ["File Upload", "URL Input", "GitHub Repository"]
)

parsed_document = None

# -- Tab 1: File Upload ---------------------------------------------------

with tab_upload:
    uploaded_file = st.file_uploader(
        "Choose a research paper",
        type=["pdf", "docx", "md", "tex", "txt"],
        help="Supported formats: PDF, DOCX, Markdown, LaTeX, plain text.",
    )

    if uploaded_file is not None:
        st.info(
            f"**File:** {uploaded_file.name} | "
            f"**Size:** {uploaded_file.size / 1024:.1f} KB | "
            f"**Type:** {uploaded_file.type or 'unknown'}"
        )

        # HIGH-U1: Application-level file size check
        if uploaded_file.size > MAX_UPLOAD_SIZE_BYTES:
            st.error(
                f"File exceeds the {MAX_UPLOAD_SIZE_MB} MB size limit "
                f"({uploaded_file.size / (1024 * 1024):.1f} MB). "
                "Please upload a smaller file."
            )
        # MED-U1: Reject empty files
        elif uploaded_file.size == 0:
            st.error("The uploaded file is empty. Please upload a valid document.")
        elif st.button("Parse Document", key="btn_parse_file"):
            with st.status("Parsing document...", expanded=True) as status:
                tmp_path: str | None = None
                try:
                    from paperverifier.parsers.router import InputRouter

                    from paperverifier.security.input_validator import (
                        validate_uploaded_file,
                        InputValidationError,
                    )
                    from paperverifier.config import get_settings

                    st.write("Reading file content...")
                    file_bytes = uploaded_file.read()
                    file_name = uploaded_file.name

                    # Server-side validation: size, extension, magic bytes
                    st.write("Validating file...")
                    file_name, file_bytes = validate_uploaded_file(
                        file_name,
                        file_bytes,
                        max_size=get_settings().max_document_size_mb * 1024 * 1024,
                    )

                    # Write to a temp file so the router can detect by extension
                    suffix = Path(file_name).suffix
                    with tempfile.NamedTemporaryFile(
                        delete=False, suffix=suffix
                    ) as tmp:
                        tmp.write(file_bytes)
                        tmp_path = tmp.name

                    st.write(f"Routing to parser for *{suffix}* format...")
                    router = InputRouter()
                    parsed_document = run_async(
                        router.parse(tmp_path, content=file_bytes)
                    )

                    # MED-U5: Clear stale downstream state on new parse
                    st.session_state["verification_report"] = None
                    st.session_state["selected_items"] = []
                    st.session_state["applied_feedback"] = None

                    st.session_state["parsed_document"] = parsed_document
                    status.update(
                        label="Parsing complete!", state="complete", expanded=False
                    )

                except Exception as exc:
                    error_id = str(_uuid.uuid4())[:8]
                    logger.error("parse_failed", exc_info=True, extra={"error_id": error_id})
                    st.error(f"Failed to parse document. Error ID: {error_id}")
                    status.update(label="Parsing failed", state="error")
                finally:
                    # MED-U2: Always clean up temp file, even on failure
                    if tmp_path is not None:
                        Path(tmp_path).unlink(missing_ok=True)

# -- Tab 2: URL Input -----------------------------------------------------

with tab_url:
    paper_url = st.text_input(
        "Paper URL",
        placeholder="https://arxiv.org/abs/2301.12345 or direct PDF link",
        help="Supports arXiv abstract pages, direct PDF links, and other paper URLs.",
    )

    if paper_url:
        if st.button("Fetch & Parse", key="btn_parse_url"):
            from paperverifier.security.input_validator import validate_url, InputValidationError

            try:
                validate_url(paper_url)
            except InputValidationError as exc:
                error_id = str(_uuid.uuid4())[:8]
                logger.error("invalid_url", exc_info=True, extra={"error_id": error_id})
                st.error(f"Invalid URL. Error ID: {error_id}")
                st.stop()

            with st.status("Fetching and parsing...", expanded=True) as status:
                try:
                    from paperverifier.parsers.router import InputRouter

                    st.write(f"Fetching from URL...")
                    router = InputRouter()
                    parsed_document = run_async(router.parse(paper_url))

                    # Clear stale downstream state on new parse (Codex-2).
                    st.session_state["verification_report"] = None
                    st.session_state["selected_items"] = []
                    st.session_state["applied_feedback"] = None

                    st.session_state["parsed_document"] = parsed_document
                    status.update(
                        label="Fetch & parse complete!",
                        state="complete",
                        expanded=False,
                    )

                except Exception as exc:
                    error_id = str(_uuid.uuid4())[:8]
                    logger.error("url_parse_failed", exc_info=True, extra={"error_id": error_id})
                    st.error(f"Failed to fetch or parse URL. Error ID: {error_id}")
                    status.update(label="Fetch failed", state="error")

# -- Tab 3: GitHub --------------------------------------------------------

with tab_github:
    github_url = st.text_input(
        "GitHub Repository URL",
        placeholder="https://github.com/owner/repo",
        help="Point to a GitHub repository containing a research paper.",
    )

    if github_url:
        if st.button("Clone & Parse", key="btn_parse_github"):
            from paperverifier.security.input_validator import validate_github_url, InputValidationError

            try:
                validate_github_url(github_url)
            except InputValidationError as exc:
                error_id = str(_uuid.uuid4())[:8]
                logger.error("invalid_github_url", exc_info=True, extra={"error_id": error_id})
                st.error(f"Invalid GitHub URL. Error ID: {error_id}")
                st.stop()

            with st.status("Cloning and parsing...", expanded=True) as status:
                try:
                    from paperverifier.parsers.router import InputRouter

                    st.write("Cloning repository...")
                    router = InputRouter()
                    parsed_document = run_async(router.parse(github_url))

                    # Clear stale downstream state on new parse (Codex-2).
                    st.session_state["verification_report"] = None
                    st.session_state["selected_items"] = []
                    st.session_state["applied_feedback"] = None

                    st.session_state["parsed_document"] = parsed_document
                    status.update(
                        label="Clone & parse complete!",
                        state="complete",
                        expanded=False,
                    )

                except Exception as exc:
                    error_id = str(_uuid.uuid4())[:8]
                    logger.error("github_parse_failed", exc_info=True, extra={"error_id": error_id})
                    st.error(f"Failed to clone or parse repository. Error ID: {error_id}")
                    status.update(label="Clone failed", state="error")


# ---------------------------------------------------------------------------
# Document preview
# ---------------------------------------------------------------------------

doc = st.session_state.get("parsed_document")

if doc is not None:
    st.divider()
    st.subheader("Parsed Document Preview")

    # Summary metrics
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Sections", len(doc.sections))
    with col2:
        st.metric("References", len(doc.references))
    with col3:
        total_sentences = len(doc.get_all_sentences())
        st.metric("Sentences", f"{total_sentences:,}")
    with col4:
        st.metric("Characters", f"{len(doc.full_text):,}")

    if doc.title:
        st.markdown(f"**Title:** {doc.title}")
    if doc.authors:
        st.markdown(f"**Authors:** {', '.join(doc.authors)}")
    if doc.abstract:
        with st.expander("Abstract", expanded=False):
            st.markdown(doc.abstract)

    # Section outline
    if doc.sections:
        with st.expander("Section Outline", expanded=True):
            for section in doc.sections:
                indent = "  " * (section.level - 1)
                para_count = len(section.paragraphs)
                st.markdown(
                    f"{indent}- **{section.title}** "
                    f"({para_count} paragraph{'s' if para_count != 1 else ''})"
                )
                for sub in section.subsections:
                    sub_indent = "  " * sub.level
                    sub_para_count = len(sub.paragraphs)
                    st.markdown(
                        f"{sub_indent}- {sub.title} "
                        f"({sub_para_count} paragraph{'s' if sub_para_count != 1 else ''})"
                    )

    # References list
    if doc.references:
        with st.expander(f"References ({len(doc.references)})", expanded=False):
            for ref in doc.references[:20]:
                ref_label = ref.title or ref.raw_text[:100]
                year_str = f" ({ref.year})" if ref.year else ""
                st.markdown(f"- {ref_label}{year_str}")
            if len(doc.references) > 20:
                st.caption(f"... and {len(doc.references) - 20} more references.")

    # ---------------------------------------------------------------------------
    # Start verification
    # ---------------------------------------------------------------------------

    st.divider()
    st.subheader("Run Verification")
    st.markdown(
        "Launch the multi-agent verification pipeline. This will analyse the "
        "document for structural issues, claim consistency, reference accuracy, "
        "hallucinations, and more."
    )

    if st.button("Start Verification", type="primary", key="btn_verify"):
        # Per-session rate-limit guard
        session_id = st.session_state.get("_pv_session_id", "anonymous")
        if not _verification_limiter.check(session_id):
            wait = _verification_limiter.remaining_wait(session_id)
            minutes = int(wait // 60)
            st.error(
                f"Rate limit exceeded. Please wait {minutes} minutes "
                "before submitting another verification."
            )
            st.stop()
        _verification_limiter.record(session_id)

        # Build client and assignments
        try:
            from paperverifier.llm.client import UnifiedLLMClient
            from paperverifier.llm.config_store import load_role_assignments
            from paperverifier.llm.roles import AgentRole
            from paperverifier.agents.orchestrator import AgentOrchestrator

            client = st.session_state.get("llm_client")
            if client is None:
                client = UnifiedLLMClient()
                st.session_state["llm_client"] = client

            assignments = st.session_state.get("role_assignments")
            if assignments is None:
                assignments = load_role_assignments()
                st.session_state["role_assignments"] = assignments

            # Build a list of agent role names we expect
            from paperverifier.agents.orchestrator import _AGENT_CLASSES

            expected_agents = [role.value for role in _AGENT_CLASSES.keys()]
            total_agents = len(expected_agents)

            # HIGH-U2 / CRIT-10: Track progress with actual updates and
            # run the verification in a background thread so we don't
            # block the Streamlit server.
            # Use a mutable dict for progress state since module-scope
            # variables cannot be accessed via `nonlocal` (Codex-1 fix #1).
            progress_lock = threading.Lock()
            progress_state: dict[str, object] = {"count": 0, "statuses": {}}

            progress_bar = st.progress(0, text="Initializing agents...")

            async def progress_callback(role_name: str, status: str) -> None:
                with progress_lock:
                    statuses = progress_state["statuses"]
                    statuses[role_name] = status  # type: ignore[union-attr]
                    if status in ("completed", "failed", "disabled"):
                        progress_state["count"] = int(progress_state["count"]) + 1  # type: ignore[arg-type]

            orchestrator = AgentOrchestrator(
                client=client,
                assignments=assignments,
                progress_callback=progress_callback,
            )

            # Container to hold result / error from the background thread
            result_holder: dict[str, object] = {}

            def _run_verification() -> None:
                """Run the async verification in a dedicated thread.

                Creates a fresh event loop for the thread since
                asyncio.get_event_loop() may fail in non-main threads
                on Python 3.10+ (Codex-1 fix #4).
                """
                import asyncio as _asyncio

                loop = _asyncio.new_event_loop()
                try:
                    # Enrich document with external API evidence
                    # before running verification (Codex-1 fix #3).
                    from paperverifier.external.enrichment import enrich_document

                    try:
                        external_data = loop.run_until_complete(
                            enrich_document(doc)
                        )
                    except Exception:
                        logger.exception("enrichment_failed")
                        external_data = {}

                    report = loop.run_until_complete(
                        orchestrator.verify(doc, external_data=external_data)
                    )
                    result_holder["report"] = report
                except Exception as thread_exc:
                    result_holder["error"] = thread_exc
                finally:
                    loop.close()

            with st.status(
                "Running verification pipeline...", expanded=True
            ) as run_status:
                for role_name in expected_agents:
                    st.write(f"Agent queued: **{role_name}**")

                # CRIT-10: Run in a thread to avoid blocking the server
                worker = threading.Thread(target=_run_verification, daemon=True)
                worker.start()

                # HIGH-U2: Poll and update progress bar while worker runs
                import time
                while worker.is_alive():
                    done = int(progress_state["count"])  # type: ignore[arg-type]
                    pct = int((done / total_agents) * 100) if total_agents else 0
                    statuses = progress_state["statuses"]
                    running = [
                        name for name, s in statuses.items() if s == "running"  # type: ignore[union-attr]
                    ]
                    label = (
                        f"Running: {', '.join(running)}... ({done}/{total_agents} done)"
                        if running
                        else f"Processing... ({done}/{total_agents} done)"
                    )
                    progress_bar.progress(min(pct, 99), text=label)
                    time.sleep(0.3)

                worker.join()

                # Check for errors from the thread
                if "error" in result_holder:
                    raise result_holder["error"]  # type: ignore[misc]

                report = result_holder["report"]  # type: ignore[assignment]
                st.session_state["verification_report"] = report

                # Display completion info
                st.write(
                    f"Completed: {report.agents_completed}/{report.agents_total} agents"
                )
                st.write(f"Total findings: {report.total_findings}")
                st.write(
                    f"Duration: {report.duration_seconds:.1f}s"
                )

                run_status.update(
                    label="Verification complete!", state="complete", expanded=False
                )

            progress_bar.progress(100, text="Verification complete!")

            # Show summary and link to review
            st.success(
                f"Verification finished! Found **{report.total_findings}** findings "
                f"across **{report.agents_completed}** agents in "
                f"**{report.duration_seconds:.1f}s**."
            )

            # Display severity breakdown
            if report.severity_counts:
                sev_cols = st.columns(len(report.severity_counts))
                for idx, (sev, count) in enumerate(
                    sorted(report.severity_counts.items())
                ):
                    with sev_cols[idx]:
                        st.metric(sev.capitalize(), count)

            st.page_link(
                "streamlit_app/pages/2_Review.py",
                label="Go to Review Findings",
                icon="\u27a1\ufe0f",
            )

        except Exception as exc:
            # Show only a sanitized error message to users; log full
            # traceback server-side only (Codex-2).
            error_id = str(_uuid.uuid4())[:8]
            logger.error(
                "verification_failed error_id=%s",
                error_id,
                exc_info=True,
            )
            st.error(
                f"Verification failed. Error ID: {error_id}\n\n"
                "If this persists, contact support with the error ID above."
            )

elif st.session_state.get("parsed_document") is None:
    st.info("Upload a document, enter a URL, or provide a GitHub link to get started.")

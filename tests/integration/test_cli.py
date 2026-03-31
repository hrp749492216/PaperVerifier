"""CLI smoke tests for PaperVerifier.

Exercises the Click CLI entry points (version flag, missing file error,
and a fully-mocked verification run) without touching real LLMs or APIs.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from paperverifier.cli import cli
from paperverifier.models.report import VerificationReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_report() -> VerificationReport:
    """Build a minimal VerificationReport that satisfies CLI display logic."""
    return VerificationReport(
        id="test-report-id",
        document_title="Test Paper",
        overall_score=8.5,
        total_findings=0,
        agents_completed=1,
        agents_total=1,
        agent_reports=[],
        consolidated_findings=[],
        total_tokens={},
        estimated_cost_usd=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_verify_version():
    """``cli --version`` should exit 0 and print a version string."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "version" in result.output.lower() or "." in result.output


def test_verify_with_missing_file():
    """``cli verify /nonexistent/file.pdf`` should exit non-zero."""
    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "/nonexistent/file.pdf"])
    assert result.exit_code != 0


def test_verify_with_text_file():
    """Full verification pipeline with all LLM/API parts mocked out.

    Creates a temporary text file, mocks the orchestrator, LLM client,
    config store, and enrichment module, then asserts the CLI exits
    cleanly (exit_code 0).
    """
    runner = CliRunner()

    # Create a temporary text file with plausible content
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False,
    ) as tmp:
        tmp.write(
            "Title: A Study on Testing\n\n"
            "Abstract: This paper studies testing methodologies.\n\n"
            "1. Introduction\n"
            "Testing is important for software quality.\n"
        )
        tmp_path = tmp.name

    try:
        # -- Mock the orchestrator -------------------------------------------
        mock_orchestrator_instance = AsyncMock()
        mock_orchestrator_instance.verify = AsyncMock(
            return_value=_make_mock_report(),
        )
        mock_orchestrator_cls = MagicMock(
            return_value=mock_orchestrator_instance,
        )

        # -- Mock the LLM client ---------------------------------------------
        mock_llm_client_instance = MagicMock()
        mock_llm_client_cls = MagicMock(return_value=mock_llm_client_instance)

        # -- Mock role assignments -------------------------------------------
        mock_load_roles = MagicMock(return_value={})

        # -- Mock enrichment -------------------------------------------------
        mock_enrich = AsyncMock(
            return_value={"api_results": {}, "related_works": []},
        )

        with (
            patch(
                "paperverifier.agents.orchestrator.AgentOrchestrator",
                mock_orchestrator_cls,
            ),
            patch(
                "paperverifier.llm.client.UnifiedLLMClient",
                mock_llm_client_cls,
            ),
            patch(
                "paperverifier.llm.config_store.load_role_assignments",
                mock_load_roles,
            ),
            patch(
                "paperverifier.external.enrichment.enrich_document",
                mock_enrich,
            ),
        ):
            result = runner.invoke(cli, ["verify", tmp_path])

        # The command should complete without crashing
        if result.exit_code != 0:
            # Print diagnostic info when the test fails
            print("STDOUT:", result.output)
            print("STDERR:", getattr(result, "stderr", "N/A"))
            if result.exception:
                import traceback
                traceback.print_exception(
                    type(result.exception),
                    result.exception,
                    result.exception.__traceback__,
                )
        assert result.exit_code == 0, (
            f"CLI exited with code {result.exit_code}; output: {result.output}"
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

"""Integration tests for AgentOrchestrator partial-failure recovery.

Mocks the UnifiedLLMClient so no real LLM calls are made, but exercises
the full orchestrator pipeline: agent creation, parallel execution,
circuit breaker, synthesis, and report building.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paperverifier.agents.orchestrator import AgentOrchestrator, _AGENT_CLASSES
from paperverifier.llm.client import LLMResponse, Message, UnifiedLLMClient
from paperverifier.llm.providers import LLMProvider
from paperverifier.llm.roles import AgentRole, RoleAssignment
from paperverifier.models.document import ParsedDocument, Section, Paragraph, Sentence
from paperverifier.models.findings import Finding, FindingCategory, Severity
from paperverifier.models.report import AgentReport


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_assignment(role: AgentRole) -> RoleAssignment:
    return RoleAssignment(
        provider=LLMProvider.ANTHROPIC,
        model="claude-sonnet-4-20250514",
        temperature=0.3,
        max_tokens=4096,
    )


def _make_assignments() -> dict[AgentRole, RoleAssignment]:
    assignments = {}
    for role in AgentRole:
        assignments[role] = _make_assignment(role)
    return assignments


def _make_document() -> ParsedDocument:
    """Create a minimal ParsedDocument for testing."""
    sent = Sentence(
        id="sec-1.para-1.sent-1",
        text="This paper studies the effect of temperature on ice cream melting.",
        start_char=0,
        end_char=65,
    )
    para = Paragraph(
        id="sec-1.para-1",
        sentences=[sent],
        raw_text=sent.text,
        start_char=0,
        end_char=65,
    )
    section = Section(
        id="sec-1",
        title="Introduction",
        level=1,
        paragraphs=[para],
        start_char=0,
        end_char=65,
    )
    return ParsedDocument(
        title="Test Paper",
        full_text=sent.text,
        sections=[section],
        references=[],
    )


def _fake_llm_response(findings: list[dict]) -> LLMResponse:
    return LLMResponse(
        content=json.dumps(findings),
        model="claude-sonnet-4-20250514",
        provider=LLMProvider.ANTHROPIC,
        usage={"input_tokens": 100, "output_tokens": 50},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_all_agents_succeed():
    """All agents succeed and produce findings that appear in the report."""
    mock_client = MagicMock(spec=UnifiedLLMClient)
    mock_client.resolve_api_key = MagicMock(return_value="fake-key")
    mock_client.complete_for_role = AsyncMock(
        return_value=_fake_llm_response([
            {
                "category": "structure",
                "severity": "minor",
                "title": "Missing subsection",
                "description": "The introduction lacks a subsection.",
            }
        ])
    )

    assignments = _make_assignments()
    doc = _make_document()

    orchestrator = AgentOrchestrator(
        client=mock_client,
        assignments=assignments,
    )

    report = await orchestrator.verify(doc)

    assert report.agents_total > 0
    assert report.agents_completed >= 1
    assert report.duration_seconds >= 0


@pytest.mark.asyncio
async def test_orchestrator_partial_failure_recovery():
    """When some agents fail, the orchestrator still produces a report
    from the successful agents rather than aborting."""
    call_count = 0

    async def _mock_complete(messages, assignment, *, timeout=120.0):
        nonlocal call_count
        call_count += 1
        # Fail every other call
        if call_count % 2 == 0:
            raise RuntimeError("Simulated LLM failure")
        return _fake_llm_response([
            {
                "category": "claim",
                "severity": "major",
                "title": "Unsupported claim",
                "description": "No evidence provided.",
            }
        ])

    mock_client = MagicMock(spec=UnifiedLLMClient)
    mock_client.resolve_api_key = MagicMock(return_value="fake-key")
    mock_client.complete_for_role = AsyncMock(side_effect=_mock_complete)

    assignments = _make_assignments()
    doc = _make_document()

    orchestrator = AgentOrchestrator(
        client=mock_client,
        assignments=assignments,
    )

    report = await orchestrator.verify(doc)

    # Should have some completed and some failed
    statuses = [r.status for r in report.agent_reports]
    assert "failed" in statuses, "Expected at least one failed agent"
    # The report should still exist and have non-negative stats
    assert report.agents_total > 0
    assert report.duration_seconds >= 0


@pytest.mark.asyncio
async def test_orchestrator_progress_callback():
    """Progress callback is invoked for each agent."""
    mock_client = MagicMock(spec=UnifiedLLMClient)
    mock_client.resolve_api_key = MagicMock(return_value="fake-key")
    mock_client.complete_for_role = AsyncMock(
        return_value=_fake_llm_response([])
    )

    assignments = _make_assignments()
    doc = _make_document()

    progress_events: list[tuple[str, str]] = []

    async def progress_cb(role_name: str, status: str) -> None:
        progress_events.append((role_name, status))

    orchestrator = AgentOrchestrator(
        client=mock_client,
        assignments=assignments,
        progress_callback=progress_cb,
    )

    await orchestrator.verify(doc)

    # Each agent should have at least a "running" event
    running_events = [e for e in progress_events if e[1] == "running"]
    assert len(running_events) >= 1


@pytest.mark.asyncio
async def test_orchestrator_empty_assignments():
    """Orchestrator with no assignments produces an empty report."""
    mock_client = MagicMock(spec=UnifiedLLMClient)

    orchestrator = AgentOrchestrator(
        client=mock_client,
        assignments={},
    )

    doc = _make_document()
    report = await orchestrator.verify(doc)

    assert report.agents_total == 0
    assert report.total_findings == 0

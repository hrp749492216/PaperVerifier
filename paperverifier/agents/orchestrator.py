"""Agent orchestrator for running all verification agents in parallel.

The :class:`AgentOrchestrator` coordinates the full verification pipeline:
1. Instantiates all specialist agents with their role assignments.
2. Runs agents in parallel with a global concurrency semaphore.
3. Handles partial failures (agents that crash still contribute nothing
   rather than aborting the entire pipeline).
4. Implements agent-level circuit breakers (disable after 3 failures).
5. Runs a final orchestrator LLM call to deduplicate, prioritise, and
   synthesise findings into a coherent report.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

import structlog

from paperverifier.llm.client import LLMResponse, Message, UnifiedLLMClient
from paperverifier.llm.roles import AgentRole, RoleAssignment
from paperverifier.models.document import ParsedDocument
from paperverifier.models.findings import Finding
from paperverifier.models.report import AgentReport, VerificationReport
from paperverifier.utils.chunking import create_document_summary
from paperverifier.utils.json_parser import JSONParseError, parse_llm_json
from paperverifier.utils.prompts import get_prompts

from paperverifier.audit import log_verification_start, log_verification_complete
from paperverifier.agents.base import BaseAgent
from paperverifier.agents.claim_verification import ClaimVerificationAgent
from paperverifier.agents.hallucination_detection import HallucinationDetectionAgent
from paperverifier.agents.language_flow import LanguageFlowAgent
from paperverifier.agents.novelty_assessment import NoveltyAssessmentAgent
from paperverifier.agents.reference_verification import ReferenceVerificationAgent
from paperverifier.agents.results_consistency import ResultsConsistencyAgent
from paperverifier.agents.section_structure import SectionStructureAgent

logger = structlog.get_logger(__name__)

# Type alias for progress callback
ProgressCallback = Callable[[str, str], Awaitable[None]]

# Agent factory registry: maps AgentRole to the agent class
_AGENT_CLASSES: dict[AgentRole, type[BaseAgent]] = {
    AgentRole.SECTION_STRUCTURE: SectionStructureAgent,
    AgentRole.CLAIM_VERIFICATION: ClaimVerificationAgent,
    AgentRole.RESEARCH: ReferenceVerificationAgent,
    AgentRole.RESULTS_CONSISTENCY: ResultsConsistencyAgent,
    AgentRole.NOVELTY_ASSESSMENT: NoveltyAssessmentAgent,
    AgentRole.LANGUAGE_FLOW: LanguageFlowAgent,
    AgentRole.HALLUCINATION_DETECTION: HallucinationDetectionAgent,
}

# Maximum consecutive failures before a circuit breaker trips
_CIRCUIT_BREAKER_THRESHOLD = 3


class AgentOrchestrator:
    """Coordinates all verification agents and produces a unified report.

    Parameters
    ----------
    client:
        The unified LLM client shared by all agents.
    assignments:
        Maps each :class:`AgentRole` to its :class:`RoleAssignment`.
    max_concurrent:
        Maximum number of agents running simultaneously.
    progress_callback:
        Optional async callback ``(role_name, status) -> None`` invoked
        after each agent completes.
    """

    def __init__(
        self,
        client: UnifiedLLMClient,
        assignments: dict[AgentRole, RoleAssignment],
        max_concurrent: int = 9,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self._client = client
        self._assignments = assignments
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._progress_callback = progress_callback
        self._logger = structlog.get_logger().bind(component="orchestrator")
        # Circuit breaker state -- reset on each verify() call (HIGH-I2).
        self._failure_counts: dict[str, int] = defaultdict(int)
        self._disabled_agents: set[str] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def verify(
        self,
        document: ParsedDocument,
        external_data: dict[str, Any] | None = None,
    ) -> VerificationReport:
        """Run all verification agents and produce a consolidated report.

        Steps:
        1. Create all agent instances from role assignments.
        2. Run agents in parallel with semaphore-controlled concurrency.
        3. Collect results, handling partial failures gracefully.
        4. Run the orchestrator agent for summary and deduplication.
        5. Generate feedback items with conflict detection.
        6. Return a complete :class:`VerificationReport`.

        Parameters
        ----------
        document:
            The parsed document to verify.
        external_data:
            Optional dict with keys ``api_results`` (for reference
            verification) and ``related_works`` (for novelty assessment).
        """
        pipeline_start = time.monotonic()
        external_data = external_data or {}

        # Reset circuit breaker state from prior verify() calls (HIGH-I2).
        self._failure_counts.clear()
        self._disabled_agents.clear()

        self._logger.info(
            "verification_started",
            document_title=document.title,
            document_hash=document.content_hash[:12] if document.content_hash else None,
        )

        # Audit log (CRIT-11).
        log_verification_start(
            document_title=document.title or "(untitled)",
            document_hash=document.content_hash or "",
        )

        # Step 1: Create agent instances
        agents_with_kwargs = self._create_agents(external_data)

        # Step 2: Run all agents in parallel
        agent_reports = await self._run_all_agents(
            agents_with_kwargs, document,
        )

        # Step 3: Run orchestrator synthesis
        consolidated_findings = await self._synthesize(document, agent_reports)

        # Step 4: Build the final report
        duration = time.monotonic() - pipeline_start
        report = self._build_report(
            document, agent_reports, consolidated_findings, duration,
        )

        self._logger.info(
            "verification_completed",
            duration_seconds=round(duration, 2),
            agents_completed=report.agents_completed,
            agents_total=report.agents_total,
            total_findings=report.total_findings,
        )

        # Audit log (CRIT-11).
        log_verification_complete(
            report_id=report.id,
            findings_count=report.total_findings,
            duration=round(duration, 2),
        )

        return report

    # ------------------------------------------------------------------
    # Agent creation
    # ------------------------------------------------------------------

    def _create_agents(
        self,
        external_data: dict[str, Any],
    ) -> list[tuple[BaseAgent, dict[str, Any]]]:
        """Instantiate all agents with their role-specific kwargs.

        Returns a list of (agent_instance, kwargs_for_analyze) tuples.
        """
        agents: list[tuple[BaseAgent, dict[str, Any]]] = []

        for role, agent_class in _AGENT_CLASSES.items():
            assignment = self._assignments.get(role)
            if assignment is None:
                self._logger.warning(
                    "no_assignment_for_role",
                    role=role.value,
                )
                continue

            agent = agent_class(
                client=self._client,
                assignment=assignment,
            )

            # Determine role-specific kwargs
            kwargs: dict[str, Any] = {}
            if role == AgentRole.RESEARCH:
                kwargs["api_results"] = external_data.get("api_results", {})
            elif role == AgentRole.NOVELTY_ASSESSMENT:
                kwargs["related_works"] = external_data.get("related_works", [])

            agents.append((agent, kwargs))

        return agents

    # ------------------------------------------------------------------
    # Parallel execution
    # ------------------------------------------------------------------

    async def _run_all_agents(
        self,
        agents_with_kwargs: list[tuple[BaseAgent, dict[str, Any]]],
        document: ParsedDocument,
    ) -> list[AgentReport]:
        """Run all agents in parallel with semaphore protection.

        Uses ``asyncio.gather(return_exceptions=True)`` so that individual
        agent failures do not abort the entire pipeline.
        """
        tasks = []
        for agent, kwargs in agents_with_kwargs:
            tasks.append(self._run_agent(agent, document, **kwargs))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        agent_reports: list[AgentReport] = []
        for (agent, _kwargs), result in zip(agents_with_kwargs, results):
            if isinstance(result, Exception):
                # Create a failure report for exceptions that escaped
                self._logger.error(
                    "agent_exception_escaped",
                    agent=agent.role.value,
                    error=str(result),
                )
                self._record_failure(agent.role.value)
                agent_reports.append(
                    AgentReport(
                        agent_role=agent.role.value,
                        status="failed",
                        error_message=f"{type(result).__name__}: {result}",
                        provider=agent._assignment.provider.value,
                        model=agent._assignment.model,
                    )
                )
            elif isinstance(result, AgentReport):
                if result.status == "failed":
                    self._record_failure(agent.role.value)
                agent_reports.append(result)
            else:
                # Unexpected result type
                self._logger.warning(
                    "unexpected_agent_result_type",
                    agent=agent.role.value,
                    result_type=type(result).__name__,
                )

        return agent_reports

    async def _run_agent(
        self,
        agent: BaseAgent,
        document: ParsedDocument,
        **kwargs: Any,
    ) -> AgentReport:
        """Run a single agent with semaphore protection."""
        # Circuit breaker check
        if agent.role.value in self._disabled_agents:
            self._logger.warning(
                "agent_circuit_breaker_open",
                agent=agent.role.value,
            )
            return AgentReport(
                agent_role=agent.role.value,
                status="disabled",
                error_message="Circuit breaker open: too many consecutive failures",
                provider=agent._assignment.provider.value,
                model=agent._assignment.model,
            )

        async with self._semaphore:
            self._logger.info("agent_starting", agent=agent.role.value)

            if self._progress_callback:
                try:
                    await self._progress_callback(agent.role.value, "running")
                except Exception:  # noqa: BLE001
                    pass  # Never let callback failures affect the pipeline

            report = await agent.analyze(document, **kwargs)

            if self._progress_callback:
                try:
                    await self._progress_callback(agent.role.value, report.status)
                except Exception:  # noqa: BLE001
                    pass

            return report

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _record_failure(self, role_name: str) -> None:
        """Record a failure for circuit breaker tracking."""
        self._failure_counts[role_name] += 1
        if self._failure_counts[role_name] >= _CIRCUIT_BREAKER_THRESHOLD:
            self._disabled_agents.add(role_name)
            self._logger.warning(
                "agent_circuit_breaker_tripped",
                agent=role_name,
                failure_count=self._failure_counts[role_name],
            )

    # ------------------------------------------------------------------
    # Orchestrator synthesis
    # ------------------------------------------------------------------

    async def _synthesize(
        self,
        document: ParsedDocument,
        agent_reports: list[AgentReport],
    ) -> list[Finding]:
        """Run the orchestrator LLM call to deduplicate and synthesise.

        Collects all findings from all agents, sends them to the
        orchestrator agent for deduplication, prioritisation, and
        synthesis, then returns the consolidated findings list.
        """
        # Collect all findings across agents
        all_findings: list[Finding] = []
        for report in agent_reports:
            all_findings.extend(report.findings)

        if not all_findings:
            self._logger.info("no_findings_to_synthesize")
            return []

        # Check if we have an orchestrator assignment
        orch_assignment = self._assignments.get(AgentRole.ORCHESTRATOR)
        if orch_assignment is None:
            self._logger.warning(
                "no_orchestrator_assignment",
                hint="Returning raw findings without synthesis",
            )
            return all_findings

        # Build the orchestrator prompt
        system_prompt, user_template = get_prompts("orchestrator")
        document_summary = create_document_summary(document)
        findings_text = self._format_findings_for_synthesis(all_findings)

        # Escape curly braces to avoid crashes on LaTeX/code (CRIT-1).
        user_msg = user_template.format(
            document_summary=document_summary.replace("{", "{{").replace("}", "}}"),
            all_findings=findings_text.replace("{", "{{").replace("}", "}}"),
        )

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_msg),
        ]

        try:
            # Create a temporary BaseAgent for the orchestrator call
            orch_agent = BaseAgent(
                role=AgentRole.ORCHESTRATOR,
                client=self._client,
                assignment=orch_assignment,
            )
            response = await orch_agent._call_llm(messages)
            consolidated = orch_agent._parse_findings(response)

            self._logger.info(
                "synthesis_completed",
                input_findings=len(all_findings),
                output_findings=len(consolidated),
            )

            return consolidated if consolidated else all_findings

        except Exception as exc:  # noqa: BLE001
            self._logger.error(
                "synthesis_failed",
                error=str(exc),
                hint="Returning raw findings without synthesis",
            )
            return all_findings

    def _format_findings_for_synthesis(
        self,
        findings: list[Finding],
    ) -> str:
        """Format all findings into a text block for the orchestrator prompt."""
        if not findings:
            return "(No findings)"

        lines: list[str] = []
        for i, finding in enumerate(findings, start=1):
            parts = [
                f"Finding #{i}:",
                f"  Agent: {finding.agent_role}",
                f"  Category: {finding.category.value}",
                f"  Severity: {finding.severity.value}",
                f"  Title: {finding.title}",
                f"  Description: {finding.description}",
            ]
            if finding.segment_id:
                parts.append(f"  Location: {finding.segment_id}")
            if finding.segment_text:
                parts.append(f'  Text: "{finding.segment_text}"')
            if finding.suggestion:
                parts.append(f"  Suggestion: {finding.suggestion}")
            if finding.evidence:
                parts.append(f"  Evidence: {'; '.join(finding.evidence)}")
            parts.append(f"  Confidence: {finding.confidence}")
            lines.append("\n".join(parts))

        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # Report building
    # ------------------------------------------------------------------

    def _build_report(
        self,
        document: ParsedDocument,
        agent_reports: list[AgentReport],
        consolidated_findings: list[Finding],
        duration: float,
    ) -> VerificationReport:
        """Build the final :class:`VerificationReport`."""
        # Aggregate token usage
        total_input = sum(
            r.tokens_used.get("input_tokens", 0) for r in agent_reports
        )
        total_output = sum(
            r.tokens_used.get("output_tokens", 0) for r in agent_reports
        )

        # Count completed agents
        completed = sum(1 for r in agent_reports if r.status == "completed")

        # Build the report
        report = VerificationReport(
            document_title=document.title,
            document_hash=document.content_hash,
            agent_reports=agent_reports,
            agents_completed=completed,
            agents_total=len(agent_reports),
            duration_seconds=round(duration, 3),
            total_tokens={
                "input_tokens": total_input,
                "output_tokens": total_output,
            },
        )

        # Replace raw agent findings with consolidated ones to avoid
        # double-counting (CRIT-4).  When the orchestrator produced consolidated
        # findings, clear the per-agent findings and use only the consolidated set.
        if consolidated_findings:
            for ar in report.agent_reports:
                ar.findings = []
            orch_report = AgentReport(
                agent_role=AgentRole.ORCHESTRATOR.value,
                status="completed",
                findings=consolidated_findings,
            )
            report.agent_reports.append(orch_report)

        # Compute summary statistics and generate feedback items
        report.compute_severity_counts()
        report.generate_feedback_items()

        # Generate summary text
        report.summary = self._generate_summary(report)

        return report

    def _generate_summary(self, report: VerificationReport) -> str:
        """Generate a concise text summary for the report."""
        parts: list[str] = []
        parts.append(
            f"Verification completed: {report.agents_completed}/{report.agents_total} "
            f"agents finished successfully."
        )
        parts.append(f"Total findings: {report.total_findings}.")

        if report.severity_counts:
            severity_parts = []
            for severity in ("critical", "major", "minor", "info"):
                count = report.severity_counts.get(severity, 0)
                if count > 0:
                    severity_parts.append(f"{count} {severity}")
            if severity_parts:
                parts.append(f"Breakdown: {', '.join(severity_parts)}.")

        failed_agents = [
            r.agent_role for r in report.agent_reports
            if r.status in ("failed", "disabled")
        ]
        if failed_agents:
            parts.append(
                f"Warning: {len(failed_agents)} agent(s) failed: "
                f"{', '.join(failed_agents)}."
            )

        return " ".join(parts)

"""Verification agents for PaperVerifier.

This package contains the multi-agent verification pipeline:

- :class:`BaseAgent` -- abstract base class with retry, timeout, and logging.
- :class:`SectionStructureAgent` -- structural integrity verification.
- :class:`ClaimVerificationAgent` -- claim support and evidence checking.
- :class:`ReferenceVerificationAgent` -- bibliographic cross-checking.
- :class:`ResultsConsistencyAgent` -- methodology/results/conclusion consistency.
- :class:`NoveltyAssessmentAgent` -- related work overlap analysis.
- :class:`LanguageFlowAgent` -- language quality and writing flow.
- :class:`HallucinationDetectionAgent` -- fabrication and hallucination detection.
- :class:`WriterAgent` -- fix generation for identified findings.
- :class:`AgentOrchestrator` -- parallel agent coordination and synthesis.
"""

from __future__ import annotations

from paperverifier.agents.base import BaseAgent
from paperverifier.agents.claim_verification import ClaimVerificationAgent
from paperverifier.agents.hallucination_detection import HallucinationDetectionAgent
from paperverifier.agents.language_flow import LanguageFlowAgent
from paperverifier.agents.novelty_assessment import NoveltyAssessmentAgent
from paperverifier.agents.orchestrator import AgentOrchestrator
from paperverifier.agents.reference_verification import ReferenceVerificationAgent
from paperverifier.agents.results_consistency import ResultsConsistencyAgent
from paperverifier.agents.section_structure import SectionStructureAgent
from paperverifier.agents.writer import WriterAgent

__all__ = [
    "AgentOrchestrator",
    "BaseAgent",
    "ClaimVerificationAgent",
    "HallucinationDetectionAgent",
    "LanguageFlowAgent",
    "NoveltyAssessmentAgent",
    "ReferenceVerificationAgent",
    "ResultsConsistencyAgent",
    "SectionStructureAgent",
    "WriterAgent",
]

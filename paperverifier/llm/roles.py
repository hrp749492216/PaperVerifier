"""Agent role definitions and default LLM assignments.

Every verification agent has a named :class:`AgentRole`.  Each role is bound
to a specific provider, model, temperature, and token budget via a
:class:`RoleAssignment`.  The :data:`DEFAULT_ASSIGNMENTS` dict provides
sensible defaults that can be overridden through the YAML config store.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from paperverifier.llm.providers import LLMProvider


class AgentRole(str, Enum):
    """Roles played by verification agents."""

    ORCHESTRATOR = "orchestrator"
    RESEARCH = "research"
    CLAIM_VERIFICATION = "claim_verification"
    HALLUCINATION_DETECTION = "hallucination_detection"
    SECTION_STRUCTURE = "section_structure"
    RESULTS_CONSISTENCY = "results_consistency"
    NOVELTY_ASSESSMENT = "novelty_assessment"
    LANGUAGE_FLOW = "language_flow"
    WRITER = "writer"


class RoleAssignment(BaseModel):
    """Binds an agent role to a concrete LLM configuration.

    Attributes:
        provider: The LLM provider to use.
        model: Model identifier within that provider.
        temperature: Sampling temperature (lower = more deterministic).
        max_tokens: Maximum number of tokens in the completion.
    """

    provider: LLMProvider
    model: str
    temperature: float = Field(default=0.3)
    max_tokens: int = Field(default=4096)


DEFAULT_ASSIGNMENTS: dict[AgentRole, RoleAssignment] = {
    AgentRole.ORCHESTRATOR: RoleAssignment(
        provider=LLMProvider.ANTHROPIC,
        model="claude-sonnet-4-20250514",
        temperature=0.2,
        max_tokens=8192,
    ),
    AgentRole.RESEARCH: RoleAssignment(
        provider=LLMProvider.OPENAI,
        model="gpt-4o",
        temperature=0.1,
        max_tokens=4096,
    ),
    AgentRole.CLAIM_VERIFICATION: RoleAssignment(
        provider=LLMProvider.ANTHROPIC,
        model="claude-sonnet-4-20250514",
        temperature=0.1,
        max_tokens=4096,
    ),
    AgentRole.HALLUCINATION_DETECTION: RoleAssignment(
        provider=LLMProvider.ANTHROPIC,
        model="claude-sonnet-4-20250514",
        temperature=0.1,
        max_tokens=4096,
    ),
    AgentRole.SECTION_STRUCTURE: RoleAssignment(
        provider=LLMProvider.ANTHROPIC,
        model="claude-sonnet-4-20250514",
        temperature=0.2,
        max_tokens=4096,
    ),
    AgentRole.RESULTS_CONSISTENCY: RoleAssignment(
        provider=LLMProvider.ANTHROPIC,
        model="claude-sonnet-4-20250514",
        temperature=0.1,
        max_tokens=4096,
    ),
    AgentRole.NOVELTY_ASSESSMENT: RoleAssignment(
        provider=LLMProvider.ANTHROPIC,
        model="claude-sonnet-4-20250514",
        temperature=0.3,
        max_tokens=4096,
    ),
    AgentRole.LANGUAGE_FLOW: RoleAssignment(
        provider=LLMProvider.ANTHROPIC,
        model="claude-sonnet-4-20250514",
        temperature=0.3,
        max_tokens=4096,
    ),
    AgentRole.WRITER: RoleAssignment(
        provider=LLMProvider.ANTHROPIC,
        model="claude-sonnet-4-20250514",
        temperature=0.4,
        max_tokens=8192,
    ),
}

"""LLM provider definitions and registry.

Each supported provider is described by a :class:`ProviderSpec` that captures
which SDK backend to use, the base URL (if non-default), the environment
variable for the API key, and the list of recommended models.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LLMProvider(StrEnum):
    """Supported LLM providers."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GROK = "grok"
    OPENROUTER = "openrouter"
    GEMINI = "gemini"
    MINIMAX = "minimax"
    KIMI = "kimi"
    DEEPSEEK = "deepseek"


class SDKBackend(StrEnum):
    """Which SDK to use for a given provider."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


@dataclass(frozen=True)
class ProviderSpec:
    """Immutable specification for an LLM provider.

    Attributes:
        display_name: Human-readable provider name.
        sdk_backend: Which SDK to use for API calls.
        base_url: Custom base URL; *None* means the SDK default.
        env_var: Environment variable name for the API key.
        default_models: Recommended model identifiers, ordered by preference.
    """

    display_name: str
    sdk_backend: SDKBackend
    base_url: str | None
    env_var: str
    default_models: tuple[str, ...]


PROVIDER_REGISTRY: dict[LLMProvider, ProviderSpec] = {
    LLMProvider.ANTHROPIC: ProviderSpec(
        display_name="Anthropic",
        sdk_backend=SDKBackend.ANTHROPIC,
        base_url=None,
        env_var="ANTHROPIC_API_KEY",
        default_models=(
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "claude-haiku-4-20250506",
        ),
    ),
    LLMProvider.OPENAI: ProviderSpec(
        display_name="OpenAI",
        sdk_backend=SDKBackend.OPENAI,
        base_url=None,
        env_var="OPENAI_API_KEY",
        default_models=("gpt-4o", "gpt-4o-mini", "o3-mini"),
    ),
    LLMProvider.GROK: ProviderSpec(
        display_name="Grok",
        sdk_backend=SDKBackend.OPENAI,
        base_url="https://api.x.ai/v1",
        env_var="GROK_API_KEY",
        default_models=("grok-3", "grok-3-mini"),
    ),
    LLMProvider.OPENROUTER: ProviderSpec(
        display_name="OpenRouter",
        sdk_backend=SDKBackend.OPENAI,
        base_url="https://openrouter.ai/api/v1",
        env_var="OPENROUTER_API_KEY",
        default_models=("anthropic/claude-sonnet-4", "openai/gpt-4o"),
    ),
    LLMProvider.GEMINI: ProviderSpec(
        display_name="Gemini",
        sdk_backend=SDKBackend.OPENAI,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        env_var="GEMINI_API_KEY",
        default_models=("gemini-2.5-pro", "gemini-2.5-flash"),
    ),
    LLMProvider.MINIMAX: ProviderSpec(
        display_name="MiniMax",
        sdk_backend=SDKBackend.OPENAI,
        base_url="https://api.minimax.chat/v1",
        env_var="MINIMAX_API_KEY",
        default_models=("MiniMax-Text-01",),
    ),
    LLMProvider.KIMI: ProviderSpec(
        display_name="Kimi",
        sdk_backend=SDKBackend.OPENAI,
        base_url="https://api.moonshot.cn/v1",
        env_var="KIMI_API_KEY",
        default_models=("moonshot-v1-auto",),
    ),
    LLMProvider.DEEPSEEK: ProviderSpec(
        display_name="DeepSeek",
        sdk_backend=SDKBackend.OPENAI,
        base_url="https://api.deepseek.com",
        env_var="DEEPSEEK_API_KEY",
        default_models=("deepseek-chat", "deepseek-reasoner"),
    ),
}

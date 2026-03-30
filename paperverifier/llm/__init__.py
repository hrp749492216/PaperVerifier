"""LLM provider abstraction layer for PaperVerifier.

This package provides a unified async interface for calling multiple LLM
providers (Anthropic, OpenAI, Grok, OpenRouter, Gemini, MiniMax, Kimi,
DeepSeek).  API keys are stored securely via the OS keyring -- never in
plaintext config files.

Quick start::

    from paperverifier.llm import UnifiedLLMClient, LLMProvider, Message

    client = UnifiedLLMClient()
    response = await client.complete(
        [Message(role="user", content="Summarise this paper.")],
        provider=LLMProvider.ANTHROPIC,
        model="claude-sonnet-4-20250514",
    )
    print(response.content)
"""

from __future__ import annotations

from paperverifier.llm.client import LLMResponse, Message, UnifiedLLMClient
from paperverifier.llm.config_store import (
    delete_api_key,
    get_api_key,
    list_configured_providers,
    load_role_assignments,
    save_role_assignments,
    store_api_key,
)
from paperverifier.llm.exceptions import (
    LLMAuthError,
    LLMContextLengthError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from paperverifier.llm.providers import (
    PROVIDER_REGISTRY,
    LLMProvider,
    ProviderSpec,
    SDKBackend,
)
from paperverifier.llm.roles import (
    DEFAULT_ASSIGNMENTS,
    AgentRole,
    RoleAssignment,
)

__all__ = [
    # Client
    "UnifiedLLMClient",
    "Message",
    "LLMResponse",
    # Providers
    "LLMProvider",
    "SDKBackend",
    "ProviderSpec",
    "PROVIDER_REGISTRY",
    # Roles
    "AgentRole",
    "RoleAssignment",
    "DEFAULT_ASSIGNMENTS",
    # Exceptions
    "LLMError",
    "LLMAuthError",
    "LLMRateLimitError",
    "LLMContextLengthError",
    "LLMTimeoutError",
    "LLMResponseError",
    # Config
    "load_role_assignments",
    "save_role_assignments",
    "store_api_key",
    "get_api_key",
    "delete_api_key",
    "list_configured_providers",
]

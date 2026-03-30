"""Unified exception hierarchy for LLM provider errors.

All provider-specific exceptions (Anthropic, OpenAI, etc.) are caught and
re-raised as one of these types so callers never depend on SDK internals.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base exception for all LLM-related errors."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.llm_message = message
        super().__init__(self._format(message))

    def _format(self, message: str) -> str:
        parts: list[str] = []
        if self.provider:
            parts.append(f"[{self.provider}]")
        if self.model:
            parts.append(f"({self.model})")
        parts.append(message)
        return " ".join(parts)


class LLMAuthError(LLMError):
    """Raised when an API key is invalid, missing, or revoked."""


class LLMRateLimitError(LLMError):
    """Raised when a provider rate-limits the request.

    Attributes:
        retry_after: Suggested wait time in seconds, if the provider
            included a ``Retry-After`` header.  *None* otherwise.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(message, provider=provider, model=model)


class LLMContextLengthError(LLMError):
    """Raised when the input exceeds the model's context window.

    Attributes:
        max_tokens: The model's maximum context length (if known).
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.max_tokens = max_tokens
        super().__init__(message, provider=provider, model=model)


class LLMTimeoutError(LLMError):
    """Raised when a request exceeds the configured per-call timeout."""


class LLMResponseError(LLMError):
    """Raised when the provider returns an unexpected response format."""

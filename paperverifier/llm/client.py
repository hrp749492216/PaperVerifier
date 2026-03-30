"""Unified async LLM client with multi-provider support.

:class:`UnifiedLLMClient` is the single entry-point for all LLM calls in the
application.  It resolves API keys (runtime dict -> OS keyring -> env var),
dispatches to the correct SDK backend, enforces per-call timeouts, and
translates every provider-specific exception into the unified hierarchy
defined in :mod:`paperverifier.llm.exceptions`.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

import structlog

from paperverifier.audit import log_api_key_access
from paperverifier.llm.exceptions import (
    LLMAuthError,
    LLMContextLengthError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from paperverifier.llm.providers import (
    PROVIDER_REGISTRY,
    LLMProvider,
    SDKBackend,
)
from paperverifier.llm.roles import RoleAssignment

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Message:
    """A single chat message.

    Attributes:
        role: One of ``"system"``, ``"user"``, or ``"assistant"``.
        content: The text content of the message.
    """

    role: str
    content: str


@dataclass(frozen=True)
class LLMResponse:
    """Normalised response from any LLM provider.

    Attributes:
        content: The generated text.
        model: The model that produced the response.
        provider: The provider that served the request.
        usage: Token counts with keys ``input_tokens`` and ``output_tokens``.
    """

    content: str
    model: str
    provider: LLMProvider
    usage: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class UnifiedLLMClient:
    """Async LLM client that abstracts over multiple providers.

    Usage::

        client = UnifiedLLMClient()
        client.set_api_key(LLMProvider.ANTHROPIC, "sk-ant-...")
        response = await client.complete(
            [Message(role="user", content="Hello!")],
            provider=LLMProvider.ANTHROPIC,
            model="claude-sonnet-4-20250514",
        )
    """

    def __init__(self) -> None:
        self._api_keys: dict[LLMProvider, str] = {}
        self._openai_clients: dict[tuple[str | None, str], Any] = {}
        self._anthropic_clients: dict[str, Any] = {}  # Cached by api_key hash (HIGH-I1)

    # -- API key resolution ------------------------------------------------

    def resolve_api_key(self, provider: LLMProvider) -> str:
        """Resolve an API key for *provider* using a three-tier lookup.

        Priority order:
        1. Runtime dict (set via :meth:`set_api_key`).
        2. OS keyring (``paperverifier`` service).
        3. Environment variable defined in :data:`PROVIDER_REGISTRY`.

        Raises:
            LLMAuthError: If no key is found in any tier.
        """
        # 1. Runtime cache
        if key := self._api_keys.get(provider):
            return key

        # 2. OS keyring
        try:
            import keyring  # noqa: PLC0415

            stored = keyring.get_password("paperverifier", provider.value)
            if stored:
                self._api_keys[provider] = stored
                return stored
        except Exception:  # noqa: BLE001
            logger.debug("keyring_lookup_failed", provider=provider.value)

        # 3. Environment variable
        spec = PROVIDER_REGISTRY[provider]
        env_val = os.environ.get(spec.env_var)
        if env_val:
            self._api_keys[provider] = env_val
            return env_val

        raise LLMAuthError(
            f"No API key found for {provider.value}. "
            f"Set it via `set_api_key()`, the OS keyring, or ${spec.env_var}.",
            provider=provider.value,
        )

    def set_api_key(
        self, provider: LLMProvider, key: str, *, persist: bool = True,
    ) -> None:
        """Store an API key in the runtime cache, optionally persisting to keyring.

        Parameters
        ----------
        provider:
            The LLM provider to set the key for.
        key:
            The API key value.
        persist:
            If ``True`` (default), also write the key to the OS keyring.
            Set to ``False`` for ephemeral use (e.g. connection tests)
            so a previously saved key is not overwritten.
        """
        self._api_keys[provider] = key
        log_api_key_access(provider.value, "write")
        if not persist:
            logger.debug("api_key_set_memory_only", provider=provider.value)
            return
        try:
            import keyring  # noqa: PLC0415

            keyring.set_password("paperverifier", provider.value, key)
            logger.info("api_key_stored_in_keyring", provider=provider.value)
        except Exception:  # noqa: BLE001
            logger.warning(
                "keyring_store_failed",
                provider=provider.value,
                hint="Key saved to runtime cache only; will not persist across restarts.",
            )

    # -- OpenAI client cache -----------------------------------------------

    def _get_openai_client(self, base_url: str | None, api_key: str) -> Any:
        """Return a cached :class:`openai.AsyncOpenAI` client."""
        # Use hash of API key rather than raw key as cache key (MED-S2).
        cache_key = (base_url, str(hash(api_key)))
        if cache_key not in self._openai_clients:
            import openai  # noqa: PLC0415

            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url is not None:
                kwargs["base_url"] = base_url
            self._openai_clients[cache_key] = openai.AsyncOpenAI(**kwargs)
        return self._openai_clients[cache_key]

    # -- Anthropic backend -------------------------------------------------

    async def _complete_anthropic(
        self,
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
        api_key: str,
    ) -> LLMResponse:
        """Send a completion request via the Anthropic SDK."""
        import anthropic  # noqa: PLC0415

        # Separate system prompt from conversation messages.
        system_prompt: str | anthropic.NotGiven = anthropic.NOT_GIVEN
        chat_messages: list[dict[str, str]] = []
        for msg in messages:
            if msg.role == "system":
                system_prompt = msg.content
            else:
                chat_messages.append({"role": msg.role, "content": msg.content})

        # Cache Anthropic clients for connection reuse (HIGH-I1).
        cache_key = str(hash(api_key))
        if cache_key not in self._anthropic_clients:
            self._anthropic_clients[cache_key] = anthropic.AsyncAnthropic(api_key=api_key)
        client = self._anthropic_clients[cache_key]
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=chat_messages,
            )
        except anthropic.AuthenticationError as exc:
            raise LLMAuthError(
                str(exc),
                provider=LLMProvider.ANTHROPIC.value,
                model=model,
            ) from exc
        except anthropic.RateLimitError as exc:
            retry_after = _parse_retry_after(getattr(exc, "response", None))
            raise LLMRateLimitError(
                str(exc),
                provider=LLMProvider.ANTHROPIC.value,
                model=model,
                retry_after=retry_after,
            ) from exc
        except anthropic.BadRequestError as exc:
            msg_lower = str(exc).lower()
            if "token" in msg_lower or "context" in msg_lower or "too long" in msg_lower:
                raise LLMContextLengthError(
                    str(exc),
                    provider=LLMProvider.ANTHROPIC.value,
                    model=model,
                ) from exc
            raise LLMResponseError(
                str(exc),
                provider=LLMProvider.ANTHROPIC.value,
                model=model,
            ) from exc
        except anthropic.APIError as exc:
            raise LLMResponseError(
                str(exc),
                provider=LLMProvider.ANTHROPIC.value,
                model=model,
            ) from exc

        # Extract text from content blocks.
        text_parts: list[str] = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)

        if not text_parts:
            raise LLMResponseError(
                "Anthropic returned an empty response.",
                provider=LLMProvider.ANTHROPIC.value,
                model=model,
            )

        return LLMResponse(
            content="\n".join(text_parts),
            model=response.model,
            provider=LLMProvider.ANTHROPIC,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        )

    # -- OpenAI-compatible backend -----------------------------------------

    async def _complete_openai(
        self,
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
        base_url: str | None,
        api_key: str,
        provider: LLMProvider,
    ) -> LLMResponse:
        """Send a completion request via the OpenAI SDK (or compatible)."""
        import openai  # noqa: PLC0415

        client = self._get_openai_client(base_url, api_key)
        oai_messages = [{"role": m.role, "content": m.content} for m in messages]

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=oai_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except openai.AuthenticationError as exc:
            raise LLMAuthError(
                str(exc),
                provider=provider.value,
                model=model,
            ) from exc
        except openai.RateLimitError as exc:
            retry_after = _parse_retry_after(getattr(exc, "response", None))
            raise LLMRateLimitError(
                str(exc),
                provider=provider.value,
                model=model,
                retry_after=retry_after,
            ) from exc
        except openai.BadRequestError as exc:
            msg_lower = str(exc).lower()
            if "token" in msg_lower or "context" in msg_lower or "length" in msg_lower:
                raise LLMContextLengthError(
                    str(exc),
                    provider=provider.value,
                    model=model,
                ) from exc
            raise LLMResponseError(
                str(exc),
                provider=provider.value,
                model=model,
            ) from exc
        except openai.APIError as exc:
            raise LLMResponseError(
                str(exc),
                provider=provider.value,
                model=model,
            ) from exc

        choice = response.choices[0] if response.choices else None
        if choice is None or choice.message is None or choice.message.content is None:
            raise LLMResponseError(
                "Provider returned an empty response.",
                provider=provider.value,
                model=model,
            )

        usage_dict: dict[str, int] = {}
        if response.usage is not None:
            usage_dict = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens or 0,
            }

        return LLMResponse(
            content=choice.message.content,
            model=response.model or model,
            provider=provider,
            usage=usage_dict,
        )

    # -- Public API --------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        *,
        provider: LLMProvider,
        model: str,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: float = 120.0,
    ) -> LLMResponse:
        """Send a chat completion request to any supported provider.

        The call is wrapped in :func:`asyncio.wait_for` so a per-call
        *timeout* is always enforced (guards against H12 / hung connections).

        Args:
            messages: Conversation history.
            provider: Target LLM provider.
            model: Model identifier.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in the completion.
            timeout: Per-call timeout in seconds.

        Returns:
            A normalised :class:`LLMResponse`.

        Raises:
            LLMAuthError: API key missing or invalid.
            LLMRateLimitError: Provider rate limit hit.
            LLMContextLengthError: Input too long for model.
            LLMTimeoutError: Request exceeded *timeout*.
            LLMResponseError: Unexpected provider response.
        """
        api_key = self.resolve_api_key(provider)
        spec = PROVIDER_REGISTRY[provider]

        logger.debug(
            "llm_request",
            provider=provider.value,
            model=model,
            message_count=len(messages),
            temperature=temperature,
            max_tokens=max_tokens,
        )

        try:
            if spec.sdk_backend == SDKBackend.ANTHROPIC:
                coro = self._complete_anthropic(
                    messages, model, temperature, max_tokens, api_key,
                )
            else:
                coro = self._complete_openai(
                    messages, model, temperature, max_tokens,
                    spec.base_url, api_key, provider,
                )
            response = await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise LLMTimeoutError(
                f"Request timed out after {timeout}s.",
                provider=provider.value,
                model=model,
            ) from exc

        logger.info(
            "llm_response",
            provider=provider.value,
            model=response.model,
            input_tokens=response.usage.get("input_tokens"),
            output_tokens=response.usage.get("output_tokens"),
        )
        return response

    async def complete_for_role(
        self,
        messages: list[Message],
        assignment: RoleAssignment,
        *,
        timeout: float = 120.0,
    ) -> LLMResponse:
        """Convenience wrapper that extracts fields from a :class:`RoleAssignment`.

        Args:
            messages: Conversation history.
            assignment: The role assignment containing provider, model, etc.
            timeout: Per-call timeout in seconds.

        Returns:
            A normalised :class:`LLMResponse`.
        """
        return await self.complete(
            messages,
            provider=assignment.provider,
            model=assignment.model,
            temperature=assignment.temperature,
            max_tokens=assignment.max_tokens,
            timeout=timeout,
        )

    async def test_connection(
        self,
        provider: LLMProvider,
        model: str | None = None,
    ) -> bool:
        """Send a lightweight test message and return *True* on success.

        If *model* is ``None`` the first default model for the provider is
        used.  Exceptions are caught and logged; only a boolean is returned.
        """
        if model is None:
            spec = PROVIDER_REGISTRY[provider]
            model = spec.default_models[0]

        try:
            await self.complete(
                [Message(role="user", content="Hello")],
                provider=provider,
                model=model,
                max_tokens=16,
                timeout=30.0,
            )
        except Exception:  # noqa: BLE001
            logger.warning("connection_test_failed", provider=provider.value, model=model)
            return False
        else:
            logger.info("connection_test_passed", provider=provider.value, model=model)
            return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_retry_after(response: Any) -> float | None:
    """Extract a ``Retry-After`` value from an HTTP response, if present."""
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None

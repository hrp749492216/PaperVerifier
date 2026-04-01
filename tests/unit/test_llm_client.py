"""Unit tests for paperverifier.llm.client."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paperverifier.llm.client import (
    LLMResponse,
    Message,
    UnifiedLLMClient,
    _is_context_length_error,
    _parse_retry_after,
)
from paperverifier.llm.exceptions import (
    LLMAuthError,
    LLMContextLengthError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from paperverifier.llm.providers import LLMProvider
from paperverifier.llm.roles import RoleAssignment

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> UnifiedLLMClient:
    """Return a fresh UnifiedLLMClient with no keys configured."""
    return UnifiedLLMClient()


@pytest.fixture
def keyed_client() -> UnifiedLLMClient:
    """Return a client with Anthropic and OpenAI keys pre-loaded."""
    c = UnifiedLLMClient()
    c._api_keys[LLMProvider.ANTHROPIC] = "sk-ant-test"
    c._api_keys[LLMProvider.OPENAI] = "sk-openai-test"
    return c


# ---------------------------------------------------------------------------
# resolve_api_key
# ---------------------------------------------------------------------------


class TestResolveApiKey:
    """Tests for UnifiedLLMClient.resolve_api_key three-tier lookup."""

    def test_tier1_runtime_key(self, client: UnifiedLLMClient) -> None:
        """Runtime dict (tier 1) is returned first."""
        client._api_keys[LLMProvider.ANTHROPIC] = "runtime-key"
        assert client.resolve_api_key(LLMProvider.ANTHROPIC) == "runtime-key"

    def test_tier2_keyring(self, client: UnifiedLLMClient) -> None:
        """OS keyring (tier 2) is used when runtime key is absent."""
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "keyring-key"
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            key = client.resolve_api_key(LLMProvider.ANTHROPIC)
        assert key == "keyring-key"
        # Should also cache the key for future lookups.
        assert client._api_keys[LLMProvider.ANTHROPIC] == "keyring-key"

    def test_tier2_keyring_via_import(self, client: UnifiedLLMClient) -> None:
        """OS keyring (tier 2) is used when runtime key is absent (import-based mock)."""
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "keyring-key-2"
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            key = client.resolve_api_key(LLMProvider.OPENAI)
        assert key == "keyring-key-2"
        assert client._api_keys[LLMProvider.OPENAI] == "keyring-key-2"

    def test_tier3_env_var(self, client: UnifiedLLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variable (tier 3) is used as last resort."""
        # Ensure keyring fails so we fall through.
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = None
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            key = client.resolve_api_key(LLMProvider.ANTHROPIC)
        assert key == "env-key"
        assert client._api_keys[LLMProvider.ANTHROPIC] == "env-key"

    def test_raises_auth_error_when_no_key(
        self, client: UnifiedLLMClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LLMAuthError is raised when all three tiers fail."""
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = None
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            with pytest.raises(LLMAuthError, match="No API key found"):
                client.resolve_api_key(LLMProvider.ANTHROPIC)

    def test_keyring_exception_falls_through(
        self, client: UnifiedLLMClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If keyring raises an exception, we fall through to env var."""
        mock_keyring = MagicMock()
        mock_keyring.get_password.side_effect = RuntimeError("keyring broken")
        monkeypatch.setenv("OPENAI_API_KEY", "env-fallback")
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            key = client.resolve_api_key(LLMProvider.OPENAI)
        assert key == "env-fallback"


# ---------------------------------------------------------------------------
# set_api_key
# ---------------------------------------------------------------------------


class TestSetApiKey:
    """Tests for UnifiedLLMClient.set_api_key with and without persist."""

    @patch("paperverifier.llm.client.log_api_key_access")
    def test_set_key_persists_to_keyring(
        self, mock_audit: MagicMock, client: UnifiedLLMClient
    ) -> None:
        """Default persist=True writes to both runtime dict and keyring."""
        mock_keyring = MagicMock()
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            client.set_api_key(LLMProvider.ANTHROPIC, "new-key")
        assert client._api_keys[LLMProvider.ANTHROPIC] == "new-key"
        mock_keyring.set_password.assert_called_once_with("paperverifier", "anthropic", "new-key")
        mock_audit.assert_called_once_with("anthropic", "write")

    @patch("paperverifier.llm.client.log_api_key_access")
    def test_set_key_no_persist(self, mock_audit: MagicMock, client: UnifiedLLMClient) -> None:
        """persist=False only sets the runtime dict, not the keyring."""
        mock_keyring = MagicMock()
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            client.set_api_key(LLMProvider.OPENAI, "ephemeral-key", persist=False)
        assert client._api_keys[LLMProvider.OPENAI] == "ephemeral-key"
        mock_keyring.set_password.assert_not_called()

    @patch("paperverifier.llm.client.log_api_key_access")
    def test_set_key_keyring_failure(self, mock_audit: MagicMock, client: UnifiedLLMClient) -> None:
        """Keyring write failure is swallowed; key remains in runtime cache."""
        mock_keyring = MagicMock()
        mock_keyring.set_password.side_effect = RuntimeError("keyring broken")
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            client.set_api_key(LLMProvider.ANTHROPIC, "survive-key")
        assert client._api_keys[LLMProvider.ANTHROPIC] == "survive-key"


# ---------------------------------------------------------------------------
# _complete_anthropic
# ---------------------------------------------------------------------------


def _make_anthropic_response(
    text: str = "Hello!", model: str = "claude-sonnet-4-20250514"
) -> SimpleNamespace:
    """Build a fake Anthropic Messages response."""
    block = SimpleNamespace(text=text)
    usage = SimpleNamespace(input_tokens=10, output_tokens=5)
    return SimpleNamespace(content=[block], model=model, usage=usage)


class TestCompleteAnthropic:
    """Tests for the Anthropic SDK backend."""

    async def test_success(self, keyed_client: UnifiedLLMClient) -> None:
        """Successful Anthropic completion returns an LLMResponse."""
        mock_resp = _make_anthropic_response("Hi there")
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_resp)

        mock_anthropic = MagicMock()
        mock_anthropic.NOT_GIVEN = object()
        mock_anthropic.AsyncAnthropic.return_value = mock_client

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            result = await keyed_client._complete_anthropic(
                [Message(role="user", content="Hello")],
                model="claude-sonnet-4-20250514",
                temperature=0.3,
                max_tokens=1024,
                api_key="sk-ant-test",
            )

        assert isinstance(result, LLMResponse)
        assert result.content == "Hi there"
        assert result.provider == LLMProvider.ANTHROPIC
        assert result.usage["input_tokens"] == 10
        assert result.usage["output_tokens"] == 5

    async def test_system_message_separated(self, keyed_client: UnifiedLLMClient) -> None:
        """System messages are extracted from the message list."""
        mock_resp = _make_anthropic_response("ok")
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_resp)

        mock_anthropic = MagicMock()
        mock_anthropic.NOT_GIVEN = "NOT_GIVEN_SENTINEL"
        mock_anthropic.AsyncAnthropic.return_value = mock_client

        msgs = [
            Message(role="system", content="You are helpful"),
            Message(role="user", content="Hi"),
        ]

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            await keyed_client._complete_anthropic(
                msgs,
                model="claude-sonnet-4-20250514",
                temperature=0.3,
                max_tokens=1024,
                api_key="sk-ant-test",
            )

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["system"] == "You are helpful"
        assert call_kwargs["messages"] == [{"role": "user", "content": "Hi"}]

    async def test_auth_error(self, keyed_client: UnifiedLLMClient) -> None:
        """AuthenticationError is translated to LLMAuthError."""
        import anthropic as real_anthropic

        mock_client = AsyncMock()
        exc = real_anthropic.AuthenticationError(
            message="invalid api key",
            response=MagicMock(status_code=401),
            body={"error": {"message": "invalid api key"}},
        )
        mock_client.messages.create = AsyncMock(side_effect=exc)

        mock_anthropic = MagicMock()
        mock_anthropic.NOT_GIVEN = real_anthropic.NOT_GIVEN
        mock_anthropic.AsyncAnthropic.return_value = mock_client
        mock_anthropic.AuthenticationError = real_anthropic.AuthenticationError
        mock_anthropic.RateLimitError = real_anthropic.RateLimitError
        mock_anthropic.BadRequestError = real_anthropic.BadRequestError
        mock_anthropic.APIError = real_anthropic.APIError

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            with pytest.raises(LLMAuthError):
                await keyed_client._complete_anthropic(
                    [Message(role="user", content="Hi")],
                    model="claude-sonnet-4-20250514",
                    temperature=0.3,
                    max_tokens=1024,
                    api_key="sk-ant-test",
                )

    async def test_rate_limit_error(self, keyed_client: UnifiedLLMClient) -> None:
        """RateLimitError is translated to LLMRateLimitError."""
        import anthropic as real_anthropic

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"retry-after": "30"}

        mock_client = AsyncMock()
        exc = real_anthropic.RateLimitError(
            message="rate limited",
            response=mock_response,
            body={"error": {"message": "rate limited"}},
        )
        mock_client.messages.create = AsyncMock(side_effect=exc)

        mock_anthropic = MagicMock()
        mock_anthropic.NOT_GIVEN = real_anthropic.NOT_GIVEN
        mock_anthropic.AsyncAnthropic.return_value = mock_client
        mock_anthropic.AuthenticationError = real_anthropic.AuthenticationError
        mock_anthropic.RateLimitError = real_anthropic.RateLimitError
        mock_anthropic.BadRequestError = real_anthropic.BadRequestError
        mock_anthropic.APIError = real_anthropic.APIError

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            with pytest.raises(LLMRateLimitError) as exc_info:
                await keyed_client._complete_anthropic(
                    [Message(role="user", content="Hi")],
                    model="claude-sonnet-4-20250514",
                    temperature=0.3,
                    max_tokens=1024,
                    api_key="sk-ant-test",
                )
        assert exc_info.value.retry_after == 30.0

    async def test_context_length_error(self, keyed_client: UnifiedLLMClient) -> None:
        """BadRequestError with context length keywords raises LLMContextLengthError."""
        import anthropic as real_anthropic

        mock_client = AsyncMock()
        exc = real_anthropic.BadRequestError(
            message="prompt is too long for this model",
            response=MagicMock(status_code=400),
            body={"error": {"message": "prompt is too long for this model"}},
        )
        mock_client.messages.create = AsyncMock(side_effect=exc)

        mock_anthropic = MagicMock()
        mock_anthropic.NOT_GIVEN = real_anthropic.NOT_GIVEN
        mock_anthropic.AsyncAnthropic.return_value = mock_client
        mock_anthropic.AuthenticationError = real_anthropic.AuthenticationError
        mock_anthropic.RateLimitError = real_anthropic.RateLimitError
        mock_anthropic.BadRequestError = real_anthropic.BadRequestError
        mock_anthropic.APIError = real_anthropic.APIError

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            with pytest.raises(LLMContextLengthError):
                await keyed_client._complete_anthropic(
                    [Message(role="user", content="Hi")],
                    model="claude-sonnet-4-20250514",
                    temperature=0.3,
                    max_tokens=1024,
                    api_key="sk-ant-test",
                )

    async def test_empty_response(self, keyed_client: UnifiedLLMClient) -> None:
        """Empty content blocks raise LLMResponseError."""
        mock_resp = SimpleNamespace(
            content=[SimpleNamespace(type="tool_use")],  # no .text attribute
            model="claude-sonnet-4-20250514",
            usage=SimpleNamespace(input_tokens=10, output_tokens=0),
        )
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_resp)

        mock_anthropic = MagicMock()
        mock_anthropic.NOT_GIVEN = object()
        mock_anthropic.AsyncAnthropic.return_value = mock_client

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            with pytest.raises(LLMResponseError, match="empty response"):
                await keyed_client._complete_anthropic(
                    [Message(role="user", content="Hi")],
                    model="claude-sonnet-4-20250514",
                    temperature=0.3,
                    max_tokens=1024,
                    api_key="sk-ant-test",
                )

    async def test_multiple_text_blocks_joined(self, keyed_client: UnifiedLLMClient) -> None:
        """Multiple text blocks are joined with newlines."""
        block1 = SimpleNamespace(text="First")
        block2 = SimpleNamespace(text="Second")
        mock_resp = SimpleNamespace(
            content=[block1, block2],
            model="claude-sonnet-4-20250514",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_resp)

        mock_anthropic = MagicMock()
        mock_anthropic.NOT_GIVEN = object()
        mock_anthropic.AsyncAnthropic.return_value = mock_client

        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            result = await keyed_client._complete_anthropic(
                [Message(role="user", content="Hi")],
                model="claude-sonnet-4-20250514",
                temperature=0.3,
                max_tokens=1024,
                api_key="sk-ant-test",
            )

        assert result.content == "First\nSecond"


# ---------------------------------------------------------------------------
# _complete_openai
# ---------------------------------------------------------------------------


def _make_openai_response(
    content: str = "Hello!",
    model: str = "gpt-4o",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> SimpleNamespace:
    """Build a fake OpenAI ChatCompletion response."""
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[choice], model=model, usage=usage)


class TestCompleteOpenai:
    """Tests for the OpenAI SDK backend."""

    async def test_success(self, keyed_client: UnifiedLLMClient) -> None:
        """Successful OpenAI completion returns an LLMResponse."""
        mock_resp = _make_openai_response("Hi there", model="gpt-4o")
        mock_async_client = AsyncMock()
        mock_async_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI.return_value = mock_async_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            result = await keyed_client._complete_openai(
                [Message(role="user", content="Hello")],
                model="gpt-4o",
                temperature=0.3,
                max_tokens=1024,
                base_url=None,
                api_key="sk-openai-test",
                provider=LLMProvider.OPENAI,
            )

        assert isinstance(result, LLMResponse)
        assert result.content == "Hi there"
        assert result.provider == LLMProvider.OPENAI
        assert result.usage["input_tokens"] == 10
        assert result.usage["output_tokens"] == 5

    async def test_auth_error(self, keyed_client: UnifiedLLMClient) -> None:
        """AuthenticationError is translated to LLMAuthError."""
        import openai as real_openai

        mock_async_client = AsyncMock()
        exc = real_openai.AuthenticationError(
            message="invalid api key",
            response=MagicMock(status_code=401),
            body={"error": {"message": "invalid api key"}},
        )
        mock_async_client.chat.completions.create = AsyncMock(side_effect=exc)

        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI.return_value = mock_async_client
        mock_openai.AuthenticationError = real_openai.AuthenticationError
        mock_openai.RateLimitError = real_openai.RateLimitError
        mock_openai.BadRequestError = real_openai.BadRequestError
        mock_openai.APIError = real_openai.APIError

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with pytest.raises(LLMAuthError):
                await keyed_client._complete_openai(
                    [Message(role="user", content="Hi")],
                    model="gpt-4o",
                    temperature=0.3,
                    max_tokens=1024,
                    base_url=None,
                    api_key="sk-openai-test",
                    provider=LLMProvider.OPENAI,
                )

    async def test_rate_limit_error(self, keyed_client: UnifiedLLMClient) -> None:
        """RateLimitError is translated to LLMRateLimitError."""
        import openai as real_openai

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "60"}

        mock_async_client = AsyncMock()
        exc = real_openai.RateLimitError(
            message="rate limited",
            response=mock_response,
            body={"error": {"message": "rate limited"}},
        )
        mock_async_client.chat.completions.create = AsyncMock(side_effect=exc)

        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI.return_value = mock_async_client
        mock_openai.AuthenticationError = real_openai.AuthenticationError
        mock_openai.RateLimitError = real_openai.RateLimitError
        mock_openai.BadRequestError = real_openai.BadRequestError
        mock_openai.APIError = real_openai.APIError

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with pytest.raises(LLMRateLimitError) as exc_info:
                await keyed_client._complete_openai(
                    [Message(role="user", content="Hi")],
                    model="gpt-4o",
                    temperature=0.3,
                    max_tokens=1024,
                    base_url=None,
                    api_key="sk-openai-test",
                    provider=LLMProvider.OPENAI,
                )
        assert exc_info.value.retry_after == 60.0

    async def test_context_length_error_via_code(self, keyed_client: UnifiedLLMClient) -> None:
        """BadRequestError with code 'context_length_exceeded' raises LLMContextLengthError."""
        import openai as real_openai

        mock_async_client = AsyncMock()
        exc = real_openai.BadRequestError(
            message="maximum context length exceeded",
            response=MagicMock(status_code=400),
            body={
                "error": {
                    "message": "maximum context length exceeded",
                    "code": "context_length_exceeded",
                }
            },
        )
        exc.code = "context_length_exceeded"
        mock_async_client.chat.completions.create = AsyncMock(side_effect=exc)

        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI.return_value = mock_async_client
        mock_openai.AuthenticationError = real_openai.AuthenticationError
        mock_openai.RateLimitError = real_openai.RateLimitError
        mock_openai.BadRequestError = real_openai.BadRequestError
        mock_openai.APIError = real_openai.APIError

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with pytest.raises(LLMContextLengthError):
                await keyed_client._complete_openai(
                    [Message(role="user", content="Hi")],
                    model="gpt-4o",
                    temperature=0.3,
                    max_tokens=1024,
                    base_url=None,
                    api_key="sk-openai-test",
                    provider=LLMProvider.OPENAI,
                )

    async def test_empty_response_no_choices(self, keyed_client: UnifiedLLMClient) -> None:
        """Empty choices list raises LLMResponseError."""
        mock_resp = SimpleNamespace(choices=[], model="gpt-4o", usage=None)
        mock_async_client = AsyncMock()
        mock_async_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI.return_value = mock_async_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with pytest.raises(LLMResponseError, match="empty response"):
                await keyed_client._complete_openai(
                    [Message(role="user", content="Hi")],
                    model="gpt-4o",
                    temperature=0.3,
                    max_tokens=1024,
                    base_url=None,
                    api_key="sk-openai-test",
                    provider=LLMProvider.OPENAI,
                )

    async def test_empty_response_none_content(self, keyed_client: UnifiedLLMClient) -> None:
        """None message content raises LLMResponseError."""
        message = SimpleNamespace(content=None)
        choice = SimpleNamespace(message=message)
        mock_resp = SimpleNamespace(choices=[choice], model="gpt-4o", usage=None)
        mock_async_client = AsyncMock()
        mock_async_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI.return_value = mock_async_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with pytest.raises(LLMResponseError, match="empty response"):
                await keyed_client._complete_openai(
                    [Message(role="user", content="Hi")],
                    model="gpt-4o",
                    temperature=0.3,
                    max_tokens=1024,
                    base_url=None,
                    api_key="sk-openai-test",
                    provider=LLMProvider.OPENAI,
                )

    async def test_o_series_model_uses_developer_role(self, keyed_client: UnifiedLLMClient) -> None:
        """O-series models map 'system' to 'developer' and use max_completion_tokens."""
        mock_resp = _make_openai_response("reasoning result", model="o3-mini")
        mock_async_client = AsyncMock()
        mock_async_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI.return_value = mock_async_client

        msgs = [
            Message(role="system", content="You are a verifier"),
            Message(role="user", content="Check this"),
        ]

        with patch.dict("sys.modules", {"openai": mock_openai}):
            result = await keyed_client._complete_openai(
                msgs,
                model="o3-mini",
                temperature=0.3,
                max_tokens=1024,
                base_url=None,
                api_key="sk-openai-test",
                provider=LLMProvider.OPENAI,
            )

        call_kwargs = mock_async_client.chat.completions.create.call_args.kwargs
        # System role should be mapped to developer.
        assert call_kwargs["messages"][0]["role"] == "developer"
        assert call_kwargs["messages"][1]["role"] == "user"
        # Should use max_completion_tokens, not max_tokens or temperature.
        assert "max_completion_tokens" in call_kwargs
        assert "temperature" not in call_kwargs
        assert "max_tokens" not in call_kwargs
        assert result.content == "reasoning result"

    async def test_non_o_series_uses_standard_params(self, keyed_client: UnifiedLLMClient) -> None:
        """Non o-series models use temperature and max_tokens normally."""
        mock_resp = _make_openai_response("standard result")
        mock_async_client = AsyncMock()
        mock_async_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI.return_value = mock_async_client

        msgs = [
            Message(role="system", content="You are helpful"),
            Message(role="user", content="Hello"),
        ]

        with patch.dict("sys.modules", {"openai": mock_openai}):
            await keyed_client._complete_openai(
                msgs,
                model="gpt-4o",
                temperature=0.5,
                max_tokens=2048,
                base_url=None,
                api_key="sk-openai-test",
                provider=LLMProvider.OPENAI,
            )

        call_kwargs = mock_async_client.chat.completions.create.call_args.kwargs
        assert call_kwargs["messages"][0]["role"] == "system"
        assert call_kwargs["temperature"] == 0.5
        assert call_kwargs["max_tokens"] == 2048
        assert "max_completion_tokens" not in call_kwargs

    async def test_usage_none(self, keyed_client: UnifiedLLMClient) -> None:
        """When usage is None, usage dict should be empty."""
        message = SimpleNamespace(content="hi")
        choice = SimpleNamespace(message=message)
        mock_resp = SimpleNamespace(choices=[choice], model="gpt-4o", usage=None)
        mock_async_client = AsyncMock()
        mock_async_client.chat.completions.create = AsyncMock(return_value=mock_resp)

        mock_openai = MagicMock()
        mock_openai.AsyncOpenAI.return_value = mock_async_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            result = await keyed_client._complete_openai(
                [Message(role="user", content="Hi")],
                model="gpt-4o",
                temperature=0.3,
                max_tokens=1024,
                base_url=None,
                api_key="sk-openai-test",
                provider=LLMProvider.OPENAI,
            )
        assert result.usage == {}


# ---------------------------------------------------------------------------
# _is_o_series
# ---------------------------------------------------------------------------


class TestIsOSeries:
    """Tests for the o-series model detection helper."""

    @pytest.mark.parametrize(
        "model",
        ["o1", "o1-mini", "o1-preview", "o3", "o3-mini", "o4-mini", "org/o3-mini"],
    )
    def test_o_series_models(self, model: str) -> None:
        assert UnifiedLLMClient._is_o_series(model) is True

    @pytest.mark.parametrize(
        "model",
        ["gpt-4o", "gpt-4o-mini", "claude-sonnet-4-20250514", "openai/gpt-4o", "o", ""],
    )
    def test_non_o_series_models(self, model: str) -> None:
        assert UnifiedLLMClient._is_o_series(model) is False


# ---------------------------------------------------------------------------
# complete (public API)
# ---------------------------------------------------------------------------


class TestComplete:
    """Tests for UnifiedLLMClient.complete dispatch and timeout."""

    async def test_dispatches_to_anthropic_backend(self, keyed_client: UnifiedLLMClient) -> None:
        """Anthropic provider dispatches to _complete_anthropic."""
        expected = LLMResponse(
            content="resp", model="claude-sonnet-4-20250514", provider=LLMProvider.ANTHROPIC
        )
        with patch.object(
            keyed_client, "_complete_anthropic", new_callable=AsyncMock, return_value=expected
        ):
            result = await keyed_client.complete(
                [Message(role="user", content="Hi")],
                provider=LLMProvider.ANTHROPIC,
                model="claude-sonnet-4-20250514",
            )
        assert result is expected

    async def test_dispatches_to_openai_backend(self, keyed_client: UnifiedLLMClient) -> None:
        """OpenAI provider dispatches to _complete_openai."""
        expected = LLMResponse(content="resp", model="gpt-4o", provider=LLMProvider.OPENAI)
        with patch.object(
            keyed_client, "_complete_openai", new_callable=AsyncMock, return_value=expected
        ):
            result = await keyed_client.complete(
                [Message(role="user", content="Hi")],
                provider=LLMProvider.OPENAI,
                model="gpt-4o",
            )
        assert result is expected

    async def test_timeout_raises_llm_timeout_error(self, keyed_client: UnifiedLLMClient) -> None:
        """asyncio.TimeoutError is translated to LLMTimeoutError."""

        async def slow_response(*args, **kwargs):
            await asyncio.sleep(10)

        with patch.object(keyed_client, "_complete_anthropic", side_effect=slow_response):
            with pytest.raises(LLMTimeoutError, match="timed out"):
                await keyed_client.complete(
                    [Message(role="user", content="Hi")],
                    provider=LLMProvider.ANTHROPIC,
                    model="claude-sonnet-4-20250514",
                    timeout=0.01,
                )

    async def test_auth_error_propagated_from_resolve(
        self, client: UnifiedLLMClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LLMAuthError from resolve_api_key propagates through complete."""
        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = None
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            with pytest.raises(LLMAuthError):
                await client.complete(
                    [Message(role="user", content="Hi")],
                    provider=LLMProvider.ANTHROPIC,
                    model="claude-sonnet-4-20250514",
                )

    async def test_dispatches_openai_compatible_provider(self, client: UnifiedLLMClient) -> None:
        """Grok (an OpenAI-compatible provider) dispatches to _complete_openai."""
        client._api_keys[LLMProvider.GROK] = "xai-test-key"
        expected = LLMResponse(content="grok resp", model="grok-3", provider=LLMProvider.GROK)
        with patch.object(
            client, "_complete_openai", new_callable=AsyncMock, return_value=expected
        ) as mock_openai:
            result = await client.complete(
                [Message(role="user", content="Hi")],
                provider=LLMProvider.GROK,
                model="grok-3",
            )
            assert result is expected
            # Verify base_url was passed from PROVIDER_REGISTRY.
            call_args = mock_openai.call_args
            assert call_args[0][4] == "https://api.x.ai/v1"  # base_url positional arg


# ---------------------------------------------------------------------------
# complete_for_role
# ---------------------------------------------------------------------------


class TestCompleteForRole:
    """Tests for UnifiedLLMClient.complete_for_role delegation."""

    async def test_delegates_to_complete(self, keyed_client: UnifiedLLMClient) -> None:
        """complete_for_role extracts RoleAssignment fields and calls complete."""
        assignment = RoleAssignment(
            provider=LLMProvider.ANTHROPIC,
            model="claude-sonnet-4-20250514",
            temperature=0.2,
            max_tokens=8192,
        )
        expected = LLMResponse(
            content="role-resp", model="claude-sonnet-4-20250514", provider=LLMProvider.ANTHROPIC
        )
        with patch.object(
            keyed_client, "complete", new_callable=AsyncMock, return_value=expected
        ) as mock_complete:
            result = await keyed_client.complete_for_role(
                [Message(role="user", content="Hi")],
                assignment,
                timeout=60.0,
            )

        assert result is expected
        mock_complete.assert_awaited_once_with(
            [Message(role="user", content="Hi")],
            provider=LLMProvider.ANTHROPIC,
            model="claude-sonnet-4-20250514",
            temperature=0.2,
            max_tokens=8192,
            timeout=60.0,
        )


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


class TestTestConnection:
    """Tests for UnifiedLLMClient.test_connection."""

    async def test_success_returns_true(self, keyed_client: UnifiedLLMClient) -> None:
        """Successful completion causes test_connection to return True."""
        expected = LLMResponse(
            content="Hi", model="claude-sonnet-4-20250514", provider=LLMProvider.ANTHROPIC
        )
        with patch.object(keyed_client, "complete", new_callable=AsyncMock, return_value=expected):
            result = await keyed_client.test_connection(
                LLMProvider.ANTHROPIC, model="claude-sonnet-4-20250514"
            )
        assert result is True

    async def test_failure_returns_false(self, keyed_client: UnifiedLLMClient) -> None:
        """Any exception causes test_connection to return False."""
        with patch.object(
            keyed_client, "complete", new_callable=AsyncMock, side_effect=LLMAuthError("bad key")
        ):
            result = await keyed_client.test_connection(
                LLMProvider.ANTHROPIC, model="claude-sonnet-4-20250514"
            )
        assert result is False

    async def test_uses_default_model_when_none(self, keyed_client: UnifiedLLMClient) -> None:
        """When model is None, the first default model for the provider is used."""
        expected = LLMResponse(
            content="Hi", model="claude-sonnet-4-20250514", provider=LLMProvider.ANTHROPIC
        )
        with patch.object(
            keyed_client, "complete", new_callable=AsyncMock, return_value=expected
        ) as mock_complete:
            result = await keyed_client.test_connection(LLMProvider.ANTHROPIC)
        assert result is True
        call_kwargs = mock_complete.call_args
        assert call_kwargs.kwargs["model"] == "claude-sonnet-4-20250514"

    async def test_uses_short_timeout_and_low_tokens(self, keyed_client: UnifiedLLMClient) -> None:
        """test_connection uses max_tokens=16 and timeout=30."""
        expected = LLMResponse(content="Hi", model="gpt-4o", provider=LLMProvider.OPENAI)
        with patch.object(
            keyed_client, "complete", new_callable=AsyncMock, return_value=expected
        ) as mock_complete:
            await keyed_client.test_connection(LLMProvider.OPENAI, model="gpt-4o")
        call_kwargs = mock_complete.call_args
        assert call_kwargs.kwargs["max_tokens"] == 16
        assert call_kwargs.kwargs["timeout"] == 30.0

    async def test_runtime_error_returns_false(self, keyed_client: UnifiedLLMClient) -> None:
        """Non-LLM exceptions are also caught and return False."""
        with patch.object(
            keyed_client, "complete", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ):
            result = await keyed_client.test_connection(LLMProvider.ANTHROPIC)
        assert result is False


# ---------------------------------------------------------------------------
# _is_context_length_error
# ---------------------------------------------------------------------------


class TestIsContextLengthError:
    """Tests for the _is_context_length_error helper."""

    def test_openai_code_attribute(self) -> None:
        """Detects via OpenAI-style .code attribute."""
        exc = Exception("error")
        exc.code = "context_length_exceeded"
        assert _is_context_length_error(exc) is True

    def test_openai_string_above_max_length_code(self) -> None:
        """Detects 'string_above_max_length' code."""
        exc = Exception("error")
        exc.code = "string_above_max_length"
        assert _is_context_length_error(exc) is True

    def test_anthropic_body_error_type(self) -> None:
        """Detects via Anthropic-style .body.error.type."""
        exc = Exception("error")
        exc.body = {"error": {"type": "context_length_exceeded", "message": "too long"}}
        assert _is_context_length_error(exc) is True

    def test_keyword_maximum_context_length(self) -> None:
        """Detects via keyword 'maximum context length' in message."""
        exc = Exception("This request exceeds the maximum context length for the model")
        assert _is_context_length_error(exc) is True

    def test_keyword_too_many_tokens(self) -> None:
        """Detects via keyword 'too many tokens' in message."""
        exc = Exception("Too many tokens in the request")
        assert _is_context_length_error(exc) is True

    def test_keyword_prompt_is_too_long(self) -> None:
        """Detects via keyword 'prompt is too long' in message."""
        exc = Exception("Your prompt is too long, please reduce it")
        assert _is_context_length_error(exc) is True

    def test_keyword_context_length_exceeded(self) -> None:
        """Detects via keyword 'context length exceeded' in message."""
        exc = Exception("Error: context length exceeded")
        assert _is_context_length_error(exc) is True

    def test_no_match_returns_false(self) -> None:
        """Returns False when nothing matches."""
        exc = Exception("something completely different")
        assert _is_context_length_error(exc) is False

    def test_non_string_code_ignored(self) -> None:
        """Non-string code attribute is ignored."""
        exc = Exception("error")
        exc.code = 400  # numeric, not string
        assert _is_context_length_error(exc) is False

    def test_body_not_dict_ignored(self) -> None:
        """Non-dict body attribute is ignored."""
        exc = Exception("error")
        exc.body = "not a dict"
        assert _is_context_length_error(exc) is False

    def test_body_error_not_dict_ignored(self) -> None:
        """body.error that is not a dict is ignored."""
        exc = Exception("error")
        exc.body = {"error": "just a string"}
        assert _is_context_length_error(exc) is False


# ---------------------------------------------------------------------------
# _parse_retry_after
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    """Tests for the _parse_retry_after helper."""

    def test_none_response(self) -> None:
        """Returns None when response is None."""
        assert _parse_retry_after(None) is None

    def test_no_headers_attribute(self) -> None:
        """Returns None when response has no headers."""
        resp = SimpleNamespace()
        assert _parse_retry_after(resp) is None

    def test_none_headers(self) -> None:
        """Returns None when headers is None."""
        resp = SimpleNamespace(headers=None)
        assert _parse_retry_after(resp) is None

    def test_lowercase_retry_after(self) -> None:
        """Parses lowercase 'retry-after' header."""
        resp = SimpleNamespace(headers={"retry-after": "30"})
        assert _parse_retry_after(resp) == 30.0

    def test_titlecase_retry_after(self) -> None:
        """Parses title-case 'Retry-After' header."""
        resp = SimpleNamespace(headers={"Retry-After": "45.5"})
        assert _parse_retry_after(resp) == 45.5

    def test_non_numeric_returns_none(self) -> None:
        """Returns None for non-numeric retry-after values."""
        resp = SimpleNamespace(headers={"retry-after": "not-a-number"})
        assert _parse_retry_after(resp) is None

    def test_missing_header_returns_none(self) -> None:
        """Returns None when retry-after header is absent."""
        resp = SimpleNamespace(headers={"content-type": "application/json"})
        assert _parse_retry_after(resp) is None

    def test_integer_value(self) -> None:
        """Parses integer retry-after values."""
        resp = SimpleNamespace(headers={"retry-after": "120"})
        assert _parse_retry_after(resp) == 120.0

    def test_float_value(self) -> None:
        """Parses float retry-after values."""
        resp = SimpleNamespace(headers={"retry-after": "1.5"})
        assert _parse_retry_after(resp) == 1.5


# ---------------------------------------------------------------------------
# Data class sanity checks
# ---------------------------------------------------------------------------


class TestDataClasses:
    """Basic tests for Message and LLMResponse."""

    def test_message_frozen(self) -> None:
        """Message is immutable."""
        msg = Message(role="user", content="Hello")
        with pytest.raises(AttributeError):
            msg.role = "system"  # type: ignore[misc]

    def test_llm_response_frozen(self) -> None:
        """LLMResponse is immutable."""
        resp = LLMResponse(content="Hi", model="gpt-4o", provider=LLMProvider.OPENAI)
        with pytest.raises(AttributeError):
            resp.content = "changed"  # type: ignore[misc]

    def test_llm_response_default_usage(self) -> None:
        """LLMResponse.usage defaults to empty dict."""
        resp = LLMResponse(content="Hi", model="gpt-4o", provider=LLMProvider.OPENAI)
        assert resp.usage == {}

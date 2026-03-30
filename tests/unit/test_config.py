"""Unit tests for paperverifier.config."""

from __future__ import annotations

import pytest

import paperverifier.config as config_module
from paperverifier.config import AppSettings, _redact_sensitive_keys, get_settings


@pytest.fixture(autouse=True)
def _reset_settings():
    """Save and restore config singleton between tests."""
    old = config_module._settings
    config_module._settings = None
    yield
    config_module._settings = old


# ---------------------------------------------------------------------------
# AppSettings defaults
# ---------------------------------------------------------------------------


class TestAppSettingsDefaults:
    """AppSettings should have sensible defaults."""

    def test_default_log_level(self) -> None:
        settings = AppSettings()
        assert settings.log_level == "INFO"

    def test_default_log_format(self) -> None:
        settings = AppSettings()
        assert settings.log_format == "json"

    def test_default_max_concurrent_agents(self) -> None:
        settings = AppSettings()
        assert settings.max_concurrent_agents == 9

    def test_default_llm_call_timeout(self) -> None:
        settings = AppSettings()
        assert settings.llm_call_timeout == 120.0

    def test_default_max_document_size_mb(self) -> None:
        settings = AppSettings()
        assert settings.max_document_size_mb == 100


# ---------------------------------------------------------------------------
# get_settings singleton
# ---------------------------------------------------------------------------


class TestGetSettingsSingleton:
    """get_settings() should return a cached singleton."""

    def test_returns_app_settings_instance(self) -> None:
        settings = get_settings()
        assert isinstance(settings, AppSettings)

    def test_returns_same_instance(self) -> None:
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_reset_forces_new_instance(self) -> None:
        s1 = get_settings()
        config_module._settings = None
        s2 = get_settings()
        # They are equal in value but different objects
        assert s1 is not s2


# ---------------------------------------------------------------------------
# _redact_sensitive_keys processor
# ---------------------------------------------------------------------------


class TestRedactSensitiveKeys:
    """The structlog processor should redact secrets but not metric keys."""

    def test_redacts_api_key(self) -> None:
        event = {"event": "test", "api_key": "sk-secret-1234"}
        result = _redact_sensitive_keys(None, "info", event)
        assert result["api_key"] == "[REDACTED]"

    def test_redacts_password(self) -> None:
        event = {"event": "test", "password": "hunter2"}
        result = _redact_sensitive_keys(None, "info", event)
        assert result["password"] == "[REDACTED]"

    def test_redacts_authorization(self) -> None:
        event = {"event": "test", "authorization": "Bearer token123"}
        result = _redact_sensitive_keys(None, "info", event)
        assert result["authorization"] == "[REDACTED]"

    def test_does_not_redact_input_tokens(self) -> None:
        """input_tokens is a metric, not a secret -- must NOT be redacted (HIGH-I4)."""
        event = {"event": "llm_call", "input_tokens": 1500, "output_tokens": 300}
        result = _redact_sensitive_keys(None, "info", event)
        assert result["input_tokens"] == 1500
        assert result["output_tokens"] == 300

    def test_does_not_redact_normal_keys(self) -> None:
        event = {"event": "test", "username": "alice", "duration": 1.5}
        result = _redact_sensitive_keys(None, "info", event)
        assert result["username"] == "alice"
        assert result["duration"] == 1.5

    def test_redacts_semantic_scholar_api_key(self) -> None:
        event = {"event": "test", "semantic_scholar_api_key": "abcdef"}
        result = _redact_sensitive_keys(None, "info", event)
        assert result["semantic_scholar_api_key"] == "[REDACTED]"

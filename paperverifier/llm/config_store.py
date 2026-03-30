"""YAML-based configuration store for role assignments and keyring helpers.

Role assignments (which provider/model/temperature to use for each agent
role) are stored in a YAML file.  **API keys are never written to YAML** --
they are managed exclusively through the OS keyring via the helper functions
:func:`store_api_key`, :func:`get_api_key`, :func:`delete_api_key`, and
:func:`list_configured_providers`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

from paperverifier.llm.providers import LLMProvider
from paperverifier.llm.roles import (
    DEFAULT_ASSIGNMENTS,
    AgentRole,
    RoleAssignment,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

CONFIG_DIR: Path = Path(__file__).resolve().parents[2] / "config"
DEFAULT_CONFIG_PATH: Path = CONFIG_DIR / "llm_config.yaml"

_KEYRING_SERVICE = "paperverifier"

# ---------------------------------------------------------------------------
# Role assignment persistence
# ---------------------------------------------------------------------------


def load_role_assignments(
    path: Path = DEFAULT_CONFIG_PATH,
) -> dict[AgentRole, RoleAssignment]:
    """Load role assignments from *path*, merging with defaults.

    Any role not present in the file will use its value from
    :data:`~paperverifier.llm.roles.DEFAULT_ASSIGNMENTS`.  If the file does
    not exist, the full default mapping is returned.

    Args:
        path: Path to the YAML config file.

    Returns:
        A complete mapping from every :class:`AgentRole` to its
        :class:`RoleAssignment`.
    """
    assignments = dict(DEFAULT_ASSIGNMENTS)

    if not path.is_file():
        logger.info("config_not_found_using_defaults", path=str(path))
        return assignments

    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        logger.error("config_parse_error", path=str(path), error=str(exc))
        return assignments

    role_list: list[dict[str, Any]] = raw.get("role_assignments", [])
    if not isinstance(role_list, list):
        logger.warning("config_invalid_role_assignments", path=str(path))
        return assignments

    for entry in role_list:
        try:
            role = AgentRole(entry["role"])
            provider = LLMProvider(entry["provider"])
            assignments[role] = RoleAssignment(
                provider=provider,
                model=entry["model"],
                temperature=float(entry.get("temperature", 0.3)),
                max_tokens=int(entry.get("max_tokens", 4096)),
            )
        except (KeyError, ValueError) as exc:
            logger.warning("config_skip_invalid_entry", entry=entry, error=str(exc))
            continue

    logger.info("config_loaded", path=str(path), roles_loaded=len(role_list))
    return assignments


def save_role_assignments(
    assignments: dict[AgentRole, RoleAssignment],
    path: Path = DEFAULT_CONFIG_PATH,
) -> None:
    """Persist role assignments to a YAML file.

    **This function never writes API keys.**  Only the provider, model,
    temperature, and max_tokens for each role are stored.

    Args:
        assignments: The role-to-assignment mapping to save.
        path: Destination file path.
    """
    role_list: list[dict[str, Any]] = []
    for role in AgentRole:
        assignment = assignments.get(role)
        if assignment is None:
            continue
        role_list.append(
            {
                "role": role.value,
                "provider": assignment.provider.value,
                "model": assignment.model,
                "temperature": assignment.temperature,
                "max_tokens": assignment.max_tokens,
            }
        )

    data: dict[str, Any] = {"role_assignments": role_list}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# PaperVerifier LLM Configuration\n"
        "# API keys are stored in the OS keyring -- never in this file.\n\n"
        + yaml.dump(data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("config_saved", path=str(path))


# ---------------------------------------------------------------------------
# Keyring helpers -- secure API key management
# ---------------------------------------------------------------------------


def store_api_key(provider: LLMProvider, key: str) -> None:
    """Store an API key in the OS keyring.

    Args:
        provider: The LLM provider the key belongs to.
        key: The secret API key value.

    Raises:
        RuntimeError: If the keyring backend is not available.
    """
    import keyring  # noqa: PLC0415

    keyring.set_password(_KEYRING_SERVICE, provider.value, key)
    logger.info("api_key_stored", provider=provider.value)


def get_api_key(provider: LLMProvider) -> str | None:
    """Retrieve an API key from the OS keyring.

    Returns:
        The API key string, or ``None`` if not set.
    """
    import keyring  # noqa: PLC0415

    try:
        return keyring.get_password(_KEYRING_SERVICE, provider.value)
    except Exception:  # noqa: BLE001
        logger.debug("keyring_get_failed", provider=provider.value)
        return None


def delete_api_key(provider: LLMProvider) -> None:
    """Remove an API key from the OS keyring.

    No error is raised if the key does not exist.

    Args:
        provider: The LLM provider whose key should be deleted.
    """
    import keyring  # noqa: PLC0415

    try:
        keyring.delete_password(_KEYRING_SERVICE, provider.value)
        logger.info("api_key_deleted", provider=provider.value)
    except keyring.errors.PasswordDeleteError:
        logger.debug("api_key_not_found_for_deletion", provider=provider.value)


def list_configured_providers() -> list[LLMProvider]:
    """Return a list of providers that have an API key stored in the keyring."""
    configured: list[LLMProvider] = []
    for provider in LLMProvider:
        if get_api_key(provider) is not None:
            configured.append(provider)
    return configured

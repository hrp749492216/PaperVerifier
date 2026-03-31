"""Settings page -- LLM provider configuration and agent role assignments.

Manages API keys (stored in the OS keyring) and role-to-model mappings
(persisted in YAML).
"""

from __future__ import annotations

import streamlit as st

from streamlit_app.auth import require_auth
from streamlit_app.utils import run_async  # noqa: F401 – shared async helper

require_auth()

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

from paperverifier.llm.providers import LLMProvider, PROVIDER_REGISTRY
from paperverifier.llm.roles import AgentRole, RoleAssignment, DEFAULT_ASSIGNMENTS
from paperverifier.llm.config_store import (
    get_api_key,
    store_api_key,
    load_role_assignments,
    save_role_assignments,
    list_configured_providers,
)

# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------

st.header("Settings")

# ---------------------------------------------------------------------------
# Session-state defaults
# ---------------------------------------------------------------------------

if "role_assignments" not in st.session_state:
    st.session_state["role_assignments"] = load_role_assignments()

if "llm_client" not in st.session_state:
    st.session_state["llm_client"] = None

# ---------------------------------------------------------------------------
# API Keys section
# ---------------------------------------------------------------------------

st.subheader("API Keys")

st.info(
    "API keys are stored in your OS keyring (macOS Keychain, Windows Credential "
    "Locker, or Linux Secret Service). They are never written to plaintext files.",
    icon="\U0001f512",
)

configured_providers = list_configured_providers()

for provider in LLMProvider:
    spec = PROVIDER_REGISTRY[provider]
    is_configured = provider in configured_providers

    status_icon = "\u2705" if is_configured else "\u274c"
    with st.expander(
        f"{status_icon} {spec.display_name}",
        expanded=False,
    ):
        st.caption(f"Environment variable: `{spec.env_var}`")
        st.caption(f"Default models: {', '.join(spec.default_models)}")

        # API key input
        if is_configured:
            st.caption("\U0001f511 A key is already saved in your keyring. Enter a new value below only to replace it.")

        # If the previous run flagged this key for clearing, remove the
        # widget-backed value from session state *before* creating the
        # widget to comply with Streamlit's Session State rules.
        _clear_flag = f"_clear_api_key_{provider.value}"
        if st.session_state.pop(_clear_flag, False):
            st.session_state.pop(f"api_key_{provider.value}", None)

        key_input = st.text_input(
            f"{spec.display_name} API Key",
            type="password",
            value="",
            key=f"api_key_{provider.value}",
            placeholder="Enter API key..." if not is_configured else "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 (key is set -- enter new to update)",
            help=f"Set via keyring or ${spec.env_var} environment variable.",
        )

        btn_col1, btn_col2 = st.columns(2)

        with btn_col1:
            if st.button("Save Key", key=f"btn_save_key_{provider.value}"):
                if key_input.strip():
                    try:
                        store_api_key(provider, key_input.strip())
                        # Update the client runtime cache too
                        client = st.session_state.get("llm_client")
                        if client is not None:
                            client.set_api_key(provider, key_input.strip())
                        # Flag the widget key for clearing on next rerun
                        # (cannot mutate widget-backed state after creation).
                        st.session_state[_clear_flag] = True
                        st.toast(f"API key for {spec.display_name} saved to keyring.", icon="\u2705")
                        st.rerun()
                    except Exception:
                        import logging as _logging
                        _logging.getLogger(__name__).error("save_key_failed", exc_info=True)
                        st.error("Failed to save key. Check logs for details.")
                else:
                    st.warning("Please enter an API key before saving.")

        with btn_col2:
            if st.button("Test Connection", key=f"btn_test_{provider.value}"):
                if not is_configured and not key_input.strip():
                    st.warning(
                        f"No API key configured for {spec.display_name}. "
                        "Please save a key first."
                    )
                else:
                    with st.spinner(f"Testing {spec.display_name} connection..."):
                        try:
                            from paperverifier.llm.client import UnifiedLLMClient

                            test_client = UnifiedLLMClient()
                            # If user typed a key but hasn't saved, set it in
                            # memory only -- do NOT persist to keyring (Codex-2).
                            if key_input.strip():
                                test_client.set_api_key(
                                    provider, key_input.strip(), persist=False,
                                )

                            success = run_async(
                                test_client.test_connection(provider)
                            )
                            if success:
                                st.success(
                                    f"Connection to {spec.display_name} successful!"
                                )
                            else:
                                st.error(
                                    f"Connection to {spec.display_name} failed. "
                                    "Check your API key."
                                )
                        except Exception:
                            import logging as _logging
                            _logging.getLogger(__name__).error("connection_test_failed", exc_info=True)
                            st.error("Connection test failed. Check logs for details.")

        # Status indicator
        if is_configured:
            st.caption("\u2705 Key is configured in keyring")
        else:
            import os

            env_val = os.environ.get(spec.env_var)
            if env_val:
                st.caption(
                    f"\u2705 Key found in environment variable ${spec.env_var}"
                )
            else:
                st.caption("\u274c No key configured")


# ---------------------------------------------------------------------------
# Agent Role Assignments
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Agent Role Assignments")
st.markdown(
    "Configure which LLM provider and model each verification agent uses. "
    "Changes are saved to a YAML configuration file."
)

assignments: dict[AgentRole, RoleAssignment] = st.session_state["role_assignments"]

# Build list of providers that have keys configured (for dropdown)
available_providers = list_configured_providers()
# Always show all providers in dropdown (users may configure keys later)
all_provider_names = [p.value for p in LLMProvider]

# Track modified assignments
modified_assignments: dict[AgentRole, RoleAssignment] = {}

for role in AgentRole:
    current = assignments.get(role, DEFAULT_ASSIGNMENTS[role])

    with st.expander(
        f"{role.value.replace('_', ' ').title()}",
        expanded=False,
    ):
        role_col1, role_col2 = st.columns(2)

        with role_col1:
            # Provider dropdown
            current_provider_idx = (
                all_provider_names.index(current.provider.value)
                if current.provider.value in all_provider_names
                else 0
            )
            selected_provider_name = st.selectbox(
                "Provider",
                options=all_provider_names,
                index=current_provider_idx,
                key=f"role_provider_{role.value}",
                help="Select the LLM provider for this agent role.",
            )
            selected_provider = LLMProvider(selected_provider_name)

            # Show whether the selected provider has a key configured
            if selected_provider in available_providers:
                st.caption("\u2705 Provider key configured")
            else:
                st.caption("\u26a0\ufe0f Provider key not yet configured")

            # Model input -- validate against selected provider's models.
            # If the provider was changed (model no longer in the new
            # provider's list), reset to the first default model for the
            # new provider so users don't inadvertently submit an
            # invalid model name (MED-U15).
            selected_spec = PROVIDER_REGISTRY[selected_provider]
            current_model = current.model
            if current_model not in selected_spec.default_models:
                current_model = selected_spec.default_models[0]
            model_value = st.selectbox(
                "Model",
                options=list(selected_spec.default_models),
                index=list(selected_spec.default_models).index(current_model),
                key=f"role_model_{role.value}",
                help=f"Available models for {selected_spec.display_name}.",
            )

        with role_col2:
            # Temperature slider
            temp_value = st.slider(
                "Temperature",
                min_value=0.0,
                max_value=1.0,
                value=current.temperature,
                step=0.05,
                key=f"role_temp_{role.value}",
                help="Lower values produce more deterministic outputs.",
            )

            # Max tokens input
            max_tokens_value = st.number_input(
                "Max Tokens",
                min_value=256,
                max_value=32768,
                value=current.max_tokens,
                step=256,
                key=f"role_maxtok_{role.value}",
                help="Maximum tokens in the LLM response.",
            )

        modified_assignments[role] = RoleAssignment(
            provider=selected_provider,
            model=model_value,
            temperature=temp_value,
            max_tokens=int(max_tokens_value),
        )


# ---------------------------------------------------------------------------
# Save / Reset buttons
# ---------------------------------------------------------------------------

st.divider()

save_col, reset_col, _ = st.columns([1, 1, 3])

with save_col:
    if st.button("Save Configuration", type="primary", key="btn_save_config"):
        try:
            save_role_assignments(modified_assignments)
            st.session_state["role_assignments"] = modified_assignments
            st.success("Role assignments saved to configuration file.")
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).error("save_config_failed", exc_info=True)
            st.error("Failed to save configuration. Check logs for details.")

with reset_col:
    if st.button("Reset to Defaults", key="btn_reset_defaults"):
        try:
            save_role_assignments(dict(DEFAULT_ASSIGNMENTS))
            st.session_state["role_assignments"] = dict(DEFAULT_ASSIGNMENTS)
            st.success("Role assignments reset to defaults.")
            st.rerun()
        except Exception:
            import logging as _logging
            _logging.getLogger(__name__).error("reset_config_failed", exc_info=True)
            st.error("Failed to reset configuration. Check logs for details.")

# ---------------------------------------------------------------------------
# Current configuration summary
# ---------------------------------------------------------------------------

st.divider()
st.subheader("Current Configuration Summary")

summary_data = []
for role in AgentRole:
    a = modified_assignments.get(role, DEFAULT_ASSIGNMENTS[role])
    provider_configured = LLMProvider(a.provider) in available_providers
    summary_data.append(
        {
            "Role": role.value.replace("_", " ").title(),
            "Provider": a.provider.value,
            "Model": a.model,
            "Temperature": a.temperature,
            "Max Tokens": a.max_tokens,
            "Key Ready": "\u2705" if provider_configured else "\u274c",
        }
    )

st.dataframe(summary_data, use_container_width=True, hide_index=True)

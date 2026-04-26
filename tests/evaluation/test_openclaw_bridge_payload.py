"""
v0.7 D2 unit tests for OpenClawAdapter._bridge_base_payload().

Locks the contract that:
- agent_llm_env_vars whitelist from yaml is forwarded into bridge payload.
- Missing yaml field falls back to empty list (not raises).
- Whitelist content is exactly what was in yaml (no implicit additions).

Without this, openclaw subprocess will not receive secret env vars and
will throw MissingEnvVarError on ${LLM_API_KEY} / ${SOPH_API_KEY}
templates in the resolved config.
"""
from __future__ import annotations

import pytest

from evaluation.src.adapters.openclaw_adapter import OpenClawAdapter


_SANDBOX_FIXTURE = {
    "resolved_config_path": "/tmp/sandbox/openclaw.json",
    "workspace_dir": "/tmp/sandbox/workspace",
    "native_store_dir": "/tmp/sandbox/state",
    "home_dir": "/tmp/sandbox/home",
    "cwd_dir": "/tmp/sandbox/cwd",
}


def _build_adapter(openclaw_cfg: dict) -> OpenClawAdapter:
    """Build adapter with explicit openclaw config for unit testing.

    Provides minimal LLM config to satisfy BaseAdapter.__init__.
    """
    config = {
        "openclaw": openclaw_cfg,
        "llm": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "test",
            "base_url": "https://test",
        },
    }
    return OpenClawAdapter(config=config)


def test_bridge_payload_includes_env_whitelist_from_yaml():
    """yaml.openclaw.agent_llm.env_vars list is forwarded as-is."""
    adapter = _build_adapter({
        "repo_path": "/tmp/openclaw-repo",
        "agent_llm": {
            "env_vars": ["LLM_API_KEY", "SOPH_API_KEY", "LLM_BASE_URL"],
        },
    })
    payload = adapter._bridge_base_payload(_SANDBOX_FIXTURE)

    assert "agent_llm_env_vars" in payload
    assert payload["agent_llm_env_vars"] == [
        "LLM_API_KEY",
        "SOPH_API_KEY",
        "LLM_BASE_URL",
    ]


def test_bridge_payload_env_vars_missing_yaml_returns_empty_list():
    """Missing agent_llm config: payload still has the field, as []."""
    adapter = _build_adapter({"repo_path": "/tmp/openclaw-repo"})
    payload = adapter._bridge_base_payload(_SANDBOX_FIXTURE)

    assert payload.get("agent_llm_env_vars") == []


def test_bridge_payload_env_vars_missing_inner_field_returns_empty_list():
    """agent_llm exists but no env_vars field: still []."""
    adapter = _build_adapter({
        "repo_path": "/tmp/openclaw-repo",
        "agent_llm": {"provider_id": "sophnet"},  # no env_vars
    })
    payload = adapter._bridge_base_payload(_SANDBOX_FIXTURE)

    assert payload.get("agent_llm_env_vars") == []


def test_bridge_payload_env_vars_handles_non_list_type():
    """Defensive: yaml mistake (string instead of list) does not crash."""
    adapter = _build_adapter({
        "repo_path": "/tmp/openclaw-repo",
        "agent_llm": {"env_vars": "LLM_API_KEY"},  # string instead of list
    })
    payload = adapter._bridge_base_payload(_SANDBOX_FIXTURE)

    # Falls back to [] rather than crashing or accepting malformed input
    assert payload.get("agent_llm_env_vars") == []


def test_bridge_payload_keeps_existing_fields():
    """Existing fields (repo_path/config_path/...) must still be present."""
    adapter = _build_adapter({
        "repo_path": "/tmp/openclaw-repo",
        "agent_llm": {"env_vars": ["X"]},
    })
    payload = adapter._bridge_base_payload(_SANDBOX_FIXTURE)

    for required in [
        "repo_path",
        "config_path",
        "workspace_dir",
        "state_dir",
        "home_dir",
        "cwd_dir",
        "agent_llm_env_vars",
    ]:
        assert required in payload, f"missing {required} in payload"

    assert payload["repo_path"] == "/tmp/openclaw-repo"
    assert payload["config_path"] == "/tmp/sandbox/openclaw.json"
    assert payload["workspace_dir"] == "/tmp/sandbox/workspace"


def test_bridge_payload_env_vars_is_a_copy_not_reference():
    """Mutating payload's list must not affect adapter's internal state."""
    cfg_env_vars = ["LLM_API_KEY"]
    adapter = _build_adapter({
        "repo_path": "/tmp/openclaw-repo",
        "agent_llm": {"env_vars": cfg_env_vars},
    })
    payload = adapter._bridge_base_payload(_SANDBOX_FIXTURE)

    payload["agent_llm_env_vars"].append("INJECTED")
    # Original config not mutated
    assert cfg_env_vars == ["LLM_API_KEY"]

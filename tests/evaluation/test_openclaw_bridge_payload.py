"""
v0.7 D2 unit tests for OpenClawAdapter._bridge_base_payload().

Locks the contract that:
- agent_llm_env_vars remains the bridge payload key.
- Its value is a stable, deduplicated union of explicit and credential refs.
- Invalid environment names are rejected before the bridge silently drops them.
- Only environment names, never values or ov_ingest credentials, are serialized.

Without this, openclaw subprocess will not receive secret env vars and
will throw MissingEnvVarError on ${LLM_API_KEY} / ${SOPH_API_KEY}
templates in the resolved config.
"""
from __future__ import annotations

import json

import pytest

from evaluation.src.adapters.openclaw.adapter import OpenClawAdapter


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
    """Explicit names come first, then missing credential references."""
    adapter = _build_adapter(
        {
            "repo_path": "/tmp/openclaw-repo",
            "agent_llm": {
                "env_vars": ["LLM_BASE_URL", "LLM_BASE_URL", "EXTRA_ENV"],
                "api_key_env": "LLM_API_KEY",
            },
            "embedding": {"api_key_env": "SOPH_API_KEY"},
        },
    )
    payload = adapter._bridge_base_payload(_SANDBOX_FIXTURE)

    assert "agent_llm_env_vars" in payload
    assert payload["agent_llm_env_vars"] == [
        "LLM_BASE_URL",
        "EXTRA_ENV",
        "LLM_API_KEY",
        "SOPH_API_KEY",
    ]


def test_bridge_payload_without_agent_llm_includes_embedding_credential_ref():
    """Host shared-LLM presets still need embedding credentials in OpenClaw."""
    adapter = _build_adapter(
        {
            "repo_path": "/tmp/openclaw-repo",
            "embedding": {"api_key_env": "SOPH_API_KEY"},
        }
    )
    payload = adapter._bridge_base_payload(_SANDBOX_FIXTURE)

    assert payload.get("agent_llm_env_vars") == ["SOPH_API_KEY"]


def test_bridge_payload_without_credential_refs_keeps_empty_whitelist():
    adapter = _build_adapter({"repo_path": "/tmp/openclaw-repo"})

    assert adapter._bridge_base_payload(_SANDBOX_FIXTURE)["agent_llm_env_vars"] == []


def test_bridge_payload_env_vars_missing_explicit_list_uses_api_key_ref():
    adapter = _build_adapter(
        {
            "repo_path": "/tmp/openclaw-repo",
            "agent_llm": {"api_key_env": "LLM_API_KEY"},
        }
    )
    payload = adapter._bridge_base_payload(_SANDBOX_FIXTURE)

    assert payload.get("agent_llm_env_vars") == ["LLM_API_KEY"]


def test_bridge_payload_env_vars_handles_non_list_type():
    """Defensive: yaml mistake (string instead of list) does not crash."""
    adapter = _build_adapter({
        "repo_path": "/tmp/openclaw-repo",
        "agent_llm": {"env_vars": "LLM_API_KEY"},  # string instead of list
    })
    payload = adapter._bridge_base_payload(_SANDBOX_FIXTURE)

    # Falls back to [] rather than crashing or accepting malformed input
    assert payload.get("agent_llm_env_vars") == []


@pytest.mark.parametrize(
    "invalid_name",
    ["bad-name", "1BAD", "${SECRET}", "/tmp/secret", "lowercase"],
)
def test_bridge_payload_rejects_invalid_env_var_names(invalid_name: str):
    adapter = _build_adapter(
        {
            "repo_path": "/tmp/openclaw-repo",
            "agent_llm": {"env_vars": [invalid_name]},
        }
    )

    with pytest.raises(ValueError, match="invalid OpenClaw environment variable"):
        adapter._bridge_base_payload(_SANDBOX_FIXTURE)


@pytest.mark.parametrize("block", ["agent_llm", "embedding"])
def test_bridge_payload_rejects_invalid_credential_env_ref(block: str):
    adapter = _build_adapter(
        {
            "repo_path": "/tmp/openclaw-repo",
            block: {"api_key_env": "bad-name"},
        }
    )

    with pytest.raises(ValueError, match="invalid OpenClaw environment variable"):
        adapter._bridge_base_payload(_SANDBOX_FIXTURE)


def test_bridge_payload_excludes_ov_ingest_secret_ref():
    adapter = _build_adapter(
        {
            "repo_path": "/tmp/openclaw-repo",
            "ov_ingest": {"api_key_env": "OPENVIKING_API_KEY"},
        }
    )

    payload = adapter._bridge_base_payload(_SANDBOX_FIXTURE)

    assert payload["agent_llm_env_vars"] == []


def test_bridge_payload_serializes_names_without_secret_values(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "materialized-llm-secret")
    monkeypatch.setenv("SOPH_API_KEY", "materialized-embedding-secret")
    adapter = _build_adapter(
        {
            "repo_path": "/tmp/openclaw-repo",
            "agent_llm": {"api_key_env": "LLM_API_KEY"},
            "embedding": {"api_key_env": "SOPH_API_KEY"},
        }
    )

    serialized = json.dumps(adapter._bridge_base_payload(_SANDBOX_FIXTURE))

    assert "LLM_API_KEY" in serialized
    assert "SOPH_API_KEY" in serialized
    assert "materialized-llm-secret" not in serialized
    assert "materialized-embedding-secret" not in serialized


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

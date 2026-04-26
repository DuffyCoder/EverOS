"""
v0.7 D2 unit tests for build_openclaw_resolved_config.

Locks the contract that:
- Real secret values never land in the resolved config (always emit
  ``${VAR}`` template strings rebuilt from ``api_key_env`` markers).
- Non-secret fields (base_url, easyllm_id, model id, etc.) carry plain
  expanded strings; their yaml ``${VAR:default}`` templates are
  expanded by evermemos's ``_replace_env_vars`` before this function
  ever sees them.
- ``plugins.allow``, ``plugins.slots``, ``plugins.entries`` are emitted
  per memory_mode (memory-core baseline / noop / other plugin).
- ``memorySearch.enabled`` flips false on noop mode so the agent has no
  memory_search/memory_get tools.
"""
from __future__ import annotations

import json

import pytest

from evaluation.src.adapters.openclaw_resolved_config import (
    build_openclaw_resolved_config,
)


# --- helpers ----------------------------------------------------------

_BASE = {
    "workspace_dir": "/tmp/ws",
    "native_store_dir": "/tmp/state",
    "backend_mode": "hybrid",
    "flush_mode": "shared_llm",
}


def _agent_llm_fixture() -> dict:
    return {
        "provider_id": "sophnet",
        "base_url": "https://expanded-url.example/v1",
        "api": "openai-completions",
        "api_key_env": "LLM_API_KEY",
        "model": {
            "id": "gpt-4.1-mini",
            "name": "GPT 4.1 Mini",
            "context_window": 128000,
            "max_tokens": 4096,
        },
    }


def _embedding_fixture() -> dict:
    return {
        "provider": "sophnet",
        "model": "text-embeddings",
        "api_key_env": "SOPH_API_KEY",
        # Non-secret fields: yaml templates already expanded by evermemos
        "base_url": "https://expanded-url.example/embed",
        "easyllm_id": "easy-id-123",
        "output_dimensionality": 1024,
    }


# --- secret hygiene --------------------------------------------------

def test_resolved_config_does_not_leak_agent_llm_secret(monkeypatch):
    """Real LLM_API_KEY value must never appear in resolved config."""
    monkeypatch.setenv("LLM_API_KEY", "sk-shouldnotleak-12345")

    cfg = build_openclaw_resolved_config(
        **_BASE, agent_llm=_agent_llm_fixture(),
    )

    serialized = json.dumps(cfg)
    assert "sk-shouldnotleak-12345" not in serialized, (
        "secret value LEAKED into resolved config; "
        "expected '${LLM_API_KEY}' template instead"
    )
    assert "${LLM_API_KEY}" in serialized
    # Sanity: provider section structure
    assert cfg["models"]["providers"]["sophnet"]["apiKey"] == "${LLM_API_KEY}"
    assert cfg["agents"]["defaults"]["model"] == "sophnet/gpt-4.1-mini"


def test_resolved_config_does_not_leak_embedding_secret(monkeypatch):
    """Real SOPH_API_KEY must never appear in resolved config (only template)."""
    monkeypatch.setenv("SOPH_API_KEY", "sk-noleak-soph-67890")

    cfg = build_openclaw_resolved_config(
        **_BASE, embedding=_embedding_fixture(),
    )

    serialized = json.dumps(cfg)
    assert "sk-noleak-soph-67890" not in serialized
    assert "${SOPH_API_KEY}" in serialized
    remote = cfg["agents"]["defaults"]["memorySearch"]["remote"]
    assert remote["apiKey"] == "${SOPH_API_KEY}"


def test_resolved_config_preserves_non_secret_expanded_values():
    """base_url and easyllm_id should be plain expanded values, not templates."""
    cfg = build_openclaw_resolved_config(
        **_BASE,
        agent_llm=_agent_llm_fixture(),
        embedding=_embedding_fixture(),
    )

    # agent provider baseUrl: plain expanded value (yaml ${LLM_BASE_URL}
    # was already expanded by evermemos before reaching this function)
    assert cfg["models"]["providers"]["sophnet"]["baseUrl"] == "https://expanded-url.example/v1"

    # embedding remote: baseUrl + easyllmId are plain values
    remote = cfg["agents"]["defaults"]["memorySearch"]["remote"]
    assert remote["baseUrl"] == "https://expanded-url.example/embed"
    assert remote["easyllmId"] == "easy-id-123"


def test_resolved_config_legacy_embedding_api_key_field_warns(caplog):
    """Backward-compat: yaml with plain api_key still works but warns."""
    embedding = {
        "provider": "sophnet",
        "model": "text-embeddings",
        "api_key": "sk-legacy-leaked",  # legacy plain field
        "base_url": "https://...",
        "easyllm_id": "x",
        "output_dimensionality": 1024,
    }
    with caplog.at_level("WARNING"):
        cfg = build_openclaw_resolved_config(**_BASE, embedding=embedding)

    # Legacy path leaks (this is a known smell, hence the warning)
    remote = cfg["agents"]["defaults"]["memorySearch"]["remote"]
    assert remote["apiKey"] == "sk-legacy-leaked"
    # But we logged the warning so reviewers can grep for it
    assert any("api_key_env" in r.message for r in caplog.records)


def test_resolved_config_agent_llm_missing_api_key_env_raises():
    agent_llm = _agent_llm_fixture()
    del agent_llm["api_key_env"]
    with pytest.raises(ValueError, match="api_key_env is required"):
        build_openclaw_resolved_config(**_BASE, agent_llm=agent_llm)


# --- plugins section -------------------------------------------------

def test_resolved_config_plugins_for_memory_core_baseline():
    cfg = build_openclaw_resolved_config(
        **_BASE, memory_mode="memory-core",
    )
    p = cfg["plugins"]
    assert p["allow"] == ["memory-core"]
    assert p["slots"] == {"memory": "memory-core"}
    assert p["entries"] == {"memory-core": {"enabled": True}}


def test_resolved_config_plugins_for_noop():
    """noop mode: plugin still loaded, but memorySearch disabled."""
    cfg = build_openclaw_resolved_config(
        **_BASE, memory_mode="noop",
    )
    p = cfg["plugins"]
    assert p["entries"]["memory-core"]["enabled"] is True  # plugin still loaded
    # Memory tools removed via memorySearch.enabled, not via plugin entry
    assert cfg["agents"]["defaults"]["memorySearch"]["enabled"] is False


def test_resolved_config_plugins_for_third_party():
    cfg = build_openclaw_resolved_config(
        **_BASE, memory_mode="mem0-openclaw",
    )
    p = cfg["plugins"]
    assert "memory-core" in p["allow"]
    assert "mem0-openclaw" in p["allow"]
    assert p["slots"]["memory"] == "mem0-openclaw"
    assert p["entries"]["memory-core"]["enabled"] is False
    assert p["entries"]["mem0-openclaw"]["enabled"] is True


def test_resolved_config_memory_core_default_when_no_memory_mode():
    cfg = build_openclaw_resolved_config(**_BASE)
    p = cfg["plugins"]
    assert p["slots"] == {"memory": "memory-core"}
    assert cfg["agents"]["defaults"]["memorySearch"]["enabled"] is True


# --- noop disables memorySearch -------------------------------------

def test_noop_disables_memory_search_enabled():
    cfg = build_openclaw_resolved_config(**_BASE, memory_mode="noop")
    assert cfg["agents"]["defaults"]["memorySearch"]["enabled"] is False


def test_noop_omits_embedding_credentials_block(monkeypatch):
    """v0.7 Codex r7 F1: noop mode must NOT emit ``${SOPH_API_KEY}``-style
    template strings in the resolved config. OpenClaw evaluates env
    substitution at startup before runtime can ignore disabled blocks,
    so a disabled-but-credential-bearing block fails on any deployment
    without sophnet env vars.

    Even when an embedding fixture is passed (as it is in our
    openclaw-docker-noop.yaml), the resolved config for noop mode must
    omit provider/remote and use ``provider="auto"``.
    """
    monkeypatch.delenv("SOPH_API_KEY", raising=False)
    cfg = build_openclaw_resolved_config(
        **_BASE,
        memory_mode="noop",
        embedding=_embedding_fixture(),  # has api_key_env="SOPH_API_KEY"
    )
    serialized = json.dumps(cfg)
    assert "${SOPH_API_KEY}" not in serialized, (
        "noop mode leaked sophnet credential template into resolved config; "
        "OpenClaw will throw MissingEnvVarError if SOPH_API_KEY is unset"
    )
    ms = cfg["agents"]["defaults"]["memorySearch"]
    assert ms["enabled"] is False
    assert ms["provider"] == "auto"
    assert "remote" not in ms
    assert "outputDimensionality" not in ms


def test_memory_core_keeps_memory_search_enabled():
    cfg = build_openclaw_resolved_config(**_BASE, memory_mode="memory-core")
    assert cfg["agents"]["defaults"]["memorySearch"]["enabled"] is True


# --- backward-compat baseline (no agent_llm, no memory_mode) --------

def test_resolved_config_minimal_backward_compatible():
    """Old call signature (no agent_llm, no memory_mode) still produces
    a valid config equivalent to v0.5 baseline + plugins section."""
    cfg = build_openclaw_resolved_config(
        workspace_dir="/tmp/ws",
        native_store_dir="/tmp/state",
        backend_mode="hybrid",
        flush_mode="shared_llm",
        embedding={
            "provider": "sophnet",
            "model": "text-embeddings",
            "api_key_env": "SOPH_API_KEY",
            "base_url": "https://...",
            "easyllm_id": "x",
            "output_dimensionality": 1024,
        },
    )
    assert cfg["memory"]["backend"] == "builtin"
    assert cfg["agents"]["defaults"]["workspace"] == "/tmp/ws"
    # No agent_llm => no models.providers
    assert "models" not in cfg or not cfg["models"].get("providers")
    # No agent_llm => no agents.defaults.model
    assert "model" not in cfg["agents"]["defaults"]
    # plugins section emitted with memory-core baseline
    assert cfg["plugins"]["slots"] == {"memory": "memory-core"}


def test_resolved_config_preserves_existing_agents_defaults_keys():
    """agents.defaults must still have workspace/userTimezone/memorySearch/compaction."""
    cfg = build_openclaw_resolved_config(
        **_BASE,
        agent_llm=_agent_llm_fixture(),
        embedding=_embedding_fixture(),
    )
    defaults = cfg["agents"]["defaults"]
    for required in ["workspace", "userTimezone", "memorySearch", "compaction", "model"]:
        assert required in defaults, f"missing {required} in agents.defaults"


# --- fts_only path ---------------------------------------------------

def test_resolved_config_fts_only_uses_auto_provider():
    cfg = build_openclaw_resolved_config(
        workspace_dir="/tmp/ws",
        native_store_dir="/tmp/state",
        backend_mode="fts_only",
        flush_mode="shared_llm",
    )
    ms = cfg["agents"]["defaults"]["memorySearch"]
    assert ms["provider"] == "auto"
    assert ms["store"]["vector"]["enabled"] is False

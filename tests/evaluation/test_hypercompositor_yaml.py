"""Stage 3 Phase 5 — hypercompositor yaml structural tests.

Validates that the new system yaml composes correctly through the
Phase 2 resolved-config + docker adapter wiring. These are pure
structural checks — they don't actually run docker or load openclaw.

Run:
    .venv/bin/python -m pytest tests/evaluation/test_hypercompositor_yaml.py -v
"""
from __future__ import annotations

from evaluation.src.adapters.openclaw.resolved_config import (
    build_openclaw_resolved_config,
)
from evaluation.src.config.system_loader import resolve_system_config


FAKE_ENVIRONMENT = {
    "LLM_API_KEY": "llm-key",
    "LLM_BASE_URL": "https://llm.example/v1",
    "OPENCLAW_EMBED_MODEL": "embedding-model",
    "OPENCLAW_EMBED_PROVIDER": "test-provider",
    "OPENCLAW_REPO_PATH": "/tmp/openclaw",
    "SOPH_EMBED_EASYLLM_ID": "deployment",
    "SOPH_EMBED_URL": "https://embed.example/v1",
}


def _load_yaml() -> dict:
    """Resolve the public id so structural tests follow registry moves."""
    return resolve_system_config(
        "openclaw-hypercompositor", environ=FAKE_ENVIRONMENT
    ).raw_config


# --- yaml shape -----------------------------------------------------------

class TestYamlShape:
    def test_public_id_resolves_from_experiments_directory(self):
        resolved = resolve_system_config(
            "openclaw-hypercompositor", environ=FAKE_ENVIRONMENT
        )

        assert resolved.source_paths[-1].as_posix().endswith(
            "/systems/experiments/openclaw-hypercompositor.yaml"
        )

    def test_adapter_is_openclaw(self):
        cfg = _load_yaml()
        assert cfg["adapter"] == "openclaw"

    def test_dual_slot_modes_set(self):
        """Memory slot stays on bundled memory-core; context-engine slot
        binds the installed hypercompositor plugin. hypermem is hypercompositor's
        internal storage dep, not a standalone openclaw plugin."""
        oc = _load_yaml()["openclaw"]
        assert oc["memory_mode"] == "memory-core"
        assert oc["context_engine_mode"] == "hypercompositor"

    def test_answer_mode_agent_local(self):
        """Phase 5 prototype runs the openclaw agent locally; without this
        the bridge spawns a different code path and the engine never
        loads through the agent_run RPC."""
        oc = _load_yaml()["openclaw"]
        assert oc["answer_mode"] == "agent_local"

    def test_agent_llm_required_fields(self):
        """build_openclaw_resolved_config asserts these on construction."""
        agent_llm = _load_yaml()["openclaw"]["agent_llm"]
        for k in ("provider_id", "base_url", "api_key_env"):
            assert k in agent_llm
        for k in ("id", "name", "context_window", "max_tokens"):
            assert k in agent_llm["model"]


# --- resolved config integration -----------------------------------------

class TestResolvedConfigIntegration:
    """Round-trip the yaml's openclaw block through build_openclaw_resolved_config
    and assert the output drives Phase 1's entrypoint render correctly."""

    def _resolved(self) -> dict:
        oc = _load_yaml()["openclaw"]
        # Stub the env-substituted fields with concrete strings so the
        # builder doesn't crash on ${VAR} placeholders.
        agent_llm = dict(oc["agent_llm"])
        agent_llm["base_url"] = "https://example/v1"
        embedding = dict(oc["embedding"])
        embedding["base_url"] = "https://example/embed"
        embedding["easyllm_id"] = "test-easyllm"
        return build_openclaw_resolved_config(
            workspace_dir="/ws",
            native_store_dir="/ws/state",
            backend_mode=oc["backend_mode"],
            memory_mode=oc["memory_mode"],
            context_engine_mode=oc["context_engine_mode"],
            agent_llm=agent_llm,
            embedding=embedding,
        )

    def test_plugins_allow_includes_memory_core_and_context_engine(self):
        """memory-core (bundled memory) + hypercompositor (context-engine
        install). hypermem is a transitive npm dep of hypercompositor, not
        a standalone openclaw plugin, so it's NOT in plugins.allow."""
        cfg = self._resolved()
        allow = cfg["plugins"]["allow"]
        assert "memory-core" in allow
        assert "hypercompositor" in allow
        assert "hypermem" not in allow

    def test_slot_resolution(self):
        cfg = self._resolved()
        slots = cfg["plugins"]["slots"]
        assert slots["memory"] == "memory-core"
        assert slots["contextEngine"] == "hypercompositor"

    def test_entry_states(self):
        """Both slots-bound plugins enabled."""
        cfg = self._resolved()
        entries = cfg["plugins"]["entries"]
        assert entries["memory-core"]["enabled"] is True
        assert entries["hypercompositor"]["enabled"] is True

    def test_memory_search_enabled(self):
        """memory_mode != 'noop' -> memorySearch.enabled is True."""
        cfg = self._resolved()
        assert (
            cfg["agents"]["defaults"]["memorySearch"]["enabled"] is True
        )

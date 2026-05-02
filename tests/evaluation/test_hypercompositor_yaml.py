"""Stage 3 Phase 5 — hypercompositor yaml structural tests.

Validates that the new system yaml composes correctly through the
Phase 2 resolved-config + docker adapter wiring. These are pure
structural checks — they don't actually run docker or load openclaw.

Run:
    .venv/bin/python -m pytest tests/evaluation/test_hypercompositor_yaml.py -v
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from evaluation.src.adapters.openclaw_resolved_config import (
    build_openclaw_resolved_config,
)


REPO_ROOT = Path(__file__).parents[2]
YAML_PATH = (
    REPO_ROOT / "evaluation" / "config" / "systems" / "openclaw-hypercompositor.yaml"
)


def _load_yaml() -> dict:
    """Load yaml without env substitution. Raw dict tests don't care
    about ${VAR} placeholder expansion."""
    return yaml.safe_load(YAML_PATH.read_text())


# --- yaml shape -----------------------------------------------------------

class TestYamlShape:
    def test_yaml_exists(self):
        assert YAML_PATH.is_file()

    def test_adapter_is_openclaw(self):
        cfg = _load_yaml()
        assert cfg["adapter"] == "openclaw"

    def test_dual_slot_modes_set(self):
        """Both memory and context-engine slots must declare a non-empty id;
        empty/missing context_engine_mode would route to default 'legacy'."""
        oc = _load_yaml()["openclaw"]
        assert oc["memory_mode"] == "hypermem"
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
            flush_mode=oc["flush_mode"],
            memory_mode=oc["memory_mode"],
            context_engine_mode=oc["context_engine_mode"],
            agent_llm=agent_llm,
            embedding=embedding,
        )

    def test_plugins_allow_includes_both_plugins_plus_memory_core(self):
        """memory-core stays in allow because it's the bundled fallback."""
        cfg = self._resolved()
        allow = cfg["plugins"]["allow"]
        assert "memory-core" in allow
        assert "hypermem" in allow
        assert "hypercompositor" in allow

    def test_slot_resolution(self):
        cfg = self._resolved()
        slots = cfg["plugins"]["slots"]
        assert slots["memory"] == "hypermem"
        assert slots["contextEngine"] == "hypercompositor"

    def test_entry_states(self):
        """Both target plugins enabled; bundled memory-core disabled (we
        substituted hypermem as the memory slot)."""
        cfg = self._resolved()
        entries = cfg["plugins"]["entries"]
        assert entries["hypermem"]["enabled"] is True
        assert entries["hypercompositor"]["enabled"] is True
        assert entries["memory-core"]["enabled"] is False

    def test_memory_search_enabled(self):
        """memory_mode != 'noop' -> memorySearch.enabled is True."""
        cfg = self._resolved()
        assert (
            cfg["agents"]["defaults"]["memorySearch"]["enabled"] is True
        )

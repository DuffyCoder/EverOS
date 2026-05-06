"""Tests for OpenClawDockerAdapter env emission with context_engine_mode.

Phase 2 of the Stage 3 plan: when yaml has `context_engine_mode`, the
docker adapter must emit CONTEXT_ENGINE_PLUGIN_ID into the docker run
env so entrypoint.sh's Phase 1 jq render picks it up.

Run:
    .venv/bin/python -m pytest tests/evaluation/test_docker_adapter_context_engine_env.py -v
"""
from __future__ import annotations

import pytest

from evaluation.src.adapters.openclaw_docker_adapter import DockerizedOpenclawAdapter


def _make_adapter(openclaw_cfg: dict, openclaw_docker_cfg: dict | None = None):
    """Build a minimal adapter with just the cfg slots _docker_env_for_container reads."""
    adapter = DockerizedOpenclawAdapter.__new__(DockerizedOpenclawAdapter)
    # _docker_env_for_container only reads self._openclaw_cfg
    adapter._openclaw_cfg = openclaw_cfg
    adapter._openclaw_docker_cfg = openclaw_docker_cfg or {}
    return adapter


def _env_dict(pairs):
    """Convert [(k, v), ...] pairs to dict, dropping Nones."""
    return {k: v for k, v in pairs if v is not None}


class TestContextEngineEnvEmit:
    def test_no_context_engine_mode_no_env(self):
        """Memory-only run: no CONTEXT_ENGINE_PLUGIN_ID env emitted."""
        adapter = _make_adapter({
            "memory_mode": "mem0",
            "agent_llm": {"env_vars": [], "model": {"id": "gpt-4.1-mini"}},
        })
        env = _env_dict(adapter._docker_env_for_container(conv_id="locomo_0"))
        assert "CONTEXT_ENGINE_PLUGIN_ID" not in env
        assert env["MEMORY_PLUGIN_ID"] == "mem0"

    def test_context_engine_only_emits_id(self):
        """Pure context-engine run: id flows into env."""
        adapter = _make_adapter({
            "memory_mode": "noop",
            "context_engine_mode": "hindsight",
            "agent_llm": {"env_vars": [], "model": {"id": "gpt-4.1-mini"}},
        })
        env = _env_dict(adapter._docker_env_for_container())
        assert env["CONTEXT_ENGINE_PLUGIN_ID"] == "hindsight"

    def test_dual_mode_emits_both(self):
        """Memory + context-engine both set: both env vars present."""
        adapter = _make_adapter({
            "memory_mode": "mem0",
            "context_engine_mode": "hindsight",
            "agent_llm": {"env_vars": [], "model": {"id": "gpt-4.1-mini"}},
        })
        env = _env_dict(adapter._docker_env_for_container())
        assert env["MEMORY_PLUGIN_ID"] == "mem0"
        assert env["CONTEXT_ENGINE_PLUGIN_ID"] == "hindsight"

    def test_context_engine_empty_string_treated_as_unset(self):
        """yaml ${VAR:default} expansion can yield empty string — must
        not emit CONTEXT_ENGINE_PLUGIN_ID= empty (would trip entrypoint
        wiring with an unregistered slot value).
        """
        adapter = _make_adapter({
            "memory_mode": "mem0",
            "context_engine_mode": "",
            "agent_llm": {"env_vars": [], "model": {"id": "gpt-4.1-mini"}},
        })
        env = _env_dict(adapter._docker_env_for_container())
        assert "CONTEXT_ENGINE_PLUGIN_ID" not in env

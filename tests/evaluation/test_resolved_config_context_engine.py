"""Tests for build_openclaw_resolved_config(context_engine_mode=...) and
_build_plugins_section composition with both memory + contextEngine plugins.

Phase 2 of the Stage 3 plan: yaml `context_engine_mode` must flow into
plugins.allow / plugins.slots.contextEngine / plugins.entries alongside
the existing memory_mode wiring, without breaking memory-only callers.

Run:
    .venv/bin/python -m pytest tests/evaluation/test_resolved_config_context_engine.py -v
"""
from __future__ import annotations

import pytest

from evaluation.src.adapters.openclaw.resolved_config import (
    _build_plugins_section,
    build_openclaw_resolved_config,
)


# Minimal stub agent_llm so build_openclaw_resolved_config doesn't crash on
# missing fields. We only care about plugins composition here.
_STUB_AGENT_LLM = {
    "provider_id": "sophnet",
    "base_url": "https://www.sophnet.com/api/open-apis/v1",
    "api": "openai-completions",
    "api_key_env": "LLM_API_KEY",
    "env_vars": ["LLM_API_KEY"],
    "model": {
        "id": "gpt-4.1-mini",
        "name": "GPT 4.1 Mini",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "context_window": 128000,
        "max_tokens": 4096,
    },
}


class TestBuildPluginsSection:
    """Direct tests of _build_plugins_section composition logic."""

    def test_memory_only_unchanged(self):
        """No context_engine_mode -> identical to pre-Stage-3 behavior."""
        out = _build_plugins_section("mem0", context_engine_mode=None)
        assert out["slots"] == {"memory": "mem0"}
        assert "contextEngine" not in out["slots"]
        assert "memory-core" in out["allow"]
        assert "mem0" in out["allow"]
        assert out["entries"]["mem0"]["enabled"] is True

    def test_memory_core_baseline_unchanged(self):
        out = _build_plugins_section("memory-core", context_engine_mode=None)
        assert out["slots"] == {"memory": "memory-core"}
        assert out["allow"] == ["memory-core"]

    def test_pure_context_engine_mode(self):
        """memory_mode='noop' + context_engine_mode='hindsight'."""
        out = _build_plugins_section("noop", context_engine_mode="hindsight")
        assert out["slots"]["memory"] == "memory-core"
        assert out["slots"]["contextEngine"] == "hindsight"
        assert "hindsight" in out["allow"]
        assert out["entries"]["hindsight"]["enabled"] is True

    def test_dual_mode_memory_and_context_engine(self):
        """Both plugins coexist in their respective slots."""
        out = _build_plugins_section("mem0", context_engine_mode="hindsight")
        assert out["slots"]["memory"] == "mem0"
        assert out["slots"]["contextEngine"] == "hindsight"
        assert set(out["allow"]) >= {"memory-core", "mem0", "hindsight"}
        assert out["entries"]["mem0"]["enabled"] is True
        assert out["entries"]["hindsight"]["enabled"] is True

    def test_context_engine_mode_empty_string_treated_as_unset(self):
        """Defensive: '' in yaml shouldn't accidentally render an empty slot."""
        out = _build_plugins_section("mem0", context_engine_mode="")
        assert "contextEngine" not in out["slots"]


class TestBuildOpenclawResolvedConfigCEMode:
    """End-to-end resolved config emission with context_engine_mode."""

    def _build(self, **kwargs):
        defaults = dict(
            workspace_dir="/ws",
            native_store_dir="/ws/state",
            backend_mode="fts_only",
            agent_llm=_STUB_AGENT_LLM,
        )
        defaults.update(kwargs)
        return build_openclaw_resolved_config(**defaults)

    def test_default_no_context_engine(self):
        cfg = self._build(memory_mode="memory-core")
        assert cfg["plugins"]["slots"] == {"memory": "memory-core"}

    def test_context_engine_only(self):
        cfg = self._build(memory_mode="noop", context_engine_mode="hindsight")
        assert cfg["plugins"]["slots"]["contextEngine"] == "hindsight"
        assert cfg["plugins"]["slots"]["memory"] == "memory-core"

    def test_dual_mode(self):
        cfg = self._build(memory_mode="mem0", context_engine_mode="hindsight")
        assert cfg["plugins"]["slots"]["memory"] == "mem0"
        assert cfg["plugins"]["slots"]["contextEngine"] == "hindsight"

    def test_existing_memory_only_callers_unchanged(self):
        """Backward-compat: callers passing only memory_mode (no kw arg
        for context_engine_mode) get the same dict shape as before.
        """
        cfg = self._build(memory_mode="evermemos")
        assert "contextEngine" not in cfg["plugins"]["slots"]
        # all the other keys still present
        assert "allow" in cfg["plugins"]
        assert "entries" in cfg["plugins"]

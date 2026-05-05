"""A1 — _flush_and_settle_if_needed must skip settle wait for context-engine mode.

When yaml sets context_engine_mode + memory_mode in {memory-core, noop},
hypercompositor (or any context-engine plugin) intercepts ingest before
the memory backend is touched. Memory backend status reports settled=false
forever, breaking the eval pipeline at the add() boundary.

Fix: short-circuit settle check when context_engine_mode is set AND memory
slot is bundled (memory-core/noop). The visibility_state is set to "settled"
to satisfy downstream invariants.

Run:
    .venv/bin/python -m pytest tests/evaluation/test_flush_settle_context_engine.py -v
"""
from __future__ import annotations

import pytest

from evaluation.src.adapters.openclaw_adapter import OpenClawAdapter


def _adapter(openclaw_cfg: dict) -> OpenClawAdapter:
    """Build a minimal adapter without going through register_adapter discovery."""
    adapter = OpenClawAdapter.__new__(OpenClawAdapter)
    adapter._openclaw_cfg = openclaw_cfg
    # Stub out the only method _flush_and_settle_if_needed touches besides cfg.
    adapter._invoke_bridge = None  # should never be called in skip path
    adapter._status_timeout = lambda: 30
    adapter._bridge_base_payload = lambda sandbox: {}
    adapter._append_events = lambda sandbox, events: None
    return adapter


@pytest.mark.asyncio
class TestFlushSettleSkipsForContextEngine:
    """Settle wait should be bypassed when context_engine_mode is set."""

    async def test_skips_settle_when_context_engine_with_memory_core(self):
        """context-engine + bundled memory: skip status RPC entirely."""
        adapter = _adapter({
            "memory_mode": "memory-core",
            "context_engine_mode": "hypercompositor",
        })
        sandbox = {
            "visibility_mode": "settled",
            "conversation_id": "locomo_0",
        }
        # _invoke_bridge is None — a call would TypeError. Method must skip it.
        await adapter._flush_and_settle_if_needed(sandbox)
        assert sandbox["visibility_state"] == "settled"

    async def test_skips_settle_when_context_engine_with_noop(self):
        """noop memory + context-engine: also skip."""
        adapter = _adapter({
            "memory_mode": "noop",
            "context_engine_mode": "hypercompositor",
        })
        sandbox = {
            "visibility_mode": "settled",
            "conversation_id": "locomo_0",
        }
        await adapter._flush_and_settle_if_needed(sandbox)
        assert sandbox["visibility_state"] == "settled"

    async def test_eventual_visibility_unchanged(self):
        """visibility_mode=eventual already short-circuits — preserved."""
        adapter = _adapter({
            "memory_mode": "memory-core",
            "context_engine_mode": "hypercompositor",
        })
        sandbox = {
            "visibility_mode": "eventual",
            "conversation_id": "locomo_0",
        }
        await adapter._flush_and_settle_if_needed(sandbox)
        assert sandbox["visibility_state"] == "indexed"


@pytest.mark.asyncio
class TestFlushSettleEnforcesForMemoryPlugin:
    """When context_engine_mode is unset OR memory plugin is the slot owner,
    the settle invariant must still hold (Stage 1/2 contract preserved)."""

    async def test_no_context_engine_still_calls_bridge(self):
        """Pure memory plugin run: bridge must be called for status."""
        called = {"count": 0}

        async def fake_bridge(sandbox, payload, timeout):
            called["count"] += 1
            return {"settled": True, "flush_epoch": 42}

        adapter = _adapter({
            "memory_mode": "memory-core",
            # no context_engine_mode
        })
        adapter._invoke_bridge = fake_bridge
        sandbox = {
            "visibility_mode": "settled",
            "conversation_id": "locomo_0",
        }
        await adapter._flush_and_settle_if_needed(sandbox)
        assert called["count"] == 1
        assert sandbox["visibility_state"] == "settled"

    async def test_external_memory_plugin_still_calls_bridge(self):
        """Stage 1 memory-mode (e.g. mem0) keeps the settle invariant."""
        called = {"count": 0}

        async def fake_bridge(sandbox, payload, timeout):
            called["count"] += 1
            return {"settled": True, "flush_epoch": 7}

        adapter = _adapter({
            "memory_mode": "mem0",
            # no context_engine_mode
        })
        adapter._invoke_bridge = fake_bridge
        sandbox = {
            "visibility_mode": "settled",
            "conversation_id": "locomo_0",
        }
        await adapter._flush_and_settle_if_needed(sandbox)
        assert called["count"] == 1
        assert sandbox["visibility_state"] == "settled"

    async def test_external_memory_with_context_engine_still_settles(self):
        """Edge case: a real memory plugin + a context-engine plugin
        co-exist. The memory plugin IS the slot owner, so settle still
        applies — context-engine intercepts assemble/ingest but not
        memory-search index. Don't skip."""
        called = {"count": 0}

        async def fake_bridge(sandbox, payload, timeout):
            called["count"] += 1
            return {"settled": True, "flush_epoch": 1}

        adapter = _adapter({
            "memory_mode": "mem0",
            "context_engine_mode": "hypercompositor",
        })
        adapter._invoke_bridge = fake_bridge
        sandbox = {
            "visibility_mode": "settled",
            "conversation_id": "locomo_0",
        }
        await adapter._flush_and_settle_if_needed(sandbox)
        assert called["count"] == 1

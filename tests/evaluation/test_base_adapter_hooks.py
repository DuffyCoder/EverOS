"""Verify BaseAdapter exposes wait_post_add_settle hook with no-op default.

Task 1 of 2026-05-21 ov-global-settle-active-poll plan.
"""
import pytest

from evaluation.src.adapters.base import BaseAdapter


class _StubAdapter(BaseAdapter):
    """Minimal adapter that satisfies abstract methods so we can test hooks."""

    async def add(self, conversations, **kwargs):
        return None

    async def search(self, query, conversation_id, index, **kwargs):
        return None


@pytest.mark.asyncio
async def test_wait_post_add_settle_default_is_noop():
    adapter = _StubAdapter(config={})
    result = await adapter.wait_post_add_settle()
    assert result is None

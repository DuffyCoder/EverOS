"""Regression test: DockerizedOpenclawAdapter.prepare() is idempotent.

The pipeline now calls ``adapter.prepare()`` explicitly on the replay path
(``--stages search answer evaluate``, i.e. Add skipped) so the per-conversation
docker runtime is spawned even though add() never ran. add() ALSO calls
prepare() internally on the full path, so prepare() can be invoked twice in one
process. Without a guard the second call re-spawns a duplicate container per
conversation. The guard ``if self._docker_handles: return`` makes prepare() a
no-op once containers already exist.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from evaluation.src.adapters.openclaw_docker_adapter import (
    DockerizedOpenclawAdapter,
)


def _make_adapter() -> DockerizedOpenclawAdapter:
    adapter = DockerizedOpenclawAdapter.__new__(DockerizedOpenclawAdapter)
    adapter._docker_handles = {}
    adapter._sandbox_by_conversation_id = {}
    adapter._image = "img:test"
    return adapter


@pytest.mark.asyncio
async def test_prepare_skips_spawn_when_handles_present():
    """If containers are already tracked (add() ran, or prepare() was already
    called), a second prepare() must not spawn anything."""
    adapter = _make_adapter()
    adapter._docker_handles = {"conv0": {"container_id": "abc", "image": "img:test"}}

    spawned: list[str] = []

    async def fake_run(conv_id, sandbox):
        spawned.append(conv_id)
        return "newcid"

    with patch.object(adapter, "_docker_run_container", side_effect=fake_run):
        await adapter.prepare(conversations=[], output_dir="/tmp/x")

    assert spawned == [], (
        "prepare() must short-circuit when _docker_handles is non-empty; "
        f"instead it spawned: {spawned}"
    )

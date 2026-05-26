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

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from evaluation.src.adapters.openclaw_docker_adapter import (
    DockerizedOpenclawAdapter,
)
from evaluation.src.core.data_models import Conversation


def _make_adapter() -> DockerizedOpenclawAdapter:
    adapter = DockerizedOpenclawAdapter.__new__(DockerizedOpenclawAdapter)
    adapter._docker_handles = {}
    adapter._sandbox_by_conversation_id = {}
    adapter._image = "img:test"
    return adapter


def _conv(cid: str) -> Conversation:
    return Conversation(conversation_id=cid, messages=[])


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
        "prepare() must short-circuit when all requested conversations already "
        f"have a handle; instead it spawned: {spawned}"
    )


@pytest.mark.asyncio
async def test_prepare_spawns_only_missing_after_partial_failure():
    """If a prior prepare() spawned some containers then raised mid-gather, a
    later prepare() must spawn ONLY the conversations still missing a handle —
    not return early (leaving them unrecoverable) and not re-spawn existing
    ones. Regression for the old ``if self._docker_handles: return`` guard."""
    adapter = _make_adapter()
    adapter._prepared = False
    adapter._run_id = "run-existing"  # already resolved → no LATEST clobber
    adapter.output_dir = "/tmp/x"
    # conv0 was spawned by the prior (partially-failed) prepare(); conv1 dropped.
    adapter._docker_handles = {"conv0": {"container_id": "abc", "image": "img:test"}}

    spawned: list[str] = []

    async def fake_run(conv_id, sandbox):
        spawned.append(conv_id)
        return f"cid-{conv_id}"

    convs = [_conv("conv0"), _conv("conv1")]

    with patch.object(adapter, "_docker_run_container", side_effect=fake_run), \
         patch.object(adapter, "_stop_orphan_containers", new=AsyncMock()), \
         patch.object(adapter, "_ensure_spawn_sem",
                      new=AsyncMock(return_value=asyncio.Semaphore(4))), \
         patch.object(adapter, "_resolve_run_root",
                      return_value=Path("/tmp/x/artifacts/openclaw/run-existing")), \
         patch.object(adapter, "_prepare_conversation_sandbox",
                      return_value={"workspace_dir": "/tmp/ws"}):
        await adapter.prepare(conversations=convs, output_dir="/tmp/x")

    assert spawned == ["conv1"], (
        f"prepare() must spawn only the missing conversation; got {spawned}"
    )
    assert "conv1" in adapter._docker_handles, "conv1 must now be recovered"
    assert adapter._docker_handles["conv0"]["container_id"] == "abc", (
        "the pre-existing conv0 handle must be left untouched"
    )

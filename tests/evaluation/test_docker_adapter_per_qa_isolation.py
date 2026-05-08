"""Integration tests for DockerizedOpenclawAdapter PR4 per-QA isolation wiring.

Verifies that the adapter's `_isolation_mode`, `_ensure_state_frozen`,
`_restore_qa_state_dir`, and `_discard_qa_state` methods correctly
delegate to evaluation/src/adapters/openclaw/per_qa_isolation.py and
behave as the public contract expects. Pure functions are tested in
test_per_qa_isolation.py; this file covers the adapter glue.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evaluation.src.adapters.openclaw.per_qa_isolation import (
    QA_STATES_DIRNAME,
    SNAPSHOT_DIRNAME,
)
from evaluation.src.adapters.openclaw_docker_adapter import (
    DockerizedOpenclawAdapter,
)


def _make_adapter(
    openclaw_cfg: dict,
    docker_cfg: dict | None = None,
):
    """Bypass __init__ so we don't need a real image / mongo / etc."""
    adapter = DockerizedOpenclawAdapter.__new__(DockerizedOpenclawAdapter)
    adapter._openclaw_cfg = openclaw_cfg
    adapter._docker_cfg = docker_cfg or {}
    # _append_events writes to sandbox-level events; tests don't care.
    adapter._append_events = lambda sandbox, events: None
    return adapter


def _sandbox(workspace: Path, conv_id: str = "conv0") -> dict:
    return {
        "conversation_id": conv_id,
        "workspace_dir": str(workspace),
    }


# ---------- _isolation_mode ------------------------------------------------

def test_isolation_mode_auto_off_for_memory_only():
    """Default 'auto' + no context engine = off."""
    adapter = _make_adapter(
        openclaw_cfg={"memory_mode": "evermemos"},
        docker_cfg={},
    )
    assert adapter._isolation_mode() == "off"


def test_isolation_mode_auto_snapshot_for_context_engine():
    """Default 'auto' + context engine = snapshot."""
    adapter = _make_adapter(
        openclaw_cfg={
            "memory_mode": "memory-core",
            "context_engine_mode": "hypercompositor",
        },
        docker_cfg={},
    )
    assert adapter._isolation_mode() == "snapshot"


def test_isolation_mode_explicit_off_overrides_auto():
    adapter = _make_adapter(
        openclaw_cfg={
            "memory_mode": "memory-core",
            "context_engine_mode": "hypercompositor",
        },
        docker_cfg={"per_qa_isolation": "off"},
    )
    assert adapter._isolation_mode() == "off"


def test_isolation_mode_explicit_snapshot_overrides_auto():
    adapter = _make_adapter(
        openclaw_cfg={"memory_mode": "evermemos"},
        docker_cfg={"per_qa_isolation": "snapshot"},
    )
    assert adapter._isolation_mode() == "snapshot"


# ---------- _ensure_state_frozen + _restore_qa_state_dir + discard ---------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_ensure_state_frozen_no_op_when_isolation_off(tmp_path: Path):
    adapter = _make_adapter(
        openclaw_cfg={"memory_mode": "evermemos"},
        docker_cfg={"per_qa_isolation": "off"},
    )
    sandbox = _sandbox(tmp_path)
    asyncio.run(adapter._ensure_state_frozen(sandbox))
    # No baseline created.
    assert not (tmp_path / SNAPSHOT_DIRNAME).exists()
    # Sandbox not marked as frozen.
    assert "_pr4_state_frozen" not in sandbox


def test_ensure_state_frozen_creates_baseline_when_snapshot(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "memory.sqlite").write_bytes(b"abc")

    adapter = _make_adapter(
        openclaw_cfg={"context_engine_mode": "hypercompositor"},
        docker_cfg={"per_qa_isolation": "snapshot"},
    )
    sandbox = _sandbox(tmp_path)
    asyncio.run(adapter._ensure_state_frozen(sandbox))
    assert (tmp_path / SNAPSHOT_DIRNAME / "memory.sqlite").read_bytes() == b"abc"
    assert sandbox["_pr4_state_frozen"] is True


def test_ensure_state_frozen_is_idempotent(tmp_path: Path):
    """Second call should not redo cp -r (sandbox flag short-circuits).

    Mutating the source state between calls and asserting baseline is
    still the original proves the second freeze didn't run.
    """
    state = tmp_path / "state"
    state.mkdir()
    (state / "x.txt").write_text("v1")

    adapter = _make_adapter(
        openclaw_cfg={"context_engine_mode": "hypercompositor"},
        docker_cfg={"per_qa_isolation": "snapshot"},
    )
    sandbox = _sandbox(tmp_path)
    asyncio.run(adapter._ensure_state_frozen(sandbox))
    # Modify source.
    (state / "x.txt").write_text("v2")
    asyncio.run(adapter._ensure_state_frozen(sandbox))
    # Baseline still has v1 (re-freeze didn't fire).
    assert (tmp_path / SNAPSHOT_DIRNAME / "x.txt").read_text() == "v1"


def test_restore_qa_state_dir_returns_default_when_off(tmp_path: Path):
    adapter = _make_adapter(
        openclaw_cfg={"memory_mode": "evermemos"},
        docker_cfg={"per_qa_isolation": "off"},
    )
    sandbox = _sandbox(tmp_path)
    container_path = asyncio.run(adapter._restore_qa_state_dir(sandbox, "qa0"))
    assert container_path == "/workspace/state"
    # No per-QA dir created.
    assert not (tmp_path / QA_STATES_DIRNAME).exists()


def test_restore_qa_state_dir_returns_per_qa_path_when_snapshot(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "memory.sqlite").write_bytes(b"baseline")

    adapter = _make_adapter(
        openclaw_cfg={"context_engine_mode": "hypercompositor"},
        docker_cfg={"per_qa_isolation": "snapshot"},
    )
    sandbox = _sandbox(tmp_path)
    asyncio.run(adapter._ensure_state_frozen(sandbox))
    container_path = asyncio.run(
        adapter._restore_qa_state_dir(sandbox, "conv0__qa3")
    )
    assert container_path == "/workspace/.qa_states/conv0__qa3"
    # Host-side dir exists with copied baseline.
    qa_host = tmp_path / QA_STATES_DIRNAME / "conv0__qa3"
    assert qa_host.exists()
    assert (qa_host / "memory.sqlite").read_bytes() == b"baseline"


def test_discard_qa_state_when_snapshot(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    adapter = _make_adapter(
        openclaw_cfg={"context_engine_mode": "hypercompositor"},
        docker_cfg={"per_qa_isolation": "snapshot"},
    )
    sandbox = _sandbox(tmp_path)
    asyncio.run(adapter._ensure_state_frozen(sandbox))
    asyncio.run(adapter._restore_qa_state_dir(sandbox, "qa0"))
    qa_host = tmp_path / QA_STATES_DIRNAME / "qa0"
    assert qa_host.exists()
    asyncio.run(adapter._discard_qa_state(sandbox, "qa0"))
    assert not qa_host.exists()


def test_discard_qa_state_no_op_when_off(tmp_path: Path):
    """Even after manually creating a fake QA dir, discard does nothing
    when isolation is off (the adapter doesn't manage per-QA dirs in
    that mode)."""
    qa_host = tmp_path / QA_STATES_DIRNAME / "qa0"
    qa_host.mkdir(parents=True)
    adapter = _make_adapter(
        openclaw_cfg={"memory_mode": "evermemos"},
        docker_cfg={"per_qa_isolation": "off"},
    )
    sandbox = _sandbox(tmp_path)
    asyncio.run(adapter._discard_qa_state(sandbox, "qa0"))
    assert qa_host.exists()  # untouched


# ---------- bridge state_dir override --------------------------------------

def test_arun_bridge_via_docker_respects_caller_state_dir():
    """When caller passes state_dir, _arun_bridge_via_docker must NOT
    overwrite it with the default. Verify by inspecting the payload that
    would have been sent (we monkeypatch docker exec)."""
    captured: dict[str, dict] = {}

    async def fake_create(*cmd, stdin, stdout, stderr):
        class _P:
            returncode = 0
            async def communicate(self, input):
                captured["payload"] = __import__("json").loads(input.decode())
                return (b'{"ok": true}', b"")
            def kill(self):
                pass
            async def wait(self):
                pass
        return _P()

    adapter = _make_adapter(
        openclaw_cfg={"agent_llm": {"env_vars": []}},
        docker_cfg={},
    )
    adapter._docker_handles = {"conv0": {
        "container_id": "fake-container",
        "volume_dir": "/tmp/ws",
        "image": "fake-image",
    }}

    import asyncio as _asyncio
    orig = _asyncio.create_subprocess_exec
    _asyncio.create_subprocess_exec = fake_create
    try:
        payload = {
            "command": "agent_run",
            "state_dir": "/workspace/.qa_states/qa17",
            "session_id": "conv0__qa17",
            "message": "hello",
        }
        asyncio.run(adapter._arun_bridge_via_docker(
            "conv0", payload, timeout=10.0,
        ))
    finally:
        _asyncio.create_subprocess_exec = orig

    assert captured["payload"]["state_dir"] == "/workspace/.qa_states/qa17"
    # Other defaults still applied.
    assert captured["payload"]["repo_path"] == "/app"
    assert captured["payload"]["workspace_dir"] == "/workspace"


def test_arun_bridge_via_docker_default_state_dir_when_caller_omits():
    """No caller state_dir -> default /workspace/state."""
    captured: dict[str, dict] = {}

    async def fake_create(*cmd, stdin, stdout, stderr):
        class _P:
            returncode = 0
            async def communicate(self, input):
                captured["payload"] = __import__("json").loads(input.decode())
                return (b'{"ok": true}', b"")
            def kill(self):
                pass
            async def wait(self):
                pass
        return _P()

    adapter = _make_adapter(
        openclaw_cfg={"agent_llm": {"env_vars": []}},
        docker_cfg={},
    )
    adapter._docker_handles = {"conv0": {
        "container_id": "fake-container",
        "volume_dir": "/tmp/ws",
        "image": "fake-image",
    }}

    import asyncio as _asyncio
    orig = _asyncio.create_subprocess_exec
    _asyncio.create_subprocess_exec = fake_create
    try:
        asyncio.run(adapter._arun_bridge_via_docker(
            "conv0",
            {"command": "agent_run", "session_id": "x", "message": "y"},
            timeout=10.0,
        ))
    finally:
        _asyncio.create_subprocess_exec = orig

    assert captured["payload"]["state_dir"] == "/workspace/state"

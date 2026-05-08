"""Integration tests for DockerizedOpenclawAdapter per-QA isolation wiring.

Verifies that the adapter's `_isolation_mode`, `_ensure_state_frozen`,
`_restore_qa_state_dir`, and `_discard_qa_state` methods correctly
delegate to evaluation/src/adapters/openclaw/per_qa_isolation.py and
behave as the public contract expects. Pure functions are tested in
test_per_qa_isolation.py; this file covers the adapter glue.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import patch

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
    from evaluation.src.adapters.openclaw.per_qa_isolation import (
        resolve_isolation_mode,
    )
    adapter = DockerizedOpenclawAdapter.__new__(DockerizedOpenclawAdapter)
    adapter._openclaw_cfg = openclaw_cfg
    adapter._docker_cfg = docker_cfg or {}
    # Mirror the bits of __init__ that the per-QA isolation methods read.
    adapter._isolation_mode_cache = resolve_isolation_mode(
        adapter._docker_cfg.get("per_qa_isolation"),
        adapter._openclaw_cfg.get("context_engine_mode"),
    )
    adapter._freeze_locks = {}
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
    assert "_state_frozen" not in sandbox


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
    assert sandbox["_state_frozen"] is True


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

def _fake_subprocess_exec(captured: dict, response: bytes = b'{"ok": true}'):
    """Build a fake asyncio.create_subprocess_exec that captures the JSON
    payload sent over stdin and returns ``response`` on stdout."""
    async def _exec(*cmd, stdin, stdout, stderr):
        class _P:
            returncode = 0
            async def communicate(self, input):
                captured["payload"] = json.loads(input.decode())
                return (response, b"")
            def kill(self): pass
            async def wait(self): pass
        return _P()
    return _exec


def _make_bridge_adapter():
    """Bare-minimum adapter for _arun_bridge_via_docker tests."""
    adapter = _make_adapter(
        openclaw_cfg={"agent_llm": {"env_vars": []}},
        docker_cfg={},
    )
    adapter._docker_handles = {"conv0": {
        "container_id": "fake-container",
        "volume_dir": "/tmp/ws",
        "image": "fake-image",
    }}
    return adapter


def test_arun_bridge_via_docker_respects_caller_state_dir():
    """When caller passes state_dir, _arun_bridge_via_docker must NOT
    overwrite it with the default."""
    captured: dict = {}
    adapter = _make_bridge_adapter()
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=_fake_subprocess_exec(captured),
    ):
        asyncio.run(adapter._arun_bridge_via_docker(
            "conv0",
            {
                "command": "agent_run",
                "state_dir": "/workspace/.qa_states/qa17",
                "session_id": "conv0__qa17",
                "message": "hello",
            },
            timeout=10.0,
        ))
    assert captured["payload"]["state_dir"] == "/workspace/.qa_states/qa17"
    assert captured["payload"]["repo_path"] == "/app"
    assert captured["payload"]["workspace_dir"] == "/workspace"


def test_arun_bridge_via_docker_default_state_dir_when_caller_omits():
    """No caller state_dir -> default /workspace/state."""
    captured: dict = {}
    adapter = _make_bridge_adapter()
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=_fake_subprocess_exec(captured),
    ):
        asyncio.run(adapter._arun_bridge_via_docker(
            "conv0",
            {"command": "agent_run", "session_id": "x", "message": "y"},
            timeout=10.0,
        ))
    assert captured["payload"]["state_dir"] == "/workspace/state"


# ---------- e2e _generate_answer_via_agent --------------------------------

def _make_e2e_adapter(workspace: Path, ce_mode: str | None = None):
    """Build an adapter capable of running _generate_answer_via_agent
    end-to-end (with mocked subprocess invocation)."""
    from evaluation.src.adapters.openclaw.per_qa_isolation import (
        resolve_isolation_mode,
    )
    adapter = DockerizedOpenclawAdapter.__new__(DockerizedOpenclawAdapter)
    adapter._openclaw_cfg = {
        "agent_llm": {"env_vars": []},
        "agent_timeout_seconds": 30,
        **({"context_engine_mode": ce_mode} if ce_mode else {}),
    }
    adapter._docker_cfg = {"per_qa_isolation": "snapshot" if ce_mode else "off"}
    adapter._isolation_mode_cache = resolve_isolation_mode(
        adapter._docker_cfg.get("per_qa_isolation"),
        adapter._openclaw_cfg.get("context_engine_mode"),
    )
    adapter._freeze_locks = {}
    adapter._exec_timeout = 30
    adapter._docker_handles = {"conv0": {
        "container_id": "fake", "volume_dir": str(workspace), "image": "fake",
    }}
    adapter._sandbox_by_conversation_id = {
        "conv0": {
            "conversation_id": "conv0",
            "workspace_dir": str(workspace),
        },
    }
    adapter._sandbox_for = lambda conv_id: adapter._sandbox_by_conversation_id[conv_id]
    adapter._bridge_base_payload = lambda sb: {}
    adapter._append_events = lambda sb, evs: None
    return adapter


def _fake_proc_factory(captured: list, ok_reply: bytes = b'{"ok": true, "reply": "ans", "stop_reason": "end_turn"}'):
    async def _create(*cmd, stdin, stdout, stderr):
        class _P:
            returncode = 0
            async def communicate(self, input):
                captured.append(json.loads(input.decode()))
                return (ok_reply, b"")
            def kill(self): pass
            async def wait(self): pass
        return _P()
    return _create


def test_e2e_two_qas_freeze_once_restore_each_discard_each(tmp_path: Path):
    """End-to-end: two QAs in same conv with snapshot mode. Freeze fires
    once, each QA gets its own state_dir, all per-QA dirs discarded."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "memory.sqlite").write_bytes(b"baseline")

    adapter = _make_e2e_adapter(tmp_path, ce_mode="hypercompositor")
    captured: list = []

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_proc_factory(captured)):
        async def two_qas():
            r1 = await adapter._generate_answer_via_agent("Q1", "conv0", "qa1")
            r2 = await adapter._generate_answer_via_agent("Q2", "conv0", "qa2")
            return r1, r2
        r1, r2 = asyncio.run(two_qas())

    assert r1 == "ans" and r2 == "ans"
    assert len(captured) == 2
    assert captured[0]["state_dir"] == "/workspace/.qa_states/qa1"
    assert captured[1]["state_dir"] == "/workspace/.qa_states/qa2"
    assert (tmp_path / SNAPSHOT_DIRNAME / "memory.sqlite").read_bytes() == b"baseline"
    qa_root = tmp_path / QA_STATES_DIRNAME
    assert not qa_root.exists() or list(qa_root.iterdir()) == []


def test_e2e_discard_runs_in_finally_on_bridge_error(tmp_path: Path):
    """If bridge raises BridgeError, the finally block still discards
    the per-QA state directory."""
    from evaluation.src.adapters.openclaw.runtime import BridgeError

    state = tmp_path / "state"
    state.mkdir()

    adapter = _make_e2e_adapter(tmp_path, ce_mode="hypercompositor")

    async def boom_bridge(conv_id, payload, timeout):
        raise BridgeError("simulated docker exec failure")

    adapter._arun_bridge_via_docker = boom_bridge

    out = asyncio.run(adapter._generate_answer_via_agent("Q", "conv0", "qa-fail"))
    assert out == ""
    qa_dir = tmp_path / QA_STATES_DIRNAME / "qa-fail"
    assert not qa_dir.exists()


def test_e2e_isolation_off_skips_freeze_restore_discard(tmp_path: Path):
    """isolation=off: no snapshot machinery runs; bridge sees default state_dir."""
    state = tmp_path / "state"
    state.mkdir()

    adapter = _make_e2e_adapter(tmp_path, ce_mode=None)
    captured: list = []

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_proc_factory(captured)):
        asyncio.run(adapter._generate_answer_via_agent("Q", "conv0", "qa1"))

    assert not (tmp_path / SNAPSHOT_DIRNAME).exists()
    assert not (tmp_path / QA_STATES_DIRNAME).exists()
    assert captured[0]["state_dir"] == "/workspace/state"


def test_e2e_replay_skips_freeze_when_baseline_exists(tmp_path: Path):
    """Risk 3 fix: pre-existing baseline (replay scenario) must NOT be
    overwritten by lazy-freeze."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "x.txt").write_text("v2-post-answer-pollution")

    baseline = tmp_path / SNAPSHOT_DIRNAME
    baseline.mkdir()
    (baseline / "x.txt").write_text("v1-clean-add-end")

    adapter = _make_e2e_adapter(tmp_path, ce_mode="hypercompositor")
    captured: list = []

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_proc_factory(captured)):
        asyncio.run(adapter._generate_answer_via_agent("Q", "conv0", "qa1"))

    assert (baseline / "x.txt").read_text() == "v1-clean-add-end"


# ---------- concurrent freeze ---------------------------------------------

def test_concurrent_ensure_state_frozen_runs_freeze_only_once(tmp_path: Path):
    """answer.max_concurrent is global, so two QAs from the same conv can
    call _ensure_state_frozen concurrently. Without the lock, both would
    call freeze_state (rmtree + copytree) racing on the same baseline
    dir. Verify the lock serializes them and freeze_state runs exactly
    once.
    """
    state = tmp_path / "state"
    state.mkdir()
    (state / "marker.txt").write_text("baseline")

    adapter = _make_adapter(
        openclaw_cfg={"context_engine_mode": "hypercompositor"},
        docker_cfg={"per_qa_isolation": "snapshot"},
    )
    sandbox = _sandbox(tmp_path)

    # Wrap freeze_state with a counter so we can verify call count.
    call_count = {"n": 0}
    real_freeze = __import__(
        "evaluation.src.adapters.openclaw.per_qa_isolation",
        fromlist=["freeze_state"],
    ).freeze_state

    def counting_freeze(workspace):
        call_count["n"] += 1
        # time.sleep is intentional here, not asyncio.sleep: freeze_state
        # is invoked via loop.run_in_executor (i.e. on a thread pool
        # worker), so blocking the worker does NOT block the event loop.
        # The loop remains free to schedule the other 4 coroutines, which
        # will either contend on the lock (correct behavior) or call
        # freeze_state again (regression). Empirically validated by
        # temporarily removing the lock — without it, this test fails
        # with FileExistsError on the second copytree.
        time.sleep(0.02)
        return real_freeze(workspace)

    with patch(
        "evaluation.src.adapters.openclaw_docker_adapter.freeze_state",
        side_effect=counting_freeze,
    ):
        async def race():
            # 5 concurrent calls from same sandbox.
            await asyncio.gather(
                *[adapter._ensure_state_frozen(sandbox) for _ in range(5)]
            )
        asyncio.run(race())

    assert call_count["n"] == 1, (
        f"freeze_state must run exactly once across concurrent callers; "
        f"actually ran {call_count['n']} times"
    )
    # Baseline content intact (not corrupted by interleaved rmtree/copytree).
    assert (tmp_path / SNAPSHOT_DIRNAME / "marker.txt").read_text() == "baseline"
    assert sandbox["_state_frozen"] is True

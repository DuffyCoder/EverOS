"""Regression test: archive_session must run BEFORE _emit_agent_run_complete.

The override gate inside _emit_agent_run_complete globs ``*.jsonl.*`` in
sessions_dir to find the just-completed QA's archived session. In docker mode
the live ``<uuid>.jsonl`` is renamed to ``<uuid>.jsonl.<ts>`` only by an
explicit ``archive_session`` bridge call. retry-fix-v2 had archive_session
placed in a ``finally:`` block that ran AFTER _emit_agent_run_complete, so
the override gate's glob saw only PRIOR QAs' archived files; the
expected_question filter then rejected them and override fired 0 times.
The fix is to archive BEFORE running the override.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from evaluation.src.adapters.openclaw_docker_adapter import (
    DockerizedOpenclawAdapter,
)


def _make_adapter() -> DockerizedOpenclawAdapter:
    adapter = DockerizedOpenclawAdapter.__new__(DockerizedOpenclawAdapter)
    adapter._openclaw_cfg = {"agent_timeout_seconds": 60}
    adapter._docker_cfg = {}
    adapter._exec_timeout = 30.0
    adapter._append_events = lambda sandbox, events: None
    adapter._sandbox_by_conversation_id = {
        "conv0": {
            "conversation_id": "conv0",
            "workspace_dir": "/tmp/ws",
            "sessions_dir": "/tmp/sessions",
            "events_path": "/tmp/events.jsonl",
            "ov_session_id": "ov-uuid-123",
        }
    }
    return adapter


@pytest.mark.asyncio
async def test_archive_session_runs_before_emit_agent_run_complete():
    """Order check: when the bridge call succeeds with a valid stop_reason,
    ``archive_session`` must be invoked BEFORE ``_emit_agent_run_complete``.
    Otherwise the override gate's glob misses the current QA's archived file
    and override never fires."""
    adapter = _make_adapter()

    call_log: list[str] = []

    async def fake_arun_bridge_via_docker(conv_id, payload, timeout):
        cmd = payload.get("command")
        if cmd == "agent_run":
            call_log.append("agent_run")
            return {
                "ok": True,
                "stop_reason": "stop",
                "reply": "preamble text",
                "duration_ms": 50,
            }
        if cmd == "archive_session":
            call_log.append("archive_session")
            return {"ok": True}
        raise AssertionError(f"unexpected bridge command: {cmd}")

    def fake_emit(sandbox, conv_id, qid, resp, query, **kwargs):
        call_log.append("emit_agent_run_complete")
        return "final answer"

    async def fake_bridge_base_payload(sandbox):
        return {}

    with patch.object(adapter, "_arun_bridge_via_docker",
                      side_effect=fake_arun_bridge_via_docker), \
         patch.object(adapter, "_emit_agent_run_complete",
                      side_effect=fake_emit), \
         patch.object(adapter, "_bridge_base_payload",
                      return_value={}):
        result = await adapter._generate_answer_via_agent(
            "What is X?", "conv0", "qa0"
        )

    assert result == "final answer"
    assert call_log == ["agent_run", "archive_session", "emit_agent_run_complete"], (
        f"archive_session must run BEFORE _emit_agent_run_complete so the "
        f"override gate can glob the just-archived session jsonl; "
        f"observed: {call_log}"
    )


@pytest.mark.asyncio
async def test_archive_failure_does_not_block_emit():
    """archive_session is best-effort — if it raises, we still must call
    _emit_agent_run_complete (the override will fall back to bridge reply
    when no archived file is found, which is acceptable degradation)."""
    adapter = _make_adapter()

    call_log: list[str] = []

    async def fake_arun_bridge_via_docker(conv_id, payload, timeout):
        cmd = payload.get("command")
        if cmd == "agent_run":
            call_log.append("agent_run")
            return {
                "ok": True,
                "stop_reason": "stop",
                "reply": "preamble text",
                "duration_ms": 50,
            }
        if cmd == "archive_session":
            call_log.append("archive_attempt")
            raise RuntimeError("archive bridge failed")
        raise AssertionError(f"unexpected bridge command: {cmd}")

    def fake_emit(sandbox, conv_id, qid, resp, query, **kwargs):
        call_log.append("emit_agent_run_complete")
        return "fallback to bridge reply"

    with patch.object(adapter, "_arun_bridge_via_docker",
                      side_effect=fake_arun_bridge_via_docker), \
         patch.object(adapter, "_emit_agent_run_complete",
                      side_effect=fake_emit), \
         patch.object(adapter, "_bridge_base_payload",
                      return_value={}):
        result = await adapter._generate_answer_via_agent(
            "What is X?", "conv0", "qa0"
        )

    assert result == "fallback to bridge reply"
    assert call_log == ["agent_run", "archive_attempt", "emit_agent_run_complete"], (
        f"emit must still run after archive failure; observed: {call_log}"
    )

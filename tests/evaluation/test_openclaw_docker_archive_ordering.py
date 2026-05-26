"""Regression test: in docker mode the per-QA reset (archive_session) must run
AFTER _emit_agent_run_complete, and on EVERY exit path.

_emit_agent_run_complete records token metrics by reading the LIVE
``<session_id>.jsonl``; archive_session renames that file away. So emit must
run first (read the live file) and archive second. archive also lives in a
``finally`` so a failed / incomplete QA still resets the session and doesn't
leak its growing short-term context into the rest of the conversation.

(An older retry-fix placed archive BEFORE emit to feed a now-removed "override
gate" that globbed archived jsonl. That gate is gone — the findLast bridge
selects the final assistant payload directly — so the correct order is now
emit-then-archive.)
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


def _bridge_factory(call_log, agent_resp):
    async def fake_arun_bridge_via_docker(conv_id, payload, timeout):
        cmd = payload.get("command")
        if cmd == "agent_run":
            call_log.append("agent_run")
            return agent_resp
        if cmd == "archive_session":
            call_log.append("archive_session")
            return {"ok": True}
        raise AssertionError(f"unexpected bridge command: {cmd}")

    return fake_arun_bridge_via_docker


@pytest.mark.asyncio
async def test_emit_runs_before_archive_on_success():
    """Token metrics read the live session jsonl, so _emit_agent_run_complete
    must run BEFORE archive_session renames it."""
    adapter = _make_adapter()
    call_log: list[str] = []

    def fake_emit(sandbox, conv_id, qid, resp, query, **kwargs):
        call_log.append("emit_agent_run_complete")
        return "final answer"

    with patch.object(adapter, "_arun_bridge_via_docker",
                      side_effect=_bridge_factory(call_log, {
                          "ok": True, "stop_reason": "stop",
                          "reply": "final answer", "duration_ms": 50,
                      })), \
         patch.object(adapter, "_emit_agent_run_complete", side_effect=fake_emit), \
         patch.object(adapter, "_bridge_base_payload", return_value={}):
        result = await adapter._generate_answer_via_agent("Q?", "conv0", "qa0")

    assert result == "final answer"
    assert call_log == ["agent_run", "emit_agent_run_complete", "archive_session"], (
        f"emit must run before archive (metrics read the live jsonl that "
        f"archive renames); observed: {call_log}"
    )


@pytest.mark.asyncio
async def test_archive_runs_on_failure_path_and_skips_emit():
    """An incomplete stop_reason is rejected (empty answer, no emit), but the
    session must STILL be archived (finally) so the next QA starts clean."""
    adapter = _make_adapter()
    call_log: list[str] = []

    def fake_emit(sandbox, conv_id, qid, resp, query, **kwargs):
        call_log.append("emit_agent_run_complete")
        return "should not be used"

    with patch.object(adapter, "_arun_bridge_via_docker",
                      side_effect=_bridge_factory(call_log, {
                          "ok": True, "stop_reason": None,
                          "reply": "stale fallback", "duration_ms": 50,
                      })), \
         patch.object(adapter, "_emit_agent_run_complete", side_effect=fake_emit), \
         patch.object(adapter, "_bridge_base_payload", return_value={}):
        result = await adapter._generate_answer_via_agent("Q?", "conv0", "qa0")

    assert result == "", "incomplete stop_reason must yield empty answer"
    assert "emit_agent_run_complete" not in call_log, (
        "a rejected QA must not emit a completed answer"
    )
    assert call_log == ["agent_run", "archive_session"], (
        f"archive must run on the failure path too (finally); observed: {call_log}"
    )


@pytest.mark.asyncio
async def test_archive_failure_does_not_break_answer():
    """archive_session is best-effort; if it raises, the already-computed
    answer is still returned."""
    adapter = _make_adapter()
    call_log: list[str] = []

    async def fake_arun_bridge_via_docker(conv_id, payload, timeout):
        cmd = payload.get("command")
        if cmd == "agent_run":
            call_log.append("agent_run")
            return {"ok": True, "stop_reason": "stop", "reply": "x", "duration_ms": 1}
        if cmd == "archive_session":
            call_log.append("archive_attempt")
            raise RuntimeError("archive bridge failed")
        raise AssertionError(f"unexpected bridge command: {cmd}")

    def fake_emit(sandbox, conv_id, qid, resp, query, **kwargs):
        call_log.append("emit_agent_run_complete")
        return "final answer"

    with patch.object(adapter, "_arun_bridge_via_docker",
                      side_effect=fake_arun_bridge_via_docker), \
         patch.object(adapter, "_emit_agent_run_complete", side_effect=fake_emit), \
         patch.object(adapter, "_bridge_base_payload", return_value={}):
        result = await adapter._generate_answer_via_agent("Q?", "conv0", "qa0")

    assert result == "final answer"
    assert call_log == ["agent_run", "emit_agent_run_complete", "archive_attempt"]

"""
v0.7 D3 unit tests for OpenClawAdapter answer-path changes.

Locks the contract that:
- ``answer_mode`` dispatches between shared_llm (Path A compat) and
  agent_local (Path B real openclaw agent loop).
- ``session_id`` for agent_local is ``<conv_id>__<qid>``.
- ``_sandbox_by_conversation_id`` is populated by both ``add()`` and
  ``build_lazy_index()`` so resume/lazy-index runs do not lose state.
- ``get_answer_timeout()`` returns ``agent_timeout_seconds + 30`` for
  agent_local, default 120 for shared_llm.
- ``search()`` returns a placeholder marked ``skipped=True`` in
  agent_local mode.
- ``_prebootstrap_workspace()`` retries up to 3 times then raises;
  also raises if expected files are missing after dummy run.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evaluation.src.adapters.openclaw_adapter import OpenClawAdapter
from evaluation.src.core.data_models import Conversation, SearchResult


def _build_adapter(answer_mode: str = "shared_llm", **extra_openclaw) -> OpenClawAdapter:
    config = {
        "openclaw": {
            "repo_path": "/tmp/openclaw-repo",
            "answer_mode": answer_mode,
            "agent_timeout_seconds": 180,
            **extra_openclaw,
        },
        "llm": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "test",
            "base_url": "https://test",
        },
    }
    return OpenClawAdapter(config=config)


def _make_sandbox(conv_id: str, workspace_dir: str = "/tmp/ws") -> dict:
    return {
        "conversation_id": conv_id,
        "resolved_config_path": "/tmp/openclaw.json",
        "workspace_dir": workspace_dir,
        "native_store_dir": "/tmp/state",
        "home_dir": "/tmp/home",
        "cwd_dir": "/tmp/cwd",
        "events_path": "/tmp/events.jsonl",
        "metrics_dir": "/tmp/metrics",
        "run_status": "ready",
        "visibility_mode": "settled",
        "visibility_state": "settled",
    }


# --- get_answer_timeout ----------------------------------------------

def test_get_answer_timeout_default_shared_llm():
    adapter = _build_adapter(answer_mode="shared_llm")
    assert adapter.get_answer_timeout() == 120.0


def test_get_answer_timeout_agent_local_with_margin():
    adapter = _build_adapter(answer_mode="agent_local", agent_timeout_seconds=180)
    assert adapter.get_answer_timeout() == 210.0  # 180 + 30 margin


def test_get_answer_timeout_agent_local_custom_value():
    adapter = _build_adapter(answer_mode="agent_local", agent_timeout_seconds=300)
    assert adapter.get_answer_timeout() == 330.0


# --- _sandbox_for / sandbox persistence ------------------------------

def test_sandbox_for_raises_when_not_populated():
    adapter = _build_adapter()
    with pytest.raises(RuntimeError, match="No sandbox found"):
        adapter._sandbox_for("any-conv")


def test_sandbox_persisted_via_add(monkeypatch, tmp_path):
    """add() populates _sandbox_by_conversation_id even when answer_mode is
    shared_llm (so sandbox lookup works for diagnostics)."""
    adapter = _build_adapter(answer_mode="shared_llm")

    # Bypass real ingest/flush - just verify the persistence side effect
    sandbox_fixture = _make_sandbox("conv-A", workspace_dir=str(tmp_path / "conv-A"))
    Path(sandbox_fixture["workspace_dir"]).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        adapter, "_prepare_conversation_sandbox",
        MagicMock(return_value=sandbox_fixture),
    )
    monkeypatch.setattr(adapter, "_ingest_conversation", AsyncMock())
    monkeypatch.setattr(adapter, "_flush_and_settle_if_needed", AsyncMock())
    monkeypatch.setattr(adapter, "_assert_visibility_contract", MagicMock())
    monkeypatch.setattr(adapter, "_write_handle", MagicMock())

    import asyncio
    conv = Conversation(conversation_id="conv-A", messages=[])
    asyncio.run(adapter.add([conv], output_dir=str(tmp_path)))

    assert "conv-A" in adapter._sandbox_by_conversation_id
    assert adapter._sandbox_by_conversation_id["conv-A"] is sandbox_fixture


def test_sandbox_persisted_via_build_lazy_index(tmp_path):
    """build_lazy_index() populates _sandbox_by_conversation_id (resume
    path). Without this, agent_local answer() would fail on resume."""
    adapter = _build_adapter(answer_mode="agent_local")

    # Set up a fake run root with one ready handle
    run_root = tmp_path / "artifacts" / "openclaw" / "run-test"
    conv_dir = run_root / "conversations" / "conv-B"
    conv_dir.mkdir(parents=True)
    handle = {
        "conversation_id": "conv-B",
        "workspace_dir": str(tmp_path / "ws"),
        "native_store_dir": str(tmp_path / "state"),
        "run_status": "ready",
        "visibility_mode": "settled",
        "visibility_state": "settled",
        "resolved_config_path": str(tmp_path / "openclaw.json"),
    }
    (conv_dir / "handle.json").write_text(json.dumps(handle))
    (run_root.parent / "LATEST").write_text("run-test")

    conv = Conversation(conversation_id="conv-B", messages=[])
    index = adapter.build_lazy_index([conv], tmp_path)

    assert "conv-B" in index["conversations"]
    # v0.7 critical: also populated for in-memory lookup
    assert "conv-B" in adapter._sandbox_by_conversation_id
    assert adapter._sandbox_for("conv-B") == handle


def test_sandbox_skipped_when_handle_not_ready(tmp_path):
    """build_lazy_index ignores conversations whose handle is not ready."""
    adapter = _build_adapter(answer_mode="agent_local")
    run_root = tmp_path / "artifacts" / "openclaw" / "run-test"
    conv_dir = run_root / "conversations" / "conv-C"
    conv_dir.mkdir(parents=True)
    (conv_dir / "handle.json").write_text(json.dumps({
        "conversation_id": "conv-C",
        "run_status": "failed",  # not ready
    }))
    (run_root.parent / "LATEST").write_text("run-test")

    conv = Conversation(conversation_id="conv-C", messages=[])
    adapter.build_lazy_index([conv], tmp_path)

    # Not added to sandbox map because run_status != "ready"
    assert "conv-C" not in adapter._sandbox_by_conversation_id


# --- search() agent_local skipped ------------------------------------

def test_search_returns_skipped_in_agent_local_mode():
    adapter = _build_adapter(answer_mode="agent_local")
    import asyncio

    result: SearchResult = asyncio.run(
        adapter.search(
            query="anything",
            conversation_id="conv-X",
            index={},  # not consulted
            question_id="q-7",
        )
    )
    assert result.results == []
    assert result.retrieval_metadata.get("skipped") is True
    assert result.retrieval_metadata.get("reason") == "agent_local_owns_retrieval"
    assert result.retrieval_metadata.get("question_id") == "q-7"


def test_search_uses_real_path_in_shared_llm(monkeypatch):
    """In shared_llm mode, search() calls the bridge as before."""
    adapter = _build_adapter(answer_mode="shared_llm")
    bridge_mock = AsyncMock(return_value={"ok": True, "hits": []})
    monkeypatch.setattr(
        "evaluation.src.adapters.openclaw_adapter.arun_bridge", bridge_mock
    )

    sandbox = _make_sandbox("conv-S")
    index = {"conversations": {"conv-S": sandbox}}

    import asyncio
    result = asyncio.run(adapter.search("query", "conv-S", index, question_id="q1"))

    # Bridge was called (skipped logic did not short-circuit)
    assert bridge_mock.called
    # No skipped flag on shared_llm path
    assert not result.retrieval_metadata.get("skipped")


# --- answer() dispatch -----------------------------------------------

def test_answer_agent_local_calls_agent_path(monkeypatch):
    adapter = _build_adapter(answer_mode="agent_local")
    sandbox = _make_sandbox("conv-A")
    adapter._sandbox_by_conversation_id["conv-A"] = sandbox

    bridge_mock = AsyncMock(return_value={
        "ok": True, "command": "agent_run",
        "reply": "AGENT_REPLY", "raw": {},
        "duration_ms": 100, "stop_reason": "stop", "aborted": False,
        "tool_names": [], "system_prompt_chars": 0, "last_call_usage": None,
    })
    monkeypatch.setattr(
        "evaluation.src.adapters.openclaw_adapter.arun_bridge", bridge_mock
    )
    monkeypatch.setattr(adapter, "_append_events", MagicMock())

    import asyncio
    result = asyncio.run(adapter.answer(
        query="What is 2+2?",
        context="(unused in agent_local)",
        conversation_id="conv-A",
        question_id="q-42",
    ))

    assert result == "AGENT_REPLY"
    # Verify session_id was per-QA: conv-A__q-42
    bridge_call_args = bridge_mock.call_args
    payload = bridge_call_args.args[1] if len(bridge_call_args.args) > 1 else bridge_call_args.kwargs.get("payload") or bridge_call_args.args[1]
    # Locate the actual payload dict - it's positional arg 1
    assert payload["command"] == "agent_run"
    assert payload["session_id"] == "conv-A__q-42"
    assert payload["message"] == "What is 2+2?"


def test_answer_agent_local_treats_stop_reason_error_as_failure(monkeypatch):
    """v0.7 D5 fix: provider rate limit / agent internal error returns
    a "graceful" reply (e.g. 'API rate limit reached') with stop_reason=
    'error'. Adapter must treat this as failure (return '') instead of
    forwarding the error message as a real answer."""
    adapter = _build_adapter(answer_mode="agent_local")
    sandbox = _make_sandbox("conv-RL")
    adapter._sandbox_by_conversation_id["conv-RL"] = sandbox

    bridge_mock = AsyncMock(return_value={
        "ok": True, "command": "agent_run",
        "reply": "⚠️ API rate limit reached. Please try again later.",
        "raw": {},
        "duration_ms": 38000, "stop_reason": "error",  # ← 关键
        "aborted": False,
        "tool_names": [], "system_prompt_chars": 0, "last_call_usage": None,
    })
    monkeypatch.setattr(
        "evaluation.src.adapters.openclaw_adapter.arun_bridge", bridge_mock
    )
    events = []
    monkeypatch.setattr(adapter, "_append_events",
                        lambda sb, evs: events.extend(evs))

    import asyncio
    result = asyncio.run(adapter.answer(
        query="when?", context="(unused)",
        conversation_id="conv-RL", question_id="q-rl",
    ))

    # Empty string returned (not the rate limit message)
    assert result == ""
    # Internal error event recorded for diagnostics
    err_events = [e for e in events if e.get("event") == "agent_run_internal_error"]
    assert len(err_events) == 1
    assert err_events[0]["question_id"] == "q-rl"
    assert "rate limit" in err_events[0]["reply_excerpt"].lower()


def test_answer_agent_local_falls_back_to_shared_llm_without_ids(monkeypatch):
    """If agent_local mode but conv_id/qid missing, log warning + fallback."""
    adapter = _build_adapter(answer_mode="agent_local")
    fallback_mock = AsyncMock(return_value="SHARED_FALLBACK_REPLY")
    monkeypatch.setattr(adapter, "_generate_answer", fallback_mock)
    monkeypatch.setattr(adapter, "_shared_answer_prompt", MagicMock(return_value="{context}\n{question}"))

    import asyncio
    # Missing question_id
    result = asyncio.run(adapter.answer(
        query="Q", context="ctx", conversation_id="conv-A"
    ))
    assert result == "SHARED_FALLBACK_REPLY"


# --- prebootstrap retry + raise --------------------------------------

def test_prebootstrap_raises_after_3_failed_attempts(monkeypatch, tmp_path):
    """Bridge always returns ok=False -> retry 3 times -> raise."""
    adapter = _build_adapter(answer_mode="agent_local")
    sandbox = _make_sandbox("conv-X", workspace_dir=str(tmp_path))
    Path(sandbox["workspace_dir"]).mkdir(parents=True, exist_ok=True)

    bridge_mock = AsyncMock(return_value={"ok": False, "error": "boom"})
    monkeypatch.setattr(
        "evaluation.src.adapters.openclaw_adapter.arun_bridge", bridge_mock
    )
    monkeypatch.setattr(
        "evaluation.src.adapters.openclaw_adapter.asyncio.sleep",
        AsyncMock(),  # don't actually sleep in test
    )

    import asyncio
    with pytest.raises(RuntimeError, match="prebootstrap agent_run failed"):
        asyncio.run(adapter._prebootstrap_workspace(sandbox))

    # Verify exactly 3 attempts
    assert bridge_mock.call_count == 3


def test_prebootstrap_raises_on_missing_workspace_files(monkeypatch, tmp_path):
    """Bridge succeeds but workspace files not written -> raise."""
    adapter = _build_adapter(answer_mode="agent_local")
    sandbox = _make_sandbox("conv-Y", workspace_dir=str(tmp_path))
    Path(sandbox["workspace_dir"]).mkdir(parents=True, exist_ok=True)
    # Note: no AGENTS.md / SOUL.md / TOOLS.md created in tmp_path

    bridge_mock = AsyncMock(return_value={"ok": True, "reply": "OK", "raw": {}})
    monkeypatch.setattr(
        "evaluation.src.adapters.openclaw_adapter.arun_bridge", bridge_mock
    )

    import asyncio
    with pytest.raises(RuntimeError, match="workspace bootstrap files missing"):
        asyncio.run(adapter._prebootstrap_workspace(sandbox))


def test_prebootstrap_succeeds_when_files_exist(monkeypatch, tmp_path):
    adapter = _build_adapter(answer_mode="agent_local")
    sandbox = _make_sandbox("conv-Z", workspace_dir=str(tmp_path))
    Path(sandbox["workspace_dir"]).mkdir(parents=True, exist_ok=True)

    # Pre-create the files openclaw would write
    for name in ["AGENTS.md", "SOUL.md", "TOOLS.md"]:
        (tmp_path / name).write_text("stub content")

    bridge_mock = AsyncMock(return_value={"ok": True, "reply": "BOOTSTRAP_OK", "raw": {}})
    monkeypatch.setattr(
        "evaluation.src.adapters.openclaw_adapter.arun_bridge", bridge_mock
    )
    monkeypatch.setattr(adapter, "_append_events", MagicMock())

    import asyncio
    asyncio.run(adapter._prebootstrap_workspace(sandbox))  # no raise

    assert bridge_mock.call_count == 1  # succeeded first try

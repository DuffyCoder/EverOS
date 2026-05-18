"""Tests for OpenClaw agent_local token metrics."""
from __future__ import annotations

import json
from pathlib import Path

from evaluation.src.adapters.openclaw.usage_metrics import (
    compute_agent_run_token_metrics,
    estimate_session_transcript_input_tokens,
    extract_agent_run_token_metrics,
    last_session_usage_input,
    resolve_state_dir_host,
    sum_session_usage_input,
    usage_input_tokens,
)


def test_usage_input_tokens_normalizes_keys():
    assert usage_input_tokens({"input": 120}) == 120
    assert usage_input_tokens({"input_tokens": 80}) == 80
    assert usage_input_tokens({"prompt_tokens": 95}) == 95
    assert usage_input_tokens({"input": 80, "cacheRead": 20}) == 100
    assert usage_input_tokens({"input": 0, "output": 5}) is None


def test_sum_and_last_session_usage_input(tmp_path: Path):
    state_dir = tmp_path / "state"
    session_dir = state_dir / "agents" / "main" / "sessions"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "conv__qa0.jsonl"
    lines = [
        {"type": "message", "message": {"role": "user", "content": "hi"}},
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "a"}],
                "usage": {"input": 100, "output": 10},
            },
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "b"}],
                "usage": {"input": 50, "output": 5},
            },
        },
    ]
    session_file.write_text(
        "\n".join(json.dumps(row) for row in lines) + "\n",
        encoding="utf-8",
    )
    assert sum_session_usage_input(state_dir, "conv__qa0") == 150
    assert last_session_usage_input(state_dir, "conv__qa0") == 50


def test_compute_agent_run_token_metrics_tier_order(tmp_path: Path):
    """Provider (A) beats transcript (B); transcript beats coarse (C)."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "agents" / "main" / "sessions"
    session_dir.mkdir(parents=True)
    session_dir.joinpath("c__q.jsonl").write_text(
        "\n".join([
            json.dumps({
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "x" * 5000}],
                },
            }),
            json.dumps({
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input": 0},
                },
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    session_path = session_dir / "c__q.jsonl"
    resp = {"system_prompt_chars": 1000, "raw": {"meta": {"systemPromptReport": {}}}}

    provider = compute_agent_run_token_metrics(
        {"last_call_usage": {"input": 900}}, session_path, "q",
    )
    assert provider.final_context_tokens_source == "last_call_usage"

    transcript = compute_agent_run_token_metrics(resp, session_path, "q")
    assert transcript.final_context_tokens_source == "session_transcript_estimate"
    assert transcript.final_context_tokens > 100

    coarse = compute_agent_run_token_metrics(
        resp, tmp_path / "missing.jsonl", "short",
    )
    assert coarse.final_context_tokens_source == "prompt_estimate"


def test_extract_agent_run_token_metrics_prefers_last_call_usage():
    metrics = extract_agent_run_token_metrics(
        {
            "last_call_usage": {"input": 4200, "output": 12},
            "system_prompt_chars": 21000,
        },
        Path("/tmp/unused"),
        "conv__qa0",
        "When did Caroline go?",
    )
    assert metrics["final_context_tokens"] == 4200
    assert metrics["final_context_tokens_source"] == "last_call_usage"


def test_extract_agent_run_token_metrics_session_transcript_fallback(tmp_path: Path):
    """Long user context in session jsonl beats short-question prompt_estimate."""
    state_dir = tmp_path / "state"
    session_dir = state_dir / "agents" / "main" / "sessions"
    session_dir.mkdir(parents=True)
    memories = "<relevant-memories>\n" + ("memory line " * 200) + "\n</relevant-memories>\n\n"
    session_dir.joinpath("conv__qa0.jsonl").write_text(
        "\n".join([
            json.dumps({
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": memories + "When did X go?"}],
                },
            }),
            json.dumps({
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "answer"}],
                    "usage": {"input": 0, "output": 0},
                },
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    resp = {
        "last_call_usage": {"input": 0},
        "system_prompt_chars": 4000,
        "raw": {"meta": {"systemPromptReport": {"tools": {"schemaChars": 2000}}}},
    }
    metrics = extract_agent_run_token_metrics(
        resp, state_dir, "conv__qa0", "When did X go?",
    )
    coarse = extract_agent_run_token_metrics(
        {**resp, "system_prompt_chars": 4000},
        Path("/nonexistent"),
        "conv__qa0",
        "When did X go?",
    )
    assert metrics["final_context_tokens_source"] == "session_transcript_estimate"
    assert metrics["agent_run_total_input_tokens_source"] == "session_transcript_sum"
    assert metrics["final_context_tokens"] > coarse["final_context_tokens"]


def test_estimate_session_transcript_multi_hop(tmp_path: Path):
    state_dir = tmp_path / "state"
    session_dir = state_dir / "agents" / "main" / "sessions"
    session_dir.mkdir(parents=True)
    session_dir.joinpath("conv__qa0.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "message", "message": {"role": "user", "content": "q"}}),
            json.dumps({
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "toolCall", "name": "memory_search", "arguments": {"q": "x"}}],
                    "usage": {"input": 0},
                },
            }),
            json.dumps({
                "type": "message",
                "message": {
                    "role": "toolResult",
                    "content": [{"type": "text", "text": '{"results": [' + ('"x",' * 100) + ']}'}],
                },
            }),
            json.dumps({
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {"input": 0},
                },
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    resp = {"raw": {"meta": {"systemPromptReport": {"systemPrompt": {"chars": 1000}, "tools": {"schemaChars": 500}}}}}
    final_hop, total = estimate_session_transcript_input_tokens(
        session_dir / "conv__qa0.jsonl", resp,
    )
    assert final_hop is not None and total is not None
    assert total > final_hop


def test_extract_agent_run_token_metrics_prompt_estimate_fallback():
    state_dir = Path("/tmp/state")
    metrics = extract_agent_run_token_metrics(
        {
            "last_call_usage": {"input": 0, "output": 0},
            "system_prompt_chars": 4000,
            "raw": {
                "meta": {
                    "systemPromptReport": {
                        "tools": {"schemaChars": 2000},
                    },
                },
            },
        },
        state_dir,
        "conv__qa0",
        "short question",
    )
    assert metrics["final_context_tokens_source"] == "prompt_estimate"
    assert metrics["final_context_tokens"] >= 50


def test_resolve_state_dir_host_container_path(tmp_path: Path):
    sandbox = {"workspace_dir": str(tmp_path), "native_store_dir": str(tmp_path / "native_store")}
    (tmp_path / "state").mkdir()
    resolved = resolve_state_dir_host(sandbox, "/workspace/.qa_states/qa0")
    assert resolved == tmp_path / ".qa_states" / "qa0"


def test_pop_answer_metrics_on_adapter():
    from evaluation.src.adapters.openclaw.adapter import OpenClawAdapter

    adapter = OpenClawAdapter({"openclaw": {}}, output_dir=None)
    sandbox = {
        "conversation_id": "locomo_0",
        "workspace_dir": "/tmp/ws",
        "events_path": "/tmp/ws/events.jsonl",
    }
    adapter._append_events = lambda s, e: None  # noqa: SLF001
    resp = {
        "reply": "ok",
        "last_call_usage": {"input": 999},
        "duration_ms": 1,
        "stop_reason": "stop",
    }
    adapter._emit_agent_run_complete(sandbox, "locomo_0", "qa0", resp, "q?")  # noqa: SLF001
    metrics = adapter.pop_answer_metrics("qa0")
    assert metrics["final_context_tokens"] == 999
    assert adapter.pop_answer_metrics("qa0") == {}

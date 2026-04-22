"""Tests for the hermes memory adapter (stub-provider based)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evaluation.src.core.data_models import Conversation, Message


def _make_conv(cid: str = "conv1") -> Conversation:
    return Conversation(
        conversation_id=cid,
        messages=[
            Message(speaker_id="u", speaker_name="u", content="hello"),
            Message(speaker_id="a", speaker_name="a", content="hi"),
        ],
    )


def _base_config(repo_path: str, plugin: str = "stub") -> dict:
    return {
        "adapter": "hermes",
        "hermes": {
            "repo_path": repo_path,
            "plugin": plugin,
            "ingest_strategy": "sync_per_turn",
        },
        "search": {"top_k": 6, "max_inflight_queries_per_conversation": 1},
        "llm": {"provider": "openai", "model": "stub", "api_key": "x"},
    }


def test_prepare_is_idempotent_and_resolves_run_root(tmp_path):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    adapter = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    asyncio.run(adapter.prepare(conversations=[_make_conv()], output_dir=tmp_path))

    run_root = tmp_path / "artifacts" / "hermes"
    latest = run_root / "LATEST"
    assert latest.exists()
    run_id_first = latest.read_text().strip()
    assert (run_root / run_id_first).is_dir()

    # Second prepare() must NOT create a new run_id. This proves idempotency
    # is a real no-op, not just crash-free.
    asyncio.run(adapter.prepare(conversations=[_make_conv()], output_dir=tmp_path))
    run_id_second = latest.read_text().strip()
    assert run_id_first == run_id_second, (
        f"prepare() regenerated run_id on second call: {run_id_first!r} -> {run_id_second!r}"
    )


class _StubProvider:
    """In-process MemoryProvider stand-in for unit tests.

    Records every call so tests can assert on session_id, content, and strategy dispatch.
    """

    name = "stub"

    def __init__(self):
        self.calls: list[tuple] = []
        self.is_available_result = True
        self.raise_on_init: Exception | None = None
        self.prefetch_result = "STUB_CONTEXT"

    def is_available(self) -> bool:
        return self.is_available_result

    def initialize(self, session_id: str, **kwargs) -> None:
        if self.raise_on_init:
            raise self.raise_on_init
        self.calls.append(("initialize", session_id, kwargs))

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        self.calls.append(("sync_turn", session_id, user_content, assistant_content))

    def on_session_end(self, messages) -> None:
        self.calls.append(("on_session_end", len(messages)))

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self.calls.append(("prefetch", session_id, query))
        return self.prefetch_result

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))

    def get_tool_schemas(self):
        return []


@pytest.fixture
def stub_provider(monkeypatch):
    stub = _StubProvider()
    from evaluation.src.adapters import hermes_adapter as _ha
    monkeypatch.setattr(_ha, "_load_memory_provider", lambda name: stub)
    return stub


def test_add_writes_handle_and_runs_ingest_sync_per_turn(tmp_path, stub_provider):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter
    import json

    adapter = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    result = asyncio.run(adapter.add(
        conversations=[_make_conv("conv1")],
        output_dir=tmp_path,
    ))

    assert result["type"] == "hermes_sandboxes"
    assert "conv1" in result["conversations"]
    handle_path = Path(result["conversations"]["conv1"]["handle_path"])
    handle = json.loads(handle_path.read_text())
    assert handle["run_status"] == "ready"
    assert handle["plugin"] == "stub"
    assert handle["ingest_turns"] == 1

    # sync_per_turn → exactly one sync_turn, no on_session_end
    kinds = [c[0] for c in stub_provider.calls]
    assert kinds.count("initialize") == 1
    assert kinds.count("sync_turn") == 1
    assert kinds.count("on_session_end") == 0

    # session_id contract: every session_id the stub saw equals conv1
    session_ids = {c[1] for c in stub_provider.calls if c[0] in ("initialize", "sync_turn")}
    assert session_ids == {"conv1"}


def test_add_session_end_strategy_calls_on_session_end_once(tmp_path, stub_provider):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    cfg = _base_config(str(tmp_path))
    cfg["hermes"]["ingest_strategy"] = "session_end"
    adapter = HermesAdapter(cfg, output_dir=tmp_path)
    asyncio.run(adapter.add(conversations=[_make_conv()], output_dir=tmp_path))

    kinds = [c[0] for c in stub_provider.calls]
    assert kinds.count("sync_turn") == 0
    assert kinds.count("on_session_end") == 1


def test_add_both_strategy_calls_both(tmp_path, stub_provider):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    cfg = _base_config(str(tmp_path))
    cfg["hermes"]["ingest_strategy"] = "both"
    adapter = HermesAdapter(cfg, output_dir=tmp_path)
    asyncio.run(adapter.add(conversations=[_make_conv()], output_dir=tmp_path))

    kinds = [c[0] for c in stub_provider.calls]
    assert kinds.count("sync_turn") >= 1
    assert kinds.count("on_session_end") == 1


def test_add_records_failed_handle_when_init_raises(tmp_path, stub_provider):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter
    import json

    stub_provider.raise_on_init = RuntimeError("init boom")

    adapter = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    result = asyncio.run(adapter.add(
        conversations=[_make_conv("conv-fail")],
        output_dir=tmp_path,
    ))

    handle_path = Path(result["conversations"]["conv-fail"]["handle_path"])
    handle = json.loads(handle_path.read_text())
    assert handle["run_status"] == "failed"
    assert "init boom" in handle["error"]


def test_build_lazy_index_rehydrates_from_handle(tmp_path, stub_provider):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    adapter = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    asyncio.run(adapter.add(
        conversations=[_make_conv("c1"), _make_conv("c2")],
        output_dir=tmp_path,
    ))

    # Fresh adapter instance — simulates a resume-from-disk run
    adapter2 = HermesAdapter(_base_config(str(tmp_path)), output_dir=tmp_path)
    index = adapter2.build_lazy_index([_make_conv("c1"), _make_conv("c2")], tmp_path)

    assert index["type"] == "hermes_sandboxes"
    assert set(index["conversations"]) == {"c1", "c2"}
    assert all(h["run_status"] == "ready" for h in index["conversations"].values())

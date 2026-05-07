"""Regression guard for spec §3.3.1 — HERMES_HOME isolation under concurrency."""
from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest

from evaluation.src.core.data_models import Conversation, Message


def _make_conv(cid: str) -> Conversation:
    return Conversation(
        conversation_id=cid,
        messages=[
            Message(speaker_id="u", speaker_name="u", content=f"hi-{cid}"),
            Message(speaker_id="a", speaker_name="a", content=f"hello-{cid}"),
        ],
    )


class _RecordingProvider:
    """Records the HERMES_HOME observed at call time and the max concurrency."""

    name = "stub"
    _in_flight = 0
    _peak = 0
    _lock = threading.Lock()

    def __init__(self):
        self.observations: list[tuple[str, str]] = []  # (session_id, hermes_home)

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._observe(session_id)

    def sync_turn(self, u: str, a: str, *, session_id: str = "") -> None:
        self._observe(session_id)

    def on_session_end(self, messages) -> None:
        pass

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def shutdown(self) -> None:
        pass

    def get_tool_schemas(self):
        return []

    def _observe(self, session_id: str) -> None:
        with _RecordingProvider._lock:
            _RecordingProvider._in_flight += 1
            _RecordingProvider._peak = max(_RecordingProvider._peak, _RecordingProvider._in_flight)
        try:
            # Hold the env for long enough that any racing caller would
            # overwrite it if the serialization boundary is broken.
            time.sleep(0.05)
            self.observations.append((session_id, os.environ.get("HERMES_HOME", "")))
        finally:
            with _RecordingProvider._lock:
                _RecordingProvider._in_flight -= 1


def _reset_peak():
    with _RecordingProvider._lock:
        _RecordingProvider._in_flight = 0
        _RecordingProvider._peak = 0


@pytest.mark.asyncio
async def test_concurrent_adds_do_not_race_on_hermes_home(tmp_path, monkeypatch):
    from evaluation.src.adapters import hermes_adapter as _ha
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    # All adapters in this process share the module-level singleton
    # executor (§3.3.1), so no two providers can ever be in a hermes call
    # at the same time regardless of which adapter owns them.
    def _fake_loader(name: str):
        return _RecordingProvider()

    monkeypatch.setattr(_ha, "_load_memory_provider", _fake_loader)
    _reset_peak()

    async def _run_one(cid: str):
        adapter = HermesAdapter({
            "hermes": {
                "repo_path": str(tmp_path),
                "plugin": "stub",
                "ingest_strategy": "sync_per_turn",
            },
            "llm": {"provider": "openai", "model": "stub", "api_key": "x"},
        }, output_dir=tmp_path / cid)
        (tmp_path / cid).mkdir()
        result = await adapter.add(
            conversations=[_make_conv(cid)],
            output_dir=tmp_path / cid,
        )
        return result

    results = await asyncio.gather(
        _run_one("c1"), _run_one("c2"), _run_one("c3"),
    )

    # All three runs succeeded
    for r in results:
        assert all(h["run_status"] == "ready" for h in r["conversations"].values())

    # Process-wide serialization: the shared singleton executor has
    # max_workers=1, so only one hermes call is ever in flight across all
    # three adapters. Peak concurrency MUST be 1.
    assert _RecordingProvider._peak == 1, (
        f"HERMES_HOME isolation broken: peak concurrency was "
        f"{_RecordingProvider._peak} (expected 1). Two provider calls were "
        "in flight simultaneously, so they raced on os.environ['HERMES_HOME']."
    )

    # Each conversation's sandbox points at its own directory.
    for r, cid in zip(results, ("c1", "c2", "c3")):
        sandbox = r["conversations"][cid]["hermes_home"]
        assert f"/{cid}" in sandbox

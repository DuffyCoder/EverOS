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
    # second call must not blow up or re-init
    asyncio.run(adapter.prepare(conversations=[_make_conv()], output_dir=tmp_path))

    run_root = tmp_path / "artifacts" / "hermes"
    assert run_root.exists()
    latest = run_root / "LATEST"
    assert latest.exists()
    run_id = latest.read_text().strip()
    assert (run_root / run_id).is_dir()

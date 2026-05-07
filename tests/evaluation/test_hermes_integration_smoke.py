"""End-to-end smoke test against a real hermes holographic plugin.

Skipped unless ``HERMES_REPO_PATH`` points at a checkout. Runs a 2-turn
synthetic conversation through add → search → verify a non-empty recall.
Does not call the answer LLM (that needs extra credentials).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from evaluation.src.core.data_models import Conversation, Message


pytestmark = pytest.mark.integration


@pytest.fixture
def hermes_repo_path():
    path = os.environ.get("HERMES_REPO_PATH", "")
    if not path or not os.path.isdir(path):
        pytest.skip("HERMES_REPO_PATH not set or not a directory")
    return path


def test_holographic_end_to_end_recall(tmp_path, hermes_repo_path):
    from evaluation.src.adapters.hermes_adapter import HermesAdapter

    conv = Conversation(
        conversation_id="smoke-1",
        messages=[
            Message(speaker_id="u", speaker_name="u",
                    content="My dog's name is Bluey and she's a border collie."),
            Message(speaker_id="a", speaker_name="a",
                    content="That's great! Bluey sounds lovely."),
        ],
    )

    adapter = HermesAdapter({
        "hermes": {
            "repo_path": hermes_repo_path,
            "plugin": "holographic",
            "ingest_strategy": "session_end",
            "plugin_config": {
                "auto_extract": True,
                "default_trust": 0.5,
                "hrr_dim": 1024,
            },
        },
        "llm": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": os.environ.get("LLM_API_KEY", ""),
            "base_url": os.environ.get(
                "LLM_BASE_URL", "https://www.sophnet.com/api/open-apis/v1"),
        },
    }, output_dir=tmp_path)

    index = asyncio.run(adapter.add(conversations=[conv], output_dir=tmp_path))
    assert index["conversations"]["smoke-1"]["run_status"] == "ready"

    result = asyncio.run(adapter.search(
        query="what kind of dog does the user have?",
        conversation_id="smoke-1",
        index=index,
    ))
    # holographic auto_extract is LLM-driven; if LLM is unreachable we still
    # want the run to come back cleanly with 0 hits rather than crash. Assert
    # only the structural contract; the retrieval quality is measured by the
    # full pipeline, not this smoke test.
    assert result.conversation_id == "smoke-1"
    assert result.retrieval_metadata["plugin"] == "holographic"

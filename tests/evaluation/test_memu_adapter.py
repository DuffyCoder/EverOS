from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evaluation.src.adapters.memu_adapter import MemuAdapter
from evaluation.src.config.system_loader import resolve_system_config
from evaluation.src.core.data_models import Conversation


class _FakeLLMProvider:
    def __init__(self, **_: Any) -> None:
        pass


class _SearchResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, object]:
        return {
            "related_memories": [
                {
                    "memory": {
                        "memory_id": "memory-1",
                        "content": "Remembered fact",
                        "category": "facts",
                    },
                    "similarity_score": 0.9,
                }
            ]
        }


def _conversation(*, dual: bool) -> Conversation:
    speakers = (
        {"speaker_a": "Alice", "speaker_b": "Bob"}
        if dual
        else {"speaker_a": "user", "speaker_b": "assistant"}
    )
    return Conversation(
        conversation_id="conversation-1",
        messages=[],
        metadata=speakers,
    )


def _adapter_and_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MemuAdapter, list[dict[str, object]]]:
    monkeypatch.setattr(
        "evaluation.src.adapters.online_base.LLMProvider", _FakeLLMProvider
    )
    payloads: list[dict[str, object]] = []

    def _post(
        url: str, *, headers: dict[str, str], json: dict[str, object]
    ) -> _SearchResponse:
        assert url.endswith("/api/v1/memory/retrieve/related-memory-items")
        assert headers["Authorization"] == "Bearer runtime-secret"
        payloads.append(dict(json))
        return _SearchResponse()

    monkeypatch.setattr(
        "evaluation.src.adapters.memu_adapter.requests.post", _post
    )
    adapter = MemuAdapter(
        {
            "adapter": "memu",
            "api_key": "runtime-secret",
            "base_url": "https://memory.example",
            "llm": {},
            "search": {"top_k": 4, "min_similarity": 0.65},
        },
        output_dir=Path("."),
    )

    async def _categories_summary(user_id: str) -> str:
        return f"Summary for {user_id}"

    monkeypatch.setattr(adapter, "_get_categories_summary", _categories_summary)
    return adapter, payloads


@pytest.mark.asyncio
@pytest.mark.parametrize("dual", [False, True], ids=("single", "dual"))
async def test_search_uses_configured_similarity_for_payload_and_metadata(
    monkeypatch: pytest.MonkeyPatch, dual: bool
) -> None:
    adapter, payloads = _adapter_and_payloads(monkeypatch)
    conversation = _conversation(dual=dual)

    result = await adapter.search(
        "What should I remember?",
        conversation.conversation_id,
        index=None,
        conversation=conversation,
    )

    assert len(payloads) == (2 if dual else 1)
    assert {payload["min_similarity"] for payload in payloads} == {0.65}
    assert result.retrieval_metadata["min_similarity"] == 0.65
    assert result.retrieval_metadata.get("dual_perspective", False) is dual


@pytest.mark.asyncio
@pytest.mark.parametrize("dual", [False, True], ids=("single", "dual"))
async def test_search_kwarg_overrides_configured_similarity_in_payload_and_metadata(
    monkeypatch: pytest.MonkeyPatch, dual: bool
) -> None:
    adapter, payloads = _adapter_and_payloads(monkeypatch)
    conversation = _conversation(dual=dual)

    result = await adapter.search(
        "What should I remember?",
        conversation.conversation_id,
        index=None,
        conversation=conversation,
        min_similarity=0.8,
    )

    assert len(payloads) == (2 if dual else 1)
    assert {payload["min_similarity"] for payload in payloads} == {0.8}
    assert result.retrieval_metadata["min_similarity"] == 0.8


def test_canonical_memu_config_owns_similarity_under_search() -> None:
    resolved = resolve_system_config(
        "memu",
        environ={
            "LLM_API_KEY": "llm-key",
            "LLM_MODEL": "test-model",
            "MEMU_API_KEY": "memu-key",
        },
    )

    assert "min_similarity" not in resolved.raw_config
    assert resolved.raw_config["search"]["min_similarity"] == 0.3

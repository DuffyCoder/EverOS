from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from evaluation.src.adapters.online_base import OnlineAPIAdapter
from evaluation.src.core.benchmark_context import LatencyRecorder, max_retries_for
from evaluation.src.core.data_models import QAPair, SearchResult
from evaluation.src.core.stages import answer_stage


class _MinimalOnlineAdapter(OnlineAPIAdapter):
    """Concrete test double for the inherited online answer implementation."""

    async def _add_user_messages(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def _search_single_user(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _build_single_search_result(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _build_dual_search_result(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _get_answer_prompt(self) -> str:
        return "Context: {context}\nQuestion: {question}"


def _online_adapter(*, answer_config: dict[str, int] | None) -> _MinimalOnlineAdapter:
    adapter = _MinimalOnlineAdapter.__new__(_MinimalOnlineAdapter)
    adapter.config = {} if answer_config is None else {"answer": answer_config}
    adapter.llm_provider = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_online_answer_max_retries_is_an_inner_retry_count() -> None:
    adapter = _online_adapter(answer_config={"max_retries": 3})
    adapter.llm_provider.generate.side_effect = [
        RuntimeError("transient-1"),
        RuntimeError("transient-2"),
        "  recovered  ",
    ]

    result = await adapter.answer("question", "context")

    assert result == "recovered"
    assert adapter.llm_provider.generate.await_count == 3


@pytest.mark.asyncio
async def test_online_answer_keeps_three_inner_attempts_when_unconfigured() -> None:
    adapter = _online_adapter(answer_config=None)
    adapter.llm_provider.generate.side_effect = [
        RuntimeError("transient-1"),
        RuntimeError("transient-2"),
        "recovered",
    ]

    assert await adapter.answer("question", "context") == "recovered"
    assert adapter.llm_provider.generate.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_policy", "expected_attempts"),
    [("strict_no_retry", 1), ("retry_once", 2), ("realistic", 3)],
)
async def test_answer_stage_uses_outer_benchmark_retry_policy(
    retry_policy: str, expected_attempts: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _TimeoutAdapter:
        config = {"answer": {"max_concurrent": 1}}

        def __init__(self) -> None:
            self.answer = AsyncMock(side_effect=asyncio.TimeoutError)

        def get_answer_timeout(self) -> float:
            return 1.0

    adapter = _TimeoutAdapter()
    no_wait = AsyncMock()
    monkeypatch.setattr(answer_stage.asyncio, "sleep", no_wait)
    recorder = LatencyRecorder(retry_policy=retry_policy)
    qa_pairs = [
        QAPair(
            question_id="q1",
            question="question",
            answer="gold",
            metadata={"conversation_id": "c1"},
        )
    ]
    search_results = [
        SearchResult(
            query="question",
            conversation_id="c1",
            results=[],
            retrieval_metadata={"question_id": "q1"},
        )
    ]

    await answer_stage.run_answer_stage(
        adapter,
        qa_pairs,
        search_results,
        checkpoint_manager=None,
        logger=logging.getLogger(__name__),
        latency_recorder=recorder,
    )

    assert expected_attempts == max_retries_for(retry_policy)
    assert adapter.answer.await_count == expected_attempts
    assert no_wait.await_count == expected_attempts - 1
    assert len(recorder.records) == 1
    assert len(recorder.records[0].attempts) == expected_attempts

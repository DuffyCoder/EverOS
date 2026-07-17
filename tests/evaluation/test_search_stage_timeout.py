from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

import pytest

from evaluation.src.adapters.base import BaseAdapter
from evaluation.src.config.system_schema import (
    SystemSchemaError,
    validate_system_config,
)
from evaluation.src.core.benchmark_context import LatencyRecorder
from evaluation.src.core.data_models import Conversation, QAPair, SearchResult
from evaluation.src.core.stages import search_stage
from tests.evaluation import system_config_legacy
from tests.evaluation.test_system_config_schema import VALID_CONFIGS


class _MinimalAdapter(BaseAdapter):
    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.timeout_hook_calls = 0

    async def add(
        self, conversations: list[Conversation], **kwargs: Any
    ) -> None:
        return None

    async def search(
        self,
        query: str,
        conversation_id: str,
        index: Any,
        **kwargs: Any,
    ) -> SearchResult:
        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=[],
        )

    def get_search_timeout_seconds(self) -> float:
        self.timeout_hook_calls += 1
        return super().get_search_timeout_seconds()


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"search": {"timeout_seconds": 42}}, 42.0),
        ({}, 300.0),
        ({"search": {"timeout_seconds": None}}, 300.0),
    ],
)
def test_base_adapter_resolves_search_timeout(
    config: dict[str, Any], expected: float
) -> None:
    adapter = _MinimalAdapter(config)

    assert adapter.get_search_timeout_seconds() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("search_config", "expected"),
    [
        ({"num_workers": 1, "timeout_seconds": 42}, 42.0),
        ({"num_workers": 1}, 300.0),
    ],
)
async def test_search_stage_uses_adapter_timeout_once_per_stage(
    monkeypatch: pytest.MonkeyPatch,
    search_config: dict[str, Any],
    expected: float,
) -> None:
    adapter = _MinimalAdapter({"search": search_config})
    observed_timeouts: list[float | None] = []

    async def wait_for_spy(coroutine: Any, timeout: float | None) -> Any:
        observed_timeouts.append(timeout)
        return await coroutine

    monkeypatch.setattr(search_stage.asyncio, "wait_for", wait_for_spy)
    qa_pairs = [
        QAPair(
            question_id="q1",
            question="question",
            answer="gold",
            metadata={"conversation_id": "c1"},
        ),
        QAPair(
            question_id="q2",
            question="another question",
            answer="another gold",
            metadata={"conversation_id": "c1"},
        ),
    ]
    conversations = [Conversation(conversation_id="c1", messages=[])]

    results = await search_stage.run_search_stage(
        adapter,
        qa_pairs,
        index={},
        conversations=conversations,
        checkpoint_manager=None,
        logger=logging.getLogger(__name__),
        latency_recorder=LatencyRecorder(retry_policy="strict_no_retry"),
    )

    assert len(results) == 2
    assert observed_timeouts == [expected, expected]
    assert adapter.timeout_hook_calls == 1


@pytest.mark.parametrize("adapter", sorted(VALID_CONFIGS))
def test_each_adapter_search_schema_accepts_positive_timeout(adapter: str) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    config["search"]["timeout_seconds"] = 42

    assert validate_system_config(adapter, config) is config


@pytest.mark.parametrize(
    "invalid_timeout",
    [0, -1, True, "42", float("nan"), float("inf")],
)
def test_common_search_timeout_rejects_invalid_values(
    invalid_timeout: object,
) -> None:
    config = deepcopy(VALID_CONFIGS["evermemos"])
    config["search"]["timeout_seconds"] = invalid_timeout

    with pytest.raises(SystemSchemaError, match=r"search\.timeout_seconds"):
        validate_system_config("evermemos", config)


def test_common_search_timeout_accepts_explicit_none() -> None:
    config = deepcopy(VALID_CONFIGS["evermemos"])
    config["search"]["timeout_seconds"] = None

    assert validate_system_config("evermemos", config) is config


def test_dataset_override_accepts_common_search_timeout() -> None:
    config = deepcopy(VALID_CONFIGS["evermemos"])
    config["dataset_overrides"] = {
        "future-dataset": {"search": {"timeout_seconds": 42}}
    }

    assert validate_system_config("evermemos", config) is config


def test_legacy_normalization_keeps_search_timeout_behavior() -> None:
    raw = {
        "adapter": "evermemos_api",
        "search": {"timeout_seconds": 300, "top_k": 20},
        "answer": {"max_retries": 3},
    }

    assert system_config_legacy.normalized_effective_config(
        "evermemos_cloud_api", raw
    ) == raw

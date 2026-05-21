"""Verify Pipeline calls adapter.wait_post_add_settle() after Stage 1 Add.

Task 4 of 2026-05-21 ov-global-settle-active-poll plan.
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from evaluation.src.core.pipeline import Pipeline
from evaluation.src.core.data_models import Conversation, Dataset, EvaluationResult


def _stub_adapter(settle_result):
    adapter = MagicMock()
    adapter.config = {}
    adapter.get_system_info.return_value = {"name": "stub"}
    adapter.build_lazy_index.return_value = None
    adapter.wait_post_add_settle = AsyncMock(return_value=settle_result)
    return adapter


def _patch_stages(monkeypatch):
    """Replace pipeline's stage runners with cheap async stubs."""
    from evaluation.src.core import pipeline as pmod

    monkeypatch.setattr(pmod, "run_add_stage", AsyncMock(return_value={"index": None}))
    monkeypatch.setattr(pmod, "run_search_stage", AsyncMock(return_value=[]))
    monkeypatch.setattr(pmod, "run_answer_stage", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        pmod,
        "run_evaluate_stage",
        AsyncMock(
            return_value=EvaluationResult(
                total_questions=0, correct=0, accuracy=0.0,
                detailed_results=[], metadata={},
            )
        ),
    )


def _empty_dataset():
    return Dataset(
        dataset_name="t",
        conversations=[Conversation(conversation_id="c0", messages=[], metadata={})],
        qa_pairs=[],
        metadata={},
    )


@pytest.mark.asyncio
async def test_pipeline_calls_wait_post_add_settle_when_add_just_completed(
    tmp_path: Path, monkeypatch
):
    """When add just ran AND search is in stages, hook is invoked once."""
    _patch_stages(monkeypatch)
    adapter = _stub_adapter(
        settle_result={"embedding": {"processed": 10, "error_count": 0, "errors": []}}
    )

    pipeline = Pipeline(
        adapter=adapter,
        evaluator=MagicMock(),
        llm_provider=MagicMock(),
        output_dir=tmp_path,
        use_checkpoint=False,
    )

    await pipeline.run(dataset=_empty_dataset(), stages=["add", "search"])

    adapter.wait_post_add_settle.assert_awaited_once()


@pytest.mark.asyncio
async def test_pipeline_skips_settle_when_only_add_stage(
    tmp_path: Path, monkeypatch
):
    """When search is NOT in stages, settle wait is skipped — saves time."""
    _patch_stages(monkeypatch)
    adapter = _stub_adapter(settle_result=None)

    pipeline = Pipeline(
        adapter=adapter,
        evaluator=MagicMock(),
        llm_provider=MagicMock(),
        output_dir=tmp_path,
        use_checkpoint=False,
    )

    await pipeline.run(dataset=_empty_dataset(), stages=["add"])

    adapter.wait_post_add_settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_pipeline_tolerates_settle_exception(
    tmp_path: Path, monkeypatch, caplog
):
    """Settle hook raising should NOT abort the pipeline — log + continue."""
    _patch_stages(monkeypatch)
    adapter = _stub_adapter(settle_result=None)
    adapter.wait_post_add_settle = AsyncMock(side_effect=RuntimeError("OV down"))

    pipeline = Pipeline(
        adapter=adapter,
        evaluator=MagicMock(),
        llm_provider=MagicMock(),
        output_dir=tmp_path,
        use_checkpoint=False,
    )

    # Should NOT raise — pipeline continues to search/answer/evaluate
    await pipeline.run(dataset=_empty_dataset(), stages=["add", "search"])

    adapter.wait_post_add_settle.assert_awaited_once()

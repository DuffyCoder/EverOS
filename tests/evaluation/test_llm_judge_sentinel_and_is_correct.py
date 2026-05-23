"""Goal 5 hardening — LLMJudge sentinel detection + per-result ``is_correct``.

Two related framework issues:

1. **Sentinel-as-wrong** (Audit #7 R1/R2/R3): when Stage 3 fails internally
   it writes ``"Error: ..."`` into ``AnswerResult.answer``. Without sentinel
   detection the judge feeds the string to the LLM and gets back a
   near-deterministic WRONG, silently pulling acc down. Treat
   sentinel/empty answers as judge-unavailable (None judgments) so they
   drop out of the accuracy denominator — same semantics as a transient
   judge failure.

2. **Missing ``is_correct`` on the result dict**: ``HybridEvaluator
   ._calculate_category_stats`` calls ``result.get("is_correct", False)``
   but ``LLMJudge`` only emits ``llm_judgments``. The fall-through default
   means every open-ended QA category counted as wrong in the hybrid
   category breakdown. Add a majority-vote ``is_correct`` field (None
   when judge unavailable) so downstream consumers (and the
   ``eval_results.json`` file itself) see a usable per-result verdict.

Run:
    .venv/bin/python -m pytest tests/evaluation/test_llm_judge_sentinel_and_is_correct.py -v
"""
from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from evaluation.src.core.data_models import AnswerResult
from evaluation.src.evaluators.llm_judge import LLMJudge
from evaluation.src.utils.llm_keys import (
    _reset_pool_cache_for_testing,
    is_alt_llm_key_var,
)


@pytest.fixture(autouse=True)
def _isolate_llm_key_pool(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    for name in list(os.environ):
        if is_alt_llm_key_var(name):
            monkeypatch.delenv(name, raising=False)
    _reset_pool_cache_for_testing()
    yield
    _reset_pool_cache_for_testing()


def _make_judge(*, num_runs: int = 3, max_retries: int | None = None):
    cfg = {
        "llm": {
            "api_key": "stub",
            "base_url": "http://stub",
            "model": "stub-model",
        },
        "num_runs": num_runs,
    }
    if max_retries is not None:
        cfg["judge_max_retries"] = max_retries
    judge = LLMJudge(cfg)
    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock()
    judge.clients = [mock_client]
    judge.client = mock_client
    return judge


def _label_response(label: str):
    msg = MagicMock()
    msg.content = json.dumps({"label": label})
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _ar(answer: str, *, qid: str = "q0", question: str = "Q?", gold: str = "G"):
    return AnswerResult(
        question_id=qid,
        question=question,
        answer=answer,
        golden_answer=gold,
        conversation_id="c0",
    )


@pytest.mark.asyncio
class TestSentinelDetection:
    async def test_error_prefix_skips_judge_calls(self):
        """``Error: ...`` answer → no API calls, all judgments None."""
        judge = _make_judge(num_runs=3)
        called = {"n": 0}

        async def should_not_fire(**_kwargs):
            called["n"] += 1
            return _label_response("CORRECT")

        judge.client.chat.completions.create = should_not_fire

        result = await judge._evaluate_single_answer(
            _ar("Error: Failed to generate answer")
        )
        assert called["n"] == 0, "judge LLM must not be called for sentinel answer"
        assert result["llm_judgments"] == {
            "judgment_1": None,
            "judgment_2": None,
            "judgment_3": None,
        }
        assert result["is_correct"] is None

    async def test_timeout_sentinel_skips_judge_calls(self):
        """``Error: Answer generation timeout after retries`` sentinel."""
        judge = _make_judge(num_runs=2)
        called = {"n": 0}

        async def should_not_fire(**_kwargs):
            called["n"] += 1
            return _label_response("CORRECT")

        judge.client.chat.completions.create = should_not_fire

        result = await judge._evaluate_single_answer(
            _ar("Error: Answer generation timeout after retries")
        )
        assert called["n"] == 0
        assert result["is_correct"] is None

    async def test_empty_answer_skips_judge_calls(self):
        judge = _make_judge(num_runs=3)
        called = {"n": 0}

        async def should_not_fire(**_kwargs):
            called["n"] += 1
            return _label_response("CORRECT")

        judge.client.chat.completions.create = should_not_fire

        result = await judge._evaluate_single_answer(_ar(""))
        assert called["n"] == 0
        assert result["is_correct"] is None
        assert all(v is None for v in result["llm_judgments"].values())

    async def test_whitespace_only_answer_skips_judge_calls(self):
        judge = _make_judge(num_runs=3)
        called = {"n": 0}

        async def should_not_fire(**_kwargs):
            called["n"] += 1
            return _label_response("CORRECT")

        judge.client.chat.completions.create = should_not_fire

        result = await judge._evaluate_single_answer(_ar("   \n\t  "))
        assert called["n"] == 0
        assert result["is_correct"] is None

    async def test_normal_answer_does_call_judge(self):
        """Non-sentinel answers must still hit the LLM."""
        judge = _make_judge(num_runs=2)
        called = {"n": 0}

        async def fire(**_kwargs):
            called["n"] += 1
            return _label_response("CORRECT")

        judge.client.chat.completions.create = fire

        result = await judge._evaluate_single_answer(_ar("This is the answer."))
        assert called["n"] == 2, "must run num_runs judge calls on a real answer"
        assert result["is_correct"] is True


@pytest.mark.asyncio
class TestIsCorrectMajority:
    async def test_unanimous_correct(self):
        judge = _make_judge(num_runs=3)
        judge.client.chat.completions.create = AsyncMock(
            return_value=_label_response("CORRECT")
        )
        r = await judge._evaluate_single_answer(_ar("the answer"))
        assert r["is_correct"] is True

    async def test_unanimous_wrong(self):
        judge = _make_judge(num_runs=3)
        judge.client.chat.completions.create = AsyncMock(
            return_value=_label_response("WRONG")
        )
        r = await judge._evaluate_single_answer(_ar("the answer"))
        assert r["is_correct"] is False

    async def test_two_of_three_majority_correct(self):
        judge = _make_judge(num_runs=3)
        seq = ["CORRECT", "WRONG", "CORRECT"]
        idx = {"i": 0}

        async def per_call(**_kw):
            label = seq[idx["i"] % len(seq)]
            idx["i"] += 1
            return _label_response(label)

        judge.client.chat.completions.create = per_call
        r = await judge._evaluate_single_answer(_ar("the answer"))
        assert r["is_correct"] is True

    async def test_tie_resolves_to_false_conservative(self):
        """Even runs with a 1-1 tie → False (conservative).

        Strict-majority test ``positives * 2 > len(non_none)`` makes a tie
        resolve to False so a borderline QA is not silently inflated.
        """
        judge = _make_judge(num_runs=2)
        seq = ["CORRECT", "WRONG"]
        idx = {"i": 0}

        async def per_call(**_kw):
            label = seq[idx["i"] % len(seq)]
            idx["i"] += 1
            return _label_response(label)

        judge.client.chat.completions.create = per_call
        r = await judge._evaluate_single_answer(_ar("the answer"))
        assert r["is_correct"] is False

    async def test_partial_unavailable_does_not_break_majority(self):
        """One judge call returning None → majority uses only the non-None
        votes. 1 CORRECT + 1 None + 1 CORRECT → True (2 of 2 non-none)."""
        judge = _make_judge(num_runs=3, max_retries=1)
        # Sequence: CORRECT, transient error (returns None after retry), CORRECT
        call_n = {"i": 0}

        async def mixed(**_kw):
            i = call_n["i"]
            call_n["i"] += 1
            if i == 1:
                raise Exception("429 rate limit exceeded")
            return _label_response("CORRECT")

        judge.client.chat.completions.create = mixed
        r = await judge._evaluate_single_answer(_ar("the answer"))
        non_none = [v for v in r["llm_judgments"].values() if v is not None]
        assert len(non_none) == 2
        assert r["is_correct"] is True

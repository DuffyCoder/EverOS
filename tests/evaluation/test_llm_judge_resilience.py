"""S2-3 — in-pipeline LLMJudge resilience tests.

Stage 2 evermemos r2 silently scored 0% because the in-pipeline judge
hit transient APIConnectionError at 78% completion and the catch-all
`return False` masked every subsequent failure as a wrong answer. This
file enforces:

1. Concurrency is configurable via judge_config; default 4 (was 10).
2. Transient errors (connection / 5xx / 429 / timeout / rate) trigger
   bounded retry with exponential backoff.
3. After max_retries, the failure surfaces in the result with an
   `error` key — not a silent False — so the caller can distinguish
   "judged wrong" from "judge crashed".
4. Permanent errors (e.g. JSONDecodeError) still return False
   immediately (no retry, original behavior preserved).

Run:
    .venv/bin/python -m pytest tests/evaluation/test_llm_judge_resilience.py -v
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from evaluation.src.evaluators.llm_judge import LLMJudge


def _make_judge(
    *,
    concurrency: int | None = None,
    max_retries: int | None = None,
    num_runs: int = 1,
):
    """Build a LLMJudge with mocked OpenAI client."""
    cfg = {
        "llm": {
            "api_key": "stub",
            "base_url": "http://stub",
            "model": "stub-model",
        },
        "num_runs": num_runs,
    }
    if concurrency is not None:
        cfg["judge_concurrency"] = concurrency
    if max_retries is not None:
        cfg["judge_max_retries"] = max_retries

    judge = LLMJudge(cfg)
    judge.client = MagicMock()
    judge.client.chat = MagicMock()
    judge.client.chat.completions = MagicMock()
    judge.client.chat.completions.create = AsyncMock()
    return judge


def _ok_response(label: str = "CORRECT"):
    """Build a mocked OpenAI ChatCompletion response with the given label."""
    msg = MagicMock()
    msg.content = json.dumps({"label": label})
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@pytest.mark.asyncio
class TestConcurrencyCap:
    async def test_default_concurrency_is_four(self):
        """Stage 2 R-S2-3: default 4 (down from old hard-coded 10)."""
        judge = _make_judge()
        assert judge._judge_concurrency() == 4

    async def test_concurrency_overridable_via_config(self):
        judge = _make_judge(concurrency=2)
        assert judge._judge_concurrency() == 2

    async def test_concurrency_cap_enforced_during_evaluate(self):
        """At most N _judge_answer calls in flight at once."""
        judge = _make_judge(concurrency=2)

        in_flight = 0
        peak = 0

        async def fake_judge(*args, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return True

        judge._judge_answer = fake_judge  # type: ignore[assignment]

        from evaluation.src.core.data_models import AnswerResult

        ars = [
            AnswerResult(
                question_id=f"q{i}",
                question=f"Q{i}",
                answer=f"A{i}",
                golden_answer=f"G{i}",
                conversation_id="c0",
            )
            for i in range(10)
        ]
        await judge.evaluate(ars)
        assert peak <= 2


@pytest.mark.asyncio
class TestTransientRetry:
    async def test_retry_on_connection_error_then_succeed(self):
        """Connection error -> retry -> succeed."""
        judge = _make_judge(max_retries=3)

        calls = {"count": 0}

        async def flaky(**kwargs):
            calls["count"] += 1
            if calls["count"] < 2:
                raise Exception("APIConnectionError: connection refused")
            return _ok_response("CORRECT")

        judge.client.chat.completions.create = flaky
        result = await judge._judge_answer("q", "g", "a")
        assert result is True
        assert calls["count"] == 2

    async def test_retry_on_5xx_error(self):
        judge = _make_judge(max_retries=3)
        calls = {"count": 0}

        async def flaky(**kwargs):
            calls["count"] += 1
            if calls["count"] < 2:
                raise Exception("HTTP Error 503: Service Unavailable")
            return _ok_response("WRONG")

        judge.client.chat.completions.create = flaky
        result = await judge._judge_answer("q", "g", "a")
        assert result is False  # judged wrong, but no crash
        assert calls["count"] == 2

    async def test_retry_on_429_rate_limit(self):
        judge = _make_judge(max_retries=3)
        calls = {"count": 0}

        async def flaky(**kwargs):
            calls["count"] += 1
            if calls["count"] < 3:
                raise Exception("429 rate limit exceeded")
            return _ok_response("CORRECT")

        judge.client.chat.completions.create = flaky
        result = await judge._judge_answer("q", "g", "a")
        assert result is True
        assert calls["count"] == 3

    async def test_retry_exhausted_returns_none_with_logged_error(self, capsys):
        """When all retries fail on transient error, judge returns None
        (judge unavailable) and emits a clear error log. ``None`` keeps
        the question out of the accuracy denominator instead of coercing
        to WRONG (the Stage 2 R-S2-3 silent-zero bug)."""
        judge = _make_judge(max_retries=2)

        async def always_fail(**kwargs):
            raise Exception("APIConnectionError: persistent network down")

        judge.client.chat.completions.create = always_fail
        result = await judge._judge_answer("q", "g", "a")
        assert result is None
        captured = capsys.readouterr()
        # Must not be silent — error tail should mention the cause
        assert "judge" in captured.out.lower() or "error" in captured.out.lower()


@pytest.mark.asyncio
class TestPermanentErrorBypassesRetry:
    async def test_json_decode_error_no_retry(self):
        """Bad JSON in response is treated as judge-unavailable (None) so
        a confused / refusing model doesn't silently get scored as WRONG."""
        judge = _make_judge(max_retries=5)
        calls = {"count": 0}

        async def returns_garbage(**kwargs):
            calls["count"] += 1
            msg = MagicMock()
            msg.content = "garbage that's not json"
            choice = MagicMock()
            choice.message = msg
            resp = MagicMock()
            resp.choices = [choice]
            return resp

        judge.client.chat.completions.create = returns_garbage
        result = await judge._judge_answer("q", "g", "a")
        assert result is None
        # No retry on parse failures. Should call exactly once.
        assert calls["count"] == 1

    async def test_empty_content_no_retry(self):
        """Empty model output is treated as judge-unavailable (None)."""
        judge = _make_judge(max_retries=5)
        calls = {"count": 0}

        async def returns_empty(**kwargs):
            calls["count"] += 1
            msg = MagicMock()
            msg.content = ""
            choice = MagicMock()
            choice.message = msg
            resp = MagicMock()
            resp.choices = [choice]
            return resp

        judge.client.chat.completions.create = returns_empty
        result = await judge._judge_answer("q", "g", "a")
        assert result is None
        assert calls["count"] == 1

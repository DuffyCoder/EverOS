"""
LLM Judge evaluator - use LLM to judge answer correctness.

Aligned with evaluation_archive logic:
- Keep independent judgments for each run (judgment_1, judgment_2, judgment_3)
- Calculate accuracy for each run separately
- Output mean and std

Three judgment outcomes per call:
  * True   — judge returned CORRECT
  * False  — judge returned WRONG
  * None   — judge unavailable (transient retries exhausted, empty/invalid
             response, permanent error). Excluded from accuracy denominator
             so a Sophnet rate-limit storm during evaluate doesn't silently
             collapse the score to 0.
"""

import asyncio
import json
import logging
import numpy as np
from typing import List, Dict, Any, Optional
from collections import defaultdict
from openai import AsyncOpenAI
from tqdm import tqdm

from evaluation.src.evaluators.base import BaseEvaluator
from evaluation.src.evaluators.registry import register_evaluator
from evaluation.src.core.data_models import AnswerResult, EvaluationResult
from evaluation.src.utils.llm_keys import collect_llm_key_pool
from evaluation.src.utils.prompts import get_prompt, format_prompt


logger = logging.getLogger(__name__)

# Default concurrent in-flight judge calls per AsyncOpenAI client.
# Conservative value 4 historically chosen before multi-key support;
# overridable per-yaml via ``judge_concurrency_per_key`` (see __init__).
# Single judge call latency is ~25-30s wall (sophnet doubao-pro), so 16
# in-flight gets ~0.55 call/s/key — fine within sophnet's per-key RPM
# ceiling. Push higher (32/64) if user can verify their sophnet plan's
# RPM allows it; watch for 429 storms in pipeline.log.
_BASE_CONCURRENCY_PER_KEY = 4


def _is_sentinel_answer(answer: Optional[str]) -> bool:
    """Detect Stage 3 sentinel / empty answers that should bypass the judge.

    ``answer_stage.py:307/340/343`` writes literal ``"Error: ..."`` strings
    into ``AnswerResult.answer`` on transient adapter failures. Feeding
    these to the LLM just produces a near-deterministic WRONG. We treat
    them as judge-unavailable (None judgments) so they drop out of the
    accuracy denominator.
    """
    if not answer:
        return True
    stripped = answer.strip()
    if not stripped:
        return True
    return stripped.startswith("Error:")


def _majority_vote(judgments: List[Optional[bool]]) -> Optional[bool]:
    """Per-result verdict for ``eval_results.json`` / hybrid category stats.

    None when all judgments are unavailable. Strict majority of non-None
    votes — a tie resolves to False (conservative; do not silently
    inflate a borderline QA). Mirrors how
    ``LLMJudge.evaluate`` already excludes None from the denominator.
    """
    non_none = [j for j in judgments if j is not None]
    if not non_none:
        return None
    positives = sum(1 for j in non_none if j)
    return positives * 2 > len(non_none)


@register_evaluator("llm_judge")
class LLMJudge(BaseEvaluator):
    """LLM judge evaluator."""

    def __init__(self, config: dict):
        super().__init__(config)

        # Initialize OpenAI client(s). Multi-key support: when
        # ``llm.api_keys`` is a list, build one AsyncOpenAI client per
        # key and round-robin between them on each judge call. This
        # spreads the per-key rate-limit window across N keys, giving
        # near-N× throughput when the bottleneck is per-key RPM
        # (typical with sophnet on full-LoCoMo judge runs of ~4620
        # calls). ``llm.api_key`` (singular) is still accepted as a
        # one-key fallback for backward compatibility.
        llm_config = config.get("llm", {})
        api_keys = llm_config.get("api_keys")
        if isinstance(api_keys, list):
            api_keys = [k for k in api_keys if isinstance(k, str) and k.strip()]
        else:
            api_keys = []
        if not api_keys:
            single = llm_config.get("api_key")
            if isinstance(single, str) and single.strip():
                api_keys = [single]
        # Env fallback: when caller passed nothing or only a single key,
        # scan LLM_API_KEY / LLM_API_KEY_2 / ... numeric suffix vars via the
        # shared helper. Adding keys to .env then auto-widens the pool here
        # too — no separate config change required.
        if len(api_keys) <= 1:
            env_keys = collect_llm_key_pool()
            # Merge yaml-provided key with env extras, preserving order
            # and dedup-ing.
            api_keys = list(dict.fromkeys(api_keys + env_keys))
        if not api_keys:
            raise ValueError(
                "LLMJudge: llm.api_key or llm.api_keys must be set "
                "(got empty / missing); or set LLM_API_KEY env"
            )
        base_url = llm_config.get("base_url", "https://api.openai.com/v1")
        self.clients = [
            AsyncOpenAI(api_key=k, base_url=base_url) for k in api_keys
        ]
        # Index-based round-robin: asyncio is single-threaded and the
        # increment+modulo is atomic between awaits, so no lock needed.
        # An explicit counter (vs itertools.cycle) lets _next_client_with_index
        # report which key is in use for diagnostic logs.
        self._client_counter = 0
        self.model = llm_config.get("model", "gpt-4o-mini")
        self.num_runs = config.get("num_runs", 3)

        # Concurrency cap + transient retry. Default scales with the
        # number of api_keys: N keys × per-key in-flight budget. Adding a
        # key to .env automatically buys parallelism instead of spreading
        # the same total in-flight across keys. Two yaml knobs:
        #   * judge_concurrency_per_key (int): per-AsyncOpenAI in-flight
        #     budget. Default _BASE_CONCURRENCY_PER_KEY (4). Bump up to
        #     saturate sophnet per-key RPM — e.g. 16 brings the 4-key
        #     pool from 16→64 in-flight, observed ~4× speedup on Stage 4
        #     in smoke3 (acc unchanged).
        #   * judge_concurrency (int): hard override on the final number.
        #     Wins over per-key calc if set.
        per_key = int(
            config.get("judge_concurrency_per_key") or _BASE_CONCURRENCY_PER_KEY
        )
        configured = config.get("judge_concurrency")
        if configured is None:
            self._concurrency = per_key * len(self.clients)
        else:
            self._concurrency = int(configured)
        self._max_retries = int(config.get("judge_max_retries", 4))
        logger.info(
            "LLMJudge initialized: keys=%d per_key=%d concurrency=%d retries=%d",
            len(self.clients), per_key, self._concurrency, self._max_retries,
        )

    def _judge_concurrency(self) -> int:
        return self._concurrency

    def _judge_max_retries(self) -> int:
        return self._max_retries

    def _next_client_with_index(self) -> tuple[int, AsyncOpenAI]:
        """Round-robin next AsyncOpenAI client + its position. Each
        call advances by one, so consecutive judge calls (and retries
        within one call) land on different keys. Returning the index
        lets the caller include it in diagnostic logs so a 429 storm
        can be traced to a specific key."""
        idx = self._client_counter % len(self.clients)
        self._client_counter += 1
        return idx, self.clients[idx]

    @staticmethod
    def _is_transient_error(err: BaseException) -> bool:
        """Classify whether a judge call error should trigger retry.

        Transient signals:
          - APIConnectionError
          - 5xx HTTP status text
          - 429 / rate limit
          - timeout
          - "temporarily" wording from upstream proxies

        Anything else (auth, JSON parse, etc.) is permanent and returns
        immediately.
        """
        msg = str(err).lower()
        return (
            "connection" in msg
            or "timeout" in msg
            or "429" in msg
            or "503" in msg
            or "502" in msg
            or "504" in msg
            or "5xx" in msg
            or "rate" in msg
            or "temporarily" in msg
        )

    async def evaluate(self, answer_results: List[AnswerResult]) -> EvaluationResult:
        """
        Evaluate answers using LLM, return statistics from multiple runs.

        Args:
            answer_results: List of answer results

        Returns:
            Evaluation result with mean and std
        """
        print(f"\n{'='*60}")
        print(f"Evaluation: LLM Judge (model={self.model}, runs={self.num_runs})")
        print(f"{'='*60}")

        detailed_results = []

        # Evaluate all answers concurrently. Cap is configurable
        # (default 4; was hard-coded 10 which saturated sophnet).
        semaphore = asyncio.Semaphore(self._judge_concurrency())

        # Use tqdm progress bar
        pbar = tqdm(total=len(answer_results), desc="⚖️  Evaluate Progress", unit="qa")

        async def evaluate_single(answer_result: AnswerResult):
            async with semaphore:
                result = await self._evaluate_single_answer(answer_result)
                pbar.update(1)  # Update progress bar
                return result

        tasks = [evaluate_single(ar) for ar in answer_results]
        results = await asyncio.gather(*tasks)

        # Close progress bar
        pbar.close()

        # Collect results
        for result in results:
            detailed_results.append(result)

        # Calculate accuracy for each run separately
        run_scores = []
        category_stats = defaultdict(
            lambda: {"correct": [0] * self.num_runs, "total": 0}
        )

        # Per-run accuracy: skip None ("judge unavailable") values from the
        # denominator so a transient rate-limit storm doesn't silently push
        # the score toward 0. ``unavailable_count`` is preserved in
        # metadata so downstream tooling can flag low-coverage runs.
        unavailable_per_run = [0] * self.num_runs
        for i in range(self.num_runs):
            judgment_key = f"judgment_{i+1}"
            correct_count = 0
            total_count = 0

            for result in detailed_results:
                llm_judgments = result.get("llm_judgments", {})
                category = result.get("category")

                if judgment_key in llm_judgments:
                    val = llm_judgments[judgment_key]
                    if val is None:
                        unavailable_per_run[i] += 1
                        # Don't count toward category total either —
                        # category accuracy uses each category's own
                        # per-run available judgment count below.
                    else:
                        total_count += 1
                        if val:
                            correct_count += 1
                            if category is not None:
                                category_stats[category]["correct"][i] += 1

                    if i == 0 and category is not None:
                        if val is not None:
                            category_stats[category]["total"] += 1

            if total_count > 0:
                run_accuracy = correct_count / total_count
                run_scores.append(run_accuracy)

        # Calculate statistics
        mean_accuracy = np.mean(run_scores) if run_scores else 0.0
        std_accuracy = np.std(run_scores) if run_scores else 0.0

        # Calculate accuracy for each category
        category_accuracies = {}
        for category, stats in category_stats.items():
            cat_accuracies = []
            for i in range(self.num_runs):
                if stats["total"] > 0:
                    cat_acc = stats["correct"][i] / stats["total"]
                    cat_accuracies.append(cat_acc)

            if cat_accuracies:
                category_accuracies[str(category)] = {
                    "mean": np.mean(cat_accuracies),
                    "std": np.std(cat_accuracies),
                    "individual_runs": cat_accuracies,
                    "total": stats["total"],
                }

        print(f"\n✅ Evaluation complete:")
        print(f"   - Total questions: {len(answer_results)}")
        print(f"   - Mean accuracy: {mean_accuracy:.4f} ({mean_accuracy*100:.2f}%)")
        print(f"   - Std deviation: {std_accuracy:.4f}")
        print(f"   - Run accuracies: {[f'{s:.4f}' for s in run_scores]}")
        if any(unavailable_per_run):
            print(
                f"   - ⚠ judge unavailable per run: {unavailable_per_run} "
                f"(excluded from denominator)"
            )

        if category_accuracies:
            print(f"\n📊 Category statistics:")
            for cat, stats in sorted(category_accuracies.items()):
                print(
                    f"   Category {cat}: {stats['mean']:.4f} ± {stats['std']:.4f} (n={stats['total']})"
                )

        # Group by conversation
        grouped_results = self._group_by_conversation(detailed_results)

        return EvaluationResult(
            total_questions=len(answer_results),
            correct=int(
                mean_accuracy * len(answer_results)
            ),  # Use mean for calculation
            accuracy=mean_accuracy,
            detailed_results=grouped_results,
            metadata={
                "model": self.model,
                "num_runs": self.num_runs,
                "mean_accuracy": mean_accuracy,
                "std_accuracy": std_accuracy,
                "run_scores": run_scores,
                "category_accuracies": category_accuracies,
                "unavailable_per_run": unavailable_per_run,
            },
        )

    def _group_by_conversation(
        self, detailed_results: List[Dict]
    ) -> Dict[str, List[Dict]]:
        """
        Group results by conversation (e.g., locomo_exp_user_0, locomo_exp_user_1, etc.).
        """
        grouped = defaultdict(list)

        for result in detailed_results:
            question_id = result.get("question_id", "")

            # Extract conversation info from question_id
            # Example: "locomo_0_qa0" -> "locomo_exp_user_0"
            # Example: "personamem_5_qa2" -> "personamem_exp_user_5"
            if "_qa" in question_id:
                parts = question_id.split("_qa")
                conv_id = parts[0]  # "locomo_0" or "personamem_5"

                # Convert to evaluation_archive format
                if "_" in conv_id:
                    dataset_name, conv_num = conv_id.rsplit("_", 1)
                    group_key = f"{dataset_name}_exp_user_{conv_num}"
                else:
                    group_key = f"{conv_id}_exp_user_0"
            else:
                # Use default group if format doesn't match
                group_key = "default_group"

            grouped[group_key].append(result)

        return dict(grouped)

    async def _evaluate_single_answer(self, answer_result: AnswerResult) -> dict:
        """
        Evaluate single answer, keep independent judgment for each run.

        Each judgment is True/False/None where None means the judge call
        could not produce a verdict (transient retries exhausted, empty
        content, parse failure, etc.). Aggregation excludes None values
        from the accuracy denominator.

        Sentinel/empty answers (Audit #7 R1/R2/R3): when Stage 3 fails
        internally it surfaces ``"Error: ..."`` (or ``""``) as the
        answer. Feeding it to the judge would burn API quota only to
        get a near-deterministic WRONG that silently pulls accuracy
        down. Treat sentinel answers as judge-unavailable (all
        judgments None) — same semantics as transient judge failures
        so they drop out of the denominator. Stage 3 still emits a
        loud ❌ log so operators see the underlying adapter failure.
        """
        question = answer_result.question
        golden_answer = answer_result.golden_answer
        generated_answer = answer_result.answer

        judgments: List[Optional[bool]] = []
        if _is_sentinel_answer(generated_answer):
            print(
                f"  ⚠️ LLMJudge: sentinel/empty answer for "
                f"{answer_result.question_id} "
                f"({(generated_answer or '')[:60]!r}) — judge skipped"
            )
            judgments = [None] * self.num_runs
        else:
            for _ in range(self.num_runs):
                judgment = await self._judge_answer(
                    question, golden_answer, generated_answer
                )
                judgments.append(judgment)

        # Use judgment_1, judgment_2, ... format
        llm_judgments = {
            f"judgment_{i+1}": judgment for i, judgment in enumerate(judgments)
        }

        return {
            "question_id": answer_result.question_id,
            "question": question,
            "golden_answer": golden_answer,
            "generated_answer": generated_answer,
            "llm_judgments": llm_judgments,
            "is_correct": _majority_vote(judgments),
            "category": answer_result.category,
        }

    async def _judge_answer(
        self, question: str, golden_answer: str, generated_answer: str
    ) -> Optional[bool]:
        """
        Use LLM to judge if answer is correct.

        Bounded retry on transient upstream errors (connection / 5xx / 429
        / timeout / rate-limit phrasing). When retries exhaust, OR the
        model returns empty/unparseable content, OR a permanent
        non-judgment error occurs, this method returns ``None`` rather
        than ``False``.

        ``None`` signals "judge unavailable" — distinct from "judge said
        wrong". The aggregation in ``evaluate()`` excludes None judgments
        from the accuracy denominator, so a Sophnet rate-limit storm
        cannot silently collapse the score to 0 the way a False fallback
        would. The log line at the failure site is still loud so operators
        can see how many calls fell through.

        Historical behavior (returning False on every failure) was the
        Stage 2 R-S2-3 silent-zero bug, observed in production when
        evaluate's 4×4×concurrent judge calls exceeded Sophnet's per-key
        rate window — 86–97% of judgments came back as a coerced False
        and the published accuracy was meaningless.

        Returns:
            True   if judge said CORRECT
            False  if judge said WRONG
            None   if judge unavailable (skip from denominator)
        """
        # Use configured prompts
        system_prompt = get_prompt("llm_judge", "system_prompt")
        user_prompt = format_prompt(
            "llm_judge",
            "user_prompt",
            question=question,
            golden_answer=golden_answer,
            generated_answer=generated_answer,
        )

        max_retries = self._judge_max_retries()
        delay = 1.0
        last_transient: Exception | None = None

        for attempt in range(max_retries):
            # Pick the next client on every attempt — both for spreading
            # rate-limit pressure across keys on the happy path and so
            # a retry after a 429/timeout lands on a fresh key window.
            key_idx, client = self._next_client_with_index()
            try:
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0,
                )
            except Exception as e:  # noqa: BLE001 — classify before re-raising
                if not self._is_transient_error(e):
                    print(f"  ⚠️ LLM Judge failed (permanent): {type(e).__name__}: {e}")
                    return None
                last_transient = e
                logger.warning(
                    "LLM judge transient error attempt=%d/%d key_idx=%d: %s: %s",
                    attempt + 1, max_retries, key_idx, type(e).__name__, e,
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
                continue

            # Successful API call — parse content; parse failures are
            # treated as judge-unavailable (None) so a confused/refusing
            # model doesn't silently get scored as WRONG.
            content = response.choices[0].message.content

            if not content:
                print(f"  ⚠️ LLM Judge: Empty response from model {self.model}")
                return None

            json_str = self._extract_json(content)
            if not json_str:
                print(f"  ⚠️ LLM Judge: No JSON found in response")
                print(f"     Raw response: {content[:200]}...")
                return None

            try:
                result = json.loads(json_str)
            except json.JSONDecodeError as e:
                print(f"  ⚠️ LLM Judge JSON parse failed: {e}")
                print(f"     Raw response: {content[:200] if content else 'None'}...")
                return None

            label = result.get("label", "")
            if not label:
                print(f"  ⚠️ LLM Judge: No label found in response")
                print(f"     Raw response: {content}...")
                return None

            return label.strip().upper() == "CORRECT"

        # Out of retries on transient errors — log loudly and return None
        # (judge unavailable). Returning False here was the Stage 2 R-S2-3
        # silent-zero bug.
        print(
            f"  ⚠️ LLM Judge transient error exhausted retries "
            f"({max_retries}): {type(last_transient).__name__}: {last_transient}"
        )
        return None

    def _extract_json(self, content: str) -> str:
        """
        Extract JSON from LLM response that may contain explanation text.

        Handles:
        1. Pure JSON: {"label": "CORRECT"}
        2. JSON with explanation: Some text... {"label": "CORRECT"}
        3. Markdown code block: ```json {"label": "CORRECT"} ```
        """
        import re

        # Try 1: Extract from markdown code block
        code_block_match = re.search(
            r'```(?:json)?\s*(\{[^`]*\})\s*```', content, re.DOTALL
        )
        if code_block_match:
            return code_block_match.group(1).strip()

        # Try 2: Find JSON object pattern
        json_match = re.search(r'\{[^{}]*"label"\s*:\s*"[^"]*"[^{}]*\}', content)
        if json_match:
            return json_match.group(0)

        # Try 3: Return original content (let json.loads handle it)
        return content.strip()

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
import numpy as np
from typing import List, Dict, Any, Optional
from collections import defaultdict
from openai import AsyncOpenAI
from tqdm import tqdm

from evaluation.src.evaluators.base import BaseEvaluator
from evaluation.src.evaluators.registry import register_evaluator
from evaluation.src.core.data_models import AnswerResult, EvaluationResult
from evaluation.src.utils.prompts import get_prompt, format_prompt


@register_evaluator("llm_judge")
class LLMJudge(BaseEvaluator):
    """LLM judge evaluator."""

    def __init__(self, config: dict):
        super().__init__(config)

        # Initialize OpenAI client
        llm_config = config.get("llm", {})
        self.client = AsyncOpenAI(
            api_key=llm_config.get("api_key"),
            base_url=llm_config.get("base_url", "https://api.openai.com/v1"),
        )
        self.model = llm_config.get("model", "gpt-4o-mini")
        self.num_runs = config.get("num_runs", 3)

        # Stage 2 R-S2-3 hardening: concurrency cap + transient retry.
        # Old hard-coded Semaphore(10) saturated sophnet during 50Q runs and
        # the catch-all `return False` masked transient connection errors as
        # silent wrong-answer judgments (evermemos r2 hit 0% via this path).
        # Both are now configurable; defaults reflect rejudge.py's empirically
        # validated values (concurrency=4, max_retries=4).
        self._concurrency = int(config.get("judge_concurrency", 4))
        self._max_retries = int(config.get("judge_max_retries", 4))

    def _judge_concurrency(self) -> int:
        return self._concurrency

    def _judge_max_retries(self) -> int:
        return self._max_retries

    @staticmethod
    def _is_transient_error(err: BaseException) -> bool:
        """Classify whether a judge call error should trigger retry.

        Transient signals (mirror rejudge.py):
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
        # (default 4; was hard-coded 10 which saturated sophnet — see
        # Stage 2 R-S2-3 / rejudge.py).
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
        """
        question = answer_result.question
        golden_answer = answer_result.golden_answer
        generated_answer = answer_result.answer

        # Multiple evaluations, keep independent judgments (Optional[bool])
        judgments: List[Optional[bool]] = []
        for _ in range(self.num_runs):
            is_correct = await self._judge_answer(
                question, golden_answer, generated_answer
            )
            judgments.append(is_correct)

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
            try:
                response = await self.client.chat.completions.create(
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

"""Rejudge an existing run's answer artifacts without re-running the agent.

Use when the LLM judge phase failed (connection errors, model bug, etc.)
but the agent's predicted answers are still valid in `answer_results.json`.

Usage:
    .venv/bin/python evaluation/scripts/rejudge.py \\
        --run-dir evaluation/results/<run-name> \\
        --dataset-config evaluation/config/datasets/locomo.yaml \\
        --concurrency 4

Outputs:
    Overwrites <run-dir>/eval_results.json
    Overwrites <run-dir>/report.txt (rebuilt from rejudged numbers)

Why this exists:
    The full pipeline takes hours (LoCoMo 50Q × 10 conv + ingest); judges
    are dirt-cheap by comparison (~10 min for 150 LLM calls). When the
    judge fails late in the run, throwing away the agent answers and
    re-running is wasteful. This script reuses the saved AnswerResult
    artifacts and just reruns the judge with retry/backoff.

Reliability tweaks vs the in-pipeline LLMJudge:
    - Lower default concurrency (4 vs 10) to avoid bursts that trip
      sophnet's connection limits during tight judge loops.
    - Bounded retry on APIConnectionError / 429 / 5xx with exponential
      backoff. The default LLMJudge in the pipeline catches errors and
      defaults the judgment to False, which silently zeros the run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import List

# Allow running as a script: prepend repo root so imports resolve.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# Match cli.py: prepend src/ so common_utils is importable.
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Load .env exactly the way evaluation.cli does, so this script can be run
# directly without pre-sourcing the env in the shell.
from common_utils.load_env import setup_environment  # noqa: E402
setup_environment(load_env_file_name=".env", check_env_var="MONGODB_HOST")

import yaml  # noqa: E402

from evaluation.src.core.data_models import AnswerResult  # noqa: E402
from evaluation.src.evaluators.llm_judge import LLMJudge  # noqa: E402


def _load_answer_results(answer_results_path: Path) -> List[AnswerResult]:
    raw = json.loads(answer_results_path.read_text())
    out: List[AnswerResult] = []
    for r in raw:
        out.append(
            AnswerResult(
                question_id=r["question_id"],
                question=r["question"],
                answer=r.get("answer", ""),
                golden_answer=str(r.get("golden_answer", "")),
                category=r.get("category"),
                conversation_id=r.get("conversation_id", ""),
                formatted_context=r.get("formatted_context", ""),
                search_results=r.get("search_results", []) or [],
                metadata=r.get("metadata", {}) or {},
            )
        )
    return out


def _build_judge_config(dataset_yaml: Path, concurrency: int) -> dict:
    """Reproduce the dataset's evaluator config + .env-resolved llm vars.

    Mirrors what `evaluation/cli.py` builds for the in-pipeline judge,
    minus the parts unrelated to evaluation (search/answer/etc.).
    """
    cfg = yaml.safe_load(dataset_yaml.read_text())
    eval_cfg = cfg.get("evaluation", {})
    llm = dict(eval_cfg.get("llm", {}))

    def _resolve(val: str) -> str:
        """Expand ${VAR} or ${VAR:default} from process env."""
        if not isinstance(val, str):
            return val
        if val.startswith("${") and val.endswith("}"):
            inner = val[2:-1]
            if ":" in inner:
                name, default = inner.split(":", 1)
                return os.environ.get(name, default)
            return os.environ.get(inner, "")
        return val

    api_key = _resolve(llm.get("api_key", ""))
    base_url = _resolve(llm.get("base_url", ""))
    return {
        "llm": {
            "api_key": api_key,
            "base_url": base_url,
            "model": llm.get("model", "gpt-4.1-mini"),
        },
        "num_runs": eval_cfg.get("num_runs", 3),
        # Override semaphore via subclass below.
    }


class _ResilientLLMJudge(LLMJudge):
    """LLMJudge with lower concurrency + bounded retry."""

    def __init__(self, config: dict, concurrency: int = 4, max_retries: int = 4):
        super().__init__(config)
        self._concurrency = concurrency
        self._max_retries = max_retries

    async def _judge_answer(self, *args, **kwargs):  # type: ignore[override]
        """Wrap parent _judge_answer with retry on transient errors."""
        delay = 1.0
        last_err: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                return await super()._judge_answer(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 — swallow only transient
                msg = str(e).lower()
                transient = (
                    "connection" in msg
                    or "timeout" in msg
                    or "429" in msg
                    or "5xx" in msg
                    or "rate" in msg
                    or "temporarily" in msg
                )
                if not transient:
                    raise
                last_err = e
                if attempt == self._max_retries - 1:
                    break
                await asyncio.sleep(delay)
                delay *= 2
        # Out of retries — re-raise so caller logs and counts as False.
        assert last_err is not None
        raise last_err

    async def evaluate(self, answer_results):  # type: ignore[override]
        # Override parent's hard-coded Semaphore(10) by patching asyncio.Semaphore
        # for the duration of the call. Cleaner than copy-pasting the parent
        # method just to change one constant.
        from asyncio import Semaphore as _OrigSem

        # Patch only the attribute LLMJudge uses (asyncio.Semaphore is captured
        # in the parent's evaluate() at call time). We monkey-patch the asyncio
        # module reference to swap concurrency.
        import asyncio as _asyncio

        original = _asyncio.Semaphore
        try:
            _asyncio.Semaphore = lambda _n=self._concurrency: _OrigSem(self._concurrency)  # type: ignore[assignment]
            return await super().evaluate(answer_results)
        finally:
            _asyncio.Semaphore = original


def _format_report(eval_result, run_name: str, source_run_dir: Path) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("📊 Evaluation Report (rejudged)")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Source run: {run_name}")
    lines.append(f"Source dir: {source_run_dir}")
    lines.append("")
    lines.append(f"Total Questions: {eval_result.total_questions}")
    lines.append(f"Correct: {eval_result.correct}")
    lines.append(f"Accuracy: {eval_result.accuracy:.2%}")
    lines.append("")
    md = eval_result.metadata or {}
    if "category_breakdown" in md:
        lines.append("Category breakdown:")
        for cat, stats in (md["category_breakdown"] or {}).items():
            lines.append(f"  cat={cat}: {stats}")
    if "run_scores" in md:
        lines.append(f"Per-run scores: {md['run_scores']}")
    return "\n".join(lines) + "\n"


async def main_async() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True, type=Path,
                   help="evaluation/results/<run-name>")
    p.add_argument("--dataset-config", required=True, type=Path,
                   help="dataset yaml with evaluation.llm config")
    p.add_argument("--concurrency", type=int, default=4,
                   help="judge concurrency (default 4; in-pipeline default is 10)")
    p.add_argument("--out", type=Path, default=None,
                   help="override eval_results.json output path")
    args = p.parse_args()

    answer_results_path = args.run_dir / "answer_results.json"
    if not answer_results_path.exists():
        print(f"[rejudge] ERROR: {answer_results_path} not found", file=sys.stderr)
        return 1
    if not args.dataset_config.exists():
        print(f"[rejudge] ERROR: {args.dataset_config} not found", file=sys.stderr)
        return 1

    print(f"[rejudge] loading {answer_results_path}")
    answer_results = _load_answer_results(answer_results_path)
    print(f"[rejudge] loaded {len(answer_results)} answers")

    cfg = _build_judge_config(args.dataset_config, args.concurrency)
    if not cfg["llm"]["api_key"]:
        print("[rejudge] ERROR: judge api_key empty (LLM_API_KEY env not set?)",
              file=sys.stderr)
        return 1

    judge = _ResilientLLMJudge(cfg, concurrency=args.concurrency)
    eval_result = await judge.evaluate(answer_results)

    out_path = args.out or (args.run_dir / "eval_results.json")
    out_path.write_text(json.dumps(
        {
            "total_questions": eval_result.total_questions,
            "correct": eval_result.correct,
            "accuracy": eval_result.accuracy,
            "detailed_results": eval_result.detailed_results,
            "metadata": eval_result.metadata,
        },
        indent=2, default=str,
    ))
    print(f"[rejudge] wrote {out_path}")

    report_path = args.run_dir / "report.txt"
    report_path.write_text(_format_report(eval_result, args.run_dir.name, args.run_dir))
    print(f"[rejudge] wrote {report_path}")
    print(f"[rejudge] accuracy: {eval_result.accuracy:.2%} ({eval_result.correct}/{eval_result.total_questions})")
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()

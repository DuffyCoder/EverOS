"""Rejudge an existing run's answer artifacts without re-running the agent.

Use when the LLM judge phase failed (connection errors, model bug, etc.)
but the agent's predicted answers are still valid in `answer_results.json`.

Usage:
    .venv/bin/python evaluation/scripts/rejudge.py \\
        --run-dir evaluation/results/<run-name> \\
        --dataset-config evaluation/config/datasets/locomo.yaml \\
        --concurrency 1
        # writes <run-dir>/eval_results_rejudged.json (does NOT overwrite
        # the original eval_results.json by default)

Outputs (default):
    Writes <run-dir>/eval_results_rejudged.json
    Writes <run-dir>/report_rejudged.txt
    Pass --overwrite to replace eval_results.json + report.txt instead.

Concurrency:
    Default 1 (serial). The rate-limit storm that produces silent-zero
    accuracy comes from running 4 concurrent judge calls against a
    single-key Sophnet endpoint; serial calls reliably succeed at full
    Sophnet QPS. For runs known to be small or against a roomier
    endpoint, raise via --concurrency.

Why this exists:
    The full pipeline takes hours (LoCoMo 1540Q × 10 conv + ingest);
    judges are dirt-cheap by comparison. When the judge silently fails
    under rate limits, throwing away the agent answers and re-running is
    wasteful. This script reuses the saved AnswerResult artifacts and
    just reruns the judge with retry/backoff.

    The in-pipeline LLMJudge now returns ``None`` (judge unavailable)
    instead of a coerced False on retry-exhaust / parse failure, so
    legacy runs whose eval_results.json looks like 0% accuracy can be
    redeemed by re-running this tool — fresh judgments will replace the
    silent-False rows with proper True / False / None.
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


def _build_resilient_judge(config: dict, concurrency: int, max_retries: int = 4) -> LLMJudge:
    """Stage 2 R-S2-3: in-pipeline LLMJudge already has retry + concurrency
    cap. This wrapper just injects per-invocation overrides via the same
    config mechanism the live pipeline uses.
    """
    cfg = dict(config)
    cfg["judge_concurrency"] = concurrency
    cfg["judge_max_retries"] = max_retries
    return LLMJudge(cfg)


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
    p.add_argument("--concurrency", type=int, default=1,
                   help="judge concurrency (default 1 = serial, avoids "
                        "Sophnet rate-limit storms; raise for roomier endpoints)")
    p.add_argument("--max-retries", type=int, default=8,
                   help="per-call retry budget on transient errors (default 8)")
    p.add_argument("--overwrite", action="store_true",
                   help="overwrite eval_results.json + report.txt in place "
                        "(default writes *_rejudged.json / *_rejudged.txt "
                        "next to them, preserving the original)")
    p.add_argument("--out", type=Path, default=None,
                   help="explicit eval_results path (takes precedence over "
                        "--overwrite naming)")
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
    print(f"[rejudge] concurrency={args.concurrency}  max_retries={args.max_retries}")

    cfg = _build_judge_config(args.dataset_config, args.concurrency)
    if not cfg["llm"]["api_key"]:
        print("[rejudge] ERROR: judge api_key empty (LLM_API_KEY env not set?)",
              file=sys.stderr)
        return 1

    judge = _build_resilient_judge(
        cfg, concurrency=args.concurrency, max_retries=args.max_retries,
    )
    eval_result = await judge.evaluate(answer_results)

    if args.out:
        out_path = args.out
        report_path = args.run_dir / "report.txt"
    elif args.overwrite:
        out_path = args.run_dir / "eval_results.json"
        report_path = args.run_dir / "report.txt"
    else:
        out_path = args.run_dir / "eval_results_rejudged.json"
        report_path = args.run_dir / "report_rejudged.txt"

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

    report_path.write_text(_format_report(eval_result, args.run_dir.name, args.run_dir))
    print(f"[rejudge] wrote {report_path}")
    unavail = (eval_result.metadata or {}).get("unavailable_per_run") or []
    print(
        f"[rejudge] accuracy: {eval_result.accuracy:.2%} "
        f"({eval_result.correct}/{eval_result.total_questions})"
    )
    if any(unavail):
        print(f"[rejudge] judge unavailable per run: {unavail} (excluded from denominator)")
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()

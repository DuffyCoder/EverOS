"""Incrementally rejudge an existing run — only re-judge ``None`` verdicts.

Use when the LLM judge phase produced ``None`` (judge unavailable) for some
fraction of (question, run) pairs — typically because of a Sophnet
rate-limit / connection storm during the live evaluate stage. Only those
None slots are re-judged; True/False verdicts from the prior run are kept
intact. The aggregate accuracy is then recomputed from the patched
detailed_results.

Usage:
    .venv/bin/python evaluation/scripts/rejudge.py \\
        --run-dir evaluation/results/<run-name> \\
        --dataset-config evaluation/config/datasets/locomo.yaml \\
        --concurrency 2

Outputs (default):
    Writes <run-dir>/eval_results_rejudged.json
    Writes <run-dir>/report_rejudged.txt
    Pass --overwrite to replace eval_results.json + report.txt instead.

Why incremental:
    A full rejudge re-runs ``num_runs * len(answers)`` (typically 4620)
    judge calls. With ~8% None per run, only ~370 calls are actually
    needed; the other 92% would be re-deciding settled True/False
    verdicts and burning Sophnet RPM for no information. Incremental
    finishes 10-30× faster on the same data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

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

import numpy as np  # noqa: E402
import yaml  # noqa: E402
from tqdm import tqdm  # noqa: E402

from evaluation.src.evaluators.llm_judge import (  # noqa: E402
    LLMJudge,
    _majority_vote,
)


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


def _build_judge_config(dataset_yaml: Path, concurrency: int, max_retries: int) -> dict:
    """Reproduce the dataset's evaluator config + .env-resolved llm vars."""
    cfg = yaml.safe_load(dataset_yaml.read_text())
    eval_cfg = cfg.get("evaluation", {})
    llm = dict(eval_cfg.get("llm", {}))
    return {
        "llm": {
            "api_key": _resolve(llm.get("api_key", "")) or os.environ.get("LLM_API_KEY", ""),
            "base_url": _resolve(llm.get("base_url", "")),
            "model": llm.get("model", "gpt-4.1-mini"),
        },
        "num_runs": eval_cfg.get("num_runs", 3),
        "judge_concurrency": concurrency,
        "judge_max_retries": max_retries,
    }


def _flatten_detailed(detailed: Any) -> list[dict]:
    """``LLMJudge`` saves detailed_results grouped by conversation as a
    dict[group_key -> list[entry]]. Flatten while preserving references
    so we can patch entries in place."""
    if isinstance(detailed, list):
        return list(detailed)
    if isinstance(detailed, dict):
        flat: list[dict] = []
        for entries in detailed.values():
            if isinstance(entries, list):
                flat.extend(entries)
        return flat
    raise TypeError(f"unexpected detailed_results shape: {type(detailed).__name__}")


def _count_none(entries: list[dict], num_runs: int) -> list[int]:
    nones = [0] * num_runs
    for e in entries:
        j = e.get("llm_judgments") or {}
        for i in range(num_runs):
            if j.get(f"judgment_{i+1}") is None:
                nones[i] += 1
    return nones


def _aggregate(entries: list[dict], num_runs: int, total_questions: int) -> dict:
    """Recompute aggregate accuracy + per-category stats from patched
    entries. Mirrors ``LLMJudge.evaluate`` aggregation exactly so numbers
    are directly comparable to the original eval_results.json."""
    run_scores: list[float] = []
    category_stats: dict = defaultdict(
        lambda: {"correct": [0] * num_runs, "total": 0}
    )
    unavailable_per_run = [0] * num_runs

    for i in range(num_runs):
        key = f"judgment_{i+1}"
        correct_count = 0
        total_count = 0
        for entry in entries:
            llm_judgments = entry.get("llm_judgments") or {}
            category = entry.get("category")
            if key not in llm_judgments:
                continue
            val = llm_judgments[key]
            if val is None:
                unavailable_per_run[i] += 1
                continue
            total_count += 1
            if val:
                correct_count += 1
                if category is not None:
                    category_stats[category]["correct"][i] += 1
            if i == 0 and category is not None:
                category_stats[category]["total"] += 1
        if total_count > 0:
            run_scores.append(correct_count / total_count)

    mean_accuracy = float(np.mean(run_scores)) if run_scores else 0.0
    std_accuracy = float(np.std(run_scores)) if run_scores else 0.0

    category_accuracies = {}
    for category, stats in category_stats.items():
        per_run = []
        if stats["total"] > 0:
            for i in range(num_runs):
                per_run.append(stats["correct"][i] / stats["total"])
        if per_run:
            category_accuracies[str(category)] = {
                "mean": float(np.mean(per_run)),
                "std": float(np.std(per_run)),
                "individual_runs": [float(x) for x in per_run],
                "total": stats["total"],
            }

    return {
        "mean_accuracy": mean_accuracy,
        "std_accuracy": std_accuracy,
        "run_scores": [float(x) for x in run_scores],
        "unavailable_per_run": unavailable_per_run,
        "category_accuracies": category_accuracies,
        "correct": int(mean_accuracy * total_questions),
    }


async def _rejudge_one(
    judge: LLMJudge,
    semaphore: asyncio.Semaphore,
    entry: dict,
    run_idx: int,
    answer_by_qid: dict,
    pbar: tqdm,
) -> None:
    """Re-judge a single (entry, run_idx) None slot. Mutates entry."""
    qid = entry.get("question_id", "")
    ans = answer_by_qid.get(qid)
    if ans is None:
        pbar.update(1)
        return
    async with semaphore:
        verdict = await judge._judge_answer(
            ans["question"], ans["golden_answer"], ans["answer"]
        )
    entry["llm_judgments"][f"judgment_{run_idx+1}"] = verdict
    pbar.update(1)


def _format_report(eval_data: dict, run_name: str, source_run_dir: Path) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("📊 Evaluation Report (rejudged, incremental)")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"Source run: {run_name}")
    lines.append(f"Source dir: {source_run_dir}")
    lines.append("")
    lines.append(f"Total Questions: {eval_data.get('total_questions')}")
    lines.append(f"Correct: {eval_data.get('correct')}")
    lines.append(f"Accuracy: {eval_data.get('accuracy', 0):.2%}")
    lines.append("")
    md = eval_data.get("metadata") or {}
    cat = md.get("category_accuracies") or {}
    if cat:
        lines.append("Category breakdown:")
        for c, stats in sorted(cat.items()):
            lines.append(f"  cat={c}: mean={stats.get('mean'):.4f} n={stats.get('total')}")
    if "run_scores" in md:
        lines.append(f"Per-run scores: {md['run_scores']}")
    if "rejudge_recovered_per_run" in md:
        lines.append(f"Recovered per run: {md['rejudge_recovered_per_run']}")
    if "unavailable_per_run" in md:
        lines.append(f"Still unavailable per run: {md['unavailable_per_run']}")
    return "\n".join(lines) + "\n"


async def main_async() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True, type=Path,
                   help="evaluation/results/<run-name>")
    p.add_argument("--dataset-config", required=True, type=Path,
                   help="dataset yaml with evaluation.llm config")
    p.add_argument("--concurrency", type=int, default=2,
                   help="judge concurrency (default 2 — safe for incremental "
                        "since call volume is small; raise for roomier endpoints)")
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

    source_eval = args.run_dir / "eval_results.json"
    answer_path = args.run_dir / "answer_results.json"
    if not source_eval.exists():
        print(f"[rejudge] ERROR: {source_eval} not found", file=sys.stderr)
        return 1
    if not answer_path.exists():
        print(f"[rejudge] ERROR: {answer_path} not found", file=sys.stderr)
        return 1
    if not args.dataset_config.exists():
        print(f"[rejudge] ERROR: {args.dataset_config} not found", file=sys.stderr)
        return 1

    print(f"[rejudge] loading {source_eval}")
    eval_data = json.loads(source_eval.read_text())
    entries = _flatten_detailed(eval_data.get("detailed_results"))
    print(f"[rejudge] {len(entries)} questions in detailed_results")

    cfg = _build_judge_config(args.dataset_config, args.concurrency, args.max_retries)
    num_runs = int(cfg.get("num_runs", 3))
    before = _count_none(entries, num_runs)
    targets = sum(before)
    print(f"[rejudge] None per run BEFORE: {before}  total to rejudge: {targets}")
    print(f"[rejudge] concurrency={args.concurrency}  max_retries={args.max_retries}")

    if targets == 0:
        print("[rejudge] no None judgments — nothing to do, exiting.")
        return 0

    if not cfg["llm"]["api_key"]:
        print("[rejudge] ERROR: judge api_key empty (LLM_API_KEY env not set?)",
              file=sys.stderr)
        return 1

    judge = LLMJudge(cfg)
    answer_by_qid = {
        ar["question_id"]: ar
        for ar in json.loads(answer_path.read_text())
    }

    semaphore = asyncio.Semaphore(args.concurrency)
    pbar = tqdm(total=targets, desc="⚖️  Rejudge None", unit="qa")
    tasks = []
    for entry in entries:
        llm_judgments = entry.setdefault("llm_judgments", {})
        for run_idx in range(num_runs):
            key = f"judgment_{run_idx+1}"
            if llm_judgments.get(key) is None:
                tasks.append(_rejudge_one(
                    judge, semaphore, entry, run_idx, answer_by_qid, pbar
                ))
    await asyncio.gather(*tasks)
    pbar.close()

    # Refresh each entry's per-question verdict from the now-patched judgments.
    # _aggregate (below) recomputes the top-level accuracy, but the per-entry
    # ``is_correct`` (read by hybrid category stats and external tooling) would
    # otherwise keep its stale pre-rejudge majority and disagree with the
    # recomputed top-level number. Use the same _majority_vote as LLMJudge.
    for entry in entries:
        lj = entry.get("llm_judgments") or {}
        verdicts = [lj.get(f"judgment_{i + 1}") for i in range(num_runs)]
        entry["is_correct"] = _majority_vote(verdicts)

    after = _count_none(entries, num_runs)
    recovered = [b - a for b, a in zip(before, after)]
    print(f"[rejudge] None per run AFTER:  {after}  recovered: {recovered}")

    total_q = eval_data.get("total_questions") or len(entries)
    agg = _aggregate(entries, num_runs, total_q)

    eval_data["accuracy"] = agg["mean_accuracy"]
    eval_data["correct"] = agg["correct"]
    metadata = eval_data.get("metadata") or {}
    metadata["unavailable_per_run"] = agg["unavailable_per_run"]
    metadata["run_scores"] = agg["run_scores"]
    metadata["category_accuracies"] = agg["category_accuracies"]
    metadata["rejudge_mode"] = "incremental"
    metadata["rejudge_recovered_per_run"] = recovered
    eval_data["metadata"] = metadata

    if args.out:
        out_path = args.out
        report_path = args.run_dir / "report.txt"
    elif args.overwrite:
        out_path = source_eval
        report_path = args.run_dir / "report.txt"
    else:
        out_path = args.run_dir / "eval_results_rejudged.json"
        report_path = args.run_dir / "report_rejudged.txt"

    out_path.write_text(json.dumps(eval_data, indent=2, default=str))
    print(f"[rejudge] wrote {out_path}")
    report_path.write_text(_format_report(eval_data, args.run_dir.name, args.run_dir))
    print(f"[rejudge] wrote {report_path}")

    print(
        f"[rejudge] accuracy: {agg['mean_accuracy']:.4f} "
        f"({agg['correct']}/{total_q})"
    )
    if any(agg["unavailable_per_run"]):
        print(f"[rejudge] judge still unavailable per run: {agg['unavailable_per_run']} "
              f"(excluded from denominator)")
    return 0


def main() -> None:
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()

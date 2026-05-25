#!/usr/bin/env python3
"""Compute per-category accuracy from eval_results.json llm_judgments.

Matches the aggregation in ``evaluation.src.evaluators.llm_judge``:
each judgment run is scored independently; reported mean is the average
across runs (not majority vote per question).

Usage:
  python evaluation/scripts/stats_eval_by_category.py \\
    evaluation/results/.../eval_results.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


# LoCoMo locomo10.json category field → task type (not paper section order).
# See https://github.com/snap-research/locomo/issues/6 for ID vs paper mismatch.
LOCOMO_CATEGORY_LABELS = {
    "1": "single-hop",
    "2": "temporal",
    "3": "token-level",
    "4": "multi-hop",
    "5": "adversarial",
}
LOCOMO_CATEGORY_LABELS_ZH = {
    "1": "事实性问答",
    "2": "时间近似问答",
    "3": "短语抽取问答",
    "4": "总结性问答",
    "5": "对抗性",
}


def _flatten_detailed_results(payload: dict) -> list[dict]:
    detailed = payload.get("detailed_results") or {}
    rows: list[dict] = []
    if isinstance(detailed, dict):
        for group in detailed.values():
            if isinstance(group, list):
                rows.extend(group)
    elif isinstance(detailed, list):
        rows.extend(detailed)
    return rows


def _judgment_keys(row: dict) -> list[str]:
    judgments = row.get("llm_judgments") or {}
    return sorted(k for k in judgments if k.startswith("judgment_"))


def compute_category_stats(rows: list[dict]) -> dict[str, Any]:
    """Return per-category stats aligned with LLMJudgeEvaluator."""
    all_keys: set[str] = set()
    for row in rows:
        all_keys.update(_judgment_keys(row))

    run_keys = sorted(all_keys, key=lambda k: int(k.split("_", 1)[1]))
    num_runs = len(run_keys)

    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        cat = row.get("category")
        if cat is None:
            cat = "unknown"
        by_category[str(cat)].append(row)

    categories_out: dict[str, Any] = {}
    for cat in sorted(by_category, key=lambda c: (c == "unknown", c)):
        items = by_category[cat]
        per_run_correct = [0] * num_runs
        per_run_total = [0] * num_runs
        majority_correct = 0
        unanimous_correct = 0
        any_run_correct = 0

        for row in items:
            judgments = row.get("llm_judgments") or {}
            vals: list[bool] = []
            for i, key in enumerate(run_keys):
                if key not in judgments:
                    continue
                val = judgments[key]
                if val is None:
                    continue
                per_run_total[i] += 1
                if val:
                    per_run_correct[i] += 1
                    vals.append(True)
                else:
                    vals.append(False)

            if not vals:
                continue
            if sum(vals) >= 2:
                majority_correct += 1
            if all(vals):
                unanimous_correct += 1
            if any(vals):
                any_run_correct += 1

        n = len(items)
        run_accuracies: list[float] = []
        per_run_detail: list[dict] = []
        for i, key in enumerate(run_keys):
            if per_run_total[i] > 0:
                acc = per_run_correct[i] / per_run_total[i]
                run_accuracies.append(acc)
                per_run_detail.append(
                    {
                        "judgment": key,
                        "correct": per_run_correct[i],
                        "total": per_run_total[i],
                        "accuracy": acc,
                    }
                )

        cat_mean = mean(run_accuracies) if run_accuracies else None
        cat_std = pstdev(run_accuracies) if len(run_accuracies) > 1 else 0.0

        categories_out[cat] = {
            "label": LOCOMO_CATEGORY_LABELS.get(cat, ""),
            "label_zh": LOCOMO_CATEGORY_LABELS_ZH.get(cat, ""),
            "n_questions": n,
            "per_run": per_run_detail,
            "mean_accuracy": cat_mean,
            "std_accuracy": cat_std,
            "majority_vote_accuracy": majority_correct / n if n else None,
            "unanimous_accuracy": unanimous_correct / n if n else None,
            "any_run_accuracy": any_run_correct / n if n else None,
        }

    return {
        "num_runs": num_runs,
        "run_keys": run_keys,
        "total_questions": len(rows),
        "categories": categories_out,
    }


def _majority_correct(judgments: dict, run_keys: list[str]) -> bool | None:
    vals = [judgments.get(k) for k in run_keys if judgments.get(k) is not None]
    if not vals:
        return None
    if len(vals) >= 3:
        return sum(vals) >= 2
    return sum(vals) >= len(vals) / 2


def write_category_report(
    path: Path,
    rows: list[dict],
    stats: dict[str, Any],
    metadata: dict | None,
) -> tuple[Path, Path]:
    """Write per-category markdown + JSON with every question from eval_results."""
    run_keys = stats["run_keys"]
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_cat[str(row.get("category", "unknown"))].append(row)

    report: dict[str, Any] = {
        "source": str(path),
        "total_questions": len(rows),
        "overall_accuracy": metadata.get("mean_accuracy") if metadata else None,
        "categories": {},
    }
    md_lines = [
        "# LoCoMo 分类型正确率（eval_results.json 真实 category）\n",
        f"- 数据源: `{path}`\n",
        f"- 总题数: {len(rows)}\n",
        f"- Judge 次数: {stats['num_runs']}\n",
    ]
    if metadata and metadata.get("mean_accuracy") is not None:
        md_lines.append(
            f"- Overall Mean accuracy: {metadata['mean_accuracy'] * 100:.2f}%\n"
        )
    md_lines.append("\n## 汇总\n\n")
    md_lines.append(
        "| Cat | 英文 | 中文 | N | J1 | J2 | J3 | Mean | Majority (≥2/3) |\n"
    )
    md_lines.append(
        "|-----|------|------|---|-----|-----|-----|------|------------------|\n"
    )

    for cat in sorted(by_cat, key=lambda c: (c == "unknown", int(c) if c.isdigit() else 99)):
        items = by_cat[cat]
        en = LOCOMO_CATEGORY_LABELS.get(cat, "?")
        zh = LOCOMO_CATEGORY_LABELS_ZH.get(cat, "?")
        cat_stats = stats["categories"].get(cat, {})
        per_run = {d["judgment"]: d["accuracy"] for d in cat_stats.get("per_run", [])}
        j_cells = [
            f"{per_run[k] * 100:.1f}%" if k in per_run else "n/a" for k in run_keys
        ]
        mean_acc = cat_stats.get("mean_accuracy") or 0.0
        maj_acc = cat_stats.get("majority_vote_accuracy") or 0.0
        maj_n = int(round(maj_acc * len(items))) if items else 0
        md_lines.append(
            f"| {cat} | {en} | {zh} | {len(items)} | "
            f"{' | '.join(j_cells)} | {mean_acc * 100:.2f}% | {maj_n}/{len(items)} ({maj_acc * 100:.1f}%) |\n"
        )

        when_cnt = sum(
            1 for r in items if (r.get("question") or "").lower().startswith("when")
        )
        what_cnt = sum(
            1 for r in items if (r.get("question") or "").lower().startswith("what")
        )
        would_cnt = sum(
            1 for r in items if (r.get("question") or "").lower().startswith("would")
        )
        how_cnt = sum(
            1 for r in items if (r.get("question") or "").lower().startswith("how")
        )

        questions_detail = []
        for r in items:
            j = r.get("llm_judgments") or {}
            questions_detail.append(
                {
                    "question_id": r.get("question_id"),
                    "question": r.get("question"),
                    "golden_answer": r.get("golden_answer"),
                    "generated_answer": r.get("generated_answer"),
                    "llm_judgments": j,
                    "majority_correct": _majority_correct(j, run_keys),
                }
            )

        report["categories"][cat] = {
            "label_en": en,
            "label_zh": zh,
            "n": len(items),
            "question_prefix_counts": {
                "when": when_cnt,
                "what": what_cnt,
                "would": would_cnt,
                "how": how_cnt,
            },
            "mean_accuracy": mean_acc,
            "majority_vote_accuracy": maj_acc,
            "per_run_accuracy": per_run,
            "questions": questions_detail,
        }

        md_lines.append(
            f"\n## Category {cat}: {en}（{zh}）— {len(items)} 题，Mean {mean_acc * 100:.2f}%\n\n"
        )
        md_lines.append(
            f"真实问题分布: When={when_cnt}, What={what_cnt}, Would={would_cnt}, How={how_cnt}\n\n"
        )
        md_lines.append("| ✓/✗ | question_id | question |\n|-----|-------------|----------|\n")
        for d in sorted(
            questions_detail,
            key=lambda x: (not x["majority_correct"], x.get("question_id") or ""),
        ):
            mark = "✓" if d["majority_correct"] else "✗"
            q = (d.get("question") or "").replace("|", "\\|")
            md_lines.append(f"| {mark} | {d.get('question_id', '')} | {q} |\n")

    out_dir = path.parent
    md_path = out_dir / "category_accuracy_report.md"
    json_path = out_dir / "category_accuracy_report.json"
    md_path.write_text("".join(md_lines), encoding="utf-8")
    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return md_path, json_path


def _print_report(stats: dict[str, Any], metadata: dict | None) -> None:
    print("=" * 72)
    print("Per-category accuracy (from llm_judgments)")
    print("=" * 72)
    print(f"Total questions: {stats['total_questions']}")
    print(f"Judge runs: {stats['num_runs']} ({', '.join(stats['run_keys'])})")
    print()
    print(
        "Primary metric (matches llm_judge.py): mean_accuracy = average of "
        "per-run accuracies within each category."
    )
    print()

    header = (
        f"{'Cat':>4}  {'Type':<12}  {'中文':<10}  {'N':>4}  "
        f"{'J1':>7}  {'J2':>7}  {'J3':>7}  "
        f"{'Mean':>7}  {'±Std':>6}  "
        f"{'Major':>7}  {'Unan':>7}"
    )
    print(header)
    print("-" * len(header))

    for cat, row in stats["categories"].items():
        label = (row.get("label") or "")[:12]
        label_zh = (row.get("label_zh") or "")[:10]
        n = row["n_questions"]
        per_run = {d["judgment"]: d["accuracy"] for d in row["per_run"]}
        j_parts = []
        for key in stats["run_keys"]:
            acc = per_run.get(key)
            j_parts.append(f"{acc * 100:6.2f}%" if acc is not None else "    n/a")
        mean_acc = row["mean_accuracy"]
        std_acc = row["std_accuracy"]
        maj = row["majority_vote_accuracy"]
        unan = row["unanimous_accuracy"]
        print(
            f"{cat:>4}  {label:<12}  {label_zh:<10}  {n:>4}  "
            f"{j_parts[0]}  {j_parts[1]}  {j_parts[2]}  "
            f"{mean_acc * 100:6.2f}%  {std_acc * 100:5.2f}%  "
            f"{maj * 100:6.2f}%  {unan * 100:6.2f}%"
        )

    print()
    print("Legend:")
    print("  J1/J2/J3     = accuracy using only that judgment_* run")
    print("  Mean ± Std   = mean/std of J1,J2,J3 accuracies (evaluator headline)")
    print("  Major        = share of questions with ≥2/3 judgments True")
    print("  Unan         = share with all 3 judgments True")

    if metadata and metadata.get("category_accuracies"):
        print()
        print("Cross-check vs eval_results.json metadata.category_accuracies:")
        for cat, stored in sorted(metadata["category_accuracies"].items()):
            recomputed = stats["categories"].get(cat, {}).get("mean_accuracy")
            if recomputed is None:
                continue
            delta = abs(stored["mean"] - recomputed)
            ok = "OK" if delta < 1e-9 else f"DIFF {delta:.6f}"
            print(
                f"  category {cat}: stored mean={stored['mean']:.6f}, "
                f"recomputed={recomputed:.6f} [{ok}]"
            )

    if metadata:
        print()
        print(
            f"Overall metadata: mean_accuracy={metadata.get('mean_accuracy', 'n/a')}, "
            f"run_scores={[round(s, 4) for s in (metadata.get('run_scores') or [])]}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "eval_results",
        type=Path,
        help="Path to eval_results.json",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Optional path to write machine-readable summary JSON",
    )
    parser.add_argument(
        "--write-report",
        action="store_true",
        help="Write category_accuracy_report.md/json next to eval_results.json",
    )
    args = parser.parse_args()

    path = args.eval_results.expanduser().resolve()
    if not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = _flatten_detailed_results(payload)
    if not rows:
        print("No detailed_results found.", file=sys.stderr)
        return 1

    stats = compute_category_stats(rows)
    metadata = payload.get("metadata") or {}
    _print_report(stats, metadata)

    if args.json_out:
        out = {
            "source": str(path),
            **stats,
        }
        args.json_out.write_text(
            json.dumps(out, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote JSON summary to {args.json_out}")

    if args.write_report:
        md_path, json_path = write_category_report(path, rows, stats, metadata)
        print(f"\nWrote per-question report:")
        print(f"  {md_path}")
        print(f"  {json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

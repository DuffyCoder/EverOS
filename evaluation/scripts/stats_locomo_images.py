#!/usr/bin/env python3
"""LoCoMo image statistics: img_url counts, image-evidence QA, eval accuracy.

Usage:
  python evaluation/scripts/stats_locomo_images.py \\
    --locomo evaluation/data/locomo/locomo10.json \\
    --eval-results evaluation/results/.../eval_results.json \\
    --conversation locomo_0
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

LOCOMO_CATEGORY_LABELS = {
    "1": ("single-hop", "事实性问答"),
    "2": ("temporal", "时间近似问答"),
    "3": ("token-level", "短语抽取问答"),
    "4": ("multi-hop", "总结性问答"),
    "5": ("adversarial", "对抗性"),
}

SESSION_KEY_RE = re.compile(r"^session_(\d+)$")


def _session_sort_key(key: str) -> int:
    m = SESSION_KEY_RE.match(key)
    return int(m.group(1)) if m else 9999


def analyze_conversations(raw_data: list[dict]) -> dict[str, Any]:
    """Part 1 & 2: per-conversation image counts and image-evidence QA."""
    conv_reports: list[dict[str, Any]] = []

    for conv_idx, item in enumerate(raw_data):
        conv_id = f"locomo_{conv_idx}"
        conv = item.get("conversation") or {}
        qa_list = item.get("qa") or []

        session_keys = sorted(
            [k for k in conv if SESSION_KEY_RE.match(k)],
            key=_session_sort_key,
        )

        # --- Part 1: img_url per session ---
        sessions_img: dict[str, dict[str, Any]] = {}
        all_image_dia_ids: set[str] = set()
        total_img_urls = 0
        total_image_messages = 0

        for sk in session_keys:
            messages = conv.get(sk) or []
            session_img_urls = 0
            session_image_messages = 0
            session_dia_ids: list[str] = []

            for msg in messages:
                urls = msg.get("img_url") or []
                if not urls:
                    continue
                session_img_urls += len(urls)
                session_image_messages += 1
                dia_id = msg.get("dia_id")
                if dia_id:
                    session_dia_ids.append(dia_id)
                    all_image_dia_ids.add(dia_id)

            total_img_urls += session_img_urls
            total_image_messages += session_image_messages
            sessions_img[sk] = {
                "img_url_count": session_img_urls,
                "image_message_count": session_image_messages,
                "image_dia_ids": session_dia_ids,
            }

        # --- Part 2: QA whose evidence hits image dia_ids ---
        qa_image_related: list[dict[str, Any]] = []
        qa_not_image: list[dict[str, Any]] = []

        for qa_idx, qa in enumerate(qa_list):
            evidence = qa.get("evidence") or []
            image_evidence = [e for e in evidence if e in all_image_dia_ids]
            is_image_related = bool(image_evidence)
            row = {
                "question_id": f"{conv_id}_qa{qa_idx}",
                "qa_index": qa_idx,
                "question": qa.get("question", ""),
                "category": str(qa.get("category")) if qa.get("category") is not None else None,
                "evidence": evidence,
                "image_evidence": image_evidence,
                "is_image_related": is_image_related,
            }
            if is_image_related:
                qa_image_related.append(row)
            else:
                qa_not_image.append(row)

        conv_reports.append(
            {
                "conversation_id": conv_id,
                "conv_index": conv_idx,
                "speaker_a": conv.get("speaker_a"),
                "speaker_b": conv.get("speaker_b"),
                "session_count": len(session_keys),
                "total_img_url_count": total_img_urls,
                "total_image_message_count": total_image_messages,
                "all_image_dia_ids": sorted(all_image_dia_ids),
                "sessions": sessions_img,
                "qa_total": len(qa_list),
                "qa_image_related_count": len(qa_image_related),
                "qa_image_related": qa_image_related,
                "qa_not_image_count": len(qa_not_image),
            }
        )

    return {
        "conversation_count": len(conv_reports),
        "conversations": conv_reports,
        "grand_total_img_urls": sum(c["total_img_url_count"] for c in conv_reports),
        "grand_total_image_messages": sum(
            c["total_image_message_count"] for c in conv_reports
        ),
        "grand_total_image_related_qa": sum(
            c["qa_image_related_count"] for c in conv_reports
        ),
    }


def _judgment_keys(row: dict) -> list[str]:
    judgments = row.get("llm_judgments") or {}
    return sorted(k for k in judgments if k.startswith("judgment_"))


def _mean_judgment_accuracy(judgments: dict, run_keys: list[str]) -> float | None:
    vals = [judgments.get(k) for k in run_keys if judgments.get(k) is not None]
    if not vals:
        return None
    return sum(1 for v in vals if v) / len(vals)


def _majority_correct(judgments: dict, run_keys: list[str]) -> bool | None:
    vals = [judgments.get(k) for k in run_keys if judgments.get(k) is not None]
    if not vals:
        return None
    if len(vals) >= 3:
        return sum(vals) >= 2
    return sum(vals) >= len(vals) / 2


def analyze_eval_for_conversation(
    eval_path: Path,
    conv_id: str,
    image_qa_by_id: dict[str, dict],
    *,
    exclude_categories: set[str] | None = None,
) -> dict[str, Any]:
    """Part 3: eval accuracy for image-related QA in one conversation."""
    payload = json.loads(eval_path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    prefix = f"{conv_id}_qa"
    for group in (payload.get("detailed_results") or {}).values():
        for row in group:
            qid = row.get("question_id", "")
            if not qid.startswith(prefix):
                continue
            cat = str(row.get("category")) if row.get("category") is not None else None
            if exclude_categories and cat in exclude_categories:
                continue
            rows.append(row)

    if not rows:
        return {"error": f"No eval rows for {conv_id}", "eval_path": str(eval_path)}

    run_keys = sorted(
        {k for r in rows for k in _judgment_keys(r)},
        key=lambda k: int(k.split("_", 1)[1]),
    )

    def bucket(rows_subset: list[dict]) -> dict[str, Any]:
        if not rows_subset:
            return {"n": 0}
        per_run_correct = [0] * len(run_keys)
        per_run_total = [0] * len(run_keys)
        by_category: dict[str, list[dict]] = defaultdict(list)
        majority_ok = 0

        for r in rows_subset:
            j = r.get("llm_judgments") or {}
            for i, k in enumerate(run_keys):
                if k not in j or j[k] is None:
                    continue
                per_run_total[i] += 1
                if j[k]:
                    per_run_correct[i] += 1
            if _majority_correct(j, run_keys):
                majority_ok += 1
            cat = str(r.get("category", "unknown"))
            by_category[cat].append(r)

        run_accs = [
            per_run_correct[i] / per_run_total[i] if per_run_total[i] else 0.0
            for i in range(len(run_keys))
        ]
        cat_stats = {}
        for cat, items in sorted(by_category.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 99):
            en, zh = LOCOMO_CATEGORY_LABELS.get(cat, ("?", "?"))
            cat_run_accs = []
            for i, k in enumerate(run_keys):
                c_ok = sum(1 for r in items if (r.get("llm_judgments") or {}).get(k))
                cat_run_accs.append(c_ok / len(items))
            cat_stats[cat] = {
                "label_en": en,
                "label_zh": zh,
                "n": len(items),
                "mean_accuracy": mean(cat_run_accs) if cat_run_accs else None,
                "per_run_accuracy": {
                    run_keys[i]: cat_run_accs[i] for i in range(len(run_keys))
                },
            }

        return {
            "n": len(rows_subset),
            "per_run_accuracy": {run_keys[i]: run_accs[i] for i in range(len(run_keys))},
            "mean_accuracy": mean(run_accs) if run_accs else None,
            "std_accuracy": pstdev(run_accs) if len(run_accs) > 1 else 0.0,
            "majority_correct": majority_ok,
            "majority_accuracy": majority_ok / len(rows_subset),
            "by_category": cat_stats,
            "questions": [
                {
                    "question_id": r.get("question_id"),
                    "question": r.get("question"),
                    "category": r.get("category"),
                    "majority_correct": _majority_correct(r.get("llm_judgments") or {}, run_keys),
                    "llm_judgments": r.get("llm_judgments"),
                }
                for r in rows_subset
            ],
        }

    image_rows = []
    non_image_rows = []
    unknown_rows = []
    for r in rows:
        qid = r.get("question_id", "")
        info = image_qa_by_id.get(qid)
        if info is None:
            unknown_rows.append(r)
            continue
        if info.get("is_image_related"):
            image_rows.append(r)
        else:
            non_image_rows.append(r)

    return {
        "conversation_id": conv_id,
        "eval_total_in_results": len(rows),
        "eval_unknown_qid": len(unknown_rows),
        "image_related": bucket(image_rows),
        "non_image_related": bucket(non_image_rows),
        "all_evaluated": bucket(rows),
    }


def _print_part1_2(report: dict[str, Any]) -> None:
    print("=" * 80)
    print("Part 1: img_url 统计（各 conversation 按 session）")
    print("=" * 80)
    print(
        f"全数据集: {report['conversation_count']} conversations, "
        f"img_url 总数={report['grand_total_img_urls']}, "
        f"含图 message 数={report['grand_total_image_messages']}, "
        f"evidence 命中图片 dia_id 的 QA 数={report['grand_total_image_related_qa']}"
    )
    print()
    header = f"{'Conv':<10} {'Sessions':>8} {'img_urls':>10} {'img_msgs':>10} {'img_QA':>8}"
    print(header)
    print("-" * len(header))
    for c in report["conversations"]:
        print(
            f"{c['conversation_id']:<10} {c['session_count']:>8} "
            f"{c['total_img_url_count']:>10} {c['total_image_message_count']:>10} "
            f"{c['qa_image_related_count']:>8}"
        )
    print()
    print("各 conversation session 明细（仅列出有图片的 session）:")
    for c in report["conversations"]:
        has_any = c["total_img_url_count"] > 0
        if not has_any:
            continue
        print(f"\n  {c['conversation_id']} ({c['speaker_a']} & {c['speaker_b']})")
        for sk, s in sorted(c["sessions"].items(), key=lambda x: _session_sort_key(x[0])):
            if s["img_url_count"] == 0:
                continue
            print(
                f"    {sk}: img_url={s['img_url_count']}, "
                f"messages={s['image_message_count']}, dia_ids={s['image_dia_ids']}"
            )


def _print_part2_conv(c: dict[str, Any]) -> None:
    print()
    print("=" * 80)
    print(f"Part 2: {c['conversation_id']} — evidence 与含图 dia_id 交集的 QA")
    print("=" * 80)
    print(f"含图 dia_id ({len(c['all_image_dia_ids'])}): {', '.join(c['all_image_dia_ids'])}")
    print(f"QA 总数={c['qa_total']}, 可能与图片相关={c['qa_image_related_count']}")
    print()
    if not c["qa_image_related"]:
        print("  (无)")
        return
    print(f"{'qa_id':<16} {'cat':>4}  {'image_evidence':<20}  question")
    print("-" * 100)
    for q in c["qa_image_related"]:
        ev = ",".join(q["image_evidence"])
        question = (q["question"] or "")[:60]
        print(f"{q['question_id']:<16} {q['category']:>4}  {ev:<20}  {question}")


def _print_part3(eval_report: dict[str, Any], *, filter_note: str) -> None:
    print()
    print("=" * 80)
    print(f"Part 3: {eval_report['conversation_id']} eval 正确率（图片相关 QA）")
    print("=" * 80)
    print(filter_note)
    print(f"eval_results 中该 conversation 题数: {eval_report['eval_total_in_results']}")
    if eval_report.get("eval_unknown_qid"):
        print(f"  警告: {eval_report['eval_unknown_qid']} 题无法在 locomo 数据集中匹配 qid")

    for label, key in [
        ("可能与图片相关 (evidence ∩ image dia_id)", "image_related"),
        ("非图片相关", "non_image_related"),
        ("全部已评测", "all_evaluated"),
    ]:
        b = eval_report[key]
        print(f"\n--- {label} ---")
        if b.get("n", 0) == 0:
            print("  n=0")
            continue
        pr = b.get("per_run_accuracy") or {}
        runs = "  ".join(f"{k}={v*100:.2f}%" for k, v in sorted(pr.items()))
        print(
            f"  n={b['n']}  {runs}  "
            f"Mean={b['mean_accuracy']*100:.2f}%  "
            f"Majority={b['majority_correct']}/{b['n']} ({b['majority_accuracy']*100:.1f}%)"
        )
        if b.get("by_category"):
            print("  按 category:")
            for cat, cs in sorted(b["by_category"].items(), key=lambda x: int(x[0])):
                print(
                    f"    cat {cat} {cs['label_zh']} ({cs['label_en']}): "
                    f"n={cs['n']} mean={cs['mean_accuracy']*100:.2f}%"
                )

    img = eval_report["image_related"]
    if img.get("n", 0) > 0:
        print("\n  图片相关 QA 逐题 (majority):")
        for q in sorted(img.get("questions") or [], key=lambda x: x.get("question_id", "")):
            mark = "✓" if q.get("majority_correct") else "✗"
            print(
                f"    {mark} [{q.get('category')}] {q.get('question_id')}: "
                f"{(q.get('question') or '')[:70]}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--locomo",
        type=Path,
        default=Path("evaluation/data/locomo/locomo10.json"),
        help="Path to locomo10.json",
    )
    parser.add_argument(
        "--eval-results",
        type=Path,
        default=None,
        help="eval_results.json for Part 3",
    )
    parser.add_argument(
        "--conversation",
        type=str,
        default="locomo_0",
        help="Conversation id for Part 3 (e.g. locomo_0)",
    )
    parser.add_argument(
        "--exclude-category",
        action="append",
        default=["5"],
        help="Categories excluded from eval (default: 5 adversarial)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Write full JSON report",
    )
    args = parser.parse_args()

    locomo_path = args.locomo.expanduser().resolve()
    if not locomo_path.is_file():
        print(f"Not found: {locomo_path}", file=sys.stderr)
        return 1

    raw_data = json.loads(locomo_path.read_text(encoding="utf-8"))
    report = analyze_conversations(raw_data)

    _print_part1_2(report)

    conv_idx = int(args.conversation.split("_")[-1])
    if conv_idx < 0 or conv_idx >= len(report["conversations"]):
        print(f"Invalid conversation: {args.conversation}", file=sys.stderr)
        return 1
    conv_report = report["conversations"][conv_idx]
    _print_part2_conv(conv_report)

    exclude = set(args.exclude_category or [])
    image_qa_by_id = {
        q["question_id"]: q for q in conv_report["qa_image_related"]
    }
    # Also index all QA for non-image lookup
    raw_item = raw_data[conv_idx]
    for qa_idx, qa in enumerate(raw_item.get("qa") or []):
        qid = f"{args.conversation}_qa{qa_idx}"
        if qid not in image_qa_by_id:
            evidence = qa.get("evidence") or []
            image_qa_by_id[qid] = {
                "question_id": qid,
                "is_image_related": False,
                "category": str(qa.get("category")) if qa.get("category") is not None else None,
            }

    eval_report = None
    if args.eval_results:
        eval_path = args.eval_results.expanduser().resolve()
        if not eval_path.is_file():
            print(f"Not found: {eval_path}", file=sys.stderr)
            return 1
        # Mark image-related for all qids
        all_qa_meta: dict[str, dict] = {}
        for qa_idx, qa in enumerate(raw_item.get("qa") or []):
            qid = f"{args.conversation}_qa{qa_idx}"
            ev = qa.get("evidence") or []
            img_ev = [e for e in ev if e in set(conv_report["all_image_dia_ids"])]
            all_qa_meta[qid] = {
                "question_id": qid,
                "is_image_related": bool(img_ev),
                "category": str(qa.get("category")) if qa.get("category") is not None else None,
            }
        eval_report = analyze_eval_for_conversation(
            eval_path,
            args.conversation,
            all_qa_meta,
            exclude_categories=exclude,
        )
        filter_note = (
            f"评测子集: 排除 category {sorted(exclude)} "
            f"(与 locomo.yaml filter_category 对齐)"
        )
        _print_part3(eval_report, filter_note=filter_note)

    if args.json_out:
        out = {
            "locomo_path": str(locomo_path),
            "dataset_report": report,
            "conversation_detail": conv_report,
            "eval_report": eval_report,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(out, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote JSON: {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

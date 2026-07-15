"""Compare OpenViking AB eval runs across accuracy, tokens, latency, and tools.

Usage:
    python evaluation/scripts/openviking_ab_compare.py \
        --baseline /path/to/baseline-locomo4 /path/to/baseline-locomo3 \
        --candidate /path/to/candidate-locomo4 /path/to/candidate-locomo3 \
        --convs locomo_4 locomo_3 locomo_8 \
        --out /tmp/openviking-ab.md \
        --json-out /tmp/openviking-ab.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CONVS = ("locomo_4", "locomo_3", "locomo_8")
_QID_CONV_RE = re.compile(r"^(locomo_\d+)_qa\d+")
_OLD_MATCH_BLOCK_RE = re.compile(
    r"## Match \d+: .*?\n(.*?)(?=\n\n## Match \d+:|\Z)",
    re.S,
)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _pp(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.1f}pp"


def _num(value: float | None) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 100:
        return f"{value:,.0f}"
    return f"{value:.3f}"


def _question_conv(row: dict[str, Any]) -> str | None:
    conv = row.get("conversation_id")
    if isinstance(conv, str) and conv:
        return conv
    qid = row.get("question_id")
    if not isinstance(qid, str):
        return None
    match = _QID_CONV_RE.match(qid)
    return match.group(1) if match else None


def _question_id(row: dict[str, Any]) -> str | None:
    qid = row.get("question_id")
    return qid if isinstance(qid, str) and qid else None


def _is_correct(row: dict[str, Any]) -> bool:
    if "is_correct" in row:
        return bool(row["is_correct"])
    if "correct" in row:
        return bool(row["correct"])
    return False


def _iter_eval_rows(eval_results: dict[str, Any]) -> Iterable[dict[str, Any]]:
    detailed = eval_results.get("detailed_results")
    if isinstance(detailed, dict):
        for rows in detailed.values():
            if isinstance(rows, list):
                yield from (row for row in rows if isinstance(row, dict))
    elif isinstance(detailed, list):
        yield from (row for row in detailed if isinstance(row, dict))


def _accuracy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    correct = sum(1 for row in rows if _is_correct(row))
    return {
        "questions": total,
        "correct": correct,
        "accuracy": correct / total if total else None,
    }


def _coerce_run_dirs(run_dirs: Path | Iterable[Path]) -> list[Path]:
    if isinstance(run_dirs, Path):
        return [run_dirs]
    return [Path(path) for path in run_dirs]


def _eval_rows_for(
    run_dirs: Path | Iterable[Path],
    convs: set[str],
    qids: set[str] | None = None,
) -> list[dict[str, Any]]:
    return [
        row
        for run_dir in _coerce_run_dirs(run_dirs)
        for row in _iter_eval_rows(_load_json(run_dir / "eval_results.json"))
        if _question_conv(row) in convs
        and (qids is None or _question_id(row) in qids)
    ]


def _selected_qids(run_dirs: Path | Iterable[Path], convs: set[str]) -> set[str]:
    return {
        qid
        for row in _eval_rows_for(run_dirs, convs)
        for qid in [_question_id(row)]
        if qid
    }


def _accuracy_summary(
    run_dirs: Path | Iterable[Path],
    convs: set[str],
    qids: set[str] | None = None,
) -> dict[str, Any]:
    rows = [
        row for row in _eval_rows_for(run_dirs, convs, qids=qids)
    ]
    per_conv = {
        conv: _accuracy([row for row in rows if _question_conv(row) == conv])
        for conv in sorted(convs, key=_conv_sort_key)
    }
    per_category: dict[str, dict[str, Any]] = {}
    categories = sorted({str(row.get("category", "unknown")) for row in rows})
    for category in categories:
        per_category[category] = _accuracy(
            [row for row in rows if str(row.get("category", "unknown")) == category]
        )
    return {
        "overall": _accuracy(rows),
        "per_conv": per_conv,
        "per_category": per_category,
    }


def _conv_sort_key(conv: str) -> tuple[str, int]:
    prefix, _, suffix = conv.rpartition("_")
    return (prefix, int(suffix) if suffix.isdigit() else -1)


def _pick(summary: dict[str, Any], path: list[str]) -> float | None:
    value: Any = summary
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return _as_float(value)


def _summary_metric(
    summaries: list[dict[str, Any]],
    path: list[str],
) -> dict[str, float] | None:
    return _stats(_pick(summary, path) for summary in summaries)


def _set_summary_stats(target: dict[str, Any], key: str, stats: dict[str, float] | None) -> None:
    if not stats:
        target[key] = None
        return
    stat_name = _stat_name_from_metric_key(key)
    target[key] = stats[stat_name]


def _stat_name_from_metric_key(key: str) -> str:
    for suffix, stat_name in (
        ("_mean_ms", "mean"),
        ("_p50_ms", "p50"),
        ("_p95_ms", "p95"),
        ("_max_ms", "max"),
        ("_mean", "mean"),
        ("_p50", "p50"),
        ("_p95", "p95"),
        ("_max", "max"),
    ):
        if key.endswith(suffix):
            return stat_name
    raise KeyError(key)


def _benchmark_summary(run_dirs: Path | Iterable[Path]) -> tuple[dict[str, Any], dict[str, Any]]:
    summaries = [
        _load_json(run_dir / "benchmark_summary.json")
        for run_dir in _coerce_run_dirs(run_dirs)
    ]
    token_paths = {
        "final_context_tokens_mean": ["diagnostics", "final_context_tokens_mean"],
        "final_context_tokens_p50": ["diagnostics", "final_context_tokens_stats", "p50"],
        "final_context_tokens_p95": ["diagnostics", "final_context_tokens_stats", "p95"],
        "final_context_tokens_max": ["diagnostics", "final_context_tokens_stats", "max"],
        "agent_run_total_input_tokens_mean": ["diagnostics", "agent_run_total_input_tokens_mean"],
        "agent_run_total_input_tokens_p50": ["diagnostics", "agent_run_total_input_tokens_stats", "p50"],
        "agent_run_total_input_tokens_p95": ["diagnostics", "agent_run_total_input_tokens_stats", "p95"],
        "agent_run_total_input_tokens_max": ["diagnostics", "agent_run_total_input_tokens_stats", "max"],
    }
    latency_paths = {
        "add_mean_ms": ["latency", "add", "wall_ms", "realistic", "mean"],
        "add_p50_ms": ["latency", "add", "wall_ms", "realistic", "p50"],
        "add_p95_ms": ["latency", "add", "wall_ms", "realistic", "p95"],
        "add_max_ms": ["latency", "add", "wall_ms", "realistic", "max"],
        "search_mean_ms": ["latency", "search", "wall_ms", "realistic", "mean"],
        "search_p50_ms": ["latency", "search", "wall_ms", "realistic", "p50"],
        "search_p95_ms": ["latency", "search", "wall_ms", "realistic", "p95"],
        "search_max_ms": ["latency", "search", "wall_ms", "realistic", "max"],
        "answer_mean_ms": ["latency", "answer", "wall_ms", "realistic", "mean"],
        "answer_p50_ms": ["latency", "answer", "wall_ms", "realistic", "p50"],
        "answer_p95_ms": ["latency", "answer", "wall_ms", "realistic", "p95"],
        "answer_max_ms": ["latency", "answer", "wall_ms", "realistic", "max"],
        "e2e_mean_ms": ["latency", "e2e_query_ms", "wall_ms", "realistic", "mean"],
        "e2e_p50_ms": ["latency", "e2e_query_ms", "wall_ms", "realistic", "p50"],
        "e2e_p95_ms": ["latency", "e2e_query_ms", "wall_ms", "realistic", "p95"],
        "e2e_max_ms": ["latency", "e2e_query_ms", "wall_ms", "realistic", "max"],
    }
    tokens: dict[str, Any] = {}
    latency: dict[str, Any] = {}
    for key, path in token_paths.items():
        _set_summary_stats(tokens, key, _summary_metric(summaries, path))
    for key, path in latency_paths.items():
        _set_summary_stats(latency, key, _summary_metric(summaries, path))
    return tokens, latency


def _stats(values: Iterable[Any]) -> dict[str, float] | None:
    samples = sorted(v for v in (_as_float(value) for value in values) if v is not None)
    if not samples:
        return None
    return {
        "mean": sum(samples) / len(samples),
        "p50": _percentile(samples, 50),
        "p95": _percentile(samples, 95),
        "max": samples[-1],
    }


def _percentile(sorted_samples: list[float], percentile: float) -> float:
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    rank = (len(sorted_samples) - 1) * (percentile / 100)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_samples) - 1)
    fraction = rank - lower
    return sorted_samples[lower] * (1 - fraction) + sorted_samples[upper] * fraction


def _answer_rows(
    run_dirs: Path | Iterable[Path],
    convs: set[str],
    qids: set[str] | None = None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for run_dir in _coerce_run_dirs(run_dirs):
        data = _load_json(run_dir / "answer_results.json")
        rows = data if isinstance(data, list) else data.get("results", [])
        if not isinstance(rows, list):
            continue
        selected.extend(
            row
            for row in rows
            if isinstance(row, dict)
            and _question_conv(row) in convs
            and (qids is None or _question_id(row) in qids)
        )
    return selected


def _search_rows(
    run_dirs: Path | Iterable[Path],
    convs: set[str],
    qids: set[str] | None = None,
) -> list[tuple[int, dict[str, Any]]]:
    selected: list[tuple[int, dict[str, Any]]] = []
    for run_index, run_dir in enumerate(_coerce_run_dirs(run_dirs)):
        data = _load_json(run_dir / "search_results.json")
        rows = data if isinstance(data, list) else data.get("results", [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            metadata = row.get("retrieval_metadata") or {}
            qid = metadata.get("question_id") or row.get("question_id")
            conv = row.get("conversation_id") or _qid_conv(qid)
            if (
                isinstance(qid, str)
                and conv in convs
                and (qids is None or qid in qids)
            ):
                selected.append((run_index, row))
    return selected


def _skipped_search_keys(
    run_dirs: Path | Iterable[Path],
    convs: set[str],
    qids: set[str] | None = None,
) -> set[str]:
    skipped: set[str] = set()
    for run_index, row in _search_rows(run_dirs, convs, qids=qids):
        metadata = row.get("retrieval_metadata") or {}
        qid = metadata.get("question_id") or row.get("question_id")
        if metadata.get("skipped") is True and isinstance(qid, str):
            skipped.add(f"{run_index}:{qid}")
    return skipped


def _qid_conv(qid: Any) -> str | None:
    if not isinstance(qid, str):
        return None
    match = _QID_CONV_RE.match(qid)
    return match.group(1) if match else None


def _set_stats(
    target: dict[str, Any],
    prefix: str,
    stats: dict[str, float] | None,
) -> None:
    if not stats:
        return
    target[f"{prefix}_mean"] = stats["mean"]
    target[f"{prefix}_p50"] = stats["p50"]
    target[f"{prefix}_p95"] = stats["p95"]
    target[f"{prefix}_max"] = stats["max"]


def _override_latency_record_metrics(
    run_dirs: Path | Iterable[Path],
    convs: set[str],
    latency: dict[str, Any],
    qids: set[str] | None = None,
) -> None:
    by_op: dict[str, dict[str, float]] = {"search": {}, "answer": {}}
    seen_op: dict[str, bool] = {"search": False, "answer": False}
    dirs = _coerce_run_dirs(run_dirs)
    skipped_search_keys = _skipped_search_keys(dirs, convs, qids=qids)
    for run_index, run_dir in enumerate(dirs):
        data = _load_json(run_dir / "latency_records.json")
        records = data if isinstance(data, list) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            op = record.get("op")
            unit_id = record.get("unit_id")
            if (
                op not in by_op
                or _qid_conv(unit_id) not in convs
                or (qids is not None and unit_id not in qids)
            ):
                continue
            seen_op[op] = True
            wall_ms = _as_float(record.get("wall_ms"))
            if wall_ms is not None:
                key = f"{run_index}:{unit_id}"
                if op == "search" and key in skipped_search_keys:
                    continue
                by_op[op][key] = wall_ms

    for op in ("search", "answer"):
        stats = _stats(by_op[op].values())
        if not stats and not seen_op[op]:
            continue
        for stat_name in ("mean", "p50", "p95", "max"):
            key = f"{op}_{stat_name}_ms"
            latency[key] = stats[stat_name] if stats else None

    e2e_samples = [
        by_op["search"][qid] + by_op["answer"][qid]
        for qid in sorted(set(by_op["search"]) & set(by_op["answer"]))
    ]
    e2e_samples.extend(
        by_op["answer"][qid]
        for qid in sorted(skipped_search_keys & set(by_op["answer"]))
    )
    e2e = _stats(e2e_samples)
    if e2e:
        latency["e2e_mean_ms"] = e2e["mean"]
        latency["e2e_p50_ms"] = e2e["p50"]
        latency["e2e_p95_ms"] = e2e["p95"]
        latency["e2e_max_ms"] = e2e["max"]


def _override_answer_metadata_metrics(
    run_dirs: Path | Iterable[Path],
    convs: set[str],
    tokens: dict[str, Any],
    latency: dict[str, Any],
    qids: set[str] | None = None,
) -> None:
    rows = _answer_rows(run_dirs, convs, qids=qids)
    metadata = [row.get("metadata") or {} for row in rows]
    final_context = _stats(meta.get("final_context_tokens") for meta in metadata)
    agent_input = _stats(
        meta.get("agent_run_total_input_tokens") for meta in metadata
    )
    answer_latency = _stats(meta.get("answer_latency_ms") for meta in metadata)
    if final_context:
        _set_stats(tokens, "final_context_tokens", final_context)
    if agent_input:
        _set_stats(tokens, "agent_run_total_input_tokens", agent_input)
    if answer_latency:
        latency["answer_mean_ms"] = answer_latency["mean"]
        latency["answer_p50_ms"] = answer_latency["p50"]
        latency["answer_p95_ms"] = answer_latency["p95"]
        latency["answer_max_ms"] = answer_latency["max"]


def _text_from_content(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    )


def _add_source_count(counts: dict[str, int], key: str, value: int = 1) -> None:
    counts[key] = counts.get(key, 0) + value


def _infer_old_source_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for body in _OLD_MATCH_BLOCK_RE.findall(text):
        stripped = body.strip()
        field = re.match(r'^"?(before|after|uri)"?\s*:', stripped)
        if field:
            _add_source_count(counts, f"memory_diff.json:{field.group(1)}")
        elif '"role"' in stripped and ('"parts"' in stripped or '"created_at"' in stripped):
            _add_source_count(counts, "messages.jsonl")
        else:
            _add_source_count(counts, "other")
    return counts


def _conv_from_session_path(path: Path) -> str | None:
    parts = path.parts
    for index, part in enumerate(parts):
        if part == "conversations" and index + 1 < len(parts):
            return parts[index + 1]
    return None


def scan_tool_artifacts(
    run_dir: Path,
    convs: Iterable[str] | None = None,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "ov_archive_search_calls": 0,
        "ov_archive_search_results": 0,
        "ov_archive_search_hit_results": 0,
        "ov_archive_search_nohit_results": 0,
        "ov_archive_search_raw_matches": 0,
        "ov_archive_search_hidden_matches": 0,
        "ov_archive_search_shown_matches": 0,
        "ov_archive_expand_calls": 0,
        "ov_archive_expand_results": 0,
        "before_leak_results": 0,
        "uri_field_leak_results": 0,
        "source_rendered_results": 0,
        "field_after_rendered_results": 0,
        "source_counts": {},
    }

    conv_filter = set(convs or [])
    for path in sorted(
        (run_dir / "artifacts").glob("openclaw/**/state/agents/main/sessions/*.jsonl*")
    ):
        if conv_filter and _conv_from_session_path(path) not in conv_filter:
            continue
        for line in path.read_text(errors="ignore").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = obj.get("message") or {}
            role = message.get("role")
            content = message.get("content") or []
            if role == "assistant" and isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "toolCall":
                        continue
                    if item.get("name") == "ov_archive_search":
                        metrics["ov_archive_search_calls"] += 1
                    elif item.get("name") == "ov_archive_expand":
                        metrics["ov_archive_expand_calls"] += 1
            if role != "toolResult":
                continue
            tool_name = message.get("toolName")
            if tool_name == "ov_archive_expand":
                metrics["ov_archive_expand_results"] += 1
                continue
            if tool_name != "ov_archive_search":
                continue
            metrics["ov_archive_search_results"] += 1
            details = message.get("details") or {}
            text = _text_from_content(content)
            match_count = int(details.get("matchCount") or 0)
            raw_count = int(details.get("rawMatchCount") or match_count)
            hidden_count = int(details.get("hiddenMatchCount") or 0)
            shown_count = int(details.get("shownMatchCount") or match_count)
            metrics["ov_archive_search_raw_matches"] += raw_count
            metrics["ov_archive_search_hidden_matches"] += hidden_count
            metrics["ov_archive_search_shown_matches"] += shown_count
            if match_count > 0:
                metrics["ov_archive_search_hit_results"] += 1
            else:
                metrics["ov_archive_search_nohit_results"] += 1
            if '"before"' in text or "field: before" in text:
                metrics["before_leak_results"] += 1
            if '"uri"' in text or "field: uri" in text:
                metrics["uri_field_leak_results"] += 1
            if "source:" in text:
                metrics["source_rendered_results"] += 1
            if "field: after" in text:
                metrics["field_after_rendered_results"] += 1

            source_counts = details.get("sourceCounts")
            if isinstance(source_counts, dict):
                for key, value in source_counts.items():
                    if isinstance(key, str) and isinstance(value, int):
                        _add_source_count(metrics["source_counts"], key, value)
            else:
                for key, value in _infer_old_source_counts(text).items():
                    _add_source_count(metrics["source_counts"], key, value)

    return metrics


def _merge_tool_metrics(metrics_list: Iterable[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "source_counts": {},
    }
    for metrics in metrics_list:
        for key, value in metrics.items():
            if key == "source_counts" and isinstance(value, dict):
                for source_key, source_value in value.items():
                    if isinstance(source_key, str) and isinstance(source_value, int):
                        _add_source_count(merged["source_counts"], source_key, source_value)
            elif isinstance(value, int):
                merged[key] = merged.get(key, 0) + value
    return merged


def summarize_run(
    run_dir: Path,
    convs: Iterable[str] = DEFAULT_CONVS,
    qids: Iterable[str] | None = None,
) -> dict[str, Any]:
    return summarize_runs([run_dir], convs, qids=qids)


def summarize_runs(
    run_dirs: Iterable[Path],
    convs: Iterable[str] = DEFAULT_CONVS,
    qids: Iterable[str] | None = None,
) -> dict[str, Any]:
    dirs = _coerce_run_dirs(run_dirs)
    conv_set = set(convs)
    qid_set = set(qids) if qids is not None else None
    accuracy = _accuracy_summary(dirs, conv_set, qids=qid_set)
    tokens, latency = _benchmark_summary(dirs)
    _override_answer_metadata_metrics(dirs, conv_set, tokens, latency, qids=qid_set)
    _override_latency_record_metrics(dirs, conv_set, latency, qids=qid_set)
    return {
        "run_dirs": [str(run_dir) for run_dir in dirs],
        "convs": sorted(conv_set, key=_conv_sort_key),
        "qids": sorted(qid_set) if qid_set is not None else None,
        **accuracy,
        "tokens": tokens,
        "latency": latency,
        "tools": _merge_tool_metrics(
            scan_tool_artifacts(run_dir, conv_set) for run_dir in dirs
        ),
    }


def _delta_dict(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "overall_accuracy": _diff(
            candidate["overall"].get("accuracy"), baseline["overall"].get("accuracy")
        )
    }
    for key in (
        "final_context_tokens_mean",
        "final_context_tokens_p50",
        "final_context_tokens_p95",
        "final_context_tokens_max",
        "agent_run_total_input_tokens_mean",
        "agent_run_total_input_tokens_p50",
        "agent_run_total_input_tokens_p95",
        "agent_run_total_input_tokens_max",
    ):
        out[key] = _diff(candidate["tokens"].get(key), baseline["tokens"].get(key))
    for key in (
        "add_mean_ms",
        "add_p50_ms",
        "add_p95_ms",
        "add_max_ms",
        "search_mean_ms",
        "search_p50_ms",
        "search_p95_ms",
        "search_max_ms",
        "answer_mean_ms",
        "answer_p50_ms",
        "answer_p95_ms",
        "answer_max_ms",
        "e2e_mean_ms",
        "e2e_p50_ms",
        "e2e_p95_ms",
        "e2e_max_ms",
    ):
        out[key] = _diff(candidate["latency"].get(key), baseline["latency"].get(key))
    return out


def _diff(candidate: Any, baseline: Any) -> float | None:
    c = _as_float(candidate)
    b = _as_float(baseline)
    if c is None or b is None:
        return None
    return c - b


def compare_runs(
    baseline_dir: Path | Iterable[Path],
    candidate_dir: Path | Iterable[Path],
    convs: Iterable[str] = DEFAULT_CONVS,
    qid_scope: str = "candidate",
) -> dict[str, Any]:
    conv_set = set(convs)
    baseline_dirs = _coerce_run_dirs(baseline_dir)
    candidate_dirs = _coerce_run_dirs(candidate_dir)
    baseline_qids = _selected_qids(baseline_dirs, conv_set)
    candidate_qids = _selected_qids(candidate_dirs, conv_set)
    if qid_scope == "candidate":
        selected_qids: set[str] | None = candidate_qids
    elif qid_scope == "intersection":
        selected_qids = baseline_qids & candidate_qids
    elif qid_scope == "all":
        selected_qids = None
    else:
        raise ValueError(
            "qid_scope must be one of: candidate, intersection, all"
        )
    baseline = summarize_runs(baseline_dirs, convs, qids=selected_qids)
    candidate = summarize_runs(candidate_dirs, convs, qids=selected_qids)
    return {
        "qid_scope": qid_scope,
        "qid_count": len(selected_qids) if selected_qids is not None else None,
        "baseline": baseline,
        "candidate": candidate,
        "delta": _delta_dict(candidate, baseline),
    }


def render_markdown(comparison: dict[str, Any]) -> str:
    baseline = comparison["baseline"]
    candidate = comparison["candidate"]
    delta = comparison["delta"]
    lines = ["# OpenViking AB Comparison", ""]

    lines.append("## Accuracy")
    lines.append("| Conv | Baseline | Candidate | Delta |")
    lines.append("|---|---:|---:|---:|")
    for conv in candidate["convs"]:
        base_acc = baseline["per_conv"].get(conv, {}).get("accuracy")
        cand_acc = candidate["per_conv"].get(conv, {}).get("accuracy")
        lines.append(
            f"| {conv} | {_pct(base_acc)} | {_pct(cand_acc)} | {_pp(_diff(cand_acc, base_acc))} |"
        )
    lines.append(
        f"| weighted overall | {_pct(baseline['overall'].get('accuracy'))} | "
        f"{_pct(candidate['overall'].get('accuracy'))} | {_pp(delta.get('overall_accuracy'))} |"
    )
    lines.append("")

    lines.append("## Token Usage")
    lines.append("| Metric | Baseline | Candidate | Delta |")
    lines.append("|---|---:|---:|---:|")
    for key in (
        "final_context_tokens_mean",
        "final_context_tokens_p50",
        "final_context_tokens_p95",
        "final_context_tokens_max",
        "agent_run_total_input_tokens_mean",
        "agent_run_total_input_tokens_p50",
        "agent_run_total_input_tokens_p95",
        "agent_run_total_input_tokens_max",
    ):
        lines.append(
            f"| {key} | {_num(baseline['tokens'].get(key))} | "
            f"{_num(candidate['tokens'].get(key))} | {_num(delta.get(key))} |"
        )
    lines.append("")

    lines.append("## Latency")
    lines.append("| Metric | Baseline | Candidate | Delta |")
    lines.append("|---|---:|---:|---:|")
    for key in (
        "add_mean_ms",
        "add_p50_ms",
        "add_p95_ms",
        "add_max_ms",
        "search_mean_ms",
        "search_p50_ms",
        "search_p95_ms",
        "search_max_ms",
        "answer_mean_ms",
        "answer_p50_ms",
        "answer_p95_ms",
        "answer_max_ms",
        "e2e_mean_ms",
        "e2e_p50_ms",
        "e2e_p95_ms",
        "e2e_max_ms",
    ):
        lines.append(
            f"| {key} | {_num(baseline['latency'].get(key))} | "
            f"{_num(candidate['latency'].get(key))} | {_num(delta.get(key))} |"
        )
    lines.append("")

    lines.append("## Tool Behavior")
    lines.append("| Metric | Baseline | Candidate |")
    lines.append("|---|---:|---:|")
    tool_keys = sorted(set(baseline["tools"]) | set(candidate["tools"]))
    for key in tool_keys:
        if key == "source_counts":
            lines.append(
                f"| source_counts | `{baseline['tools'].get(key, {})}` | "
                f"`{candidate['tools'].get(key, {})}` |"
            )
            continue
        lines.append(
            f"| {key} | {baseline['tools'].get(key, 0)} | {candidate['tools'].get(key, 0)} |"
        )

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path, nargs="+")
    parser.add_argument("--candidate", required=True, type=Path, nargs="+")
    parser.add_argument("--convs", nargs="+", default=list(DEFAULT_CONVS))
    parser.add_argument(
        "--qid-scope",
        choices=["candidate", "intersection", "all"],
        default="candidate",
        help=(
            "Question set used for row-level metrics. 'candidate' compares "
            "baseline on the candidate's qids, useful for full-baseline vs smoke."
        ),
    )
    parser.add_argument("--out", type=Path, help="Optional markdown output path")
    parser.add_argument("--json-out", type=Path, help="Optional JSON output path")
    args = parser.parse_args()

    comparison = compare_runs(
        args.baseline,
        args.candidate,
        args.convs,
        qid_scope=args.qid_scope,
    )
    markdown = render_markdown(comparison)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown)
    else:
        print(markdown)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(comparison, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

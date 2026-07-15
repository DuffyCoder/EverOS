import json
from pathlib import Path

from evaluation.scripts.openviking_ab_compare import (
    compare_runs,
    render_markdown,
    summarize_run,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _make_run(root: Path, name: str, *, correct_by_conv: dict[str, int]) -> Path:
    run = root / name
    details = {}
    for conv, correct in correct_by_conv.items():
        rows = []
        for idx in range(4):
            rows.append(
                {
                    "question_id": f"{conv}_qa{idx}",
                    "category": str((idx % 4) + 1),
                    "is_correct": idx < correct,
                }
            )
        details[f"{conv}_user"] = rows

    _write_json(
        run / "eval_results.json",
        {
            "total_questions": 12,
            "correct": sum(correct_by_conv.values()),
            "accuracy": sum(correct_by_conv.values()) / 12,
            "detailed_results": details,
        },
    )
    _write_json(
        run / "benchmark_summary.json",
        {
            "answer_level": {"accuracy": sum(correct_by_conv.values()) / 12},
            "diagnostics": {
                "final_context_tokens_mean": 1000 if name == "baseline" else 800,
                "final_context_tokens_stats": {"p95": 1500 if name == "baseline" else 1100},
                "agent_run_total_input_tokens_mean": 4000 if name == "baseline" else 3500,
                "agent_run_total_input_tokens_stats": {"p95": 6000 if name == "baseline" else 5000},
            },
            "latency": {
                "answer": {
                    "wall_ms": {
                        "realistic": {
                            "mean": 20000 if name == "baseline" else 18000,
                            "p95": 45000 if name == "baseline" else 42000,
                        }
                    }
                },
                "e2e_query_ms": {
                    "wall_ms": {
                        "realistic": {
                            "mean": 21000 if name == "baseline" else 18500,
                            "p95": 47000 if name == "baseline" else 43000,
                        }
                    }
                },
            },
        },
    )
    session = (
        run
        / "artifacts/openclaw/run-x/conversations/locomo_4/state/agents/main/sessions/qa0.jsonl"
    )
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "toolCall",
                                    "id": "call-search",
                                    "name": "ov_archive_search",
                                    "arguments": {"query": "career"},
                                },
                                {
                                    "type": "toolCall",
                                    "id": "call-expand",
                                    "name": "ov_archive_expand",
                                    "arguments": {"archiveId": "archive_001"},
                                },
                            ],
                        }
                    }
                ),
                json.dumps(
                    {
                        "message": {
                            "role": "toolResult",
                            "toolCallId": "call-search",
                            "toolName": "ov_archive_search",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        'Found 2 relevant match(es) for "career" '
                                        "(4 raw; 2 stale/metadata raw match(es) hidden):\n\n"
                                        "## Match 1: archive_001\n"
                                        "source: messages.jsonl\n"
                                        "line: 10\n"
                                        '{"role":"user","parts":[{"type":"text","text":"career plan"}]}\n\n'
                                        "## Match 2: archive_001\n"
                                        "source: memory_diff.json\n"
                                        "field: after\n"
                                        "line: 11\n"
                                        '"after": "career plan"'
                                    ),
                                }
                            ],
                            "details": {
                                "query": "career",
                                "matchCount": 2,
                                "rawMatchCount": 4,
                                "hiddenMatchCount": 2,
                                "shownMatchCount": 2,
                                "sourceCounts": {
                                    "messages.jsonl": 1,
                                    "memory_diff.json:after": 1,
                                },
                            },
                        }
                    }
                ),
                json.dumps(
                    {
                        "message": {
                            "role": "toolResult",
                            "toolCallId": "call-expand",
                            "toolName": "ov_archive_expand",
                            "content": [{"type": "text", "text": "expanded"}],
                            "details": {"archiveId": "archive_001"},
                        }
                    }
                ),
            ]
        )
    )
    return run


def test_compare_runs_aligns_baseline_to_candidate_qids_by_default(tmp_path):
    baseline = _make_run(
        tmp_path,
        "baseline",
        correct_by_conv={"locomo_4": 4, "locomo_3": 4, "locomo_8": 4},
    )
    candidate = _make_run(
        tmp_path,
        "candidate",
        correct_by_conv={"locomo_4": 3, "locomo_3": 3, "locomo_8": 3},
    )
    data = json.loads((baseline / "eval_results.json").read_text())
    extra_rows = [
        {
            "question_id": f"locomo_4_qa_extra_{idx}",
            "category": "1",
            "is_correct": False,
        }
        for idx in range(6)
    ]
    data["detailed_results"]["locomo_4_user"].extend(extra_rows)
    data["total_questions"] += len(extra_rows)
    data["accuracy"] = 12 / data["total_questions"]
    (baseline / "eval_results.json").write_text(json.dumps(data))

    comparison = compare_runs(
        baseline,
        candidate,
        convs=["locomo_4", "locomo_3", "locomo_8"],
    )

    assert comparison["qid_scope"] == "candidate"
    assert comparison["baseline"]["overall"]["questions"] == 12
    assert comparison["baseline"]["overall"]["accuracy"] == 1.0
    assert comparison["candidate"]["overall"]["questions"] == 12
    assert comparison["candidate"]["overall"]["accuracy"] == 0.75


def test_summarize_run_collects_accuracy_tokens_latency_and_tool_metrics(tmp_path):
    run = _make_run(
        tmp_path,
        "candidate",
        correct_by_conv={"locomo_4": 4, "locomo_3": 3, "locomo_8": 2},
    )

    summary = summarize_run(run, convs=["locomo_4", "locomo_3", "locomo_8"])

    assert summary["overall"]["accuracy"] == 0.75
    assert summary["per_conv"]["locomo_4"]["accuracy"] == 1.0
    assert summary["per_conv"]["locomo_8"]["correct"] == 2
    assert summary["tokens"]["final_context_tokens_mean"] == 800
    assert summary["latency"]["answer_mean_ms"] == 18000
    assert summary["tools"]["ov_archive_search_calls"] == 1
    assert summary["tools"]["ov_archive_search_hit_results"] == 1
    assert summary["tools"]["ov_archive_search_raw_matches"] == 4
    assert summary["tools"]["ov_archive_search_hidden_matches"] == 2
    assert summary["tools"]["source_counts"]["messages.jsonl"] == 1
    assert summary["tools"]["source_counts"]["memory_diff.json:after"] == 1
    assert summary["tools"]["before_leak_results"] == 0
    assert summary["tools"]["uri_field_leak_results"] == 0
    assert summary["tools"]["ov_archive_expand_calls"] == 1


def test_summarize_run_filters_tool_artifacts_to_selected_convs(tmp_path):
    run = _make_run(
        tmp_path,
        "candidate",
        correct_by_conv={"locomo_4": 4, "locomo_3": 3, "locomo_8": 2},
    )
    other_session = (
        run
        / "artifacts/openclaw/run-x/conversations/locomo_9/state/agents/main/sessions/qa0.jsonl"
    )
    other_session.parent.mkdir(parents=True, exist_ok=True)
    other_session.write_text(
        json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-other",
                            "name": "ov_archive_search",
                            "arguments": {"query": "ignored"},
                        }
                    ],
                }
            }
        )
    )

    summary = summarize_run(run, convs=["locomo_4"])

    assert summary["tools"]["ov_archive_search_calls"] == 1


def test_summarize_run_prefers_selected_conv_answer_metadata_for_tokens_and_latency(tmp_path):
    run = _make_run(
        tmp_path,
        "candidate",
        correct_by_conv={"locomo_4": 4, "locomo_3": 3, "locomo_8": 2},
    )
    _write_json(
        run / "answer_results.json",
        [
            {
                "question_id": "locomo_4_qa0",
                "conversation_id": "locomo_4",
                "metadata": {
                    "answer_latency_ms": 100.0,
                    "final_context_tokens": 700,
                    "agent_run_total_input_tokens": 900,
                },
            },
            {
                "question_id": "locomo_4_qa1",
                "conversation_id": "locomo_4",
                "metadata": {
                    "answer_latency_ms": 300.0,
                    "final_context_tokens": 900,
                    "agent_run_total_input_tokens": 1100,
                },
            },
            {
                "question_id": "locomo_9_qa0",
                "conversation_id": "locomo_9",
                "metadata": {
                    "answer_latency_ms": 99999.0,
                    "final_context_tokens": 99999,
                    "agent_run_total_input_tokens": 99999,
                },
            },
        ],
    )
    _write_json(
        run / "latency_records.json",
        [
            {"op": "search", "unit_id": "locomo_4_qa0", "wall_ms": 10.0},
            {"op": "search", "unit_id": "locomo_4_qa1", "wall_ms": 20.0},
            {"op": "answer", "unit_id": "locomo_4_qa0", "wall_ms": 100.0},
            {"op": "answer", "unit_id": "locomo_4_qa1", "wall_ms": 300.0},
            {"op": "search", "unit_id": "locomo_9_qa0", "wall_ms": 99999.0},
            {"op": "answer", "unit_id": "locomo_9_qa0", "wall_ms": 99999.0},
        ],
    )

    summary = summarize_run(run, convs=["locomo_4"])

    assert summary["tokens"]["final_context_tokens_mean"] == 800
    assert summary["tokens"]["final_context_tokens_p50"] == 800
    assert summary["tokens"]["final_context_tokens_max"] == 900
    assert summary["tokens"]["agent_run_total_input_tokens_mean"] == 1000
    assert summary["latency"]["search_mean_ms"] == 15
    assert summary["latency"]["search_p50_ms"] == 15
    assert summary["latency"]["answer_mean_ms"] == 200
    assert summary["latency"]["answer_max_ms"] == 300
    assert summary["latency"]["e2e_mean_ms"] == 215


def test_summarize_run_ignores_skipped_search_latency_for_agent_local(tmp_path):
    run = _make_run(
        tmp_path,
        "candidate",
        correct_by_conv={"locomo_4": 4, "locomo_3": 3, "locomo_8": 2},
    )
    _write_json(
        run / "search_results.json",
        [
            {
                "query": "q0",
                "conversation_id": "locomo_4",
                "results": [],
                "retrieval_metadata": {
                    "skipped": True,
                    "reason": "agent_local_owns_retrieval",
                    "question_id": "locomo_4_qa0",
                },
            },
            {
                "query": "q1",
                "conversation_id": "locomo_4",
                "results": [],
                "retrieval_metadata": {
                    "skipped": True,
                    "reason": "agent_local_owns_retrieval",
                    "question_id": "locomo_4_qa1",
                },
            },
        ],
    )
    _write_json(
        run / "latency_records.json",
        [
            {"op": "search", "unit_id": "locomo_4_qa0", "wall_ms": 10.0},
            {"op": "search", "unit_id": "locomo_4_qa1", "wall_ms": 20.0},
            {"op": "answer", "unit_id": "locomo_4_qa0", "wall_ms": 100.0},
            {"op": "answer", "unit_id": "locomo_4_qa1", "wall_ms": 300.0},
        ],
    )

    summary = summarize_run(run, convs=["locomo_4"])

    assert summary["latency"]["search_mean_ms"] is None
    assert summary["latency"]["search_p50_ms"] is None
    assert summary["latency"]["search_p95_ms"] is None
    assert summary["latency"]["search_max_ms"] is None
    assert summary["latency"]["answer_mean_ms"] == 200
    assert summary["latency"]["e2e_mean_ms"] == 200
    assert summary["latency"]["e2e_p50_ms"] == 200
    assert summary["latency"]["e2e_max_ms"] == 300


def test_compare_runs_and_render_markdown_include_required_ab_dimensions(tmp_path):
    baseline = _make_run(
        tmp_path,
        "baseline",
        correct_by_conv={"locomo_4": 2, "locomo_3": 2, "locomo_8": 2},
    )
    candidate = _make_run(
        tmp_path,
        "candidate",
        correct_by_conv={"locomo_4": 4, "locomo_3": 3, "locomo_8": 2},
    )

    comparison = compare_runs(
        baseline,
        candidate,
        convs=["locomo_4", "locomo_3", "locomo_8"],
    )
    markdown = render_markdown(comparison)

    assert comparison["delta"]["overall_accuracy"] == 0.25
    assert comparison["delta"]["final_context_tokens_mean"] == -200
    assert comparison["delta"]["answer_mean_ms"] == -2000
    assert "| locomo_4 | 50.0% | 100.0% | +50.0pp |" in markdown
    assert "final_context_tokens_mean" in markdown
    assert "answer_mean_ms" in markdown
    assert "ov_archive_search_hit_results" in markdown


def test_compare_runs_aggregates_multiple_run_dirs_per_variant(tmp_path):
    baseline_4 = _make_run(
        tmp_path / "baseline-runs",
        "locomo4",
        correct_by_conv={"locomo_4": 2},
    )
    baseline_3 = _make_run(
        tmp_path / "baseline-runs",
        "locomo3",
        correct_by_conv={"locomo_3": 1},
    )
    candidate_4 = _make_run(
        tmp_path / "candidate-runs",
        "locomo4",
        correct_by_conv={"locomo_4": 3},
    )
    candidate_3 = _make_run(
        tmp_path / "candidate-runs",
        "locomo3",
        correct_by_conv={"locomo_3": 2},
    )

    comparison = compare_runs(
        [baseline_4, baseline_3],
        [candidate_4, candidate_3],
        convs=["locomo_4", "locomo_3"],
    )

    assert comparison["baseline"]["overall"]["correct"] == 3
    assert comparison["baseline"]["overall"]["questions"] == 8
    assert comparison["candidate"]["overall"]["correct"] == 5
    assert comparison["candidate"]["overall"]["questions"] == 8
    assert comparison["delta"]["overall_accuracy"] == 0.25
    assert comparison["candidate"]["tools"]["ov_archive_search_calls"] == 2

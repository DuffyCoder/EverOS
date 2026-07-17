"""Tests for raw_dump — the per-stage RAW dumper.

Each stage file must contain only the raw text/log/json of its source;
no markdown wrapping, no derived tables, no chosen marks.
"""
from __future__ import annotations

import json
from pathlib import Path

from evaluation.tools.qa_logs.cli import CLIArgs
from evaluation.tools.qa_logs.raw_dump import dump_qa_logs


EXPECTED_FILES = [
    "01_question.txt",
    "02_ingest.log",
    "03_storage.txt",
    "04_recall.log",
    "05_rerank.log",
    "06_prompt.txt",
    "07_thinking.txt",
    "08_answer.txt",
    "09_judge.json",
]


# ── end-to-end ───────────────────────────────────────────────────────────────


def test_writes_eight_stage_files(tmp_path):
    out = _run_dump(tmp_path, session_jsonl=None)
    missing = [f for f in EXPECTED_FILES if not (out / f).exists()]
    assert not missing, f"missing files: {missing}"


def test_no_markdown_index_file(tmp_path):
    out = _run_dump(tmp_path, session_jsonl=None)
    # 00_index.md was removed; no manifest/index file should be written.
    assert not (out / "00_index.md").exists()
    # No leftover markdown-wrapped files from prior layouts.
    assert not (out / "03_storage.md").exists()
    assert not (out / "05_rerank.md").exists()
    assert not (out / "07_agent_llm.log").exists()


def test_question_txt_is_plain_concat(tmp_path):
    out = _run_dump(tmp_path, session_jsonl=None)
    body = (out / "01_question.txt").read_text()
    # No markdown headings, no tables.
    assert "# " not in body
    assert "|" not in body  # no markdown table pipes
    # Minimal labels and verbatim text are both present.
    assert body.startswith("QUESTION: ")
    assert "GOLDEN: January 25, 2023" in body
    assert "Hello, I visited last week" in body
    # Evidence dia_id labeling preserved.
    assert "[D1:1]" in body


def test_ingest_log_is_raw_unmodified_lines(tmp_path):
    out = _run_dump(tmp_path, session_jsonl=None)
    body = (out / "02_ingest.log").read_text()
    # The fixture log line should be present verbatim — full vector array
    # NOT truncated, no markdown header.
    assert "Enqueued embedding message" in body
    assert "viking://user/locomo_0/memories/test.md" in body
    # Vector preserved (not "<truncated N floats>").
    assert "'vector': [0.1, 0.2, 0.3]" in body
    assert "truncated" not in body.lower()
    # No markdown wrapping anywhere.
    assert "#" not in body[:200] or body.startswith("#")  # only unavailable-stub may begin with #


def test_recall_log_uses_exact_window_and_excludes_rerank(tmp_path):
    out = _run_dump(tmp_path, session_jsonl=None)
    body = (out / "04_recall.log").read_text()
    assert "untagged inside exact window" in body
    assert "tagged inside exact window" in body
    assert "tagged outside exact window" not in body
    # rerank-phase markers must NOT appear in recall.log
    assert "[RecursiveSearch]" not in body
    assert "openai_rerank" not in body


def test_rerank_log_uses_exact_window_and_supported_sources(tmp_path):
    out = _run_dump(tmp_path, session_jsonl=None)
    body = (out / "05_rerank.log").read_text()
    assert "Initial candidate" in body
    assert "Added initial candidate" in body
    assert "Telemetry summary" in body
    assert "rerank outside exact window" not in body
    assert "recall_trace" not in body


def test_storage_lists_md_files_with_uri_separator(tmp_path):
    """03_storage.txt cat's every .md under .ovdata for this conv with
    ===viking://...=== separators and verbatim body."""
    out = _run_dump_with_ovdata(tmp_path)
    body = (out / "03_storage.txt").read_text()
    assert "===viking://user/locomo_0/memories/" in body
    assert "Hello, I visited last week" in body  # raw .md body present
    # No markdown wrapping
    assert "## " not in body
    assert "```" not in body


def test_missing_session_yields_unavailable_in_three_files(tmp_path):
    out = _run_dump(tmp_path, session_jsonl=None)
    for fname in ("06_prompt.txt", "07_thinking.txt", "08_answer.txt"):
        body = (out / fname).read_text()
        assert body.startswith("# unavailable"), f"{fname} should be unavailable stub"


def test_judge_json_is_verbatim_record(tmp_path):
    out = _run_dump(tmp_path, session_jsonl=None)
    body = (out / "09_judge.json").read_text()
    rec = json.loads(body)
    # Verbatim record fields preserved
    assert rec["question_id"] == "locomo_0_qa0"
    assert rec["is_correct"] is False
    assert rec["llm_judgments"] == {
        "judgment_1": False, "judgment_2": False, "judgment_3": False,
    }


# ── fixtures ─────────────────────────────────────────────────────────────────


def _run_dump(tmp_path: Path, session_jsonl, ovdata_root: Path = None) -> Path:
    dataset_path = _write_fixture_dataset(tmp_path)
    eval_results_path = _write_fixture_eval_results(tmp_path)
    ov_log = _write_fixture_ov_log(tmp_path)
    args = CLIArgs(
        qid="locomo_0_qa0", qid_mode="single", run_name="testrun",
        system="testsystem", ov_log=None, ovdata=None, dataset=None,
        results_root=eval_results_path.parent.parent, out=None,
    )
    out_dir = tmp_path / "out"
    dump_qa_logs(
        args, out_dir, dataset_path=dataset_path,
        ov_log=ov_log, ovdata_root=ovdata_root or (tmp_path / "no_ovdata"),
        session_jsonl=session_jsonl,
        answer_results_path=eval_results_path.parent / "answer_results.json",
    )
    return out_dir


def _run_dump_with_ovdata(tmp_path: Path) -> Path:
    import os, time
    ovdata = tmp_path / "ovdata"
    mem = ovdata / "viking" / "default" / "user" / "locomo_0" / "memories" / "events"
    mem.mkdir(parents=True)
    md = mem / "test.md"
    md.write_text("Hello, I visited last week\n")
    # Set mtime to fall inside the run window (2026-05-27 13:09:00 → 13:15:00)
    target_ts = time.mktime((2026, 5, 27, 13, 12, 0, 0, 0, -1))
    os.utime(md, (target_ts, target_ts))
    return _run_dump(tmp_path, session_jsonl=None, ovdata_root=ovdata)


def _write_fixture_dataset(tmp_path: Path) -> Path:
    dataset = [{
        "sample_id": "conv-26",
        "qa": [{
            "question": "When did Caroline visit?",
            "answer": "January 25, 2023",
            "evidence": ["D1:1"],
            "category": 2,
        }],
        "conversation": {
            "speaker_a": "Caroline", "speaker_b": "Bob",
            "session_1_date_time": "2026-05-27 13:00",
            "session_1": [
                {"speaker": "Caroline", "dia_id": "D1:1",
                 "text": "Hello, I visited last week"},
            ],
        },
    }]
    p = tmp_path / "dataset.json"
    p.write_text(json.dumps(dataset))
    return p


def _write_fixture_eval_results(tmp_path: Path) -> Path:
    import os, time
    run_dir = tmp_path / "results" / "locomo-testsystem-testrun"
    run_dir.mkdir(parents=True)
    eval_results = {
        "detailed_results": {
            "u0": [{
                "question_id": "locomo_0_qa0",
                "generated_answer": "no info",
                "is_correct": False,
                "llm_judgments": {"judgment_1": False, "judgment_2": False, "judgment_3": False},
            }],
        },
    }
    p = run_dir / "eval_results.json"
    p.write_text(json.dumps(eval_results))
    # answer_results.json so qa_window has answer_latency_ms
    qa_start_ts = time.mktime((2026, 5, 27, 13, 10, 0, 0, 0, -1))
    (run_dir / "answer_results.json").write_text(json.dumps([{
        "question_id": "locomo_0_qa0",
        "metadata": {
            "answer_latency_ms": 120_000,
            "qa_start_unix_ms": int(qa_start_ts * 1000),
        },
    }]))
    # artifacts dir so run_window has unix-epoch mtimes covering 2026-05-27
    # 13:09:00 → 13:15:00 (matches ov-server.log fixture below). Create
    # all dirs first, THEN utime — creating a child dir updates parent mtime.
    art_dir = run_dir / "artifacts" / "openclaw" / "run-20260527T130900"
    sess_dir = art_dir / "conversations" / "locomo_0" / "state" / "agents" / "main" / "sessions"
    sess_dir.mkdir(parents=True)
    sentinel = sess_dir / "qa0.jsonl"
    sentinel.write_text("")
    start_ts = time.mktime((2026, 5, 27, 13, 9, 0, 0, 0, -1))
    end_ts = time.mktime((2026, 5, 27, 13, 15, 0, 0, 0, -1))
    os.utime(sentinel, (end_ts, end_ts))
    os.utime(art_dir, (start_ts, start_ts))
    return p


def _write_fixture_ov_log(tmp_path: Path) -> Path:
    p = tmp_path / "ov-server.log"
    p.write_text(
        "2026-05-27 13:09:46,000 - openviking.storage.queuefs.embedding_queue - DEBUG - "
        "Enqueued embedding message: uri='viking://user/locomo_0/memories/test.md' "
        "'vector': [0.1, 0.2, 0.3]\n"
        "2026-05-27 13:10:10,000 - openviking.retrieve.hierarchical_retriever - DEBUG - "
        "[retrieve] untagged inside exact window\n"
        "2026-05-27 13:10:11,000 - openviking.storage.viking_vector_index_backend - DEBUG - "
        "[qid=locomo_0_qa0] tagged inside exact window\n"
        "2026-05-27 13:10:30,000 - openviking.retrieve.hierarchical_retriever - DEBUG - "
        "[qid=locomo_0_qa0] [RecursiveSearch] Initial candidate "
        "viking://user/locomo_0/memories/foo.md score 0.5\n"
        "2026-05-27 13:10:31,000 - openviking.retrieve.openai_rerank - INFO - "
        "[qid=locomo_0_qa0] [RecursiveSearch] Added initial candidate: "
        "viking://user/locomo_0/memories/foo.md (score: 0.91)\n"
        "2026-05-27 13:10:32,000 - openviking.telemetry.execution - INFO - "
        "[qid=locomo_0_qa0] Telemetry summary\n"
        "2026-05-27 13:12:01,000 - openviking.retrieve.hierarchical_retriever - DEBUG - "
        "[qid=locomo_0_qa0] [retrieve] tagged outside exact window\n"
        "2026-05-27 13:12:02,000 - openviking.retrieve.hierarchical_retriever - DEBUG - "
        "[qid=locomo_0_qa0] [RecursiveSearch] rerank outside exact window\n"
    )
    return p

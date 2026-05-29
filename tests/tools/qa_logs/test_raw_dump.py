"""Tests for raw_dump — the unprocessed source dumper that supersedes the
markdown report renderer."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from evaluation.tools.qa_logs.cli import CLIArgs
from evaluation.tools.qa_logs.raw_dump import (
    WindowSpec,
    _infer_window,
    _match_qid,
    _strip_vector_arrays,
    dump_qa_logs,
)


# ── vector truncation ─────────────────────────────────────────────────────────

def test_strip_vector_arrays_python_repr():
    line = "before 'vector': [0.123, -0.45, 1.2e-5, 3.14], 'meta': {} after"
    out = _strip_vector_arrays(line)
    assert "'vector': [<truncated 4 floats>]" in out
    assert "'meta': {} after" in out  # surrounding fields preserved


def test_strip_vector_arrays_json_double_quotes():
    line = 'foo "vector": [1.0, 2.0, 3.0] bar'
    out = _strip_vector_arrays(line)
    assert '"vector": [<truncated 3 floats>]' in out


def test_strip_vector_arrays_no_vector_field_is_noop():
    line = "no vector here just text"
    assert _strip_vector_arrays(line) == line


def test_strip_vector_arrays_vector_none_is_noop():
    # 'vector': None is not an array — must not match
    line = "data={'vector': None, 'meta': {}}"
    assert _strip_vector_arrays(line) == line


# ── qid matching ─────────────────────────────────────────────────────────────

def test_match_qid_picks_tagged_line():
    line = "2026-05-27 13:00:00,000 [qid=locomo_0_qa0] some payload"
    assert _match_qid(line, qid="locomo_0_qa0", conv="locomo_0") is True


def test_match_qid_rejects_other_qid_of_same_conv():
    """Default mode must NOT leak rows from concurrent qa of the same conv."""
    line = "2026-05-27 13:00:00,000 [qid=locomo_0_qa7] some payload"
    # Same conv (locomo_0), different qid — must be filtered out.
    assert _match_qid(line, qid="locomo_0_qa0", conv="locomo_0") is False


def test_match_qid_rejects_untagged_line_by_default():
    line = "2026-05-27 13:00:00,000 - some logger - locomo_0 mentioned"
    assert _match_qid(line, qid="locomo_0_qa0", conv="locomo_0") is False


# ── window inference ─────────────────────────────────────────────────────────

def test_infer_window_unavailable_when_no_session(tmp_path):
    args = _make_args(tmp_path, qid="locomo_7_qa9")
    w = _infer_window(
        args=args,
        conv="locomo_7",
        session_jsonl=None,
        answer_record=None,
    )
    assert w.source == "unavailable_no_session_jsonl"
    assert w.start is None
    assert w.end is None
    assert w.user_ts_ms is None
    assert w.answer_latency_ms is None


def test_infer_window_unavailable_when_no_latency(tmp_path):
    """Session jsonl present but answer_record missing → unavailable, no padding guess."""
    session = tmp_path / "qa9.jsonl"
    user_ts_ms = 1748352000000  # arbitrary
    session.write_text(json.dumps({
        "type": "message",
        "message": {
            "role": "user",
            "timestamp": user_ts_ms,
            "content": [{"type": "text", "text": "hi"}],
        },
    }) + "\n")
    args = _make_args(tmp_path, qid="locomo_7_qa9")
    w = _infer_window(
        args=args,
        conv="locomo_7",
        session_jsonl=session,
        answer_record=None,
    )
    assert w.source == "unavailable_no_latency_in_answer_results"
    assert w.start is None
    assert w.end is None
    assert w.user_ts_ms == user_ts_ms


def test_infer_window_exact_bounds_when_session_and_latency_present(tmp_path):
    session = tmp_path / "qa9.jsonl"
    user_ts_ms = 1748352000000  # 2025-05-27 ...
    session.write_text(json.dumps({
        "type": "message",
        "message": {
            "role": "user",
            "timestamp": user_ts_ms,
            "content": [{"type": "text", "text": "hi"}],
        },
    }) + "\n")
    answer_record = {"metadata": {"answer_latency_ms": 4321}}
    args = _make_args(tmp_path, qid="locomo_7_qa9")
    w = _infer_window(
        args=args,
        conv="locomo_7",
        session_jsonl=session,
        answer_record=answer_record,
    )
    assert w.source == "session_jsonl_exact"
    assert w.user_ts_ms == user_ts_ms
    assert w.answer_latency_ms == 4321
    assert w.start == datetime.fromtimestamp(user_ts_ms / 1000.0)
    assert w.end == datetime.fromtimestamp((user_ts_ms + 4321) / 1000.0)


# ── end-to-end dump_qa_logs ──────────────────────────────────────────────────

def test_dump_qa_logs_writes_ten_files(tmp_path):
    """Smoke: all 10 expected files materialize even when most sources are sparse."""
    dataset_path = _write_fixture_dataset(tmp_path)
    eval_results_path = _write_fixture_eval_results(tmp_path)
    ov_log = _write_fixture_ov_log(tmp_path)
    ovdata_root = _write_fixture_ovdata(tmp_path)

    args = _make_args(
        tmp_path, qid="locomo_0_qa0",
        results_root=eval_results_path.parent.parent,
    )
    out_dir = tmp_path / "out"
    dump_qa_logs(
        args, out_dir,
        dataset_path=dataset_path,
        ov_log=ov_log,
        ovdata_root=ovdata_root,
        session_jsonl=None,
    )

    expected = [
        "00_summary.txt",
        "01_dataset_record.json",
        "02_evidence_turns.json",
        "03_ovdata_golden.md",
        "04_log_ingest.txt",
        "05_log_archive.txt",
        "06_session_jsonl_record.json",
        "07_log_recall.txt",
        "08_eval_record.json",
        "09_window.txt",
    ]
    for fname in expected:
        assert (out_dir / fname).exists(), f"missing {fname}"


def test_dump_qa_logs_summary_contains_qid_question_judge(tmp_path):
    dataset_path = _write_fixture_dataset(tmp_path)
    eval_results_path = _write_fixture_eval_results(tmp_path)
    ov_log = _write_fixture_ov_log(tmp_path)
    ovdata_root = _write_fixture_ovdata(tmp_path)

    args = _make_args(
        tmp_path, qid="locomo_0_qa0",
        results_root=eval_results_path.parent.parent,
    )
    out_dir = tmp_path / "out"
    dump_qa_logs(
        args, out_dir,
        dataset_path=dataset_path,
        ov_log=ov_log,
        ovdata_root=ovdata_root,
        session_jsonl=None,
    )

    body = (out_dir / "00_summary.txt").read_text()
    assert "qid: locomo_0_qa0" in body
    assert "When did" in body  # question
    assert "January" in body  # golden
    assert "judge.is_correct: False" in body


def test_dump_qa_logs_dataset_record_includes_dual_id_trace(tmp_path):
    """01 must expose both dataset_index (eval-side) and sample_id_in_dataset
    so reviewers can verify the mapping is right."""
    dataset_path = _write_fixture_dataset(tmp_path)
    eval_results_path = _write_fixture_eval_results(tmp_path)
    ov_log = _write_fixture_ov_log(tmp_path)
    ovdata_root = _write_fixture_ovdata(tmp_path)

    args = _make_args(
        tmp_path, qid="locomo_0_qa0",
        results_root=eval_results_path.parent.parent,
    )
    out_dir = tmp_path / "out"
    dump_qa_logs(
        args, out_dir,
        dataset_path=dataset_path,
        ov_log=ov_log,
        ovdata_root=ovdata_root,
        session_jsonl=None,
    )

    record = json.loads((out_dir / "01_dataset_record.json").read_text())
    assert record["dataset_index"] == 0
    assert record["sample_id_in_dataset"] == "conv-26"
    assert record["conv_id_eval_side"] == "locomo_0"
    assert record["qid_idx"] == 0
    assert "question" in record["qa"]


def test_dump_qa_logs_missing_session_yields_note(tmp_path):
    dataset_path = _write_fixture_dataset(tmp_path)
    eval_results_path = _write_fixture_eval_results(tmp_path)
    ov_log = _write_fixture_ov_log(tmp_path)
    ovdata_root = _write_fixture_ovdata(tmp_path)

    args = _make_args(
        tmp_path, qid="locomo_0_qa0",
        results_root=eval_results_path.parent.parent,
    )
    out_dir = tmp_path / "out"
    dump_qa_logs(
        args, out_dir,
        dataset_path=dataset_path,
        ov_log=ov_log,
        ovdata_root=ovdata_root,
        session_jsonl=None,
    )

    record = json.loads((out_dir / "06_session_jsonl_record.json").read_text())
    assert "no session jsonl" in record["note"].lower()


def test_dump_qa_logs_archive_grabs_task_tracker_lines(tmp_path):
    dataset_path = _write_fixture_dataset(tmp_path)
    eval_results_path = _write_fixture_eval_results(tmp_path)
    ov_log = _write_fixture_ov_log(tmp_path)
    ovdata_root = _write_fixture_ovdata(tmp_path)

    args = _make_args(
        tmp_path, qid="locomo_0_qa0",
        results_root=eval_results_path.parent.parent,
    )
    out_dir = tmp_path / "out"
    dump_qa_logs(
        args, out_dir,
        dataset_path=dataset_path,
        ov_log=ov_log,
        ovdata_root=ovdata_root,
        session_jsonl=None,
    )

    archive = (out_dir / "05_log_archive.txt").read_text()
    assert "TaskTracker" in archive
    assert "Task abc-123 completed" in archive


# ── fixtures ─────────────────────────────────────────────────────────────────

def _make_args(tmp_path: Path, qid: str = "locomo_0_qa0", results_root: Path = None) -> CLIArgs:
    return CLIArgs(
        qid=qid,
        qid_mode="single",
        run_name="testrun",
        system="testsystem",
        ov_log=None,
        ovdata=None,
        dataset=None,
        results_root=results_root or tmp_path / "results",
        out=None,
    )


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
            "speaker_a": "Caroline",
            "speaker_b": "Bob",
            "session_1_date_time": "2026-05-27 13:00",
            "session_1": [
                {"speaker": "Caroline", "dia_id": "D1:1", "text": "Hello, I visited last week"},
            ],
        },
    }]
    p = tmp_path / "dataset.json"
    p.write_text(json.dumps(dataset))
    return p


def _write_fixture_eval_results(tmp_path: Path) -> Path:
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
    return p


def _write_fixture_ov_log(tmp_path: Path) -> Path:
    """Fixture ov-server.log carries [qid=locomo_0_qa0] tags (post feat/qid-tagging).

    The ingest line is NOT tagged because ingest happens at archive-build time,
    not during a QA — it must stay reachable via the conv-based ingest scan.
    The task_tracker and recall lines ARE tagged because they happen during
    the QA's retrieval phase.
    """
    p = tmp_path / "ov-server.log"
    p.write_text(
        "2026-05-27 13:09:46,000 - openviking.storage.queuefs.embedding_queue - DEBUG - "
        "Enqueued embedding message: uri='viking://user/locomo_0/memories/test.md' "
        "'vector': [0.1, 0.2, 0.3]\n"
        "2026-05-27 13:10:00,000 - openviking.service.task_tracker - INFO - "
        "[qid=locomo_0_qa0] [TaskTracker] Task abc-123 completed\n"
        "2026-05-27 13:10:30,000 - openviking.retrieve.hierarchical_retriever - DEBUG - "
        "[qid=locomo_0_qa0] [RecursiveSearch] Initial candidate URI "
        "viking://user/locomo_0/memories/foo.md "
        "score 0.5 did not pass threshold 0.1\n"
    )
    return p


def _write_fixture_ovdata(tmp_path: Path) -> Path:
    ovdata = tmp_path / "ovdata"
    mem = ovdata / "viking" / "default" / "user" / "locomo_0" / "memories" / "events"
    mem.mkdir(parents=True)
    (mem / "test.md").write_text("Hello, I visited last week\nSome more content\n")
    return ovdata

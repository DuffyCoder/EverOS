"""End-to-end test of dump_qa_logs for locomo_7_qa9.

Replaces the deleted test_e2e_qa9.py that exercised the legacy markdown
renderer. Builds in-tree fixtures (eval_results, answer_results, session jsonl,
ovdata md files, qid-tagged ov-server.log) and asserts that dump_qa_logs
produces all 12 expected files with qid-strict 07 recall (no untagged-row
leak, even in-window) and a heuristic-free 09 window.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from evaluation.tools.qa_logs.cli import CLIArgs
from evaluation.tools.qa_logs.raw_dump import dump_qa_logs

QID, CONV, QID_IDX, CONV_POS = "locomo_7_qa9", "locomo_7", 9, 7
USER_TS_MS = 1748352000000              # 2025-05-27 21:20:00.000
ANSWER_LATENCY_MS = 17894               # 17.894s — distinctive
QUESTION = "When did Deborah's father pass away?"
GOLDEN_ANSWER = "January 25, 2023"
GOLDEN_PHRASE = "Deborah's father passed away on January 25, 2023"
BULLETS = [
    "Deborah's father passed away on January 25, 2023 after a long illness",
    "Deborah's father was a civil engineer who retired in 2015 after 30 years",
    "Deborah grew up in Springfield in a large Irish-American family of seven",
    "Deborah's father immigrated from Ireland in 1980 and became a US citizen",
    "Deborah and Alice met at a community event in Chicago in early 2010s",
]
EXPECTED_FILES = [
    "00_summary.txt", "01_dataset_record.json", "02_evidence_turns.json",
    "03_ovdata_golden.md", "04_log_ingest.txt", "05_log_archive.txt",
    "06_session_jsonl_record.json", "07_log_recall.txt", "08_eval_record.json",
    "09_window.txt", "10_recall_topk.txt", "11_picking_analysis.txt",
]


def _log_ts(ms_offset: int) -> str:
    abs_ms = USER_TS_MS + ms_offset
    return datetime.fromtimestamp(abs_ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def _build_fixtures(tmp: Path) -> tuple[Path, Path, Path, Path, Path]:
    # dataset.json — data[CONV_POS] carries qa[QID_IDX]
    samples = [{"sample_id": f"conv-{i}", "qa": [], "conversation": {}} for i in range(CONV_POS)]
    qa_entries = [{"question": f"q{i}", "answer": ".", "evidence": []} for i in range(QID_IDX)]
    qa_entries.append({"question": QUESTION, "answer": GOLDEN_ANSWER,
                       "evidence": ["D5:3"], "category": 2})
    samples.append({
        "sample_id": "conv-7", "qa": qa_entries,
        "conversation": {
            "session_5_date_time": "2023-01-27 09:00",
            "session_5": [{"speaker": "Deborah", "dia_id": "D5:3",
                           "text": GOLDEN_PHRASE + " — she misses him."}],
        },
    })
    dataset = tmp / "dataset.json"
    dataset.write_text(json.dumps(samples))

    # eval_results + answer_results
    run_dir = tmp / "results" / "locomo-testsystem-testrun"
    run_dir.mkdir(parents=True)
    (run_dir / "eval_results.json").write_text(json.dumps({"detailed_results": {"u0": [{
        "question_id": QID, "generated_answer": "I don't know.",
        "is_correct": False,
        "llm_judgments": {"judgment_1": False, "judgment_2": False, "judgment_3": False},
    }]}}))
    (run_dir / "answer_results.json").write_text(json.dumps([{
        "question_id": QID, "metadata": {"answer_latency_ms": ANSWER_LATENCY_MS},
    }]))

    # 7-record session jsonl with one user + one assistant message
    sess_dir = (run_dir / "artifacts" / "openclaw" / "run-x" / "conversations"
                / CONV / "state" / "agents" / "main" / "sessions")
    sess_dir.mkdir(parents=True)
    session = sess_dir / f"qa{QID_IDX}.jsonl"
    user_text = (
        "<relevant-memories>\nThe following OpenViking memories\nrelevant:\n"
        + "\n".join(f"- [] {b}" for b in BULLETS)
        + f"\n</relevant-memories>\n\n{QUESTION}"
    )
    records = [
        {"type": "session", "version": 3, "id": "sid",
         "timestamp": "2025-05-27T21:19:58.000Z", "cwd": "/wd"},
        {"type": "model_change", "id": "mc", "parentId": None,
         "timestamp": "2025-05-27T21:19:58.010Z",
         "provider": "sophnet", "modelId": "gpt-4.1-mini"},
        {"type": "message", "id": "u", "parentId": "mc",
         "timestamp": "2025-05-27T21:20:00.000Z",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": user_text}],
                     "timestamp": USER_TS_MS}},
        {"type": "custom", "customType": "model-snapshot",
         "data": {"timestamp": USER_TS_MS + 50}, "id": "snap",
         "parentId": "u", "timestamp": "2025-05-27T21:20:00.050Z"},
        {"type": "message", "id": "a", "parentId": "snap",
         "timestamp": "2025-05-27T21:20:17.894Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking",
              "thinking": "The relevant-memories give no clear date.",
              "thinkingSignature": "rc"},
             {"type": "text",
              "text": "I don't have enough information to answer."},
         ], "timestamp": USER_TS_MS + ANSWER_LATENCY_MS}},
        {"type": "custom", "customType": "model-snapshot",
         "data": {"timestamp": USER_TS_MS + ANSWER_LATENCY_MS + 1},
         "id": "snap2", "parentId": "a",
         "timestamp": "2025-05-27T21:20:17.895Z"},
        {"type": "custom", "customType": "stop",
         "data": {"reason": "end_turn"}, "id": "stp", "parentId": "snap2",
         "timestamp": "2025-05-27T21:20:17.900Z"},
    ]
    session.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    # ovdata: golden md + 4 supplementary md matching bullets 1..4
    ovdata = tmp / "ovdata"
    mem = ovdata / "viking" / "default" / "user" / CONV / "memories"
    (mem / "events" / "2023" / "01" / "27").mkdir(parents=True)
    (mem / "events" / "2023" / "01" / "27" / "deborah_father_passed.md").write_text(
        f"{BULLETS[0]}\n\ncontext: {GOLDEN_PHRASE}\n"
    )
    (mem / "abstracts").mkdir()
    for i in range(1, 5):
        (mem / "abstracts" / f"abstract_{i}.md").write_text(BULLETS[i] + "\n")

    # ov-server.log: mix of qid-tagged + legacy untagged rows
    golden_uri = f"viking://user/{CONV}/memories/events/2023/01/27/deborah_father_passed.md"
    alt_uri = f"viking://user/{CONV}/memories/abstracts/abstract_1.md"
    rows = [
        # conv-scoped ingest (never qid-tagged) — for 04
        f"{_log_ts(-60_000)} - openviking.storage.queuefs.embedding_queue - DEBUG - "
        f"Enqueued embedding message: uri='{golden_uri}' 'vector': [0.1, 0.2, 0.3]",
        # IN-WINDOW legacy untagged recall row — MUST be excluded by qid filter
        f"{_log_ts(2_000)} - openviking.retrieve.hierarchical_retriever - DEBUG - "
        f"[RecursiveSearch] {CONV} legacy untagged concurrent-qa3 leakage",
        # IN-WINDOW qid-tagged rows — included in 05 / 07 / 10
        f"{_log_ts(3_000)} - openviking.service.task_tracker - INFO - "
        f"[qid={QID}] [TaskTracker] Task xyz-789 completed",
        f"{_log_ts(4_000)} - openviking.retrieve.hierarchical_retriever - DEBUG - "
        f"[qid={QID}] [RecursiveSearch] vector top "
        f"[1] URI: {golden_uri}, score: 0.8123, level: 0, meta: {{}}",
        f"{_log_ts(5_000)} - openviking.retrieve.hierarchical_retriever - DEBUG - "
        f"[qid={QID}] [RecursiveSearch] vector top "
        f"[2] URI: {alt_uri}, score: 0.7714, level: 0, meta: {{}}",
        f"{_log_ts(6_000)} - openviking.retrieve.openai_rerank - INFO - "
        f"[qid={QID}] [RecursiveSearch] Added initial candidate: {golden_uri} (score: 0.9540)",
        f"{_log_ts(7_000)} - openviking.retrieve.openai_rerank - INFO - "
        f"[qid={QID}] [RecursiveSearch] Added initial candidate: {alt_uri} (score: 0.7321)",
        f"{_log_ts(8_000)} - openviking.telemetry.execution - INFO - "
        f"[qid={QID}] Telemetry summary returned: 'returned': 5",
        f"{_log_ts(9_000)} - openviking.retrieve.openai_embedders - DEBUG - "
        f"[qid={QID}] query embed done dim=1024",
        # OUT-OF-WINDOW legacy untagged row — MUST be excluded
        f"{_log_ts(60_000)} - openviking.retrieve.hierarchical_retriever - DEBUG - "
        f"[RecursiveSearch] {CONV} legacy out-of-window row",
    ]
    ov_log = tmp / "ov-server.log"
    ov_log.write_text("\n".join(rows) + "\n")

    return dataset, run_dir, session, ovdata, ov_log


def test_dump_qa_logs_e2e_qa9_produces_twelve_files_with_qid_strict_scoping(tmp_path):
    dataset, run_dir, session, ovdata, ov_log = _build_fixtures(tmp_path)
    args = CLIArgs(qid=QID, qid_mode="single", run_name="testrun",
                   system="testsystem", ov_log=None, ovdata=None, dataset=None,
                   results_root=tmp_path / "results", out=None)
    out = tmp_path / "out"
    dump_qa_logs(args, out, dataset_path=dataset, ov_log=ov_log,
                 ovdata_root=ovdata, session_jsonl=session,
                 answer_results_path=run_dir / "answer_results.json")

    missing = [f for f in EXPECTED_FILES if not (out / f).exists()]
    assert not missing, f"missing files: {missing}"

    summary = (out / "00_summary.txt").read_text()
    assert f"qid: {QID}" in summary and QUESTION in summary
    assert GOLDEN_ANSWER in summary and "judge.is_correct: False" in summary

    rec = json.loads((out / "01_dataset_record.json").read_text())
    assert rec["dataset_index"] == CONV_POS
    assert rec["sample_id_in_dataset"] == "conv-7"
    assert rec["conv_id_eval_side"] == CONV
    assert rec["qid_idx"] == QID_IDX
    assert rec["qa"]["question"] == QUESTION

    sess_rec = json.loads((out / "06_session_jsonl_record.json").read_text())
    assert sess_rec["user_message_unix_ts_ms"] == USER_TS_MS
    assert len(sess_rec["injected_bullets"]) == 5
    assert sess_rec["injected_bullets"][0]["text"].startswith("Deborah's father passed away")
    assert "I don't have enough information" in sess_rec["assistant_text"]

    # 07: every non-header non-blank line MUST carry the qid tag (no leak).
    recall = (out / "07_log_recall.txt").read_text()
    tag = f"[qid={QID}]"
    leaks = [ln for ln in recall.splitlines()
             if ln and not ln.startswith("#") and tag not in ln]
    assert not leaks, f"untagged rows leaked into 07: {leaks!r}"
    assert "[RecursiveSearch] vector top" in recall
    assert "Added initial candidate" in recall

    # 09: exact bounds, no heuristic padding markers anywhere.
    window = (out / "09_window.txt").read_text()
    assert "source: session_jsonl_exact" in window
    ts_iso = datetime.fromtimestamp(USER_TS_MS / 1000.0).isoformat()
    end_iso = datetime.fromtimestamp((USER_TS_MS + ANSWER_LATENCY_MS) / 1000.0).isoformat()
    assert f"user_ts: {ts_iso}" in window
    assert f"window_end: {end_iso}" in window
    assert f"answer_latency_ms: {ANSWER_LATENCY_MS}" in window
    for marker in ("+5s", "-15s", "max(15s", "padded", "heuristic"):
        assert marker not in window, f"heuristic marker {marker!r} in 09_window.txt"

    topk = (out / "10_recall_topk.txt").read_text()
    assert "=== VECTOR TOPK" in topk and "=== RERANK CANDIDATES" in topk
    assert "deborah_father_passed.md" in topk and "0.8123" in topk

    picking = (out / "11_picking_analysis.txt").read_text()
    assert "=== PICKED 5 BULLETS" in picking and "REVERSE LOOKUP" in picking
    # Each picked row begins with right-justified rank index 0..4.
    padded = "\n" + picking
    for i in range(5):
        assert f"\n{i:>2}  " in padded, f"missing pick row {i}"

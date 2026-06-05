"""End-to-end test of dump_qa_logs for locomo_7_qa9 with the 9-file pure-raw layout."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from evaluation.tools.qa_logs.cli import CLIArgs
from evaluation.tools.qa_logs.raw_dump import dump_qa_logs


QID, CONV, QID_IDX, CONV_POS = "locomo_7_qa9", "locomo_7", 9, 7
USER_TS_MS = 1748352000000
ANSWER_LATENCY_MS = 17894
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


def _log_ts(ms_offset: int) -> str:
    abs_ms = USER_TS_MS + ms_offset
    return datetime.fromtimestamp(abs_ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def _build_fixtures(tmp: Path) -> tuple[Path, Path, Path, Path, Path]:
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

    run_dir = tmp / "results" / "locomo-testsystem-testrun"
    run_dir.mkdir(parents=True)
    (run_dir / "eval_results.json").write_text(json.dumps({"detailed_results": {"u0": [{
        "question_id": QID, "generated_answer": "I don't know.",
        "is_correct": False,
        "llm_judgments": {"judgment_1": False, "judgment_2": False, "judgment_3": False},
    }]}}))
    # answer_results provides qa latency for qa_window
    (run_dir / "answer_results.json").write_text(json.dumps([{
        "question_id": QID, "metadata": {"answer_latency_ms": ANSWER_LATENCY_MS},
    }]))

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
        {"type": "message", "id": "u",
         "timestamp": "2025-05-27T21:20:00.000Z",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": user_text}],
                     "timestamp": USER_TS_MS}},
        {"type": "message", "id": "a",
         "timestamp": "2025-05-27T21:20:17.894Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking",
              "thinking": "The relevant-memories give no clear date.",
              "thinkingSignature": "rc"},
             {"type": "text",
              "text": "I don't have enough information to answer."},
         ], "timestamp": USER_TS_MS + ANSWER_LATENCY_MS}},
    ]
    session.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    golden_uri = f"viking://user/{CONV}/memories/events/2023/01/27/deborah_father_passed.md"
    alt_uri = f"viking://user/{CONV}/memories/abstracts/abstract_1.md"
    # ov-server.log rows mirroring real OV emit shape:
    #   * ingest: embedding_queue / Enqueued (conv-scoped, no qid tag)
    #   * recall (vector retrieval): hierarchical_retriever w/ [retrieve] prefix
    #   * rerank (post-rerank scoring): hierarchical_retriever w/ [RecursiveSearch] prefix
    #     + openai_rerank brief summary + recall_trace JSON + telemetry
    rows = [
        # ingest (conv-scoped, untagged)
        f"{_log_ts(-60_000)} - openviking.storage.queuefs.embedding_queue - DEBUG - "
        f"Enqueued embedding message: uri='{golden_uri}' 'vector': [0.1, 0.2, 0.3]",
        # untagged recall row — MUST NOT appear anywhere qid-scoped
        f"{_log_ts(2_000)} - openviking.retrieve.hierarchical_retriever - DEBUG - "
        f"[retrieve] {CONV} untagged",
        # qid-tagged recall (vector retrieval phase, [retrieve] prefix)
        f"{_log_ts(3_500)} - openviking.storage.viking_vector_index_backend - DEBUG - "
        f"[qid={QID}] [_SingleAccountBackend.query] Called with filter=...",
        f"{_log_ts(4_000)} - openviking.retrieve.hierarchical_retriever - DEBUG - "
        f"[qid={QID}] [retrieve] Step 2 completed, global_results contains 2 items:",
        f"{_log_ts(4_001)} - openviking.retrieve.hierarchical_retriever - DEBUG - "
        f"[qid={QID}]   [0] URI: {golden_uri}, score: 0.8123, level: 2, account_id: UNKNOWN_ACCOUNT_ID",
        f"{_log_ts(4_002)} - openviking.retrieve.hierarchical_retriever - DEBUG - "
        f"[qid={QID}]   [1] URI: {alt_uri}, score: 0.7714, level: 2, account_id: UNKNOWN_ACCOUNT_ID",
        f"{_log_ts(4_500)} - openviking.models.embedder.openai_embedders - DEBUG - "
        f"[qid={QID}] query embed done dim=1024",
        # qid-tagged rerank ([RecursiveSearch] prefix on hierarchical_retriever)
        f"{_log_ts(6_000)} - openviking.retrieve.hierarchical_retriever - DEBUG - "
        f"[qid={QID}] [RecursiveSearch] Added initial candidate: {golden_uri} (score: 0.9540)",
        f"{_log_ts(6_500)} - openviking.retrieve.hierarchical_retriever - DEBUG - "
        f"[qid={QID}] [RecursiveSearch] Initial candidate URI {alt_uri} score 0.7321 did not pass threshold 0.8",
        # qid-tagged rerank logger summary
        f"{_log_ts(7_000)} - openviking.models.rerank.openai_rerank - DEBUG - "
        f"[qid={QID}] [OpenAIRerankClient] Reranked 2 documents",
        # recall_trace structured JSON (qid lives inside the JSON body, not [qid=...] tag)
        f"{_log_ts(7_500)} - openviking.observability.recall_trace - INFO - "
        f"[recall_trace] {{\"duration_ms\": 705.0, \"vector_returned\": 2, \"passed_threshold\": 1, "
        f"\"returned_top\": [[\"{golden_uri}\", 0.954]], \"rerank_used\": true, "
        f"\"qid\": \"{QID}\", \"conv_id\": \"{CONV}\", \"stage\": \"server_summary\"}}",
        # qid-tagged telemetry summary
        f"{_log_ts(8_000)} - openviking.telemetry.execution - INFO - "
        f"[qid={QID}] Telemetry summary (id=tm_xyz): {{'operation': 'search.find', 'status': 'ok'}}",
    ]
    ov_log = tmp / "ov-server.log"
    ov_log.write_text("\n".join(rows) + "\n")

    # ovdata so 03_storage.txt has content
    ovdata = tmp / "ovdata"
    mem = ovdata / "viking" / "default" / "user" / CONV / "memories" / "events" / "2023" / "01" / "27"
    mem.mkdir(parents=True)
    (mem / "deborah_father_passed.md").write_text(GOLDEN_PHRASE + "\n")

    return dataset, run_dir, session, ovdata, ov_log


def _dump(tmp_path: Path) -> Path:
    import os
    dataset, run_dir, session, ovdata, ov_log = _build_fixtures(tmp_path)
    # Stamp ovdata .md + run artifacts inside the qa wall-clock window so
    # 02/03/04/05 pass the freshness check. Order: utime AFTER all mkdirs
    # (creating a subdir bumps parent mtime).
    target = USER_TS_MS / 1000.0
    for md in ovdata.rglob("*.md"):
        os.utime(md, (target, target))
    art_root = run_dir / "artifacts" / "openclaw" / "run-x"
    os.utime(session, (target + 120, target + 120))
    os.utime(art_root, (target - 120, target - 120))
    args = CLIArgs(qid=QID, qid_mode="single", run_name="testrun",
                   system="testsystem", ov_log=None, ovdata=None, dataset=None,
                   results_root=tmp_path / "results", out=None)
    out = tmp_path / "out"
    dump_qa_logs(args, out, dataset_path=dataset, ov_log=ov_log,
                 ovdata_root=ovdata, session_jsonl=session,
                 answer_results_path=run_dir / "answer_results.json")
    return out


def test_e2e_produces_nine_files(tmp_path):
    out = _dump(tmp_path)
    missing = [f for f in EXPECTED_FILES if not (out / f).exists()]
    assert not missing


def test_e2e_question_is_plain_concat(tmp_path):
    out = _dump(tmp_path)
    body = (out / "01_question.txt").read_text()
    assert "# " not in body
    assert "|" not in body
    assert body.startswith(f"QUESTION: {QUESTION}")
    assert f"GOLDEN: {GOLDEN_ANSWER}" in body
    assert "[D5:3]" in body
    assert GOLDEN_PHRASE in body


def test_e2e_ingest_is_raw_vectors_not_truncated(tmp_path):
    out = _dump(tmp_path)
    body = (out / "02_ingest.log").read_text()
    assert "Enqueued embedding message" in body
    assert "'vector': [0.1, 0.2, 0.3]" in body
    assert "truncated" not in body.lower()


def test_e2e_storage_lists_md_files(tmp_path):
    out = _dump(tmp_path)
    body = (out / "03_storage.txt").read_text()
    assert "===viking://user/locomo_7/memories/events/2023/01/27/deborah_father_passed.md===" in body
    assert GOLDEN_PHRASE in body
    # No markdown wrappers
    assert "## " not in body
    assert "```" not in body


def test_e2e_recall_qid_strict_excludes_rerank(tmp_path):
    out = _dump(tmp_path)
    body = (out / "04_recall.log").read_text()
    tag = f"[qid={QID}]"
    for ln in body.splitlines():
        if not ln or ln.startswith("#"):
            continue
        assert tag in ln, f"untagged row leaked into recall: {ln!r}"
        assert "[RecursiveSearch]" not in ln, f"rerank line leaked into recall: {ln!r}"
        assert "openai_rerank" not in ln
        assert "recall_trace" not in ln
    # Vector retrieval markers ARE present
    assert "[retrieve] Step 2 completed" in body
    assert "URI: " in body and "0.8123" in body
    assert "viking_vector_index_backend" in body
    assert "openai_embedders" in body


def test_e2e_rerank_captures_recursive_search_plus_returned_top(tmp_path):
    out = _dump(tmp_path)
    body = (out / "05_rerank.log").read_text()
    tag = f"[qid={QID}]"
    trace_qid = f'"qid": "{QID}"'
    for ln in body.splitlines():
        if not ln or ln.startswith("#"):
            continue
        assert tag in ln or trace_qid in ln, f"untagged row leaked into rerank: {ln!r}"
    # Per-URI rerank scores from RecursiveSearch
    assert "Added initial candidate" in body
    assert "0.9540" in body
    assert "did not pass threshold" in body
    # openai_rerank summary
    assert "Reranked 2 documents" in body
    # recall_trace JSON with returned_top final result
    assert "returned_top" in body
    assert '"qid": "locomo_7_qa9"' in body
    # telemetry
    assert "Telemetry summary" in body


def test_e2e_prompt_thinking_answer_are_three_separate_raw_files(tmp_path):
    out = _dump(tmp_path)
    prompt = (out / "06_prompt.txt").read_text()
    thinking = (out / "07_thinking.txt").read_text()
    answer = (out / "08_answer.txt").read_text()
    assert "<relevant-memories>" in prompt
    assert QUESTION in prompt
    assert "## " not in prompt
    assert "```" not in prompt
    assert thinking.strip() == "The relevant-memories give no clear date."
    assert answer.strip() == "I don't have enough information to answer."


def test_e2e_judge_json_is_raw_record(tmp_path):
    out = _dump(tmp_path)
    body = (out / "09_judge.json").read_text()
    rec = json.loads(body)
    assert rec["question_id"] == QID
    assert rec["llm_judgments"]["judgment_1"] is False
    assert rec["is_correct"] is False

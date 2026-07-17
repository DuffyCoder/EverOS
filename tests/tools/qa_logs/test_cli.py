from pathlib import Path

from evaluation.src.config.system_index import load_system_index
from evaluation.src.config.system_loader import resolve_system_config
from evaluation.tools.qa_logs.cli import (
    DEFAULT_SYSTEM,
    normalize_qid,
    parse_args,
    resolve_latest_run,
)

EXPECTED_DEFAULT_SYSTEM = "openclaw-docker-openviking-session-bundle-noop"


def test_default_system_is_a_registered_public_id():
    args = parse_args(["--run-name", "default-system"])

    assert DEFAULT_SYSTEM == EXPECTED_DEFAULT_SYSTEM
    assert args.system == EXPECTED_DEFAULT_SYSTEM
    assert DEFAULT_SYSTEM in load_system_index().systems
    resolved = resolve_system_config(
        DEFAULT_SYSTEM,
        environ={"LLM_API_KEY": "test-key", "OPENCLAW_REPO_PATH": "/tmp/openclaw"},
    )
    assert resolved.requested_id == DEFAULT_SYSTEM


def test_normalize_qid_underscore_form_to_canonical():
    # 用户友好输入(带下划线)→ dataset/eval_results 实际格式(无下划线)
    assert normalize_qid("locomo_7_qa_9") == "locomo_7_qa9"
    assert normalize_qid("locomo_7_qa9") == "locomo_7_qa9"  # 兼容直传
    assert normalize_qid("locomo_10_qa_123") == "locomo_10_qa123"


def test_normalize_qid_rejects_bad_form():
    import pytest

    with pytest.raises(ValueError):
        normalize_qid("foo")
    with pytest.raises(ValueError):
        normalize_qid("locomo_7_question_9")


def test_cli_qid_underscore_is_normalized():
    args = parse_args(["--qid", "locomo_7_qa_9", "--run-name", "main-noproxy-c4"])
    assert args.qid == "locomo_7_qa9"
    assert args.run_name == "main-noproxy-c4"
    assert args.qid_mode == "single"


def test_cli_qid_omitted_means_all_errors():
    args = parse_args(["--run-name", "main-noproxy-c4"])
    assert args.qid is None
    assert args.qid_mode == "all_errors"


def test_cli_run_name_omitted_means_latest(tmp_path, monkeypatch):
    # 构造两个 results 目录,一个新一个旧
    results = tmp_path / "evaluation" / "results"
    old = results / "locomo-sys-old"
    new = results / "locomo-sys-fresh"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "eval_results.json").write_text("{}")
    (new / "eval_results.json").write_text("{}")
    import os
    import time

    os.utime(old, (time.time() - 3600, time.time() - 3600))
    os.utime(new, (time.time(), time.time()))
    monkeypatch.chdir(tmp_path)
    rn = resolve_latest_run(results_root=results)
    assert rn == "fresh"  # 剥 "locomo-sys-" 前缀


def test_cli_run_name_omitted_with_no_results_raises(tmp_path):
    import pytest

    empty = tmp_path / "no_results"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        resolve_latest_run(results_root=empty)

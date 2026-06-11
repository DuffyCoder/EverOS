import json
from pathlib import Path

import pytest

from evaluation.tools.qa_logs.materialize_qa_jsonl import materialize_qa_jsonl


def _write_jsonl(path: Path, text: str) -> None:
    path.write_text(json.dumps({"type": "message", "text": text}) + "\n")


def test_materialize_archived_session_jsonl_to_qa_files(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_jsonl(sessions / "sid.jsonl.300", "third")
    _write_jsonl(sessions / "sid.jsonl.100", "first")
    _write_jsonl(sessions / "sid.jsonl.200", "second")
    (sessions / "sessions.json").write_text("{}")
    _write_jsonl(sessions / "sid.jsonl", "live")

    written = materialize_qa_jsonl(sessions)

    assert [p.name for p in written] == ["qa0.jsonl", "qa1.jsonl", "qa2.jsonl"]
    assert (sessions / "qa0.jsonl").read_text() == (
        sessions / "sid.jsonl.100"
    ).read_text()
    assert (sessions / "qa1.jsonl").read_text() == (
        sessions / "sid.jsonl.200"
    ).read_text()
    assert (sessions / "qa2.jsonl").read_text() == (
        sessions / "sid.jsonl.300"
    ).read_text()


def test_materialize_can_write_to_separate_output_dir(tmp_path):
    sessions = tmp_path / "sessions"
    out = tmp_path / "out"
    sessions.mkdir()
    _write_jsonl(sessions / "sid.jsonl.100", "first")

    written = materialize_qa_jsonl(sessions, out_dir=out)

    assert written == [out / "qa0.jsonl"]
    assert (out / "qa0.jsonl").exists()
    assert not (sessions / "qa0.jsonl").exists()


def test_materialize_refuses_to_overwrite_without_force(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_jsonl(sessions / "sid.jsonl.100", "first")
    _write_jsonl(sessions / "qa0.jsonl", "existing")

    with pytest.raises(FileExistsError):
        materialize_qa_jsonl(sessions)


def test_materialize_start_idx_offsets_output_names(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_jsonl(sessions / "sid.jsonl.100", "first")

    written = materialize_qa_jsonl(sessions, start_idx=10)

    assert written == [sessions / "qa10.jsonl"]

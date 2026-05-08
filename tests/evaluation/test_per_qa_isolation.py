"""Unit tests for evaluation.src.adapters.openclaw.per_qa_isolation (PR4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from evaluation.src.adapters.openclaw.per_qa_isolation import (
    QA_STATES_DIRNAME,
    SNAPSHOT_DIRNAME,
    container_state_dir,
    discard_qa_state,
    freeze_state,
    resolve_isolation_mode,
    restore_state_for_qa,
)


# ---------- resolve_isolation_mode -----------------------------------------

@pytest.mark.parametrize("setting, ce, expected", [
    # Explicit values bypass the auto resolution.
    ("snapshot", "", "snapshot"),
    ("snapshot", "hypercompositor", "snapshot"),
    ("off", "", "off"),
    ("off", "hypercompositor", "off"),
    # auto: snapshot iff ce_mode is non-empty.
    ("auto", "", "off"),
    ("auto", None, "off"),
    ("auto", "hypercompositor", "snapshot"),
    ("auto", "  hypercompositor  ", "snapshot"),
    # Default (no setting): same as auto.
    (None, "", "off"),
    (None, "hypercompositor", "snapshot"),
])
def test_resolve_isolation_mode(setting, ce, expected):
    assert resolve_isolation_mode(setting, ce) == expected


def test_resolve_isolation_mode_rejects_unknown_values():
    """Codex review of PR5: unknown values must error early instead of
    silently coercing to auto. Common typo example: 'snapshots' (plural)."""
    with pytest.raises(ValueError, match="per_qa_isolation must be one of"):
        resolve_isolation_mode("snapshots", "hypercompositor")
    with pytest.raises(ValueError, match="per_qa_isolation must be one of"):
        resolve_isolation_mode("bogus", "")


# ---------- freeze_state ---------------------------------------------------

def test_freeze_state_copies_existing_state(tmp_path: Path):
    workspace = tmp_path / "workspace"
    state = workspace / "state"
    state.mkdir(parents=True)
    (state / "sessions").mkdir()
    (state / "sessions" / "conv__bootstrap.json").write_text("{}")
    (state / "memory.sqlite").write_bytes(b"\x00\x01\x02")

    snapshot = freeze_state(workspace)

    assert snapshot == workspace / SNAPSHOT_DIRNAME
    assert (snapshot / "sessions" / "conv__bootstrap.json").read_text() == "{}"
    assert (snapshot / "memory.sqlite").read_bytes() == b"\x00\x01\x02"


def test_freeze_state_creates_empty_baseline_when_state_missing(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    snapshot = freeze_state(workspace)
    assert snapshot.exists()
    assert snapshot.is_dir()
    assert list(snapshot.iterdir()) == []


def test_freeze_state_rebuilds_existing_baseline(tmp_path: Path):
    workspace = tmp_path / "workspace"
    state = workspace / "state"
    state.mkdir(parents=True)
    (state / "v1.txt").write_text("v1")
    freeze_state(workspace)
    # Modify state to simulate post-add() updates.
    (state / "v1.txt").write_text("v2")
    (state / "v2.txt").write_text("new")
    snapshot = freeze_state(workspace)
    # Snapshot reflects the second freeze; v1 has new content, v2 added.
    assert (snapshot / "v1.txt").read_text() == "v2"
    assert (snapshot / "v2.txt").read_text() == "new"


# ---------- restore_state_for_qa -------------------------------------------

def test_restore_copies_baseline_to_per_qa_dir(tmp_path: Path):
    workspace = tmp_path / "workspace"
    state = workspace / "state"
    state.mkdir(parents=True)
    (state / "memory.sqlite").write_bytes(b"hello")
    freeze_state(workspace)

    qa_dir = restore_state_for_qa(workspace, "conv0__qa3")
    assert qa_dir == workspace / QA_STATES_DIRNAME / "conv0__qa3"
    assert (qa_dir / "memory.sqlite").read_bytes() == b"hello"


def test_restore_isolates_each_qa(tmp_path: Path):
    """Two QAs must get separate, fresh copies — modifying one does not
    affect the other or the baseline."""
    workspace = tmp_path / "workspace"
    state = workspace / "state"
    state.mkdir(parents=True)
    (state / "shared.txt").write_text("baseline")
    freeze_state(workspace)

    qa1 = restore_state_for_qa(workspace, "qa1")
    qa2 = restore_state_for_qa(workspace, "qa2")

    (qa1 / "shared.txt").write_text("modified-by-qa1")
    (qa1 / "qa1-only.txt").write_text("hi")

    # qa2 not affected
    assert (qa2 / "shared.txt").read_text() == "baseline"
    assert not (qa2 / "qa1-only.txt").exists()

    # baseline not affected
    baseline = workspace / SNAPSHOT_DIRNAME
    assert (baseline / "shared.txt").read_text() == "baseline"
    assert not (baseline / "qa1-only.txt").exists()


def test_restore_overwrites_existing_per_qa_dir(tmp_path: Path):
    """Re-running QA with same id starts from baseline, not from the
    previous run's state."""
    workspace = tmp_path / "workspace"
    state = workspace / "state"
    state.mkdir(parents=True)
    (state / "x.txt").write_text("baseline")
    freeze_state(workspace)

    qa = restore_state_for_qa(workspace, "qa0")
    (qa / "leftover.txt").write_text("from previous run")

    qa_again = restore_state_for_qa(workspace, "qa0")
    assert qa_again == qa
    assert not (qa_again / "leftover.txt").exists()
    assert (qa_again / "x.txt").read_text() == "baseline"


def test_restore_without_baseline_creates_empty_dir(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    qa = restore_state_for_qa(workspace, "qa0")
    assert qa.exists()
    assert list(qa.iterdir()) == []


# ---------- discard_qa_state -----------------------------------------------

def test_discard_removes_per_qa_dir(tmp_path: Path):
    workspace = tmp_path / "workspace"
    state = workspace / "state"
    state.mkdir(parents=True)
    freeze_state(workspace)
    qa = restore_state_for_qa(workspace, "qa0")
    assert qa.exists()
    discard_qa_state(workspace, "qa0")
    assert not qa.exists()


def test_discard_is_idempotent(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    discard_qa_state(workspace, "qa0")  # no-op
    discard_qa_state(workspace, "qa0")  # still no-op


# ---------- container_state_dir --------------------------------------------

def test_container_state_dir_returns_in_container_posix_path(tmp_path: Path):
    workspace = tmp_path / "workspace"
    qa_host = workspace / QA_STATES_DIRNAME / "qa0"
    assert container_state_dir(qa_host, workspace) == "/workspace/.qa_states/qa0"


def test_container_state_dir_with_complex_qid(tmp_path: Path):
    workspace = tmp_path / "workspace"
    qa_host = workspace / QA_STATES_DIRNAME / "conv0__qa17"
    assert container_state_dir(qa_host, workspace) == "/workspace/.qa_states/conv0__qa17"


# ---------- _safe_qid via integration --------------------------------------

def test_safe_qid_sanitizes_path_separators(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    qa = restore_state_for_qa(workspace, "../escape")
    # Sanitized: "../escape" -> ".._escape" (no leading-dot escape)
    assert qa.parent == workspace / QA_STATES_DIRNAME
    assert qa.name == ".._escape"
    # Forward slash also stripped.
    qa2 = restore_state_for_qa(workspace, "conv/qa")
    assert qa2.name == "conv_qa"


def test_safe_qid_rejects_dots():
    """Pure '.' / '..' qids would resolve to the parent dir; reject."""
    workspace = Path("/tmp/should-not-matter")
    with pytest.raises(ValueError, match="invalid filename"):
        restore_state_for_qa(workspace, ".")
    with pytest.raises(ValueError, match="invalid filename"):
        restore_state_for_qa(workspace, "..")


# ---------- end-to-end choreography ----------------------------------------

def test_full_choreography(tmp_path: Path):
    """add() finishes -> freeze_state(). For each QA: restore_state_for_qa,
    use it, discard. Final state dir holds whatever add() left, untouched."""
    workspace = tmp_path / "workspace"
    state = workspace / "state"
    state.mkdir(parents=True)
    (state / "memory").mkdir()
    (state / "memory" / "session-S1-2023.md").write_text("# session 1\n")

    # End-of-add freeze
    freeze_state(workspace)

    # Three QAs run in sequence; each writes "answer.txt" into its dir.
    for qid in ("qa1", "qa2", "qa3"):
        qa = restore_state_for_qa(workspace, qid)
        assert (qa / "memory" / "session-S1-2023.md").read_text() == "# session 1\n"
        (qa / "answer.txt").write_text(f"answer for {qid}")
        discard_qa_state(workspace, qid)

    # Original state untouched.
    assert (state / "memory" / "session-S1-2023.md").read_text() == "# session 1\n"
    assert not (state / "answer.txt").exists()
    # Baseline untouched.
    baseline = workspace / SNAPSHOT_DIRNAME
    assert (baseline / "memory" / "session-S1-2023.md").read_text() == "# session 1\n"
    assert not (baseline / "answer.txt").exists()
    # No QA dirs left.
    qa_root = workspace / QA_STATES_DIRNAME
    assert not qa_root.exists() or list(qa_root.iterdir()) == []

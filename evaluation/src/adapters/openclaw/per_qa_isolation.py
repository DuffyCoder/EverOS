"""Workspace state snapshot/restore for per-QA isolation (PR4 plugin-cli-unify).

Memory-plugin runs already get per-QA isolation "for free" because
their memory store is read-only after add() (see
``evaluation/docs/openclaw_adapter.md``). Context-engine plugins
under R2 routing break this property: ``ContextEngine.afterTurn``
writes the just-finished turn back into per-conversation engine state,
so QA n+1's ``assemble()`` sees QA n's question and answer.

This module gives every QA a fresh copy of the engine state that
add() finalized, so cross-QA leakage is eliminated:

  add() ----------> /workspace/state/                <- engine writes here
                       │
                       │  freeze_state()              (one cp -r at end of add)
                       ▼
                    /workspace/.qa_state_baseline/    <- frozen copy
                       │
                       │  restore_state_for_qa(qid)   (one cp -r per QA)
                       ▼
                    /workspace/.qa_states/<qid>/      <- agent_run uses this
                       │
                       │  discard_qa_state(qid)       (rm after answer)

The path naming is on the host volume that's bind-mounted as
``/workspace`` inside the container, so container code accesses
the same files via ``/workspace/.qa_states/<qid>/``.

Implementation v1 uses ``cp -r`` (via ``shutil.copytree``). v2 could
swap to overlayfs if cost dominates; the public API stays the same.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional


SNAPSHOT_DIRNAME = ".qa_state_baseline"
QA_STATES_DIRNAME = ".qa_states"


def resolve_isolation_mode(
    isolation_setting: Optional[str],
    context_engine_mode: Optional[str],
) -> str:
    """Resolve the configured ``per_qa_isolation`` value.

    - ``"snapshot"`` / ``"off"`` -> returned as-is.
    - ``"auto"`` (default when None) -> ``"snapshot"`` iff
      ``context_engine_mode`` is non-empty, else ``"off"``.

    Memory-plugin-only runs default to ``"off"`` because the
    per-QA isolation is structural; running snapshot adds I/O cost
    without changing answers (see ``evaluation/docs/per_qa_isolation.md``).
    """
    setting = (isolation_setting or "auto").strip().lower()
    if setting in ("snapshot", "off"):
        return setting
    if setting != "auto":
        # Fall through: unknown values are coerced to auto rather than
        # raising. Validation belongs at CLI / config-load time.
        pass
    return "snapshot" if (context_engine_mode and context_engine_mode.strip()) else "off"


def freeze_state(workspace_dir: Path) -> Path:
    """Snapshot ``<workspace>/state/`` to ``<workspace>/.qa_state_baseline/``.

    Rebuilds the snapshot if it already exists. If the source state
    directory does not exist, an empty baseline is created so that
    ``restore_state_for_qa`` always has something to copy from.

    Returns the absolute path of the snapshot directory.
    """
    workspace_dir = Path(workspace_dir)
    state = workspace_dir / "state"
    snapshot = workspace_dir / SNAPSHOT_DIRNAME
    if snapshot.exists():
        shutil.rmtree(snapshot)
    if state.exists():
        shutil.copytree(state, snapshot, symlinks=True, dirs_exist_ok=False)
    else:
        snapshot.mkdir(parents=True)
    return snapshot


def restore_state_for_qa(workspace_dir: Path, qid: str) -> Path:
    """Copy the frozen baseline to a per-QA state dir.

    The per-QA dir is ``<workspace>/.qa_states/<safe_qid>/``. ``qid``
    is sanitized so values containing ``/`` or whitespace don't break
    out of the directory.

    Returns the absolute path of the per-QA state dir. The caller is
    responsible for translating this to a container-side path via
    ``container_state_dir`` and passing it to the bridge as ``state_dir``.
    """
    workspace_dir = Path(workspace_dir)
    baseline = workspace_dir / SNAPSHOT_DIRNAME
    target = workspace_dir / QA_STATES_DIRNAME / _safe_qid(qid)
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if baseline.exists():
        shutil.copytree(baseline, target, symlinks=True, dirs_exist_ok=False)
    else:
        target.mkdir()
    return target


def discard_qa_state(workspace_dir: Path, qid: str) -> None:
    """Remove the per-QA state dir if it exists. No-op otherwise."""
    workspace_dir = Path(workspace_dir)
    target = workspace_dir / QA_STATES_DIRNAME / _safe_qid(qid)
    if target.exists():
        shutil.rmtree(target)


def container_state_dir(qa_state_host: Path, workspace_dir: Path) -> str:
    """Translate a host-side per-QA state dir to its in-container path.

    The container has ``<workspace>`` bind-mounted at ``/workspace``.
    ``Path.as_posix()`` keeps forward slashes regardless of host OS.
    """
    rel = Path(qa_state_host).relative_to(Path(workspace_dir))
    return f"/workspace/{rel.as_posix()}"


def _safe_qid(qid: str) -> str:
    """Sanitize a question id for filesystem use.

    Replaces path separators and whitespace with underscores so callers
    can't escape the per-QA root or collide via implicit normalization.
    """
    safe = (
        qid.replace("/", "_").replace("\\", "_").replace(" ", "_")
    )
    if not safe or safe in (".", ".."):
        raise ValueError(f"qid {qid!r} sanitizes to an invalid filename")
    return safe

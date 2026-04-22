"""Tests for hermes_runtime module."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


def test_ensure_hermes_importable_prepends_repo_to_syspath(tmp_path):
    from evaluation.src.adapters.hermes_runtime import ensure_hermes_importable

    fake_repo = tmp_path / "hermes"
    fake_repo.mkdir()
    # Marker so we can prove sys.path was extended
    (fake_repo / "agent").mkdir()
    (fake_repo / "agent" / "__init__.py").write_text("MARKER = 'hermes-agent'\n")

    original_path = list(sys.path)
    try:
        ensure_hermes_importable(str(fake_repo))
        assert sys.path[0] == str(fake_repo)
    finally:
        sys.path[:] = original_path
        sys.modules.pop("agent", None)


def test_ensure_hermes_importable_idempotent(tmp_path):
    from evaluation.src.adapters.hermes_runtime import ensure_hermes_importable

    fake_repo = tmp_path / "hermes"
    fake_repo.mkdir()

    original_path = list(sys.path)
    try:
        ensure_hermes_importable(str(fake_repo))
        ensure_hermes_importable(str(fake_repo))
        ensure_hermes_importable(str(fake_repo))
        count = sum(1 for p in sys.path if p == str(fake_repo))
        assert count == 1, f"repo should appear once, got {count}"
    finally:
        sys.path[:] = original_path


def test_ensure_hermes_importable_rejects_missing_repo(tmp_path):
    from evaluation.src.adapters.hermes_runtime import ensure_hermes_importable

    with pytest.raises(ValueError, match="repo_path"):
        ensure_hermes_importable("")

    with pytest.raises(FileNotFoundError):
        ensure_hermes_importable(str(tmp_path / "does-not-exist"))

"""Tests for evaluation.src.adapters.hermes.runtime."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest


def test_ensure_hermes_importable_prepends_repo_to_syspath(tmp_path):
    from evaluation.src.adapters.hermes.runtime import ensure_hermes_importable

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
    from evaluation.src.adapters.hermes.runtime import ensure_hermes_importable

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
    from evaluation.src.adapters.hermes.runtime import ensure_hermes_importable

    with pytest.raises(ValueError, match="repo_path"):
        ensure_hermes_importable("")

    with pytest.raises(FileNotFoundError):
        ensure_hermes_importable(str(tmp_path / "does-not-exist"))


def test_hermes_home_env_sets_and_restores(tmp_path, monkeypatch):
    from evaluation.src.adapters.hermes.runtime import hermes_home_env

    monkeypatch.setenv("HERMES_HOME", "/old")
    with hermes_home_env(str(tmp_path)):
        assert os.environ["HERMES_HOME"] == str(tmp_path)
    assert os.environ["HERMES_HOME"] == "/old"


def test_hermes_home_env_restores_when_unset_before(tmp_path, monkeypatch):
    from evaluation.src.adapters.hermes.runtime import hermes_home_env

    monkeypatch.delenv("HERMES_HOME", raising=False)
    with hermes_home_env(str(tmp_path)):
        assert os.environ["HERMES_HOME"] == str(tmp_path)
    assert "HERMES_HOME" not in os.environ


def test_hermes_executor_runs_callables():
    from evaluation.src.adapters.hermes.runtime import HermesExecutor

    executor = HermesExecutor()

    async def go():
        return await executor.run(lambda: 1 + 2)

    try:
        result = asyncio.run(go())
    finally:
        executor.shutdown()
    assert result == 3


def test_hermes_executor_propagates_exceptions():
    from evaluation.src.adapters.hermes.runtime import HermesExecutor

    executor = HermesExecutor()

    def boom():
        raise RuntimeError("provider exploded")

    async def go():
        return await executor.run(boom)

    try:
        with pytest.raises(RuntimeError, match="provider exploded"):
            asyncio.run(go())
    finally:
        executor.shutdown()

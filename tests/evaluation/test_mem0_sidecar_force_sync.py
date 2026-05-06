"""Regression test for the mem0 sidecar /sync endpoint's force semantics.

The bridge sends `force: true` on every index call to emulate the
old `openclaw memory index --force` behavior. The sidecar previously
ignored that flag and only appended, so a checkpoint resume / retry
re-running /sync would duplicate every memory in the persistent Chroma
store. The fix: when force=true, the sidecar must call
`memory.delete_all(user_id=user_id)` before re-ingesting session files.

Tests use FastAPI's TestClient + a fake Memory injected into the
server module's global, so we never have to install mem0 / chromadb /
sentence-transformers in the eval test env.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest


SIDECAR_DIR = (
    Path(__file__).resolve().parents[2]
    / "openclaw-eval"
    / "plugins"
    / "mem0"
    / "sidecar"
)


@pytest.fixture
def server_module(monkeypatch):
    """Import openclaw-eval/plugins/mem0/sidecar/server.py as a module.
    The path isn't on sys.path normally; we add it for the test only."""
    monkeypatch.syspath_prepend(str(SIDECAR_DIR))
    if "server" in sys.modules:
        del sys.modules["server"]
    mod = importlib.import_module("server")
    yield mod
    if "server" in sys.modules:
        del sys.modules["server"]


class FakeMemory:
    """Minimal Memory stand-in that records call order so we can assert
    delete_all happens before any subsequent add calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.delete_all_should_raise: Exception | None = None

    def delete_all(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("delete_all", kwargs))
        if self.delete_all_should_raise is not None:
            raise self.delete_all_should_raise
        return {"status": "Success"}

    def add(self, content: str, **kwargs: Any) -> None:
        self.calls.append(("add", {"content": content, **kwargs}))


@pytest.fixture
def client_with_files(server_module, monkeypatch, tmp_path):
    """TestClient + fake memory + a workspace with two session files."""
    workspace = tmp_path / "workspace"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "session_001.md").write_text("turn one")
    (memory_dir / "session_002.md").write_text("turn two")

    monkeypatch.setenv("WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("MEM0_DEFAULT_USER_ID", "openclaw")

    fake = FakeMemory()
    monkeypatch.setattr(server_module, "_memory", fake, raising=False)

    from fastapi.testclient import TestClient
    return TestClient(server_module.app), fake


def _kinds(calls: list[tuple[str, Any]]) -> list[str]:
    return [k for k, _ in calls]


def test_force_true_deletes_then_re_adds(client_with_files):
    """force=true → delete_all(user_id=...) BEFORE any add(). Mirrors
    the rebuild semantics of `openclaw memory index --force`."""
    client, fake = client_with_files
    resp = client.post(
        "/sync",
        json={
            "reason": "eval_index",
            "force": True,
            "session_files": ["memory/session_001.md", "memory/session_002.md"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["ingested"] == 2
    # Order matters: delete_all must precede every add.
    assert _kinds(fake.calls) == ["delete_all", "add", "add"]
    # delete_all must be scoped to the per-conv user_id.
    assert fake.calls[0][1] == {"user_id": "openclaw"}


def test_force_false_appends_without_reset(client_with_files):
    """force=false (or missing) preserves the original append behavior —
    no delete_all is invoked."""
    client, fake = client_with_files
    resp = client.post(
        "/sync",
        json={
            "reason": "eval_index",
            "force": False,
            "session_files": ["memory/session_001.md"],
        },
    )
    assert resp.status_code == 200
    assert "delete_all" not in _kinds(fake.calls)
    assert _kinds(fake.calls) == ["add"]


def test_force_omitted_defaults_to_no_reset(client_with_files):
    """Defensive: a /sync request without force in the body must NOT
    reset (default False per SyncRequest schema)."""
    client, fake = client_with_files
    resp = client.post(
        "/sync",
        json={"session_files": ["memory/session_001.md"]},
    )
    assert resp.status_code == 200
    assert "delete_all" not in _kinds(fake.calls)


def test_force_true_with_failing_delete_returns_ok_false(client_with_files):
    """If delete_all raises under force=true we must NOT silently fall
    through to append (which would re-introduce duplicates). Surface
    the failure so the bridge's caller can react."""
    client, fake = client_with_files
    fake.delete_all_should_raise = RuntimeError("chroma collection locked")
    resp = client.post(
        "/sync",
        json={
            "reason": "eval_index",
            "force": True,
            "session_files": ["memory/session_001.md"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["ingested"] == 0
    assert "force-reset failed" in body["error"]
    # No add should have been attempted after the failed reset.
    assert "add" not in _kinds(fake.calls)


def test_force_true_with_no_session_files_still_resets(client_with_files):
    """Edge: force=true with empty session_files (e.g. operator triggered
    a re-index without staging new files) still wipes the store. Useful
    as a 'clear memory' primitive."""
    client, fake = client_with_files
    resp = client.post(
        "/sync",
        json={"reason": "manual_reset", "force": True, "session_files": []},
    )
    assert resp.status_code == 200
    assert resp.json()["ingested"] == 0
    assert _kinds(fake.calls) == ["delete_all"]

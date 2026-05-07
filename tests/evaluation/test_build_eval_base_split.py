"""Unit tests for the eval-base / eval-plugin split-build scaffolding.

The split-build pipeline lets the team push a single plugin-agnostic
eval-base image to a shared registry and have each evaluator build the
plugin layer locally. These tests cover the pure-Python pieces — Docker
invocations are mocked or skipped (the integration path is exercised by
``openclaw-eval/scripts/push_eval_base.sh`` against a real daemon).

Run:
    .venv/bin/python -m pytest tests/evaluation/test_build_eval_base_split.py -v
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_PY = REPO_ROOT / "openclaw-eval" / "harness" / "build.py"
EVAL_DIR = REPO_ROOT / "openclaw-eval"


def _import_build():
    spec = importlib.util.spec_from_file_location("openclaw_eval_build_split", BUILD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- eval_base_rev


def test_eval_base_rev_is_deterministic():
    """Same inputs → same rev (lets cache hits work across machines)."""
    build = _import_build()
    a = build.eval_base_rev(EVAL_DIR)
    b = build.eval_base_rev(EVAL_DIR)
    assert a == b
    assert len(a) == 7
    assert all(c in "0123456789abcdef" for c in a)


def test_eval_base_rev_changes_when_bridge_changes(tmp_path):
    """Mutating the bridge source must invalidate the cached rev so
    framework upgrades produce a fresh image tag."""
    build = _import_build()
    fake_eval = tmp_path / "openclaw-eval"
    (fake_eval / "container").mkdir(parents=True)
    (fake_eval / "Dockerfile.eval-base").write_text("FROM scratch\n")
    (fake_eval / "container" / "openclaw.template.json").write_text("{}\n")
    (fake_eval / "container" / "entrypoint.sh").write_text("#!/bin/sh\n")
    (fake_eval / "container" / "openclaw_eval_bridge.mjs").write_text("// v1\n")
    (fake_eval / "container" / "openclaw_eval_bridge_lib.mjs").write_text("// lib\n")

    rev_before = build.eval_base_rev(fake_eval)
    (fake_eval / "container" / "openclaw_eval_bridge.mjs").write_text("// v2\n")
    rev_after = build.eval_base_rev(fake_eval)

    assert rev_before != rev_after, "bridge mutation must change rev"


def test_eval_base_rev_handles_missing_files(tmp_path):
    """Missing inputs are skipped silently (gives a stable rev for empty
    eval dirs rather than crashing)."""
    build = _import_build()
    empty = tmp_path / "empty"
    empty.mkdir()
    rev = build.eval_base_rev(empty)
    assert len(rev) == 7


# ------------------------------------------------ argparse / split-mode validation


def _run_build_args(*args: str) -> subprocess.CompletedProcess:
    """Invoke build.py with given args; do NOT execute docker (early
    validation should reject before reaching the docker steps)."""
    return subprocess.run(
        [sys.executable, str(BUILD_PY), *args],
        capture_output=True, text=True,
    )


def test_push_eval_base_requires_build_eval_base():
    res = _run_build_args("--push-eval-base", "--registry", "ghcr.io/foo")
    assert res.returncode != 0
    assert "--push-eval-base requires --build-eval-base" in res.stderr


def test_push_eval_base_requires_registry():
    res = _run_build_args("--build-eval-base", "--push-eval-base")
    assert res.returncode != 0
    assert "--push-eval-base requires --registry" in res.stderr


def test_eval_base_image_and_build_eval_base_are_mutually_exclusive():
    res = _run_build_args(
        "--build-eval-base",
        "--eval-base-image", "ghcr.io/foo/openclaw-eval-base:abc-clean-XXX-slim",
    )
    assert res.returncode != 0
    assert "mutually exclusive" in res.stderr


def test_eval_base_image_requires_install_spec():
    """Pre-built eval-base only works for install-spec mode (bundled
    plugins still need to bake into openclaw-base, not the eval-base layer)."""
    res = _run_build_args(
        "--eval-base-image", "ghcr.io/foo/openclaw-eval-base:abc-clean-XXX-slim",
        "--memory-plugin", "stub-engine",
    )
    assert res.returncode != 0
    assert "only supports install-spec mode" in res.stderr


# -------------------------------------------------------------- push_eval_base


def test_push_eval_base_rejects_malformed_tag(monkeypatch):
    build = _import_build()
    with __import__("pytest").raises(SystemExit, match="malformed eval-base tag"):
        build.push_eval_base("invalidtag-no-colon", "ghcr.io/foo")


def test_push_eval_base_constructs_remote_tag(monkeypatch):
    """Verify remote_tag is composed correctly without actually invoking docker."""
    build = _import_build()
    captured: list[list[str]] = []

    def fake_run_step(label, cmd, cwd=None):
        captured.append(cmd)

    monkeypatch.setattr(build, "run_step", fake_run_step)
    remote = build.push_eval_base(
        "openclaw-eval-base:7da23c3-clean-abcd123-slim",
        "ghcr.io/duffycoder",
    )
    assert remote == "ghcr.io/duffycoder/openclaw-eval-base:7da23c3-clean-abcd123-slim"
    # First call: docker tag <local> <remote>; second: docker push <remote>
    assert captured[0] == [
        "docker", "tag",
        "openclaw-eval-base:7da23c3-clean-abcd123-slim",
        remote,
    ]
    assert captured[1] == ["docker", "push", remote]


def test_push_eval_base_strips_trailing_slash():
    build = _import_build()
    captured: list[list[str]] = []
    import unittest.mock
    with unittest.mock.patch.object(build, "run_step", lambda *a, **k: captured.append(a[1])):
        remote = build.push_eval_base(
            "openclaw-eval-base:abc-clean-XXX-slim",
            "ghcr.io/duffycoder/",  # trailing slash
        )
    assert remote == "ghcr.io/duffycoder/openclaw-eval-base:abc-clean-XXX-slim"


# ------------------------------------------------- Dockerfile structural sanity


def test_eval_base_dockerfile_exists_and_uses_BASE_IMAGE_arg():
    df = EVAL_DIR / "Dockerfile.eval-base"
    assert df.exists(), "Dockerfile.eval-base must be present for split builds"
    text = df.read_text()
    assert "ARG BASE_IMAGE=openclaw-base:latest" in text
    assert "FROM ${BASE_IMAGE}" in text
    # Plugin-specific things must NOT appear in the base layer.
    assert "INSTALL_SPEC" not in text, \
        "Dockerfile.eval-base must be plugin-agnostic (no INSTALL_SPEC)"
    assert "_active_sidecar" not in text, \
        "Dockerfile.eval-base must not pre-stage a sidecar"


def test_eval_plugin_dockerfile_exists_and_layers_on_eval_base():
    df = EVAL_DIR / "Dockerfile.eval-plugin"
    assert df.exists()
    text = df.read_text()
    assert "ARG BASE_IMAGE" in text
    assert "FROM ${BASE_IMAGE}" in text
    # All plugin-specific operations live here:
    assert "INSTALL_SPEC" in text
    assert "_active_sidecar/" in text
    assert "/opt/openclaw/extensions/" in text
    assert "chown -R root:root /opt/openclaw/extensions" in text

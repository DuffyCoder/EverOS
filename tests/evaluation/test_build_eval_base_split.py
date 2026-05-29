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
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_PY = REPO_ROOT / "openclaw-eval" / "harness" / "build.py"
EVAL_DIR = REPO_ROOT / "openclaw-eval"


def _import_build():
    spec = importlib.util.spec_from_file_location("openclaw_eval_build_split", BUILD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Cache the imported module across tests — every previous version reloaded
# build.py for each helper call (5+ test methods × ~20ms re-exec = ~100ms
# saved, but more importantly side-effect-free reuse).
_BUILD = _import_build()


def _call_main_expect_exit(monkeypatch, capsys, *args: str) -> str:
    """Invoke build.main() in-process with mocked argv. Returns captured
    stderr. Replaces a subprocess.run() per validation assertion (~150ms
    interpreter spawn each); now ~1ms per call.
    """
    monkeypatch.setattr("sys.argv", ["build.py", *args])
    with pytest.raises(SystemExit) as exc_info:
        _BUILD.main()
    assert exc_info.value.code != 0, "expected non-zero exit"
    return capsys.readouterr().err


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


def test_push_eval_base_requires_build_eval_base(monkeypatch, capsys):
    err = _call_main_expect_exit(
        monkeypatch, capsys,
        "--push-eval-base", "--registry", "ghcr.io/foo",
    )
    assert "--push-eval-base requires --build-eval-base" in err


def test_push_eval_base_requires_registry(monkeypatch, capsys):
    err = _call_main_expect_exit(
        monkeypatch, capsys,
        "--build-eval-base", "--push-eval-base",
    )
    assert "--push-eval-base requires --registry" in err


def test_eval_base_image_and_build_eval_base_are_mutually_exclusive(
    monkeypatch, capsys
):
    err = _call_main_expect_exit(
        monkeypatch, capsys,
        "--build-eval-base",
        "--eval-base-image", "ghcr.io/foo/openclaw-eval-base:abc-clean-XXX-slim",
    )
    assert "mutually exclusive" in err


def test_eval_base_image_requires_install_spec(monkeypatch, capsys):
    """Pre-built eval-base only works for install-spec mode (bundled
    plugins still need to bake into openclaw-base, not the eval-base layer)."""
    err = _call_main_expect_exit(
        monkeypatch, capsys,
        "--eval-base-image", "ghcr.io/foo/openclaw-eval-base:abc-clean-XXX-slim",
        "--memory-plugin", "stub-engine",
    )
    assert "only supports install-spec mode" in err


# -------------------------------------------------------------- push_eval_base


def test_push_eval_base_rejects_malformed_tag():
    with pytest.raises(SystemExit, match="malformed eval-base tag"):
        _BUILD.push_eval_base("invalidtag-no-colon", "ghcr.io/foo")


def test_push_eval_base_constructs_remote_tag(monkeypatch):
    """Verify remote_tag is composed correctly without invoking docker."""
    captured: list[list[str]] = []
    monkeypatch.setattr(
        _BUILD, "run_step",
        lambda label, cmd, cwd=None: captured.append(cmd),
    )
    remote = _BUILD.push_eval_base(
        "openclaw-eval-base:7da23c3-clean-abcd123-slim",
        "ghcr.io/duffycoder",
    )
    assert remote == "ghcr.io/duffycoder/openclaw-eval-base:7da23c3-clean-abcd123-slim"
    assert captured[0] == [
        "docker", "tag",
        "openclaw-eval-base:7da23c3-clean-abcd123-slim",
        remote,
    ]
    assert captured[1] == ["docker", "push", remote]


def test_push_eval_base_strips_trailing_slash(monkeypatch):
    captured: list[list[str]] = []
    monkeypatch.setattr(
        _BUILD, "run_step",
        lambda label, cmd, cwd=None: captured.append(cmd),
    )
    remote = _BUILD.push_eval_base(
        "openclaw-eval-base:abc-clean-XXX-slim",
        "ghcr.io/duffycoder/",  # trailing slash
    )
    assert remote == "ghcr.io/duffycoder/openclaw-eval-base:abc-clean-XXX-slim"


# ---------------------------------------------------------------- eval_layer_tag


def test_eval_layer_tag_install_mode():
    """Single source of truth for install-mode tag shape: prevents the
    build_eval_layer / build_eval_plugin_layer paths from drifting."""
    tag = _BUILD.eval_layer_tag(
        openclaw_sha="7da23c3",
        memory_plugin="memory-core",
        plugin_rev="abcd123",
        variant="slim",
        install_plugin_id="openviking",
    )
    assert tag == "openclaw-eval:7da23c3-install-openviking-abcd123-slim"


def test_eval_layer_tag_install_mode_with_extras():
    tag = _BUILD.eval_layer_tag(
        openclaw_sha="7da23c3",
        memory_plugin="memory-core",
        plugin_rev="abcd123",
        variant="slim",
        install_plugin_id="hypercompositor",
        extra_count=2,
    )
    assert tag == "openclaw-eval:7da23c3-install-hypercompositor-x2-abcd123-slim"


def test_eval_layer_tag_bundled_mode():
    tag = _BUILD.eval_layer_tag(
        openclaw_sha="7da23c3",
        memory_plugin="stub-engine",
        plugin_rev="0badf00",
        variant="slim",
    )
    assert tag == "openclaw-eval:7da23c3-stub-engine-0badf00-slim"


# ---------------------------------------------------------------- _hash_files_to_rev


def test_hash_files_to_rev_skips_missing_silently(tmp_path):
    """eval_base_rev relies on this — partial trees must not crash."""
    rev = _BUILD._hash_files_to_rev([
        ("missing.txt", tmp_path / "missing.txt"),
    ])
    assert len(rev) == 7
    assert all(c in "0123456789abcdef" for c in rev)


def test_hash_files_to_rev_is_order_sensitive(tmp_path):
    """Same files in different order yield different revs (caller controls
    ordering by sorting upfront — protects against silent rev collisions)."""
    a = tmp_path / "a"; a.write_text("alpha")
    b = tmp_path / "b"; b.write_text("beta")
    rev_ab = _BUILD._hash_files_to_rev([("a", a), ("b", b)])
    rev_ba = _BUILD._hash_files_to_rev([("b", b), ("a", a)])
    assert rev_ab != rev_ba


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

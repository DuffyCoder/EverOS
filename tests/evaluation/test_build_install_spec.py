"""Unit tests for build.py --install-spec scaffolding.

Covers:
  - install_spec_hash determinism + length
  - derive_plugin_id_from_spec for each spec format
  - argparse / validation behavior (memory-plugin must match install id;
    bundled ids rejected; missing id when underivable rejected)

Run:
    .venv/bin/python -m pytest tests/evaluation/test_build_install_spec.py -v
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_PY = REPO_ROOT / "openclaw-eval" / "harness" / "build.py"


def _import_build():
    """Import build.py as a module without going through package paths
    (openclaw-eval has a hyphen, can't be imported as a regular package).
    """
    spec = importlib.util.spec_from_file_location("openclaw_eval_build", BUILD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_spec_hash_is_deterministic():
    build = _import_build()
    a = build.install_spec_hash("npm:@mem0/openclaw-plugin@1.2.0")
    b = build.install_spec_hash("npm:@mem0/openclaw-plugin@1.2.0")
    assert a == b
    assert len(a) == 7


def test_install_spec_hash_differs_per_version():
    build = _import_build()
    h1 = build.install_spec_hash("npm:@mem0/openclaw-plugin@1.2.0")
    h2 = build.install_spec_hash("npm:@mem0/openclaw-plugin@1.2.1")
    assert h1 != h2


def test_derive_plugin_id_npm_scoped():
    build = _import_build()
    assert build.derive_plugin_id_from_spec("npm:@mem0/openclaw-plugin@1.2.0") == "openclaw-plugin"


def test_derive_plugin_id_npm_unscoped():
    build = _import_build()
    assert build.derive_plugin_id_from_spec("npm:hindsight-plugin@0.5.1") == "hindsight-plugin"


def test_derive_plugin_id_npm_no_version():
    build = _import_build()
    assert build.derive_plugin_id_from_spec("npm:hindsight-plugin") == "hindsight-plugin"


def test_derive_plugin_id_clawhub():
    build = _import_build()
    assert build.derive_plugin_id_from_spec("clawhub:owner/cool-plugin") == "cool-plugin"


def test_derive_plugin_id_marketplace():
    build = _import_build()
    assert build.derive_plugin_id_from_spec("marketplace:my-engine") == "my-engine"


def test_derive_plugin_id_unknown_returns_none():
    build = _import_build()
    assert build.derive_plugin_id_from_spec("./local-path") is None
    assert build.derive_plugin_id_from_spec("/abs/path/to/plugin.zip") is None


def _run_build(*extra_args, expect_exit: int = 0):
    """Run build.py main() in a subprocess so argparse + sys.exit() runs
    naturally. Stops before docker invocation by intentionally pointing
    --openclaw-repo at a path with no Dockerfile when args are otherwise
    valid; this tests argparse + validation only.
    """
    cmd = [sys.executable, str(BUILD_PY), *extra_args]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    return res


def test_install_spec_requires_matching_memory_plugin():
    res = _run_build(
        "--memory-plugin", "evermemos",
        "--install-spec", "npm:hindsight-plugin@0.5.0",
    )
    assert res.returncode != 0
    assert "must match installed plugin id" in res.stderr


def test_install_spec_rejects_bundled_id():
    res = _run_build(
        "--memory-plugin", "memory-core",
        "--install-spec", "npm:memory-core@1.0.0",
    )
    assert res.returncode != 0
    assert "not supported" in res.stderr or "bundled plugin" in res.stderr


def test_install_spec_underivable_id_requires_explicit():
    # raw path can't be parsed; without --install-plugin-id should error
    res = _run_build(
        "--memory-plugin", "anything",
        "--install-spec", "/absolute/path/plugin.zip",
    )
    assert res.returncode != 0
    assert "could not derive a plugin id" in res.stderr


def test_install_spec_explicit_id_overrides_derivation():
    """Even when spec is parseable, --install-plugin-id wins. Uses an
    invalid openclaw-repo path so the build aborts before docker but
    after argparse/validation, proving args were accepted.
    """
    res = _run_build(
        "--memory-plugin", "custom-id",
        "--install-spec", "npm:hindsight-plugin@0.5.0",
        "--install-plugin-id", "custom-id",
        "--openclaw-repo", "/nonexistent/path",
    )
    # Validation passes; build aborts later (no Dockerfile at fake path).
    assert res.returncode != 0
    assert "must match installed plugin id" not in res.stderr
    assert "Dockerfile not found" in res.stderr or "openclaw-repo" in res.stderr.lower()

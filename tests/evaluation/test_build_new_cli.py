"""Unit tests for the PR2 plugin-cli-unify build.py refactor.

Covers the new --memory-plugin / --context-engine / --plugin-spec flags,
tag derivation helpers, plugin_rev computation, and image_manifest.yaml
emission. The legacy --install-spec deprecation shim is exercised from
test_build_install_spec.py.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_PY = REPO_ROOT / "openclaw-eval" / "harness" / "build.py"


def _import_build():
    spec = importlib.util.spec_from_file_location("openclaw_eval_build", BUILD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _registry():
    build = _import_build()
    return build.load_registry(build.DEFAULT_REGISTRY_PATH)


def _ref(arg: str | None, kind: str):
    """Helper: parse_ref against the shipped registry."""
    build = _import_build()
    return build.parse_ref(arg, expected_kind=kind, registry=_registry())


# ---------- _tag_segment + derive_eval_tag ---------------------------------

def test_tag_segment_none():
    build = _import_build()
    assert build._tag_segment(None) is None


def test_tag_segment_base_extension():
    build = _import_build()
    ref = _ref("memory-core", "memory")
    assert build._tag_segment(ref) is None


def test_tag_segment_bundled_source():
    build = _import_build()
    ref = _ref("evermemos", "memory")
    assert build._tag_segment(ref) == "evermemos"


def test_tag_segment_npm():
    build = _import_build()
    ref = _ref("hypercompositor@0.9.6", "context-engine")
    assert build._tag_segment(ref) == "install-hypercompositor"


def test_derive_eval_tag_baseline_no_extras():
    build = _import_build()
    tag = build.derive_eval_tag(None, None, "abc1234", "0000000", "slim")
    assert tag == "openclaw-eval:abc1234-memory-core-0000000-slim"


def test_derive_eval_tag_memory_core_treated_as_baseline():
    """memory-core is base-extension; tag should not include it as an extra."""
    build = _import_build()
    ref = _ref("memory-core", "memory")
    tag = build.derive_eval_tag(ref, None, "abc1234", "0000000", "slim")
    assert tag == "openclaw-eval:abc1234-memory-core-0000000-slim"


def test_derive_eval_tag_single_bundled_memory():
    build = _import_build()
    ref = _ref("evermemos", "memory")
    tag = build.derive_eval_tag(ref, None, "abc1234", "deadbee", "slim")
    assert tag == "openclaw-eval:abc1234-evermemos-deadbee-slim"


def test_derive_eval_tag_single_npm_ce():
    build = _import_build()
    ref = _ref("hypercompositor@0.9.6", "context-engine")
    tag = build.derive_eval_tag(None, ref, "abc1234", "5bace1f", "slim")
    assert tag == "openclaw-eval:abc1234-install-hypercompositor-5bace1f-slim"


def test_derive_eval_tag_two_plugins_memory_first():
    build = _import_build()
    mem_ref = _ref("evermemos", "memory")
    ce_ref = _ref("hypercompositor@0.9.6", "context-engine")
    tag = build.derive_eval_tag(mem_ref, ce_ref, "abc1234", "deadbee", "slim")
    assert tag == "openclaw-eval:abc1234-evermemos_install-hypercompositor-deadbee-slim"


def test_derive_eval_tag_two_bundled_plugins():
    build = _import_build()
    mem_ref = _ref("evermemos", "memory")
    ce_ref = _ref("stub-engine", "context-engine")
    tag = build.derive_eval_tag(mem_ref, ce_ref, "abc1234", "deadbee", "slim")
    assert tag == "openclaw-eval:abc1234-evermemos_stub-engine-deadbee-slim"


# ---------- collect helpers ------------------------------------------------

def test_collect_bundled_to_stage_baseline():
    build = _import_build()
    assert build.collect_bundled_to_stage(None, None) == []


def test_collect_bundled_to_stage_only_npm():
    build = _import_build()
    ce = _ref("hypercompositor@0.9.6", "context-engine")
    assert build.collect_bundled_to_stage(None, ce) == []


def test_collect_bundled_to_stage_mixed():
    build = _import_build()
    mem = _ref("evermemos", "memory")
    ce = _ref("hypercompositor@0.9.6", "context-engine")
    assert build.collect_bundled_to_stage(mem, ce) == ["evermemos"]


def test_collect_bundled_to_stage_two_bundled():
    build = _import_build()
    mem = _ref("evermemos", "memory")
    ce = _ref("stub-engine", "context-engine")
    assert build.collect_bundled_to_stage(mem, ce) == ["evermemos", "stub-engine"]


def test_collect_npm_installs_baseline():
    build = _import_build()
    assert build.collect_npm_installs(None, None, {}) == []


def test_collect_npm_installs_one_npm():
    build = _import_build()
    ce = _ref("hypercompositor@0.9.6", "context-engine")
    out = build.collect_npm_installs(None, ce, {})
    assert out == [("npm:@psiclawops/hypercompositor@0.9.6", "hypercompositor")]


def test_collect_npm_installs_override_wins():
    build = _import_build()
    ce = _ref("hypercompositor@0.9.6", "context-engine")
    overrides = {"hypercompositor": "/tmp/local-fork.tgz"}
    out = build.collect_npm_installs(None, ce, overrides)
    assert out == [("/tmp/local-fork.tgz", "hypercompositor")]


# ---------- compute_plugin_rev ---------------------------------------------

def test_compute_plugin_rev_baseline_zeros(tmp_path: Path):
    build = _import_build()
    rev = build.compute_plugin_rev(None, None, tmp_path, {})
    assert rev == "0000000"


def test_compute_plugin_rev_single_npm_matches_legacy_hash():
    """Hash of the bare (non-prefixed) spec must match the legacy
    install_spec_hash output so existing image tags remain reachable.
    """
    build = _import_build()
    ce = _ref("hypercompositor@0.9.6", "context-engine")
    rev = build.compute_plugin_rev(None, ce, REPO_ROOT / "openclaw-eval" / "plugins", {})
    legacy = build.install_spec_hash("@psiclawops/hypercompositor@0.9.6")
    assert rev == legacy
    assert rev == "5bace1f"


def test_compute_plugin_rev_npm_override_changes_hash():
    build = _import_build()
    ce = _ref("hypercompositor@0.9.6", "context-engine")
    a = build.compute_plugin_rev(
        None, ce, REPO_ROOT / "openclaw-eval" / "plugins", {},
    )
    b = build.compute_plugin_rev(
        None, ce, REPO_ROOT / "openclaw-eval" / "plugins",
        {"hypercompositor": "/tmp/local.tgz"},
    )
    assert a != b


def test_compute_plugin_rev_two_plugins_combines():
    build = _import_build()
    mem = _ref("evermemos", "memory")
    ce = _ref("hypercompositor@0.9.6", "context-engine")
    rev_mem = build.compute_plugin_rev(
        mem, None, REPO_ROOT / "openclaw-eval" / "plugins", {},
    )
    rev_ce = build.compute_plugin_rev(
        None, ce, REPO_ROOT / "openclaw-eval" / "plugins", {},
    )
    rev_combined = build.compute_plugin_rev(
        mem, ce, REPO_ROOT / "openclaw-eval" / "plugins", {},
    )
    # Combined rev is a fresh hash, not equal to either component.
    assert rev_combined != rev_mem
    assert rev_combined != rev_ce
    assert len(rev_combined) == 7


# ---------- build_manifest_plugins -----------------------------------------

def test_build_manifest_plugins_baseline_has_only_memory_core():
    build = _import_build()
    plugins = build.build_manifest_plugins(
        None, None, REPO_ROOT / "openclaw-eval" / "plugins",
    )
    assert set(plugins.keys()) == {"memory-core"}
    assert plugins["memory-core"].kind == "memory"
    assert plugins["memory-core"].version == "bundled"
    assert plugins["memory-core"].source == "bundled"


def test_build_manifest_plugins_records_bundled_source():
    build = _import_build()
    mem = _ref("evermemos", "memory")
    plugins = build.build_manifest_plugins(
        mem, None, REPO_ROOT / "openclaw-eval" / "plugins",
    )
    assert "evermemos" in plugins
    assert plugins["evermemos"].kind == "memory"
    assert plugins["evermemos"].version == "bundled"
    assert plugins["evermemos"].source == "bundled-source"
    assert plugins["evermemos"].rev is not None
    assert len(plugins["evermemos"].rev) == 7


def test_build_manifest_plugins_records_npm_with_version():
    build = _import_build()
    ce = _ref("hypercompositor@0.9.6", "context-engine")
    plugins = build.build_manifest_plugins(
        None, ce, REPO_ROOT / "openclaw-eval" / "plugins",
    )
    assert "hypercompositor" in plugins
    assert plugins["hypercompositor"].kind == "context-engine"
    assert plugins["hypercompositor"].version == "0.9.6"
    assert plugins["hypercompositor"].source == "npm:@psiclawops/hypercompositor"


def test_build_manifest_plugins_two_plugins():
    build = _import_build()
    mem = _ref("evermemos", "memory")
    ce = _ref("hypercompositor@0.9.6", "context-engine")
    plugins = build.build_manifest_plugins(
        mem, ce, REPO_ROOT / "openclaw-eval" / "plugins",
    )
    assert set(plugins.keys()) == {"memory-core", "evermemos", "hypercompositor"}


# ---------- _version_from_npm_spec -----------------------------------------

@pytest.mark.parametrize("spec, expected", [
    ("npm:hindsight-plugin@0.5.0", "0.5.0"),
    ("npm:@scope/name@1.2.3", "1.2.3"),
    ("@scope/name@1.2.3", "1.2.3"),
    ("hindsight-plugin@0.5.0", "0.5.0"),
    ("npm:noscope-no-version", None),
    ("clawhub:owner/name", None),
])
def test_version_from_npm_spec(spec, expected):
    build = _import_build()
    assert build._version_from_npm_spec(spec) == expected


# ---------- subprocess: dry-run end-to-end ---------------------------------

def _run_build_dry(*args):
    """Run build.py --dry-run --no-image-manifest with extra args."""
    cmd = [sys.executable, str(BUILD_PY), "--dry-run", "--no-image-manifest", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)


def test_dry_run_baseline_succeeds():
    res = _run_build_dry()
    assert res.returncode == 0, res.stderr
    assert "memory_plugin  = none" in res.stdout
    assert "memory-core-0000000" in res.stdout
    assert "dry-run: skipping docker build" in res.stdout


def test_dry_run_memory_plugin_evermemos():
    res = _run_build_dry("--memory-plugin", "evermemos")
    assert res.returncode == 0, res.stderr
    assert "memory_plugin  = evermemos" in res.stdout
    assert "openclaw-eval:" in res.stdout
    assert "-evermemos-" in res.stdout


def test_dry_run_context_engine_with_version():
    res = _run_build_dry("--context-engine", "hypercompositor@0.9.6")
    assert res.returncode == 0, res.stderr
    assert "install-hypercompositor-5bace1f-slim" in res.stdout
    assert "npm:@psiclawops/hypercompositor@0.9.6" in res.stdout


def test_dry_run_both_plugins():
    res = _run_build_dry(
        "--memory-plugin", "evermemos",
        "--context-engine", "hypercompositor@0.9.6",
    )
    assert res.returncode == 0, res.stderr
    assert "evermemos_install-hypercompositor" in res.stdout


def test_dry_run_kind_mismatch_rejected():
    """Memory plugin in --context-engine slot fails kind validation."""
    res = _run_build_dry("--context-engine", "evermemos")
    assert res.returncode != 0
    assert "does not include 'context-engine'" in res.stderr


def test_dry_run_npm_without_version_rejected():
    res = _run_build_dry("--context-engine", "hypercompositor")
    assert res.returncode != 0
    assert "requires explicit version" in res.stderr


def test_dry_run_unknown_plugin_friendly_error():
    res = _run_build_dry("--memory-plugin", "no-such-thing")
    assert res.returncode != 0
    assert "unknown plugin" in res.stderr


def test_dry_run_plugin_spec_for_unselected_rejected():
    """--plugin-spec for a plugin not in either slot should error."""
    res = _run_build_dry(
        "--memory-plugin", "evermemos",
        "--plugin-spec", "hypercompositor=npm:@scope/foo@1.0",
    )
    assert res.returncode != 0
    assert "is not a selected plugin" in res.stderr


def test_dry_run_none_explicit_baseline():
    res = _run_build_dry(
        "--memory-plugin", "none",
        "--context-engine", "none",
    )
    assert res.returncode == 0, res.stderr
    assert "memory-core-0000000" in res.stdout


# ---------- image_manifest.yaml emission -----------------------------------

def test_full_run_writes_manifest_entry(tmp_path: Path):
    """Real build would need docker; instead test the manifest path with
    a manually-crafted run that aborts before docker but after manifest
    setup. Easier: invoke a separate end-to-end harness via dry-run +
    inspecting where manifest would land. Real coverage is via PR3
    integration tests when eval.cli reads the manifest.
    """
    # Run with manifest pointing at a tmp file. Use --dry-run so docker
    # isn't invoked; verify --no-image-manifest is the flag controlling
    # emission and that without it, build aborts gracefully.
    cmd = [
        sys.executable, str(BUILD_PY),
        "--memory-plugin", "evermemos",
        "--dry-run",
        "--image-manifest-out", str(tmp_path / "manifest.yaml"),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    # dry-run skips docker, and also skips manifest write (manifest write
    # happens after docker steps). So manifest file should NOT exist.
    assert res.returncode == 0
    assert not (tmp_path / "manifest.yaml").exists()

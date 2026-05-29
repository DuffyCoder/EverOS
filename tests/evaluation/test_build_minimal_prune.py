"""Unit tests for build.py minimal-prune helpers (KEEP_EXTENSIONS + node_modules
prune list).

The eval-layer Dockerfile prunes upstream openclaw bloat (101 unused dist/
extension bundles, 53 unused skills, 14M docs, 4.8M control-ui, plus 6
node_modules packages tied to plugins we never enable) so the smoke image is
~830MB lighter than the upstream slim variant. build.py is responsible for
computing the keep-list whitelist from CLI args and forwarding it as a
docker --build-arg.

Run:
    .venv/bin/python -m pytest tests/evaluation/test_build_minimal_prune.py -v
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_PY = REPO_ROOT / "openclaw-eval" / "harness" / "build.py"


def _import_build():
    spec = importlib.util.spec_from_file_location("openclaw_eval_build", BUILD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------ keep list


_BOOTSTRAP = (
    "image-generation-core",
    "llm-task",
    "media-understanding-core",
    "speech-core",
    "video-generation-core",
)


def test_keep_extensions_default_includes_memory_core_plus_bootstrap():
    """memory-core is the slot owner; bootstrap extensions are imported
    unconditionally by openclaw runtime (e.g. speech-core/runtime-api.js)
    even when not in plugins.allow. Removing them breaks agent --local."""
    build = _import_build()
    keep = build.compute_keep_extensions(
        memory_plugin="memory-core",
        install_plugin_id=None,
        extra_install_plugin_ids=None,
    )
    assert sorted(keep) == sorted({"memory-core", *_BOOTSTRAP})


def test_keep_extensions_noop_still_includes_memory_core_and_bootstrap():
    build = _import_build()
    keep = build.compute_keep_extensions(
        memory_plugin="noop",
        install_plugin_id=None,
        extra_install_plugin_ids=None,
    )
    assert sorted(keep) == sorted({"memory-core", *_BOOTSTRAP})


def test_keep_extensions_external_memory_plugin_added():
    build = _import_build()
    keep = build.compute_keep_extensions(
        memory_plugin="mem0",
        install_plugin_id=None,
        extra_install_plugin_ids=None,
    )
    assert sorted(keep) == sorted({"mem0", "memory-core", *_BOOTSTRAP})


def test_keep_extensions_install_id_added():
    build = _import_build()
    keep = build.compute_keep_extensions(
        memory_plugin="memory-core",
        install_plugin_id="hypercompositor",
        extra_install_plugin_ids=None,
    )
    assert sorted(keep) == sorted({"hypercompositor", "memory-core", *_BOOTSTRAP})


def test_keep_extensions_extras_added():
    build = _import_build()
    keep = build.compute_keep_extensions(
        memory_plugin="memory-core",
        install_plugin_id="hypercompositor",
        extra_install_plugin_ids=["hypermem"],
    )
    assert sorted(keep) == sorted({"hypercompositor", "hypermem", "memory-core", *_BOOTSTRAP})


def test_keep_extensions_dedupes_overlap():
    build = _import_build()
    keep = build.compute_keep_extensions(
        memory_plugin="hypercompositor",
        install_plugin_id="hypercompositor",
        extra_install_plugin_ids=None,
    )
    assert sorted(keep) == sorted({"hypercompositor", "memory-core", *_BOOTSTRAP})


def test_keep_extensions_returns_sorted_unique_list():
    build = _import_build()
    keep = build.compute_keep_extensions(
        memory_plugin="evermemos",
        install_plugin_id=None,
        extra_install_plugin_ids=None,
    )
    assert keep == sorted(set(keep))


def test_bootstrap_keep_extensions_pinned():
    """The bootstrap keep set is a runtime contract — accidentally shrinking
    it breaks `agent --local` prebootstrap. Pin the exact members so the
    contract is visible in the test diff."""
    build = _import_build()
    assert sorted(build.BOOTSTRAP_KEEP_EXTENSIONS) == sorted(_BOOTSTRAP)


# ----------------------------------------------------------- node_modules prune


def test_prune_node_modules_packages_includes_known_unused():
    """The default prune list must include the 6 packages we verified are
    only pulled in by extensions we never enable: lancedb, pdfjs-dist,
    tloncorp, node-llama-cpp(2 packages), larksuiteoapi, opentelemetry.

    Keeping this list pinned in code (not just Dockerfile) lets us assert
    the contract from a unit test instead of grepping the Dockerfile.
    """
    build = _import_build()
    pkgs = build.PRUNE_NODE_MODULES_PACKAGES
    expected = {
        "@lancedb",
        "pdfjs-dist",
        "@tloncorp",
        "node-llama-cpp",
        "@node-llama-cpp",
        "@larksuiteoapi",
        "@opentelemetry",
    }
    assert expected.issubset(set(pkgs)), (
        f"missing safe-to-prune packages: {expected - set(pkgs)}"
    )


def test_prune_node_modules_packages_excludes_dev_only_uncertain():
    """Don't prune koffi / @napi-rs / rolldown / oxlint / typescript by
    default — they're either used transitively by core runtime or are
    pnpm prune --prod leftovers that could break tooling. This is a
    safety guard so future edits don't quietly enlarge the list.
    """
    build = _import_build()
    pkgs = build.PRUNE_NODE_MODULES_PACKAGES
    risky = {"koffi", "@napi-rs", "rolldown", "@rolldown", "@oxlint",
             "@oxlint-tsgolint", "typescript", "@typescript"}
    assert not (set(pkgs) & risky), (
        f"these need a separate review before pruning: {set(pkgs) & risky}"
    )


# ----------------------------------------------------- build-arg integration


def test_build_eval_layer_emits_keep_and_prune_args(monkeypatch):
    """build_eval_layer must forward KEEP_EXTENSIONS + PRUNE_NODE_MODULES_PACKAGES
    as docker --build-arg pairs by default, so the Dockerfile prune layer
    actually fires. Captures the full subprocess command without running docker.
    """
    build = _import_build()
    captured: dict = {}

    def _fake_run_step(label, cmd, cwd=None):
        captured["cmd"] = list(cmd)

    monkeypatch.setattr(build, "run_step", _fake_run_step)
    # stage_active_sidecar would touch the filesystem; bypass it.
    monkeypatch.setattr(build, "stage_active_sidecar", lambda *a, **kw: None)

    eval_dir = REPO_ROOT / "openclaw-eval"
    build.build_eval_layer(
        eval_dir=eval_dir,
        base_tag="openclaw-base:abc1234-memory-core-slim",
        memory_plugin="memory-core",
        openclaw_sha="abc1234",
        plugin_rev="0000000",
        plugins_dir=eval_dir / "plugins",
    )
    cmd = captured["cmd"]
    keep_arg = next(a for a in cmd if a.startswith("KEEP_EXTENSIONS="))
    prune_arg = next(a for a in cmd if a.startswith("PRUNE_NODE_MODULES_PACKAGES="))
    keep_value = keep_arg.split("=", 1)[1].split(" ")
    assert "memory-core" in keep_value
    assert "speech-core" in keep_value  # bootstrap-required
    assert "@lancedb" in prune_arg
    assert "node-llama-cpp" in prune_arg


def test_build_eval_layer_skips_prune_args_when_disabled(monkeypatch):
    build = _import_build()
    captured: dict = {}

    def _fake_run_step(label, cmd, cwd=None):
        captured["cmd"] = list(cmd)

    monkeypatch.setattr(build, "run_step", _fake_run_step)
    monkeypatch.setattr(build, "stage_active_sidecar", lambda *a, **kw: None)

    eval_dir = REPO_ROOT / "openclaw-eval"
    build.build_eval_layer(
        eval_dir=eval_dir,
        base_tag="openclaw-base:abc1234-memory-core-slim",
        memory_plugin="memory-core",
        openclaw_sha="abc1234",
        plugin_rev="0000000",
        plugins_dir=eval_dir / "plugins",
        minimal_prune=False,
    )
    cmd = captured["cmd"]
    assert not any(a.startswith("KEEP_EXTENSIONS=") for a in cmd)
    assert not any(a.startswith("PRUNE_NODE_MODULES_PACKAGES=") for a in cmd)


def test_build_eval_layer_keep_args_include_install_id(monkeypatch):
    """Install-mode build (e.g. hypercompositor) must whitelist the
    install id in addition to memory-core."""
    build = _import_build()
    captured: dict = {}

    def _fake_run_step(label, cmd, cwd=None):
        captured["cmd"] = list(cmd)

    monkeypatch.setattr(build, "run_step", _fake_run_step)
    monkeypatch.setattr(build, "stage_active_sidecar", lambda *a, **kw: None)

    eval_dir = REPO_ROOT / "openclaw-eval"
    build.build_eval_layer(
        eval_dir=eval_dir,
        base_tag="openclaw-base:abc1234-memory-core-slim",
        memory_plugin="memory-core",
        openclaw_sha="abc1234",
        plugin_rev="abcd123",
        plugins_dir=eval_dir / "plugins",
        install_spec="npm:@psiclawops/hypercompositor@0.9.6",
        install_plugin_id="hypercompositor",
        extra_install_specs=[("npm:@psiclawops/hypermem@0.9.6", "hypermem")],
    )
    cmd = captured["cmd"]
    keep_arg = next(a for a in cmd if a.startswith("KEEP_EXTENSIONS="))
    keep_value = keep_arg.split("=", 1)[1].split(" ")
    assert "hypercompositor" in keep_value
    assert "hypermem" in keep_value
    assert "memory-core" in keep_value
    # Bootstrap-required core extensions still present
    for required in _BOOTSTRAP:
        assert required in keep_value, f"missing bootstrap extension {required}"

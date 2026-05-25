"""Unit tests for evaluation.src.plugins.cli_overrides (PR3)."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from evaluation.src.plugins.cli_overrides import (
    PluginOverrideResult,
    apply_plugin_overrides,
)
from evaluation.src.plugins.manifest import (
    ManifestEntry,
    ManifestPlugin,
    append_entry,
    now_iso,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_REGISTRY = REPO_ROOT / "evaluation" / "config" / "plugin_registry.yaml"


def _seed_manifest(path: Path, entries: list[ManifestEntry]) -> None:
    for e in entries:
        append_entry(path, e)


def _entry(image: str, plugins: dict[str, dict]) -> ManifestEntry:
    return ManifestEntry(
        image=image,
        openclaw_sha="7da23c3",
        built_at=now_iso(),
        plugins={
            pid: ManifestPlugin(
                id=pid,
                kind=p.get("kind", "memory"),
                version=p.get("version", "bundled"),
                rev=p.get("rev"),
                source=p.get("source", "bundled"),
            )
            for pid, p in plugins.items()
        },
    )


def _seeded_manifest(tmp_path: Path) -> Path:
    """Return a manifest path with three reference entries."""
    p = tmp_path / "manifest.yaml"
    _seed_manifest(p, [
        _entry("openclaw-eval:7da23c3-memory-core-0000000-slim", {
            "memory-core": {"kind": "memory", "source": "bundled"},
        }),
        _entry("openclaw-eval:7da23c3-evermemos-9b3a1f4-slim", {
            "memory-core": {"kind": "memory", "source": "bundled"},
            "evermemos": {
                "kind": "memory", "version": "bundled",
                "rev": "9b3a1f4", "source": "bundled-source",
            },
        }),
        _entry("openclaw-eval:7da23c3-install-hypercompositor-5bace1f-slim", {
            "memory-core": {"kind": "memory", "source": "bundled"},
            "hypercompositor": {
                "kind": "context-engine", "version": "0.9.6",
                "source": "npm:@psiclawops/hypercompositor",
            },
        }),
    ])
    return p


def _baseline_yaml() -> dict[str, Any]:
    """Minimal openclaw-docker-style system config."""
    return {
        "adapter": "openclaw-docker",
        "openclaw": {
            "memory_mode": "memory-core",
            "context_engine_mode": "",
        },
        "openclaw_docker": {
            "image": "openclaw-eval:legacy-image-from-yaml-slim",
        },
    }


# ---------- no overrides path ----------------------------------------------

def test_no_cli_args_preserves_yaml(tmp_path: Path):
    """When all CLI flags are None, yaml must be untouched."""
    cfg = _baseline_yaml()
    res = apply_plugin_overrides(
        cfg,
        memory_plugin=None,
        context_engine=None,
        image=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=_seeded_manifest(tmp_path),
    )
    assert cfg["openclaw"]["memory_mode"] == "memory-core"
    assert cfg["openclaw_docker"]["image"] == "openclaw-eval:legacy-image-from-yaml-slim"
    assert res == PluginOverrideResult(
        image_resolved=None,
        memory_mode_applied=None,
        context_engine_mode_applied=None,
        triggered_build=False,
    )


# ---------- --memory-plugin -------------------------------------------------

def test_memory_plugin_evermemos_overrides_and_resolves_image(tmp_path: Path):
    cfg = _baseline_yaml()
    res = apply_plugin_overrides(
        cfg,
        memory_plugin="evermemos",
        context_engine=None,
        image=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=_seeded_manifest(tmp_path),
    )
    assert cfg["openclaw"]["memory_mode"] == "evermemos"
    assert cfg["openclaw_docker"]["image"] == "openclaw-eval:7da23c3-evermemos-9b3a1f4-slim"
    assert res.memory_mode_applied == "evermemos"
    assert res.image_resolved == "openclaw-eval:7da23c3-evermemos-9b3a1f4-slim"
    assert not res.triggered_build


def test_memory_plugin_none_wires_to_noop(tmp_path: Path):
    cfg = _baseline_yaml()
    apply_plugin_overrides(
        cfg,
        memory_plugin="none",
        context_engine=None,
        image=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=_seeded_manifest(tmp_path),
    )
    assert cfg["openclaw"]["memory_mode"] == "noop"


def test_memory_plugin_memory_core_no_image_lookup(tmp_path: Path):
    """memory-core is base-extension and adds no image constraint. The
    short-circuit must trigger: yaml image stays untouched, manifest is
    NOT consulted (so even an ambiguous multi-entry manifest is fine)."""
    cfg = _baseline_yaml()
    yaml_image_before = cfg["openclaw_docker"]["image"]
    res = apply_plugin_overrides(
        cfg,
        memory_plugin="memory-core",
        context_engine=None,
        image=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=_seeded_manifest(tmp_path),  # 3 entries; would be ambiguous
    )
    # Short-circuit: yaml image preserved, no resolved image returned.
    assert cfg["openclaw_docker"]["image"] == yaml_image_before
    assert res.image_resolved is None
    assert res.memory_mode_applied == "memory-core"
    assert res.triggered_build is False


# ---------- --context-engine -----------------------------------------------

def test_context_engine_hypercompositor_resolves_image(tmp_path: Path):
    cfg = _baseline_yaml()
    apply_plugin_overrides(
        cfg,
        memory_plugin=None,
        context_engine="hypercompositor@0.9.6",
        image=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=_seeded_manifest(tmp_path),
    )
    assert cfg["openclaw"]["context_engine_mode"] == "hypercompositor"
    assert cfg["openclaw_docker"]["image"] == \
        "openclaw-eval:7da23c3-install-hypercompositor-5bace1f-slim"


def test_context_engine_none_unsets_field(tmp_path: Path):
    cfg = _baseline_yaml()
    cfg["openclaw"]["context_engine_mode"] = "memclaw-context-engine"
    apply_plugin_overrides(
        cfg,
        memory_plugin="evermemos",  # need at least one constraint for image
        context_engine="none",
        image=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=_seeded_manifest(tmp_path),
    )
    assert "context_engine_mode" not in cfg["openclaw"]


# ---------- --image highest precedence -------------------------------------

def test_image_override_skips_manifest(tmp_path: Path):
    cfg = _baseline_yaml()
    res = apply_plugin_overrides(
        cfg,
        memory_plugin="evermemos",
        context_engine=None,
        image="openclaw-eval:custom-tag",
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=tmp_path / "no-manifest.yaml",  # nonexistent OK
    )
    assert cfg["openclaw_docker"]["image"] == "openclaw-eval:custom-tag"
    assert res.image_resolved == "openclaw-eval:custom-tag"
    assert not res.triggered_build


# ---------- --per-qa-isolation ---------------------------------------------

def test_kind_mismatch_exits(tmp_path: Path):
    cfg = _baseline_yaml()
    with pytest.raises(SystemExit):
        apply_plugin_overrides(
            cfg,
            memory_plugin="hypercompositor@0.9.6",  # wrong slot
            context_engine=None,
            image=None,
            build_missing=False,
            registry_path=SHIPPED_REGISTRY,
            manifest_path=_seeded_manifest(tmp_path),
        )


def test_unknown_plugin_exits(tmp_path: Path):
    cfg = _baseline_yaml()
    with pytest.raises(SystemExit):
        apply_plugin_overrides(
            cfg,
            memory_plugin="totally-unknown",
            context_engine=None,
            image=None,
            build_missing=False,
            registry_path=SHIPPED_REGISTRY,
            manifest_path=_seeded_manifest(tmp_path),
        )


def test_npm_without_version_exits(tmp_path: Path):
    cfg = _baseline_yaml()
    with pytest.raises(SystemExit):
        apply_plugin_overrides(
            cfg,
            memory_plugin=None,
            context_engine="hypercompositor",  # no @version
            image=None,
            build_missing=False,
            registry_path=SHIPPED_REGISTRY,
            manifest_path=_seeded_manifest(tmp_path),
        )


def test_image_lookup_miss_without_build_missing_exits(tmp_path: Path):
    cfg = _baseline_yaml()
    with pytest.raises(SystemExit):
        apply_plugin_overrides(
            cfg,
            memory_plugin="evermemos",
            context_engine="hypercompositor@0.9.6",  # not in seeded manifest
            image=None,
            build_missing=False,
            registry_path=SHIPPED_REGISTRY,
            manifest_path=_seeded_manifest(tmp_path),
        )


# ---------- --build-missing ------------------------------------------------

def test_build_missing_invokes_build_py_and_re_resolves(tmp_path: Path):
    """When manifest lacks the image and --build-missing is on, shell out
    to build.py. Mock subprocess.run to simulate a successful build that
    appends the entry."""
    cfg = _baseline_yaml()
    manifest_path = _seeded_manifest(tmp_path)

    target_image = "openclaw-eval:7da23c3-evermemos_install-hypercompositor-deadbee-slim"

    def fake_build(cmd, *a, **kw):
        # Append the missing entry to the manifest.
        append_entry(manifest_path, _entry(target_image, {
            "memory-core": {"kind": "memory", "source": "bundled"},
            "evermemos": {
                "kind": "memory", "version": "bundled",
                "rev": "9b3a1f4", "source": "bundled-source",
            },
            "hypercompositor": {
                "kind": "context-engine", "version": "0.9.6",
                "source": "npm:@psiclawops/hypercompositor",
            },
        }))
        class Result:
            returncode = 0
        return Result()

    with patch("evaluation.src.plugins.cli_overrides.subprocess.run", side_effect=fake_build):
        res = apply_plugin_overrides(
            cfg,
            memory_plugin="evermemos",
            context_engine="hypercompositor@0.9.6",
            image=None,
            build_missing=True,
            registry_path=SHIPPED_REGISTRY,
            manifest_path=manifest_path,
        )
    assert res.triggered_build
    assert res.image_resolved == target_image
    assert cfg["openclaw_docker"]["image"] == target_image


def test_build_missing_failure_exits(tmp_path: Path):
    cfg = _baseline_yaml()

    class FailedResult:
        returncode = 1

    with patch("evaluation.src.plugins.cli_overrides.subprocess.run", return_value=FailedResult()):
        with pytest.raises(SystemExit):
            apply_plugin_overrides(
                cfg,
                memory_plugin="evermemos",
                context_engine="hypercompositor@0.9.6",
                image=None,
                build_missing=True,
                registry_path=SHIPPED_REGISTRY,
                manifest_path=_seeded_manifest(tmp_path),
            )


# ---------- combined behavior ----------------------------------------------

def test_apply_overrides_is_idempotent(tmp_path: Path):
    """Calling apply_plugin_overrides twice with the same args on the
    same dict should produce the same final state."""
    cfg = _baseline_yaml()
    manifest_path = _seeded_manifest(tmp_path)

    res1 = apply_plugin_overrides(
        cfg,
        memory_plugin="evermemos",
        context_engine=None,
        image=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=manifest_path,
    )
    snapshot1 = {
        "memory_mode": cfg["openclaw"]["memory_mode"],
        "image": cfg["openclaw_docker"]["image"],
    }

    res2 = apply_plugin_overrides(
        cfg,
        memory_plugin="evermemos",
        context_engine=None,
        image=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=manifest_path,
    )
    snapshot2 = {
        "memory_mode": cfg["openclaw"]["memory_mode"],
        "image": cfg["openclaw_docker"]["image"],
    }
    assert snapshot1 == snapshot2
    assert res1 == res2


def test_build_missing_invokes_build_with_correct_argv(tmp_path: Path):
    """Verify the subprocess.run argv contains the plugin selection — a
    silent regression where --memory-plugin / --context-engine got
    dropped from the build invocation would otherwise pass earlier
    tests (returncode=0 is enough)."""
    cfg = _baseline_yaml()
    manifest_path = _seeded_manifest(tmp_path)
    target_image = "openclaw-eval:7da23c3-evermemos_install-hypercompositor-deadbee-slim"
    captured: dict[str, list[str]] = {}

    def fake_build(cmd, *a, **kw):
        captured["argv"] = list(cmd)
        # Append the missing entry so the second find_image succeeds.
        append_entry(manifest_path, _entry(target_image, {
            "memory-core": {"kind": "memory", "source": "bundled"},
            "evermemos": {
                "kind": "memory", "version": "bundled",
                "rev": "9b3a1f4", "source": "bundled-source",
            },
            "hypercompositor": {
                "kind": "context-engine", "version": "0.9.6",
                "source": "npm:@psiclawops/hypercompositor",
            },
        }))
        class Result:
            returncode = 0
        return Result()

    with patch("evaluation.src.plugins.cli_overrides.subprocess.run", side_effect=fake_build):
        apply_plugin_overrides(
            cfg,
            memory_plugin="evermemos",
            context_engine="hypercompositor@0.9.6",
            image=None,
            build_missing=True,
            registry_path=SHIPPED_REGISTRY,
            manifest_path=manifest_path,
        )

    argv = captured["argv"]
    # First arg = sys.executable; second = build.py path
    assert argv[1].endswith("build.py")
    # Must carry both plugin selections forward to build.py
    assert "--memory-plugin" in argv
    mp_idx = argv.index("--memory-plugin")
    assert argv[mp_idx + 1] == "evermemos"
    assert "--context-engine" in argv
    ce_idx = argv.index("--context-engine")
    assert argv[ce_idx + 1] == "hypercompositor@0.9.6"
    # Manifest path passed through so build.py appends to the same file
    assert "--image-manifest-out" in argv
    mo_idx = argv.index("--image-manifest-out")
    assert Path(argv[mo_idx + 1]) == manifest_path


def test_build_missing_does_not_pass_none_plugin_to_build(tmp_path: Path):
    """When --memory-plugin none is the trigger, build.py should NOT
    receive --memory-plugin none (which would shadow build.py's default).

    Use a manifest seeded only with the baseline image (no hypercompositor)
    so the resolution miss actually triggers the build path."""
    cfg = _baseline_yaml()
    manifest_path = tmp_path / "manifest.yaml"
    _seed_manifest(manifest_path, [
        _entry("openclaw-eval:7da23c3-memory-core-0000000-slim", {
            "memory-core": {"kind": "memory", "source": "bundled"},
        }),
    ])
    target_image = "openclaw-eval:7da23c3-install-hypercompositor-deadbee-slim"
    captured: dict[str, list[str]] = {}

    def fake_build(cmd, *a, **kw):
        captured["argv"] = list(cmd)
        append_entry(manifest_path, _entry(target_image, {
            "memory-core": {"kind": "memory", "source": "bundled"},
            "hypercompositor": {
                "kind": "context-engine", "version": "0.9.6",
                "source": "npm:@psiclawops/hypercompositor",
            },
        }))
        class Result:
            returncode = 0
        return Result()

    with patch("evaluation.src.plugins.cli_overrides.subprocess.run", side_effect=fake_build):
        apply_plugin_overrides(
            cfg,
            memory_plugin="none",
            context_engine="hypercompositor@0.9.6",
            image=None,
            build_missing=True,
            registry_path=SHIPPED_REGISTRY,
            manifest_path=manifest_path,
        )

    # 'none' in eval CLI means 'wire to noop'; build.py's image only needs
    # hypercompositor, so memory-plugin shouldn't appear in the build argv.
    argv = captured["argv"]
    assert "--memory-plugin" not in argv
    assert "--context-engine" in argv


def test_two_plugins_both_overrides_image_resolves_full_combo(tmp_path: Path):
    cfg = _baseline_yaml()
    manifest_path = tmp_path / "manifest.yaml"
    full_image = "openclaw-eval:7da23c3-evermemos_install-hypercompositor-deadbee-slim"
    _seed_manifest(manifest_path, [
        _entry(full_image, {
            "memory-core": {"kind": "memory", "source": "bundled"},
            "evermemos": {
                "kind": "memory", "version": "bundled",
                "rev": "9b3a1f4", "source": "bundled-source",
            },
            "hypercompositor": {
                "kind": "context-engine", "version": "0.9.6",
                "source": "npm:@psiclawops/hypercompositor",
            },
        }),
    ])
    apply_plugin_overrides(
        cfg,
        memory_plugin="evermemos",
        context_engine="hypercompositor@0.9.6",
        image=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=manifest_path,
    )
    assert cfg["openclaw"]["memory_mode"] == "evermemos"
    assert cfg["openclaw"]["context_engine_mode"] == "hypercompositor"
    assert cfg["openclaw_docker"]["image"] == full_image

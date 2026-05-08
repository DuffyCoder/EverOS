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
        per_qa_isolation=None,
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
        per_qa_isolation=None,
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
        per_qa_isolation=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=_seeded_manifest(tmp_path),
    )
    assert cfg["openclaw"]["memory_mode"] == "noop"


def test_memory_plugin_memory_core_no_image_constraint(tmp_path: Path):
    """memory-core is base-extension; sets memory_mode but doesn't filter
    images (memory-core is in every image)."""
    cfg = _baseline_yaml()
    res = apply_plugin_overrides(
        cfg,
        memory_plugin="memory-core",
        context_engine=None,
        image=None,
        per_qa_isolation=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=_seeded_manifest(tmp_path),
    )
    assert cfg["openclaw"]["memory_mode"] == "memory-core"
    # All 3 manifest entries match (no constraint), so resolution is ambiguous.
    # Wait — no: the function only filters when constraint is non-None. With
    # no constraints either side, find_image with both None on >1 entries
    # raises. But our test flow only fires resolution when ONE plugin override
    # is passed AND has a non-baseline constraint. Here memory-core is
    # base-extension -> _ref_to_constraint returns None -> no constraint
    # -> find_image with both None on 3 images -> ambiguous error.
    # Verify the error path: this should sys.exit. Actually we got here in
    # the test; let's check what happened.
    # Actually re-read: we DO call find_image when memory_plugin is not None,
    # even if constraint is None. That's the bug. Test will catch it.
    assert res.memory_mode_applied == "memory-core"


# ---------- --context-engine -----------------------------------------------

def test_context_engine_hypercompositor_resolves_image(tmp_path: Path):
    cfg = _baseline_yaml()
    apply_plugin_overrides(
        cfg,
        memory_plugin=None,
        context_engine="hypercompositor@0.9.6",
        image=None,
        per_qa_isolation=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=_seeded_manifest(tmp_path),
    )
    assert cfg["openclaw"]["context_engine_mode"] == "hypercompositor"
    assert cfg["openclaw_docker"]["image"] == \
        "openclaw-eval:7da23c3-install-hypercompositor-5bace1f-slim"


def test_context_engine_none_unsets_field(tmp_path: Path):
    cfg = _baseline_yaml()
    cfg["openclaw"]["context_engine_mode"] = "memclaw"
    apply_plugin_overrides(
        cfg,
        memory_plugin="evermemos",  # need at least one constraint for image
        context_engine="none",
        image=None,
        per_qa_isolation=None,
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
        per_qa_isolation=None,
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=tmp_path / "no-manifest.yaml",  # nonexistent OK
    )
    assert cfg["openclaw_docker"]["image"] == "openclaw-eval:custom-tag"
    assert res.image_resolved == "openclaw-eval:custom-tag"
    assert not res.triggered_build


# ---------- --per-qa-isolation ---------------------------------------------

def test_per_qa_isolation_passes_through(tmp_path: Path):
    cfg = _baseline_yaml()
    apply_plugin_overrides(
        cfg,
        memory_plugin=None,
        context_engine=None,
        image=None,
        per_qa_isolation="snapshot",
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=_seeded_manifest(tmp_path),
    )
    assert cfg["openclaw_docker"]["per_qa_isolation"] == "snapshot"


# ---------- error paths ----------------------------------------------------

def test_kind_mismatch_exits(tmp_path: Path):
    cfg = _baseline_yaml()
    with pytest.raises(SystemExit):
        apply_plugin_overrides(
            cfg,
            memory_plugin="hypercompositor@0.9.6",  # wrong slot
            context_engine=None,
            image=None,
            per_qa_isolation=None,
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
            per_qa_isolation=None,
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
            per_qa_isolation=None,
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
            per_qa_isolation=None,
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
            per_qa_isolation=None,
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
                per_qa_isolation=None,
                build_missing=True,
                registry_path=SHIPPED_REGISTRY,
                manifest_path=_seeded_manifest(tmp_path),
            )


# ---------- combined behavior ----------------------------------------------

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
        per_qa_isolation="auto",
        build_missing=False,
        registry_path=SHIPPED_REGISTRY,
        manifest_path=manifest_path,
    )
    assert cfg["openclaw"]["memory_mode"] == "evermemos"
    assert cfg["openclaw"]["context_engine_mode"] == "hypercompositor"
    assert cfg["openclaw_docker"]["image"] == full_image
    assert cfg["openclaw_docker"]["per_qa_isolation"] == "auto"

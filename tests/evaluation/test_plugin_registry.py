"""Unit tests for evaluation.src.plugins.registry."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from evaluation.src.plugins.registry import (
    PluginEntry,
    RegistryError,
    by_kind,
    get,
    load_registry,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_REGISTRY = REPO_ROOT / "evaluation" / "config" / "plugin_registry.yaml"


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "registry.yaml"
    p.write_text(textwrap.dedent(body))
    return p


# ---------- shipped registry sanity ----------------------------------------

def test_shipped_registry_loads():
    """The committed plugin_registry.yaml must be valid."""
    reg = load_registry(SHIPPED_REGISTRY)
    # at minimum the bundled essentials and one npm plugin must be there
    for required in ("memory-core", "noop", "evermemos", "hypercompositor"):
        assert required in reg, f"shipped registry missing '{required}'"


def test_shipped_registry_kinds():
    reg = load_registry(SHIPPED_REGISTRY)
    assert reg["memory-core"].supports_kind("memory")
    assert not reg["memory-core"].supports_kind("context-engine")
    assert reg["hypercompositor"].supports_kind("context-engine")
    assert not reg["hypercompositor"].supports_kind("memory")


def test_shipped_registry_npm_entries_have_package():
    reg = load_registry(SHIPPED_REGISTRY)
    for plugin in reg.values():
        if plugin.type == "npm":
            assert plugin.npm_package, f"{plugin.id} type=npm but no npm_package"


# ---------- minimal valid file ---------------------------------------------

def test_load_minimal_valid(tmp_path: Path):
    p = _write(tmp_path, """
        memory-core:
          kind: memory
          type: base-extension
        evermemos:
          kind: memory
          type: bundled-source
    """)
    reg = load_registry(p)
    assert set(reg.keys()) == {"memory-core", "evermemos"}
    assert reg["evermemos"].source_dir == "openclaw-eval/plugins/evermemos"


def test_dual_kind_plugin(tmp_path: Path):
    p = _write(tmp_path, """
        my-dual:
          kind: [memory, context-engine]
          type: bundled-source
    """)
    reg = load_registry(p)
    e = reg["my-dual"]
    assert e.supports_kind("memory")
    assert e.supports_kind("context-engine")


def test_npm_with_package(tmp_path: Path):
    p = _write(tmp_path, """
        hyper:
          kind: context-engine
          type: npm
          npm_package: "@psiclawops/hypercompositor"
    """)
    reg = load_registry(p)
    assert reg["hyper"].npm_package == "@psiclawops/hypercompositor"


def test_explicit_source_dir_override(tmp_path: Path):
    p = _write(tmp_path, """
        forked:
          kind: memory
          type: bundled-source
          source_dir: /custom/path/here
    """)
    reg = load_registry(p)
    assert reg["forked"].source_dir == "/custom/path/here"


# ---------- error cases ----------------------------------------------------

def test_unknown_type_rejected(tmp_path: Path):
    p = _write(tmp_path, """
        bad:
          kind: memory
          type: cosmic-ray
    """)
    with pytest.raises(RegistryError, match="type='cosmic-ray'"):
        load_registry(p)


def test_unknown_kind_rejected(tmp_path: Path):
    p = _write(tmp_path, """
        bad:
          kind: time-engine
          type: bundled-source
    """)
    with pytest.raises(RegistryError, match="unknown kind"):
        load_registry(p)


def test_npm_without_package_rejected(tmp_path: Path):
    p = _write(tmp_path, """
        bad:
          kind: memory
          type: npm
    """)
    with pytest.raises(RegistryError, match="no npm_package"):
        load_registry(p)


def test_base_extension_with_extra_fields_rejected(tmp_path: Path):
    p = _write(tmp_path, """
        bad:
          kind: memory
          type: base-extension
          npm_package: "should-not-be-here"
    """)
    with pytest.raises(RegistryError, match="npm_package/source_dir not allowed"):
        load_registry(p)


def test_missing_kind_rejected(tmp_path: Path):
    p = _write(tmp_path, """
        bad:
          type: bundled-source
    """)
    with pytest.raises(RegistryError):
        load_registry(p)


def test_top_level_must_be_mapping(tmp_path: Path):
    p = _write(tmp_path, "- foo\n- bar\n")
    with pytest.raises(RegistryError, match="top-level mapping"):
        load_registry(p)


def test_missing_file_rejected(tmp_path: Path):
    with pytest.raises(RegistryError, match="not found"):
        load_registry(tmp_path / "does-not-exist.yaml")


def test_empty_yaml_returns_empty_registry(tmp_path: Path):
    """yaml that parses to None (empty file) should yield {}."""
    p = tmp_path / "empty.yaml"
    p.write_text("")
    assert load_registry(p) == {}


def test_explicit_empty_mapping_returns_empty(tmp_path: Path):
    """yaml '{}' should yield empty registry without error."""
    p = tmp_path / "empty.yaml"
    p.write_text("{}\n")
    assert load_registry(p) == {}


def test_id_with_underscore_rejected(tmp_path: Path):
    """Build/eval tags join plugin ids with '_'; ids containing '_' would
    create ambiguous bundles (a_b vs a + b)."""
    p = _write(tmp_path, """
        bad_id:
          kind: memory
          type: bundled-source
    """)
    with pytest.raises(RegistryError, match="not allowed"):
        load_registry(p)


def test_id_with_whitespace_rejected(tmp_path: Path):
    p = _write(tmp_path, """
        "bad id":
          kind: memory
          type: bundled-source
    """)
    with pytest.raises(RegistryError, match="not allowed"):
        load_registry(p)


def test_id_with_at_or_slash_rejected(tmp_path: Path):
    """'@' is the version delimiter; '/' is path separator — both unsafe."""
    for bad in ("a@b", "a/b", "a+b"):
        p = tmp_path / f"bad_{hash(bad)}.yaml"
        p.write_text(textwrap.dedent(f"""
            "{bad}":
              kind: memory
              type: bundled-source
        """))
        with pytest.raises(RegistryError, match="not allowed"):
            load_registry(p)


# ---------- query helpers --------------------------------------------------

def test_get_unknown_id_friendly_error(tmp_path: Path):
    p = _write(tmp_path, """
        evermemos:
          kind: memory
          type: bundled-source
    """)
    reg = load_registry(p)
    with pytest.raises(RegistryError, match="unknown plugin"):
        get(reg, "no-such-thing")


def test_by_kind_filters_correctly(tmp_path: Path):
    p = _write(tmp_path, """
        m1:
          kind: memory
          type: bundled-source
        c1:
          kind: context-engine
          type: bundled-source
        d1:
          kind: [memory, context-engine]
          type: bundled-source
    """)
    reg = load_registry(p)
    memory_ids = {e.id for e in by_kind(reg, "memory")}
    ce_ids = {e.id for e in by_kind(reg, "context-engine")}
    assert memory_ids == {"m1", "d1"}
    assert ce_ids == {"c1", "d1"}


# ---------- dataclass invariants -------------------------------------------

def test_plugin_entry_is_frozen():
    e = PluginEntry(
        id="x",
        kinds=frozenset({"memory"}),
        type="base-extension",
        npm_package=None,
        source_dir=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        e.id = "y"  # type: ignore[misc]

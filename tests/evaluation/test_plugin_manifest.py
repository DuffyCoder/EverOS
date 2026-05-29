"""Unit tests for evaluation.src.plugins.manifest."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from evaluation.src.plugins.manifest import (
    ManifestEntry,
    ManifestError,
    ManifestPlugin,
    append_entry,
    find_image,
    load_manifest,
    now_iso,
)


def _entry(
    image: str,
    plugins: dict[str, dict],
    sha: str = "7da23c3",
) -> ManifestEntry:
    return ManifestEntry(
        image=image,
        openclaw_sha=sha,
        built_at="2026-05-08T00:00:00Z",
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


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "manifest.yaml"
    p.write_text(textwrap.dedent(body))
    return p


# ---------- load_manifest --------------------------------------------------

def test_load_missing_file_returns_empty(tmp_path: Path):
    assert load_manifest(tmp_path / "no-such.yaml") == []


def test_load_empty_file_returns_empty(tmp_path: Path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    assert load_manifest(p) == []


def test_load_simple(tmp_path: Path):
    p = _write(tmp_path, """
        - image: openclaw-eval:abc123-mc-0000000-slim
          openclaw_sha: abc123
          built_at: 2026-05-08T00:00:00Z
          plugins:
            memory-core:
              kind: memory
              version: bundled
              source: bundled
    """)
    entries = load_manifest(p)
    assert len(entries) == 1
    assert entries[0].image == "openclaw-eval:abc123-mc-0000000-slim"
    assert "memory-core" in entries[0].plugins
    assert entries[0].plugins["memory-core"].version == "bundled"


def test_load_multiple_entries(tmp_path: Path):
    p = _write(tmp_path, """
        - image: img-a
          openclaw_sha: aaa
          built_at: t1
          plugins:
            memory-core: {kind: memory, version: bundled, source: bundled}
            evermemos: {kind: memory, version: bundled, rev: 9b3a1f4, source: bundled-source}
        - image: img-b
          openclaw_sha: bbb
          built_at: t2
          plugins:
            memory-core: {kind: memory, version: bundled, source: bundled}
            hypercompositor: {kind: context-engine, version: 0.9.6, source: 'npm:@psiclawops/hypercompositor'}
    """)
    entries = load_manifest(p)
    assert len(entries) == 2
    assert entries[0].plugins["evermemos"].rev == "9b3a1f4"
    assert entries[1].plugins["hypercompositor"].version == "0.9.6"


def test_load_top_level_must_be_list(tmp_path: Path):
    p = _write(tmp_path, """
        not-a-list: yes
    """)
    with pytest.raises(ManifestError, match="top-level list"):
        load_manifest(p)


def test_load_entry_missing_image(tmp_path: Path):
    p = _write(tmp_path, """
        - openclaw_sha: abc
          built_at: t1
          plugins: {}
    """)
    with pytest.raises(ManifestError, match="missing required field 'image'"):
        load_manifest(p)


def test_load_entry_missing_openclaw_sha(tmp_path: Path):
    p = _write(tmp_path, """
        - image: img-1
          built_at: t1
          plugins: {}
    """)
    with pytest.raises(ManifestError, match="missing required field 'openclaw_sha'"):
        load_manifest(p)


def test_load_entry_missing_built_at(tmp_path: Path):
    p = _write(tmp_path, """
        - image: img-1
          openclaw_sha: abc
          plugins: {}
    """)
    with pytest.raises(ManifestError, match="missing required field 'built_at'"):
        load_manifest(p)


# ---------- append_entry ---------------------------------------------------

def test_append_to_empty_creates_file(tmp_path: Path):
    path = tmp_path / "manifest.yaml"
    entry = _entry("img-1", {"memory-core": {}})
    append_entry(path, entry)
    assert path.exists()
    entries = load_manifest(path)
    assert len(entries) == 1
    assert entries[0].image == "img-1"


def test_append_preserves_existing(tmp_path: Path):
    path = tmp_path / "manifest.yaml"
    append_entry(path, _entry("img-1", {"memory-core": {}}))
    append_entry(path, _entry("img-2", {"memory-core": {}, "evermemos": {}}))
    entries = load_manifest(path)
    assert [e.image for e in entries] == ["img-1", "img-2"]


def test_append_creates_parent_dirs(tmp_path: Path):
    path = tmp_path / "deep" / "nested" / "manifest.yaml"
    append_entry(path, _entry("img-1", {"memory-core": {}}))
    assert path.exists()


def test_append_round_trip_preserves_rev(tmp_path: Path):
    path = tmp_path / "manifest.yaml"
    entry = _entry("img-1", {"evermemos": {"rev": "abc1234"}})
    append_entry(path, entry)
    entries = load_manifest(path)
    assert entries[0].plugins["evermemos"].rev == "abc1234"


# ---------- find_image -----------------------------------------------------

def _three_image_manifest():
    return [
        _entry("img-baseline", {"memory-core": {}}),
        _entry("img-evermemos", {
            "memory-core": {},
            "evermemos": {"rev": "9b3a1f4"},
        }),
        _entry("img-hyper", {
            "memory-core": {},
            "hypercompositor": {"version": "0.9.6", "kind": "context-engine"},
        }),
    ]


def test_find_image_by_memory_only():
    entries = _three_image_manifest()
    e = find_image(entries, memory_plugin=("evermemos", None), context_engine=None)
    assert e.image == "img-evermemos"


def test_find_image_by_ce_only():
    entries = _three_image_manifest()
    e = find_image(entries, memory_plugin=None, context_engine=("hypercompositor", "0.9.6"))
    assert e.image == "img-hyper"


def test_find_image_no_constraints_ambiguous():
    entries = _three_image_manifest()
    with pytest.raises(
        ManifestError,
        match=r"3 images registered.*specify at least one",
    ):
        find_image(entries, memory_plugin=None, context_engine=None)


def test_find_image_no_constraints_single_entry_ok():
    """When only one image is registered, both constraints None resolves to it."""
    entries = [_entry("only-image", {"memory-core": {}})]
    e = find_image(entries, memory_plugin=None, context_engine=None)
    assert e.image == "only-image"


def test_find_image_version_mismatch_no_match():
    entries = _three_image_manifest()
    with pytest.raises(ManifestError, match="no image matches"):
        find_image(
            entries,
            memory_plugin=None,
            context_engine=("hypercompositor", "0.9.7"),
        )


def test_find_image_id_unknown_no_match():
    entries = _three_image_manifest()
    with pytest.raises(ManifestError, match="no image matches"):
        find_image(
            entries,
            memory_plugin=("does-not-exist", None),
            context_engine=None,
        )


def test_find_image_combined_constraint():
    entries = [
        _entry("img-both", {
            "memory-core": {},
            "evermemos": {},
            "hypercompositor": {"version": "0.9.6", "kind": "context-engine"},
        }),
        _entry("img-evermemos", {"memory-core": {}, "evermemos": {}}),
    ]
    e = find_image(
        entries,
        memory_plugin=("evermemos", None),
        context_engine=("hypercompositor", "0.9.6"),
    )
    assert e.image == "img-both"


def test_find_image_empty_manifest():
    with pytest.raises(ManifestError, match="no image matches"):
        find_image([], memory_plugin=("evermemos", None), context_engine=None)


def test_find_image_version_None_matches_any():
    """Version=None means 'any version of this plugin id'."""
    entries = [
        _entry("img-a", {
            "memory-core": {},
            "hypercompositor": {"version": "0.9.6", "kind": "context-engine"},
        }),
    ]
    e = find_image(
        entries,
        memory_plugin=None,
        context_engine=("hypercompositor", None),
    )
    assert e.image == "img-a"


def test_find_image_ambiguous_lists_candidates():
    entries = [
        _entry("img-v1", {
            "memory-core": {},
            "hypercompositor": {"version": "0.9.5", "kind": "context-engine"},
        }),
        _entry("img-v2", {
            "memory-core": {},
            "hypercompositor": {"version": "0.9.6", "kind": "context-engine"},
        }),
    ]
    with pytest.raises(ManifestError, match="img-v1.*img-v2"):
        find_image(
            entries,
            memory_plugin=None,
            context_engine=("hypercompositor", None),
        )


# ---------- has_plugin -----------------------------------------------------

def test_has_plugin_no_version():
    e = _entry("x", {"evermemos": {"version": "bundled"}})
    assert e.has_plugin("evermemos")
    assert e.has_plugin("evermemos", None)
    assert not e.has_plugin("nope")


def test_has_plugin_with_version():
    e = _entry("x", {"hypercompositor": {"version": "0.9.6", "kind": "context-engine"}})
    assert e.has_plugin("hypercompositor", "0.9.6")
    assert not e.has_plugin("hypercompositor", "0.9.7")


# ---------- now_iso --------------------------------------------------------

def test_now_iso_format():
    s = now_iso()
    # rough shape: 2026-05-08T12:34:56Z
    assert len(s) == 20
    assert s.endswith("Z")
    assert s[4] == "-" and s[7] == "-" and s[10] == "T"


# ---------- frozen invariants ----------------------------------------------

def test_manifest_entry_is_frozen():
    e = _entry("x", {"memory-core": {}})
    with pytest.raises((AttributeError, TypeError)):
        e.image = "y"  # type: ignore[misc]


def test_manifest_entry_plugins_dict_is_immutable(tmp_path: Path):
    """plugins must be a read-only Mapping (MappingProxyType wrap)."""
    path = tmp_path / "manifest.yaml"
    append_entry(path, _entry("img-1", {"memory-core": {}}))
    [entry] = load_manifest(path)
    # MappingProxyType raises TypeError on item assignment / deletion
    with pytest.raises(TypeError):
        entry.plugins["evermemos"] = ManifestPlugin(  # type: ignore[index]
            id="evermemos", kind="memory", version="bundled",
            rev=None, source="bundled-source",
        )
    with pytest.raises(TypeError):
        del entry.plugins["memory-core"]  # type: ignore[attr-defined]


def test_round_trip_append_then_find_image(tmp_path: Path):
    """PR2 will use append_entry then find_image — verify round trip works."""
    path = tmp_path / "manifest.yaml"
    append_entry(path, _entry("baseline", {"memory-core": {}}))
    append_entry(path, _entry("with-evermemos", {
        "memory-core": {},
        "evermemos": {"rev": "abc1234"},
    }))
    append_entry(path, _entry("with-hypercompositor", {
        "memory-core": {},
        "hypercompositor": {"version": "0.9.6", "kind": "context-engine"},
    }))
    entries = load_manifest(path)
    e = find_image(entries, memory_plugin=("evermemos", None), context_engine=None)
    assert e.image == "with-evermemos"
    assert e.plugins["evermemos"].rev == "abc1234"


def test_load_unknown_kind_rejected(tmp_path: Path):
    """Symmetry with registry: manifest plugin kind is also validated."""
    p = _write(tmp_path, """
        - image: img-1
          openclaw_sha: abc
          built_at: t1
          plugins:
            x:
              kind: time-engine
              version: bundled
    """)
    with pytest.raises(ManifestError, match="unknown kind"):
        load_manifest(p)


def test_load_plugins_must_be_mapping(tmp_path: Path):
    p = _write(tmp_path, """
        - image: img-1
          openclaw_sha: abc
          built_at: t1
          plugins:
            - not-a-mapping
    """)
    with pytest.raises(ManifestError, match="'plugins' must be a mapping"):
        load_manifest(p)


def test_load_plugin_body_must_be_mapping(tmp_path: Path):
    p = _write(tmp_path, """
        - image: img-1
          openclaw_sha: abc
          built_at: t1
          plugins:
            x: 42
    """)
    with pytest.raises(ManifestError, match="body must be a mapping"):
        load_manifest(p)

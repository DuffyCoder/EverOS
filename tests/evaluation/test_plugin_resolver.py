"""Unit tests for evaluation.src.plugins.resolver."""
from __future__ import annotations

import pytest

from evaluation.src.plugins.registry import PluginEntry, RegistryError
from evaluation.src.plugins.resolver import (
    PluginRef,
    ResolverError,
    parse_plugin_spec_overrides,
    parse_ref,
)


def _entry(
    pid: str,
    kinds: tuple[str, ...] = ("memory",),
    type_: str = "bundled-source",
    npm_package: str | None = None,
    source_dir: str | None = None,
) -> PluginEntry:
    return PluginEntry(
        id=pid,
        kinds=frozenset(kinds),
        type=type_,
        npm_package=npm_package,
        source_dir=source_dir,
    )


@pytest.fixture
def reg() -> dict[str, PluginEntry]:
    return {
        "memory-core": _entry("memory-core", type_="base-extension"),
        "evermemos": _entry("evermemos", source_dir="openclaw-eval/plugins/evermemos"),
        "stub-engine": _entry(
            "stub-engine",
            kinds=("context-engine",),
            source_dir="openclaw-eval/plugins/stub-engine",
        ),
        "hypercompositor": _entry(
            "hypercompositor",
            kinds=("context-engine",),
            type_="npm",
            npm_package="@psiclawops/hypercompositor",
        ),
        "dual-kind": _entry(
            "dual-kind",
            kinds=("memory", "context-engine"),
            source_dir="openclaw-eval/plugins/dual-kind",
        ),
    }


# ---------- parse_ref: None / "none" ---------------------------------------

def test_parse_ref_none_returns_none(reg):
    assert parse_ref(None, expected_kind="memory", registry=reg) is None


def test_parse_ref_string_none(reg):
    assert parse_ref("none", expected_kind="memory", registry=reg) is None
    assert parse_ref("None", expected_kind="memory", registry=reg) is None
    assert parse_ref("  none  ", expected_kind="memory", registry=reg) is None


def test_parse_ref_empty_string(reg):
    assert parse_ref("", expected_kind="memory", registry=reg) is None
    assert parse_ref("   ", expected_kind="memory", registry=reg) is None


# ---------- parse_ref: bundled plugins -------------------------------------

def test_parse_ref_bundled_id_only(reg):
    ref = parse_ref("evermemos", expected_kind="memory", registry=reg)
    assert ref is not None
    assert ref.id == "evermemos"
    assert ref.version is None
    assert not ref.is_npm


def test_parse_ref_bundled_rejects_version(reg):
    with pytest.raises(ResolverError, match="bundled plugin does not take @version"):
        parse_ref("evermemos@1.0", expected_kind="memory", registry=reg)


def test_parse_ref_base_extension(reg):
    ref = parse_ref("memory-core", expected_kind="memory", registry=reg)
    assert ref.id == "memory-core"
    assert ref.version is None


# ---------- parse_ref: npm plugins -----------------------------------------

def test_parse_ref_npm_with_version(reg):
    ref = parse_ref(
        "hypercompositor@0.9.6",
        expected_kind="context-engine",
        registry=reg,
    )
    assert ref.id == "hypercompositor"
    assert ref.version == "0.9.6"
    assert ref.is_npm
    assert ref.npm_spec() == "npm:@psiclawops/hypercompositor@0.9.6"


def test_parse_ref_npm_without_version_rejected(reg):
    with pytest.raises(ResolverError, match="requires explicit version"):
        parse_ref("hypercompositor", expected_kind="context-engine", registry=reg)


def test_parse_ref_npm_spec_with_override(reg):
    ref = parse_ref(
        "hypercompositor@0.9.6",
        expected_kind="context-engine",
        registry=reg,
    )
    assert ref.npm_spec(override="/tmp/local.tgz") == "/tmp/local.tgz"


# ---------- parse_ref: kind validation -------------------------------------

def test_parse_ref_kind_mismatch_memory_into_ce(reg):
    with pytest.raises(ResolverError, match="does not include 'context-engine'"):
        parse_ref("evermemos", expected_kind="context-engine", registry=reg)


def test_parse_ref_kind_mismatch_ce_into_memory(reg):
    with pytest.raises(ResolverError, match="does not include 'memory'"):
        parse_ref(
            "hypercompositor@0.9.6",
            expected_kind="memory",
            registry=reg,
        )


def test_parse_ref_dual_kind_works_for_either_slot(reg):
    a = parse_ref("dual-kind", expected_kind="memory", registry=reg)
    b = parse_ref("dual-kind", expected_kind="context-engine", registry=reg)
    assert a.id == "dual-kind"
    assert b.id == "dual-kind"


# ---------- parse_ref: malformed / unknown ---------------------------------

def test_parse_ref_unknown_id(reg):
    with pytest.raises(ResolverError, match="unknown plugin"):
        parse_ref("does-not-exist", expected_kind="memory", registry=reg)


def test_parse_ref_malformed_at(reg):
    with pytest.raises(ResolverError, match="malformed"):
        parse_ref("@", expected_kind="memory", registry=reg)
    with pytest.raises(ResolverError, match="malformed"):
        parse_ref("evermemos@", expected_kind="memory", registry=reg)
    with pytest.raises(ResolverError, match="malformed"):
        parse_ref("@1.0", expected_kind="memory", registry=reg)


# ---------- npm_spec() error path ------------------------------------------

def test_npm_spec_on_bundled_raises(reg):
    ref = parse_ref("evermemos", expected_kind="memory", registry=reg)
    assert ref is not None
    with pytest.raises(RegistryError, match="not an npm plugin"):
        ref.npm_spec()


# ---------- parse_plugin_spec_overrides ------------------------------------

def test_plugin_spec_overrides_basic():
    out = parse_plugin_spec_overrides([
        "my-fork=/tmp/foo.tgz",
        "other=npm:@scope/other@1.0",
    ])
    assert out == {
        "my-fork": "/tmp/foo.tgz",
        "other": "npm:@scope/other@1.0",
    }


def test_plugin_spec_overrides_empty():
    assert parse_plugin_spec_overrides([]) == {}


def test_plugin_spec_overrides_missing_eq():
    with pytest.raises(ResolverError, match="requires id=spec"):
        parse_plugin_spec_overrides(["bare-token"])


def test_plugin_spec_overrides_empty_id_or_spec():
    with pytest.raises(ResolverError, match="malformed"):
        parse_plugin_spec_overrides(["=something"])
    with pytest.raises(ResolverError, match="malformed"):
        parse_plugin_spec_overrides(["id="])


def test_plugin_spec_overrides_duplicate_id():
    with pytest.raises(ResolverError, match="specified twice"):
        parse_plugin_spec_overrides([
            "my-fork=/tmp/a.tgz",
            "my-fork=/tmp/b.tgz",
        ])


def test_plugin_spec_overrides_spec_with_equals():
    """Spec value may itself contain '=' (e.g. URL with query string)."""
    out = parse_plugin_spec_overrides([
        "x=https://example.com/path?ref=v1.0",
    ])
    assert out["x"] == "https://example.com/path?ref=v1.0"


# ---------- PluginRef invariants -------------------------------------------

def test_plugin_ref_is_frozen(reg):
    ref = parse_ref("evermemos", expected_kind="memory", registry=reg)
    with pytest.raises((AttributeError, TypeError)):
        ref.version = "x"  # type: ignore[misc]

"""Stage 3 Phase 4 — stub-engine plugin manifest + source structure tests.

These are static structural checks that don't require docker/openclaw to
run. They guard against accidental drift in the plugin's surface that
would break the smoke gate.

Run:
    .venv/bin/python -m pytest tests/evaluation/test_stub_engine_plugin_manifest.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parents[2]
PLUGIN_DIR = REPO_ROOT / "openclaw-eval" / "plugins" / "stub-engine"


# --- manifest -------------------------------------------------------------

class TestPluginManifest:
    """openclaw.plugin.json structural invariants."""

    def test_manifest_exists(self):
        assert (PLUGIN_DIR / "openclaw.plugin.json").is_file()

    def test_manifest_id_matches_dir_name(self):
        m = json.loads((PLUGIN_DIR / "openclaw.plugin.json").read_text())
        assert m["id"] == "stub-engine"

    def test_manifest_kind_is_context_engine(self):
        """Smoke gate depends on slot resolution; wrong kind = wrong slot."""
        m = json.loads((PLUGIN_DIR / "openclaw.plugin.json").read_text())
        assert m["kind"] == "context-engine"

    def test_manifest_config_schema_is_empty_object(self):
        """Stub takes no config — keep schema minimal."""
        m = json.loads((PLUGIN_DIR / "openclaw.plugin.json").read_text())
        assert m["configSchema"]["type"] == "object"
        assert m["configSchema"].get("properties", {}) == {}


# --- package.json --------------------------------------------------------

class TestPluginPackageJson:
    def test_package_json_exists(self):
        assert (PLUGIN_DIR / "package.json").is_file()

    def test_package_json_has_openclaw_extensions(self):
        """openclaw discovery reads package.json#openclaw.extensions to find
        the entry file."""
        pkg = json.loads((PLUGIN_DIR / "package.json").read_text())
        assert pkg["type"] == "module"
        assert "./index.ts" in pkg["openclaw"]["extensions"]

    def test_package_json_no_runtime_deps(self):
        """Mirror the existing memory stub: no `dependencies` block keeps
        pnpm --frozen-lockfile happy when the workspace is added."""
        pkg = json.loads((PLUGIN_DIR / "package.json").read_text())
        assert "dependencies" not in pkg

    def test_package_json_is_private(self):
        pkg = json.loads((PLUGIN_DIR / "package.json").read_text())
        assert pkg.get("private") is True


# --- index.ts source ------------------------------------------------------

class TestPluginSource:
    def test_index_ts_exists(self):
        assert (PLUGIN_DIR / "index.ts").is_file()

    def test_index_ts_passphrase_constant(self):
        """The smoke gate asserts the reply contains WOMBAT_42; the source
        must export that exact string."""
        src = (PLUGIN_DIR / "index.ts").read_text()
        assert 'WOMBAT_42' in src

    def test_index_ts_uses_plugin_sdk_imports(self):
        """Type imports must come from openclaw/plugin-sdk (top-level), not
        a non-existent context-engine subpath."""
        src = (PLUGIN_DIR / "index.ts").read_text()
        assert 'from "openclaw/plugin-sdk/plugin-entry"' in src
        assert 'from "openclaw/plugin-sdk"' in src
        # Guard against the early-draft mistake of importing from a
        # subpath that doesn't exist in the package.json exports map.
        assert 'from "openclaw/plugin-sdk/context-engine"' not in src

    def test_index_ts_calls_register_context_engine(self):
        """register() must call api.registerContextEngine — anything else
        means the slot will resolve to "legacy" and the smoke gate fails
        with a confusing 'engine id not registered' error."""
        src = (PLUGIN_DIR / "index.ts").read_text()
        assert 'registerContextEngine("stub-engine"' in src

    def test_index_ts_kind_is_context_engine(self):
        """definePluginEntry kind must agree with the manifest."""
        src = (PLUGIN_DIR / "index.ts").read_text()
        assert 'kind: "context-engine"' in src

    def test_index_ts_assemble_returns_system_prompt_addition(self):
        """The smoke gate's load-bearing observable: assemble() must populate
        systemPromptAddition with the passphrase. Without this, the model
        won't see the sentinel and the smoke gate fails at criterion 4b."""
        src = (PLUGIN_DIR / "index.ts").read_text()
        assert 'systemPromptAddition' in src
        # Coarse but effective: the assemble body must reference both the
        # passphrase token (literal or via STUB_PASSPHRASE const) and the
        # systemPromptAddition field. We don't parse TS — would over-specify
        # the layout — but confirm both tokens occur after `async assemble`.
        idx_assemble = src.index('async assemble')
        body = src[idx_assemble:]
        assert 'systemPromptAddition' in body
        # Either the literal string or the STUB_PASSPHRASE const reference
        # must appear in the body (the const itself is defined to WOMBAT_42).
        assert 'STUB_PASSPHRASE' in body or 'WOMBAT_42' in body


# --- tsconfig --------------------------------------------------------------

class TestPluginTsConfig:
    def test_tsconfig_exists(self):
        assert (PLUGIN_DIR / "tsconfig.json").is_file()

    def test_tsconfig_extends_workspace_base(self):
        """openclaw's plugin build relies on the shared base tsconfig for
        package boundaries."""
        cfg = json.loads((PLUGIN_DIR / "tsconfig.json").read_text())
        assert cfg["extends"] == "../tsconfig.package-boundary.base.json"

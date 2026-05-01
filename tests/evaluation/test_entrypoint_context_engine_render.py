"""Tests for openclaw-eval/container/entrypoint.sh contextEngine slot render.

Drives the bash script in a docker-less harness: copies a minimal
template.json into a tmpdir, sets the env vars the entrypoint reads,
runs `bash entrypoint.sh true` (true = a quick CMD so the trap-then-wait
returns immediately), parses the rendered openclaw.docker.json, and
asserts on the plugin slot wiring.

Phase 1 of the Stage 3 plan: slots.contextEngine should be rendered
ONLY when CONTEXT_ENGINE_PLUGIN_ID env is set, and bundled-mode runs
(memory plugin only, no CONTEXT_ENGINE_PLUGIN_ID) should remain
unchanged from current behavior.

Run:
    .venv/bin/python -m pytest tests/evaluation/test_entrypoint_context_engine_render.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "openclaw-eval" / "container" / "entrypoint.sh"
TEMPLATE = REPO_ROOT / "openclaw-eval" / "container" / "openclaw.template.json"


def _run_entrypoint(tmp_path: Path, env: dict[str, str]) -> dict:
    """Run entrypoint.sh in tmp_path and return parsed openclaw.docker.json."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    out_path = tmp_path / "openclaw.docker.json"

    full_env = {
        **os.environ,
        "WORKSPACE_DIR": str(workspace),
        "TEMPLATE_PATH": str(TEMPLATE),
        "OPENCLAW_CONFIG_PATH": str(out_path),
        # Side effects we don't want to trigger in test:
        "MEM0_PORT": "0",  # makes any sidecar bind to ephemeral port (no-op since no /sidecar/server.py)
    }
    full_env.update(env)

    res = subprocess.run(
        ["bash", str(ENTRYPOINT), "true"],
        env=full_env,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=30,
    )
    if res.returncode != 0:
        raise AssertionError(
            f"entrypoint.sh exit {res.returncode}\nstderr:\n{res.stderr}\nstdout:\n{res.stdout}"
        )
    return json.loads(out_path.read_text())


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq required")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
class TestEntrypointContextEngineRender:
    def test_memory_only_no_context_engine_slot(self, tmp_path):
        """Bundled memory plugin: no CONTEXT_ENGINE_PLUGIN_ID env. Existing
        bundled-mode runs must be unchanged — no slots.contextEngine key
        should appear, so resolveContextEngine falls back to default 'legacy'.
        """
        cfg = _run_entrypoint(tmp_path, {
            "MEMORY_PLUGIN_ID": "mem0",
            "MEMORY_MODE": "mem0",
        })
        assert cfg["plugins"]["slots"]["memory"] == "mem0"
        assert "contextEngine" not in cfg["plugins"]["slots"]

    def test_context_engine_only_slot_rendered(self, tmp_path):
        """Pure context-engine plugin: memory_mode=noop, context_engine_mode
        set via CONTEXT_ENGINE_PLUGIN_ID env. Both slots should render;
        memory slot points at memory-core (the default for noop), context-
        engine slot points at our plugin.
        """
        cfg = _run_entrypoint(tmp_path, {
            "MEMORY_PLUGIN_ID": "noop",
            "MEMORY_MODE": "noop",
            "CONTEXT_ENGINE_PLUGIN_ID": "stub-engine",
        })
        assert cfg["plugins"]["slots"]["memory"] == "memory-core"
        assert cfg["plugins"]["slots"]["contextEngine"] == "stub-engine"

    def test_dual_kind_both_slots_rendered(self, tmp_path):
        """Memory + context-engine plugins coexist: both slots must render
        with the right ids.
        """
        cfg = _run_entrypoint(tmp_path, {
            "MEMORY_PLUGIN_ID": "mem0",
            "MEMORY_MODE": "mem0",
            "CONTEXT_ENGINE_PLUGIN_ID": "stub-engine",
        })
        assert cfg["plugins"]["slots"]["memory"] == "mem0"
        assert cfg["plugins"]["slots"]["contextEngine"] == "stub-engine"

    def test_context_engine_in_plugins_allow(self, tmp_path):
        """When CONTEXT_ENGINE_PLUGIN_ID is set, the engine id must be in
        plugins.allow — otherwise openclaw's loader denies it before
        resolveContextEngine ever runs.
        """
        cfg = _run_entrypoint(tmp_path, {
            "MEMORY_PLUGIN_ID": "noop",
            "MEMORY_MODE": "noop",
            "CONTEXT_ENGINE_PLUGIN_ID": "stub-engine",
        })
        assert "stub-engine" in cfg["plugins"]["allow"]

    def test_context_engine_in_plugins_entries(self, tmp_path):
        """The engine plugin must have an entry with enabled=true so the
        loader actually loads + registers it.
        """
        cfg = _run_entrypoint(tmp_path, {
            "MEMORY_PLUGIN_ID": "noop",
            "MEMORY_MODE": "noop",
            "CONTEXT_ENGINE_PLUGIN_ID": "stub-engine",
        })
        entries = cfg["plugins"]["entries"]
        assert "stub-engine" in entries
        assert entries["stub-engine"]["enabled"] is True

    def test_existing_install_mode_load_paths_still_works(self, tmp_path):
        """Phase 1 changes must not break Plan A install-mode wiring. When
        INSTALL_PLUGIN_ID is set AND the install dir exists, plugins.load.paths
        should still get injected.
        """
        # Stage a fake installed plugin dir so the entrypoint detects it.
        opt = tmp_path / "opt-openclaw"
        plugin_dir = opt / "extensions" / "fake-installed"
        plugin_dir.mkdir(parents=True)
        # Plugin minimal manifest — entrypoint just checks dir existence
        (plugin_dir / "openclaw.plugin.json").write_text(
            '{"id":"fake-installed","kind":"memory","configSchema":{}}'
        )

        cfg = _run_entrypoint(tmp_path, {
            "MEMORY_PLUGIN_ID": "fake-installed",
            "MEMORY_MODE": "fake-installed",
            "INSTALL_PLUGIN_ID": "fake-installed",
            "OPENCLAW_HOME": str(opt),
        })
        load_paths = cfg.get("plugins", {}).get("load", {}).get("paths", [])
        assert str(plugin_dir) in load_paths
        assert cfg["plugins"]["slots"]["memory"] == "fake-installed"
        assert "contextEngine" not in cfg["plugins"]["slots"]

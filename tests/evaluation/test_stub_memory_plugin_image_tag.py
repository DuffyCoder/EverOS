from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from evaluation.src.config.system_index import load_system_index
from evaluation.src.config.system_loader import resolve_system_config

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_PY = REPO_ROOT / "openclaw-eval" / "harness" / "build.py"
PLUGINS_DIR = REPO_ROOT / "openclaw-eval" / "plugins"
FAKE_ENVIRONMENT = {
    "LLM_API_KEY": "llm-key",
    "LLM_BASE_URL": "https://llm.example/v1",
    "OPENCLAW_REPO_PATH": "/tmp/openclaw",
}


def _load_build_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stub_image_build", BUILD_PY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stub_image_tag_is_derived_from_bundled_plugin_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _load_build_module()
    monkeypatch.setattr(
        build.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("tag derivation must not call Docker"),
    )
    registry = build.load_registry(build.DEFAULT_REGISTRY_PATH)
    memory_ref = build.parse_ref("stub", expected_kind="memory", registry=registry)

    plugin_rev = build.compute_plugin_rev(memory_ref, None, PLUGINS_DIR, {})
    expected_tag = build.derive_eval_tag(
        memory_ref, None, "7da23c3", plugin_rev, "slim"
    )
    resolved = resolve_system_config("openclaw-docker-stub", environ=FAKE_ENVIRONMENT)

    assert plugin_rev == "c1e9088"
    assert expected_tag == "openclaw-eval:7da23c3-stub-c1e9088-slim"
    assert resolved.raw_config["openclaw_docker"]["image"] == expected_tag


def test_stub_index_remains_experimental_and_documents_local_build() -> None:
    entry = load_system_index().systems["openclaw-docker-stub"]

    assert entry.category == "experiment"
    assert entry.status == "experimental"
    assert "built locally" in entry.description.lower()

"""Regression test for OpenClawAdapter._prepare_conversation_sandbox: when
yaml sets context_engine_mode, the value must flow through to
build_openclaw_resolved_config so the emitted on-disk config carries
plugins.slots.contextEngine. Without this, the host adapter writes a
config that omits the slot and openclaw never loads the engine plugin
even though downstream adapter logic treats context-engine mode as on.
"""
from __future__ import annotations

import json
from pathlib import Path

from evaluation.src.adapters.openclaw_adapter import OpenClawAdapter
from evaluation.src.core.data_models import Conversation


_AGENT_LLM = {
    "provider_id": "sophnet",
    "base_url": "https://www.sophnet.com/api/open-apis/v1",
    "api": "openai-completions",
    "api_key_env": "LLM_API_KEY",
    "env_vars": ["LLM_API_KEY"],
    "model": {
        "id": "gpt-4.1-mini",
        "name": "GPT 4.1 Mini",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "context_window": 128000,
        "max_tokens": 4096,
    },
}


def _adapter_with_cfg(tmp_path: Path, **openclaw_extra) -> OpenClawAdapter:
    config = {
        "openclaw": {
            "repo_path": str(tmp_path / "openclaw_repo_stub"),
            "backend_mode": "hybrid",
            "flush_mode": "shared_llm",
            "memory_mode": "memory-core",
            "agent_llm": _AGENT_LLM,
            **openclaw_extra,
        },
        "dataset_name": "locomo",
    }
    return OpenClawAdapter(config, output_dir=tmp_path)


def _conv() -> Conversation:
    return Conversation(conversation_id="conv_0", messages=[])


def _read_resolved(sandbox: dict) -> dict:
    return json.loads(Path(sandbox["resolved_config_path"]).read_text())


def test_context_engine_mode_passes_through_to_resolved_config(tmp_path: Path) -> None:
    """yaml openclaw.context_engine_mode → resolved.plugins.slots.contextEngine."""
    adapter = _adapter_with_cfg(tmp_path, context_engine_mode="hypercompositor")
    root_dir = tmp_path / "artifacts" / "openclaw" / "run-1"
    root_dir.mkdir(parents=True)
    sandbox = adapter._prepare_conversation_sandbox(root_dir, _conv())
    resolved = _read_resolved(sandbox)
    slots = resolved.get("plugins", {}).get("slots") or {}
    assert slots.get("contextEngine") == "hypercompositor"


def test_no_context_engine_mode_omits_slot(tmp_path: Path) -> None:
    """Backwards compat: no context_engine_mode → slot omitted, memory unchanged."""
    adapter = _adapter_with_cfg(tmp_path)  # no context_engine_mode
    root_dir = tmp_path / "artifacts" / "openclaw" / "run-1"
    root_dir.mkdir(parents=True)
    sandbox = adapter._prepare_conversation_sandbox(root_dir, _conv())
    resolved = _read_resolved(sandbox)
    slots = resolved.get("plugins", {}).get("slots") or {}
    assert "contextEngine" not in slots
    assert slots.get("memory") == "memory-core"


def test_blank_context_engine_mode_treated_as_unset(tmp_path: Path) -> None:
    """Empty string (yaml ${VAR:default} expansion when neither var nor default
    exists) must not enable the slot."""
    adapter = _adapter_with_cfg(tmp_path, context_engine_mode="")
    root_dir = tmp_path / "artifacts" / "openclaw" / "run-1"
    root_dir.mkdir(parents=True)
    sandbox = adapter._prepare_conversation_sandbox(root_dir, _conv())
    resolved = _read_resolved(sandbox)
    slots = resolved.get("plugins", {}).get("slots") or {}
    assert "contextEngine" not in slots

"""Adapter registry must surface the hermes adapter."""
from __future__ import annotations


def test_registry_lists_hermes():
    from evaluation.src.adapters.registry import list_adapters
    assert "hermes" in list_adapters()


def test_registry_can_create_hermes_adapter(tmp_path):
    from evaluation.src.adapters.registry import create_adapter

    config = {
        "adapter": "hermes",
        "hermes": {"repo_path": str(tmp_path), "plugin": "holographic"},
        "llm": {"provider": "openai", "model": "stub", "api_key": "x"},
    }
    adapter = create_adapter("hermes", config, output_dir=tmp_path)
    assert type(adapter).__name__ == "HermesAdapter"

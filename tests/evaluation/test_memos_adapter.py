from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evaluation.src.adapters.memos_adapter import MemosAdapter
from evaluation.src.config.system_loader import resolve_system_config


class _FakeLLMProvider:
    def __init__(self, **_: Any) -> None:
        pass


def _adapter(
    monkeypatch: pytest.MonkeyPatch, config_overrides: dict[str, object]
) -> MemosAdapter:
    monkeypatch.setattr(
        "evaluation.src.adapters.online_base.LLMProvider", _FakeLLMProvider
    )
    config: dict[str, object] = {
        "adapter": "memos",
        "api_url": "https://memory.example/v1",
        "api_key": "runtime-secret",
        "llm": {},
        **config_overrides,
    }
    return MemosAdapter(config, output_dir=Path("."))


def test_requests_per_second_configures_the_runtime_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter(monkeypatch, {"requests_per_second": 4})

    assert adapter.rate_limiter.max_rate == 4
    assert adapter.rate_limiter.time_period == 1
    assert adapter._session is None


@pytest.mark.parametrize(
    "config_overrides",
    [
        {},
        {"request_interval": 0.1},
        {"requests_per_second": 10},
    ],
    ids=("default", "legacy-request-interval", "explicit-rps"),
)
def test_default_legacy_and_explicit_configs_preserve_ten_requests_per_second(
    monkeypatch: pytest.MonkeyPatch, config_overrides: dict[str, object]
) -> None:
    adapter = _adapter(monkeypatch, config_overrides)

    assert adapter.rate_limiter.max_rate == 10
    assert adapter.rate_limiter.time_period == 1
    assert adapter._session is None


def test_canonical_memos_config_owns_the_runtime_rate_limit_field() -> None:
    resolved = resolve_system_config(
        "memos",
        environ={
            "LLM_API_KEY": "llm-key",
            "LLM_MODEL": "test-model",
            "MEMOS_KEY": "memos-key",
        },
    )

    assert "request_interval" not in resolved.raw_config
    assert resolved.raw_config["requests_per_second"] == 10

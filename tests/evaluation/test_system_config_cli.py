from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import evaluation.src.config.cli_support as cli_support
from evaluation.src.config.cli_support import (
    PreparedSystemConfig,
    apply_plugin_overrides_for_cli,
    default_result_dir,
    prepare_system_config_for_cli,
    resolve_system_for_cli,
    system_cli_warnings,
)
from evaluation.src.config.system_index import DEFAULT_SYSTEM_INDEX_PATH
from evaluation.src.config.system_loader import ResolvedSystemConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMS_ROOT = REPO_ROOT / "evaluation" / "config" / "systems"
FAKE_ENVIRONMENT = {
    "EVERMEMOS_API_KEY": "evermemos-key",
    "EVERMEMOS_API_URL": "https://memory.example/api/v1/memories",
    "HERMES_REPO_PATH": "/tmp/hermes",
    "LLM_API_KEY": "llm-key",
    "LLM_BASE_URL": "https://llm.example/v1",
    "LLM_MODEL": "test-model",
    "MEM0_API_KEY": "mem0-key",
    "MEMOS_KEY": "memos-key",
    "MEMU_API_KEY": "memu-key",
    "OPENCLAW_EMBED_MODEL": "embedding-model",
    "OPENCLAW_EMBED_PROVIDER": "test-provider",
    "OPENCLAW_REPO_PATH": "/tmp/openclaw",
    "OPENVIKING_API_KEY": "openviking-key",
    "OPENVIKING_INGEST_URL": "http://127.0.0.1:1933",
    "SOPH_API_KEY": "embed-key",
    "SOPH_EMBED_EASYLLM_ID": "deployment",
    "SOPH_EMBED_URL": "https://embed.example/v1",
    "ZEP_API_KEY": "zep-key",
}


def _write_modified_index(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> Path:
    document = yaml.safe_load(DEFAULT_SYSTEM_INDEX_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    mutate(document)
    path = tmp_path / "index.yaml"
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def test_cli_resolution_preserves_alias_identity_and_requested_result_name() -> None:
    resolution = resolve_system_for_cli("openclaw-hybrid", environ=FAKE_ENVIRONMENT)

    assert resolution.requested_id == "openclaw-hybrid"
    assert resolution.canonical_id == "openclaw"
    assert resolution.alias_chain == ("openclaw-hybrid", "openclaw")
    assert (
        default_result_dir(
            REPO_ROOT / "evaluation",
            dataset_id="locomo",
            requested_system_id="openclaw-hybrid",
            run_name=None,
        ).name
        == "locomo-openclaw-hybrid"
    )
    assert (
        default_result_dir(
            REPO_ROOT / "evaluation",
            dataset_id="locomo",
            requested_system_id="openclaw-hybrid",
            run_name="baseline",
        ).name
        == "locomo-openclaw-hybrid-baseline"
    )


def test_unknown_cli_system_exits_two_and_prints_close_matches(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        resolve_system_for_cli("openclaw-vectr", environ=FAKE_ENVIRONMENT)

    captured = capsys.readouterr()
    assert error.value.code == 2
    assert "unknown system id" in captured.err
    assert "openclaw-vector" in captured.err


def test_deprecated_and_experimental_entries_produce_visible_warnings(
    tmp_path: Path,
) -> None:
    def deprecate(document: dict[str, Any]) -> None:
        entry = document["systems"]["evermemos"]
        entry["status"] = "deprecated"
        entry["replacement"] = "mem0"

    index_path = _write_modified_index(tmp_path, deprecate)
    deprecated = resolve_system_for_cli(
        "evermemos",
        index_path=index_path,
        systems_root=SYSTEMS_ROOT,
        environ=FAKE_ENVIRONMENT,
    )
    experimental = resolve_system_for_cli(
        "openclaw-agent-local", environ=FAKE_ENVIRONMENT
    )

    assert any(
        "deprecated" in warning and "mem0" in warning
        for warning in system_cli_warnings(deprecated)
    )
    assert system_cli_warnings(experimental) == (
        "system 'openclaw-agent-local' is experimental",
    )


def test_non_docker_plugin_flags_are_one_noop_warning_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution = resolve_system_for_cli("mem0", environ=FAKE_ENVIRONMENT)
    config = deepcopy(resolution.config)
    original = deepcopy(config)

    def forbidden_apply(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("non-Docker adapters must not call plugin overrides")

    monkeypatch.setattr(cli_support, "apply_plugin_overrides", forbidden_apply)
    outcome = apply_plugin_overrides_for_cli(
        resolution,
        config,
        memory_plugin="evermemos",
        context_engine="openviking",
        image="openclaw-eval:ignored",
        build_missing=True,
    )

    assert config == original
    assert outcome.override is None
    assert outcome.warnings == (
        "plugin/image override flags are ignored for adapter 'mem0'",
    )


def test_docker_wrong_kind_plugin_is_rejected_before_any_mutation() -> None:
    resolution = resolve_system_for_cli("openclaw-docker", environ=FAKE_ENVIRONMENT)
    config = deepcopy(resolution.config)
    original = deepcopy(config)

    with pytest.raises(SystemExit) as error:
        apply_plugin_overrides_for_cli(
            resolution,
            config,
            memory_plugin="openviking",
            context_engine=None,
            image=None,
            build_missing=False,
        )

    assert error.value.code == 2
    assert config == original


def test_prepare_applies_dataset_patch_then_removes_patch_map_and_context() -> None:
    resolution = resolve_system_for_cli("memu", environ=FAKE_ENVIRONMENT)

    prepared = prepare_system_config_for_cli(
        resolution,
        dataset_id="locomo",
        clean_groups=True,
        memory_plugin=None,
        context_engine=None,
        image=None,
        build_missing=False,
    )

    assert prepared.config["num_workers"] == 1
    assert "dataset_overrides" not in prepared.config
    assert "dataset_overrides" in resolution.config
    assert "dataset_name" not in prepared.config
    assert "clean_groups" not in prepared.config
    assert prepared.runtime_context == {"dataset_name": "locomo", "clean_groups": True}


def test_prepare_applies_docker_plugin_override_after_dataset_patch() -> None:
    resolution = resolve_system_for_cli("openclaw-docker", environ=FAKE_ENVIRONMENT)

    prepared = prepare_system_config_for_cli(
        resolution,
        dataset_id="locomo",
        clean_groups=False,
        memory_plugin="none",
        context_engine=None,
        image=None,
        build_missing=False,
    )

    assert prepared.config["openclaw"]["memory_mode"] == "noop"
    assert prepared.plugin_override is not None
    assert prepared.plugin_override.memory_mode_applied == "noop"


def test_prepare_revalidates_cli_image_override_with_runtime_policy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    resolution = resolve_system_for_cli("openclaw-docker", environ=FAKE_ENVIRONMENT)

    with pytest.raises(SystemExit) as error:
        prepare_system_config_for_cli(
            resolution,
            dataset_id="locomo",
            clean_groups=False,
            memory_plugin=None,
            context_engine=None,
            image="openclaw-eval:TODO",
            build_missing=False,
        )

    assert error.value.code == 2
    assert "runtime policy" in capsys.readouterr().err


def test_prepare_shipped_stub_has_no_legacy_policy_findings() -> None:
    resolution = resolve_system_for_cli(
        "openclaw-docker-stub", environ=FAKE_ENVIRONMENT
    )

    prepared = prepare_system_config_for_cli(
        resolution,
        dataset_id="locomo",
        clean_groups=False,
        memory_plugin=None,
        context_engine=None,
        image=None,
        build_missing=False,
    )

    assert prepared.runtime_policy_findings == ()


def _synthetic_resolution(tmp_path: Path) -> ResolvedSystemConfig:
    source = tmp_path / "systems" / "mem0.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("adapter: mem0\n", encoding="utf-8")
    config = {
        "adapter": "mem0",
        "llm": {
            "provider": "openai",
            "model": "test-model",
            "api_key": "secret",
            "base_url": "https://llm.example/v1",
            "max_tokens": 1024,
        },
    }
    return ResolvedSystemConfig(
        requested_id="mem0",
        canonical_id="mem0",
        category="canonical",
        status="active",
        adapter="mem0",
        config=config,
        raw_config=config,
        source_paths=(source.resolve(),),
        alias_chain=("mem0",),
        policy_findings=(),
        warning=None,
    )


def _synthetic_prepared(resolution: ResolvedSystemConfig) -> PreparedSystemConfig:
    return PreparedSystemConfig(
        config=deepcopy(resolution.config),
        runtime_context={"dataset_name": "locomo", "clean_groups": False},
        plugin_override=None,
        warnings=(),
        runtime_policy_findings=(),
    )


def _import_cli(monkeypatch: pytest.MonkeyPatch):
    import common_utils.load_env as load_env

    monkeypatch.setattr(load_env, "setup_environment", lambda **_kwargs: True)
    sys.modules.pop("evaluation.cli", None)
    return importlib.import_module("evaluation.cli")


def test_cli_metadata_failure_exits_before_adapter_and_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _import_cli(monkeypatch)

    resolution = _synthetic_resolution(tmp_path)
    prepared = _synthetic_prepared(resolution)
    dataset = SimpleNamespace(conversations=[], qa_pairs=[])
    calls: list[str] = []

    monkeypatch.setattr(cli, "resolve_system_for_cli", lambda *_a, **_k: resolution)
    monkeypatch.setattr(
        cli, "prepare_system_config_for_cli", lambda *_a, **_k: prepared
    )
    monkeypatch.setattr(cli, "load_dataset", lambda *_a, **_k: dataset)

    def reject_metadata(*_args: object, **_kwargs: object) -> None:
        calls.append("metadata")
        raise SystemExit(2)

    monkeypatch.setattr(cli, "write_resolved_system_metadata", reject_metadata)
    monkeypatch.setattr(
        cli, "create_adapter", lambda *_a, **_k: calls.append("adapter")
    )
    monkeypatch.setattr(cli, "Pipeline", lambda *_a, **_k: calls.append("pipeline"))

    with pytest.raises(SystemExit) as error:
        asyncio.run(
            cli.main(
                [
                    "--dataset",
                    "locomo",
                    "--system",
                    "mem0",
                    "--output-dir",
                    str(tmp_path / "run"),
                ]
            )
        )

    assert error.value.code == 2
    assert calls == ["metadata"]


def test_cli_writes_metadata_before_adapter_and_emits_adoption_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cli = _import_cli(monkeypatch)

    resolution = _synthetic_resolution(tmp_path)
    prepared = _synthetic_prepared(resolution)
    dataset = SimpleNamespace(conversations=[], qa_pairs=[])
    output_dir = tmp_path / "run"
    metadata_path = output_dir / "resolved-system-config.json"

    monkeypatch.setattr(cli, "resolve_system_for_cli", lambda *_a, **_k: resolution)
    monkeypatch.setattr(
        cli, "prepare_system_config_for_cli", lambda *_a, **_k: prepared
    )
    monkeypatch.setattr(cli, "load_dataset", lambda *_a, **_k: dataset)

    def write_metadata(*_args: object, **_kwargs: object) -> SimpleNamespace:
        final_config = _args[1]
        runtime_context = _args[2]
        assert isinstance(final_config, dict)
        assert "dataset_name" not in final_config
        assert "clean_groups" not in final_config
        assert runtime_context == {"dataset_name": "locomo", "clean_groups": False}
        assert _kwargs["adopt_legacy"] is True
        assert _kwargs["systems_root"] == (
            REPO_ROOT / "evaluation" / "config" / "systems"
        )
        output_dir.mkdir(parents=True)
        metadata_path.write_text("{}\n", encoding="utf-8")
        return SimpleNamespace(warning="UNVERIFIED legacy checkpoint adopted")

    monkeypatch.setattr(cli, "write_resolved_system_metadata", write_metadata)

    class Adapter:
        def get_system_info(self) -> dict[str, str]:
            return {"name": "test-adapter"}

        async def close(self) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    def create_test_adapter(
        _adapter: str, config: dict[str, Any], *, output_dir: Path
    ) -> Adapter:
        assert metadata_path.is_file()
        assert config["dataset_name"] == "locomo"
        assert config["clean_groups"] is False
        return Adapter()

    monkeypatch.setattr(cli, "create_adapter", create_test_adapter)
    monkeypatch.setattr(
        cli,
        "create_evaluator",
        lambda *_a, **_k: SimpleNamespace(get_name=lambda: "test-evaluator"),
    )
    monkeypatch.setattr(cli, "LLMProvider", lambda **_kwargs: object())

    class TestPipeline:
        def __init__(self, **_kwargs: object) -> None:
            assert metadata_path.is_file()

        async def run(self, **_kwargs: object) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(cli, "Pipeline", TestPipeline)

    asyncio.run(
        cli.main(
            [
                "--dataset",
                "locomo",
                "--system",
                "mem0",
                "--output-dir",
                str(output_dir),
                "--adopt-legacy-result-dir",
            ]
        )
    )

    assert "UNVERIFIED legacy checkpoint adopted" in capsys.readouterr().out

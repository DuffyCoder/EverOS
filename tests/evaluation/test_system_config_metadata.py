from __future__ import annotations

import hashlib
import json
import os
import queue
import stat
import threading
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evaluation.src.config import system_metadata as metadata_module
from evaluation.src.config.system_loader import ResolvedSystemConfig
from evaluation.src.config.system_metadata import (
    METADATA_FILENAME,
    write_resolved_system_metadata,
)


def _resolution(
    systems_root: Path, *, runtime_secret: str = "runtime-secret-value"
) -> ResolvedSystemConfig:
    source = systems_root / "canonical" / "openclaw.yaml"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("fixture\n", encoding="utf-8")
    raw = {
        "adapter": "openclaw",
        "llm": {
            "provider": "openai",
            "model": "${LLM_MODEL:test-model}",
            "api_key": "${LLM_API_KEY}",
            "base_url": "${LLM_BASE_URL:https://llm.example/v1}",
            "max_tokens": 1024,
        },
        "openclaw": {
            "agent_llm": {
                "api_key_env": "LLM_API_KEY",
                "env_vars": ["LLM_API_KEY", "EXTRA_ALLOWED_ENV"],
            }
        },
        "dataset_overrides": {"locomo": {"num_workers": 1}},
    }
    runtime = {
        "adapter": "openclaw",
        "llm": {
            "provider": "openai",
            "model": "machine-model",
            "api_key": runtime_secret,
            "base_url": "https://machine-llm.example/v1",
            "max_tokens": 1024,
        },
        "openclaw": {
            "agent_llm": {
                "api_key_env": "LLM_API_KEY",
                "env_vars": ["LLM_API_KEY", "EXTRA_ALLOWED_ENV"],
            }
        },
        "dataset_overrides": {"locomo": {"num_workers": 1}},
    }
    return ResolvedSystemConfig(
        requested_id="openclaw-hybrid",
        canonical_id="openclaw",
        category="alias",
        status="compatibility",
        adapter="openclaw",
        config=runtime,
        raw_config=raw,
        source_paths=(source.resolve(),),
        alias_chain=("openclaw-hybrid", "openclaw"),
        policy_findings=(),
        warning=None,
    )


def _final_runtime(resolution: ResolvedSystemConfig) -> dict[str, Any]:
    config = dict(resolution.config)
    config.pop("dataset_overrides", None)
    config["num_workers"] = 1
    return config


def _write(
    resolution: ResolvedSystemConfig,
    output_dir: Path,
    systems_root: Path,
    *,
    dataset_id: str = "locomo",
    runtime_context: dict[str, Any] | None = None,
    adopt_legacy: bool = False,
):
    return write_resolved_system_metadata(
        resolution,
        _final_runtime(resolution),
        runtime_context or {"dataset_name": dataset_id, "clean_groups": False},
        output_dir,
        dataset_id,
        systems_root=systems_root,
        adopt_legacy=adopt_legacy,
    )


def _concurrent_write(
    resolution: ResolvedSystemConfig,
    final_runtime_config: dict[str, Any],
    runtime_context: dict[str, Any],
    output_dir: Path,
    dataset_id: str,
    systems_root: Path,
    start: threading.Event,
    results: queue.Queue[tuple[str, object]],
) -> None:
    start.wait()
    try:
        result = write_resolved_system_metadata(
            resolution,
            final_runtime_config,
            runtime_context,
            output_dir,
            dataset_id,
            systems_root=systems_root,
        )
    except SystemExit as exc:
        results.put(("exit", exc.code))
    except BaseException as exc:  # pragma: no cover - diagnostic for child failures
        results.put(("error", repr(exc)))
    else:
        results.put(("created", result.created))


def test_metadata_has_exact_v1_schema_relative_sources_and_no_secret(
    tmp_path: Path,
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "results" / "run"

    result = _write(resolution, output_dir, systems_root)

    metadata_path = output_dir / METADATA_FILENAME
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema",
        "provenance_status",
        "dataset_id",
        "requested_id",
        "canonical_id",
        "adapter",
        "category",
        "status",
        "alias_chain",
        "source_paths",
        "raw_config_sha256",
        "runtime_config_sha256",
        "environment_references",
        "runtime_context",
        "config",
    }
    assert payload["schema"] == "evaluation-system-config/v1"
    assert payload["provenance_status"] == "verified"
    assert payload["dataset_id"] == "locomo"
    assert payload["requested_id"] == "openclaw-hybrid"
    assert payload["canonical_id"] == "openclaw"
    assert payload["adapter"] == "openclaw"
    assert payload["category"] == "alias"
    assert payload["status"] == "compatibility"
    assert payload["alias_chain"] == ["openclaw-hybrid", "openclaw"]
    assert payload["source_paths"] == ["canonical/openclaw.yaml"]
    assert payload["environment_references"] == [
        "EXTRA_ALLOWED_ENV",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
    ]
    assert payload["runtime_context"] == {
        "dataset_name": "locomo",
        "clean_groups": False,
    }
    assert len(payload["raw_config_sha256"]) == 64
    assert len(payload["runtime_config_sha256"]) == 64
    serialized = metadata_path.read_text(encoding="utf-8")
    assert "runtime-secret-value" not in serialized
    assert "${LLM_API_KEY}" in serialized
    assert result.created is True
    assert result.adopted_legacy is False


def test_metadata_hashes_never_depend_on_materialized_environment_values(
    tmp_path: Path,
) -> None:
    systems_root = tmp_path / "systems"
    first = _resolution(systems_root, runtime_secret="first-secret")
    second = _resolution(systems_root, runtime_secret="second-secret")
    first_output = tmp_path / "first"
    second_output = tmp_path / "second"

    _write(first, first_output, systems_root)
    _write(second, second_output, systems_root)

    first_payload = json.loads(
        (first_output / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    second_payload = json.loads(
        (second_output / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert first_payload == second_payload


@pytest.mark.parametrize(
    ("first_default", "second_default"),
    [
        (
            "https://raw-user:first-raw-secret@llm.example/v1",
            "https://raw-user:second-raw-secret@llm.example/v1",
        ),
        (
            "https:/raw-user:first-raw-secret@llm.example/v1",
            "https:/raw-user:second-raw-secret@llm.example/v1",
        ),
        (
            "https:raw-user:first-raw-secret@llm.example/v1",
            "https:raw-user:second-raw-secret@llm.example/v1",
        ),
        (
            "///raw-user:first-raw-secret@llm.example/v1",
            "///raw-user:second-raw-secret@llm.example/v1",
        ),
        (
            "https://llm.example/v1?api_key=first-raw-secret",
            "https://llm.example/v1?api_key=second-raw-secret",
        ),
        (
            "https://llm.example/v1?apikey=first-raw-secret",
            "https://llm.example/v1?apikey=second-raw-secret",
        ),
        (
            "https://llm.example/#/route?api_key=first-raw-secret",
            "https://llm.example/#/route?api_key=second-raw-secret",
        ),
    ],
)
def test_raw_hash_never_depends_on_credential_environment_marker_defaults(
    tmp_path: Path, first_default: str, second_default: str
) -> None:
    systems_root = tmp_path / "systems"
    first = _resolution(systems_root)
    first_raw = deepcopy(first.raw_config)
    first_raw["llm"]["base_url"] = f"${{LLM_BASE_URL:{first_default}}}"
    second_raw = deepcopy(first_raw)
    second_raw["llm"]["base_url"] = f"${{LLM_BASE_URL:{second_default}}}"
    first = replace(first, raw_config=first_raw)
    second = replace(first, raw_config=second_raw)

    _write(first, tmp_path / "first", systems_root)
    _write(second, tmp_path / "second", systems_root)

    first_payload = json.loads(
        (tmp_path / "first" / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    second_payload = json.loads(
        (tmp_path / "second" / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert first_payload == second_payload
    serialized = json.dumps(first_payload, sort_keys=True)
    assert "first-raw-secret" not in serialized
    assert "second-raw-secret" not in serialized


def test_raw_hash_distinguishes_non_secret_environment_marker_defaults(
    tmp_path: Path,
) -> None:
    systems_root = tmp_path / "systems"
    first = _resolution(systems_root)
    first_raw = deepcopy(first.raw_config)
    first_raw["llm"]["model"] = "${LLM_MODEL:model-a}"
    second_raw = deepcopy(first_raw)
    second_raw["llm"]["model"] = "${LLM_MODEL:model-b}"
    first = replace(first, raw_config=first_raw)
    second = replace(first, raw_config=second_raw)

    _write(first, tmp_path / "first", systems_root)
    _write(second, tmp_path / "second", systems_root)

    first_payload = json.loads(
        (tmp_path / "first" / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    second_payload = json.loads(
        (tmp_path / "second" / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert first_payload["raw_config_sha256"] != second_payload["raw_config_sha256"]
    assert (
        first_payload["runtime_config_sha256"]
        == second_payload["runtime_config_sha256"]
    )


@pytest.mark.parametrize(
    "raw_marker",
    [
        "${OPENAI_API_KEY}",
        "prefix-${OPENAI_API_KEY}-suffix",
        "${GITHUB_TOKEN}",
        "prefix-${GITHUB_TOKEN}-suffix",
        "${BEARER_TOKEN}",
        "${PRIVATE_KEY_PEM}",
    ],
)
def test_secret_like_environment_markers_in_non_secret_fields_are_redacted(
    tmp_path: Path, raw_marker: str
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    raw = deepcopy(resolution.raw_config)
    raw["llm"]["model"] = raw_marker
    first_runtime = deepcopy(resolution.config)
    first_runtime["llm"]["model"] = "first-materialized-secret"
    second_runtime = deepcopy(first_runtime)
    second_runtime["llm"]["model"] = "second-materialized-secret"
    first = replace(resolution, raw_config=raw, config=first_runtime)
    second = replace(resolution, raw_config=raw, config=second_runtime)

    _write(first, tmp_path / "first", systems_root)
    _write(second, tmp_path / "second", systems_root)

    first_payload = json.loads(
        (tmp_path / "first" / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    second_payload = json.loads(
        (tmp_path / "second" / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert first_payload == second_payload
    assert first_payload["config"]["llm"]["model"] == raw_marker
    serialized = json.dumps(first_payload, sort_keys=True)
    assert "first-materialized-secret" not in serialized
    assert "second-materialized-secret" not in serialized


@pytest.mark.parametrize(
    "environment_name",
    ["OPENAI_API_KEY", "GITHUB_TOKEN", "BEARER_TOKEN", "PRIVATE_KEY_PEM"],
)
def test_secret_like_environment_marker_defaults_never_affect_hashes(
    tmp_path: Path, environment_name: str
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    first_raw = deepcopy(resolution.raw_config)
    first_raw["llm"]["model"] = f"${{{environment_name}:first-default-secret}}"
    second_raw = deepcopy(first_raw)
    second_raw["llm"]["model"] = f"${{{environment_name}:second-default-secret}}"
    first_runtime = deepcopy(resolution.config)
    first_runtime["llm"]["model"] = "first-materialized-secret"
    second_runtime = deepcopy(first_runtime)
    second_runtime["llm"]["model"] = "second-materialized-secret"
    first = replace(resolution, raw_config=first_raw, config=first_runtime)
    second = replace(resolution, raw_config=second_raw, config=second_runtime)

    _write(first, tmp_path / "first", systems_root)
    _write(second, tmp_path / "second", systems_root)

    first_payload = json.loads(
        (tmp_path / "first" / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    second_payload = json.loads(
        (tmp_path / "second" / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert first_payload == second_payload
    assert (
        first_payload["config"]["llm"]["model"]
        == f"${{{environment_name}:<redacted-default>}}"
    )
    serialized = json.dumps(first_payload, sort_keys=True)
    for secret in (
        "first-default-secret",
        "second-default-secret",
        "first-materialized-secret",
        "second-materialized-secret",
    ):
        assert secret not in serialized


def test_materialized_private_key_is_redacted_for_non_secret_field(
    tmp_path: Path,
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    raw = deepcopy(resolution.raw_config)
    raw["llm"]["model"] = "${SIGNING_KEY}"
    first_runtime = deepcopy(resolution.config)
    first_runtime["llm"][
        "model"
    ] = "-----BEGIN PRIVATE KEY-----\nfirst-private-key\n-----END PRIVATE KEY-----"
    second_runtime = deepcopy(first_runtime)
    second_runtime["llm"][
        "model"
    ] = "-----BEGIN PRIVATE KEY-----\nsecond-private-key\n-----END PRIVATE KEY-----"
    first = replace(resolution, raw_config=raw, config=first_runtime)
    second = replace(resolution, raw_config=raw, config=second_runtime)

    _write(first, tmp_path / "first", systems_root)
    _write(second, tmp_path / "second", systems_root)

    first_payload = json.loads(
        (tmp_path / "first" / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    second_payload = json.loads(
        (tmp_path / "second" / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert first_payload == second_payload
    assert first_payload["config"]["llm"]["model"] == "${SIGNING_KEY}"
    serialized = json.dumps(first_payload, sort_keys=True)
    assert "first-private-key" not in serialized
    assert "second-private-key" not in serialized


def test_selected_dataset_override_and_nested_structured_secrets_are_redacted(
    tmp_path: Path,
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    raw = deepcopy(resolution.raw_config)
    raw["llm"]["api_key"] = "${BASE_LLM_KEY}"
    raw["service"] = {
        "webhookToken": "${BASE_WEBHOOK_TOKEN}",
        "proxy-authorization": {
            "username": "machine-user",
            "password": "${PROXY_PASSWORD}",
        },
        "nestedCredentials": [
            {"access-token": "${ACCESS_TOKEN}"},
            "${STRUCTURED_TOKEN}",
        ],
        "apiKeyEnv": "BARE_API_KEY_ENV",
        "env-vars": ["ALLOWED_ONE", "ALLOWED_TWO"],
        "honorSilentToken": "not-a-secret",
    }
    raw["dataset_overrides"] = {
        "locomo": {
            "llm": {"api_key": "${LOCOMO_LLM_KEY}"},
            "service": {"webhookToken": "${LOCOMO_WEBHOOK_TOKEN}"},
        },
        "other": {
            "llm": {"api_key": "${UNSELECTED_LLM_KEY}"},
            "service": {"webhookToken": "${UNSELECTED_WEBHOOK_TOKEN}"},
        },
    }
    runtime = deepcopy(raw)
    runtime["llm"]["api_key"] = "base-runtime-secret"
    runtime["service"]["webhookToken"] = "base-webhook-secret"
    runtime["service"]["proxy-authorization"]["password"] = "proxy-secret"
    runtime["service"]["nestedCredentials"] = [
        {"access-token": "access-secret"},
        "structured-secret",
    ]
    runtime["dataset_overrides"]["locomo"]["llm"]["api_key"] = "locomo-runtime-secret"
    runtime["dataset_overrides"]["locomo"]["service"][
        "webhookToken"
    ] = "locomo-webhook-secret"
    changed = replace(resolution, raw_config=raw, config=runtime)
    final_runtime = deepcopy(runtime)
    overrides = final_runtime.pop("dataset_overrides")
    final_runtime["llm"].update(overrides["locomo"]["llm"])
    final_runtime["service"].update(overrides["locomo"]["service"])
    output_dir = tmp_path / "run"

    write_resolved_system_metadata(
        changed,
        final_runtime,
        {"dataset_name": "locomo", "clean_groups": False},
        output_dir,
        "locomo",
        systems_root=systems_root,
    )

    payload = json.loads((output_dir / METADATA_FILENAME).read_text(encoding="utf-8"))
    config = payload["config"]
    assert config["llm"]["api_key"] == "${LOCOMO_LLM_KEY}"
    assert config["service"]["webhookToken"] == "${LOCOMO_WEBHOOK_TOKEN}"
    assert config["service"]["proxy-authorization"] == {
        "username": "<redacted>",
        "password": "${PROXY_PASSWORD}",
    }
    assert config["service"]["nestedCredentials"] == [
        {"access-token": "${ACCESS_TOKEN}"},
        "${STRUCTURED_TOKEN}",
    ]
    assert config["service"]["apiKeyEnv"] == "BARE_API_KEY_ENV"
    assert config["service"]["env-vars"] == ["ALLOWED_ONE", "ALLOWED_TWO"]
    assert config["service"]["honorSilentToken"] == "not-a-secret"
    assert payload["environment_references"] == [
        "ACCESS_TOKEN",
        "ALLOWED_ONE",
        "ALLOWED_TWO",
        "BARE_API_KEY_ENV",
        "EXTRA_ALLOWED_ENV",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LOCOMO_LLM_KEY",
        "LOCOMO_WEBHOOK_TOKEN",
        "PROXY_PASSWORD",
        "STRUCTURED_TOKEN",
    ]
    serialized = json.dumps(payload, sort_keys=True)
    for secret in (
        "base-runtime-secret",
        "base-webhook-secret",
        "locomo-runtime-secret",
        "locomo-webhook-secret",
        "proxy-secret",
        "access-secret",
        "structured-secret",
    ):
        assert secret not in serialized
    assert "BASE_LLM_KEY" not in payload["environment_references"]
    assert "BASE_WEBHOOK_TOKEN" not in payload["environment_references"]
    assert "UNSELECTED_LLM_KEY" not in payload["environment_references"]
    assert "UNSELECTED_WEBHOOK_TOKEN" not in payload["environment_references"]


def test_hashes_use_stable_compact_redacted_json(tmp_path: Path) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    context = {"dataset_name": "locomo", "clean_groups": False}

    _write(resolution, output_dir, systems_root, runtime_context=context)

    payload = json.loads((output_dir / METADATA_FILENAME).read_text(encoding="utf-8"))
    compact = {"sort_keys": True, "separators": (",", ":"), "ensure_ascii": False}
    expected_raw = hashlib.sha256(
        json.dumps(resolution.raw_config, allow_nan=False, **compact).encode("utf-8")
    ).hexdigest()
    expected_runtime = hashlib.sha256(
        json.dumps(
            {"config": payload["config"], "runtime_context": context},
            allow_nan=False,
            **compact,
        ).encode("utf-8")
    ).hexdigest()

    assert payload["raw_config_sha256"] == expected_raw
    assert payload["runtime_config_sha256"] == expected_runtime


def test_source_path_outside_systems_root_is_rejected(tmp_path: Path) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    outside = tmp_path / "outside.yaml"
    outside.write_text("fixture\n", encoding="utf-8")
    changed = replace(resolution, source_paths=(outside.resolve(),))
    output_dir = tmp_path / "run"

    with pytest.raises(SystemExit) as error:
        _write(changed, output_dir, systems_root)

    assert error.value.code == 2
    assert not output_dir.exists()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://runtime-user:runtime-url-secret@llm.example/v1",
        "https://runtime-user:runtime-url-secret@[invalid",
        "https:/runtime-user:runtime-url-secret@llm.example/v1",
        "https:runtime-user:runtime-url-secret@llm.example/v1",
        "//runtime-user:runtime-url-secret@llm.example/v1",
        "//runtime-user:runtime-url-secret@[invalid",
        "///runtime-user:runtime-url-secret@llm.example/v1",
        "https://llm.example/v1?apikey=runtime-url-secret",
        "https://llm.example/v1#apikey=runtime-url-secret",
        "https://llm.example/#/route?api_key=runtime-url-secret",
        "https://llm.example/v1?jwt=runtime-url-secret",
        "https://llm.example/v1?passwd=runtime-url-secret",
        "https://llm.example/v1?pat=runtime-url-secret",
    ],
)
def test_materialized_url_credentials_are_rejected_without_leaking(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], base_url: str
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    final_runtime = _final_runtime(resolution)
    final_runtime["llm"]["base_url"] = base_url
    output_dir = tmp_path / "run"

    with pytest.raises(SystemExit) as error:
        write_resolved_system_metadata(
            resolution,
            final_runtime,
            {"dataset_name": "locomo", "clean_groups": False},
            output_dir,
            "locomo",
            systems_root=systems_root,
        )

    assert error.value.code == 2
    assert "runtime-url-secret" not in capsys.readouterr().err
    assert not output_dir.exists()


@pytest.mark.parametrize(
    "base_url",
    [
        "https:/runtime-user:runtime-url-secret@llm.example/v1",
        "https:runtime-user:runtime-url-secret@llm.example/v1",
        "https://runtime-user:runtime-url-secret@llm.example/${PATH_SUFFIX}",
        "///runtime-user:runtime-url-secret@llm.example/v1",
        "https://llm.example/v1?apikey=runtime-url-secret",
        "https://llm.example/v1#apikey=runtime-url-secret",
        "https://llm.example/#/route?api_key=runtime-url-secret",
    ],
)
def test_sensitive_raw_marker_does_not_bypass_url_credential_rejection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], base_url: str
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    raw = deepcopy(resolution.raw_config)
    raw["llm"]["base_url"] = "${OPENAI_API_KEY}"
    resolution = replace(resolution, raw_config=raw)
    final_runtime = _final_runtime(resolution)
    final_runtime["llm"]["base_url"] = base_url
    output_dir = tmp_path / "run"

    with pytest.raises(SystemExit) as error:
        write_resolved_system_metadata(
            resolution,
            final_runtime,
            {"dataset_name": "locomo", "clean_groups": False},
            output_dir,
            "locomo",
            systems_root=systems_root,
        )

    assert error.value.code == 2
    assert "runtime-url-secret" not in capsys.readouterr().err
    assert not output_dir.exists()


def test_unmaterialized_sensitive_marker_cannot_hide_literal_url_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    template = "https://runtime-user:runtime-url-secret@llm.example/${OPENAI_API_KEY}"
    raw = deepcopy(resolution.raw_config)
    raw["llm"]["base_url"] = template
    runtime = deepcopy(resolution.config)
    runtime["llm"]["base_url"] = template
    resolution = replace(resolution, raw_config=raw, config=runtime)
    output_dir = tmp_path / "run"

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root)

    assert error.value.code == 2
    assert "runtime-url-secret" not in capsys.readouterr().err
    assert not output_dir.exists()


def test_new_metadata_is_installed_with_same_directory_atomic_no_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    observed: list[tuple[Path, Path, int, int]] = []
    real_link = os.link

    def record_link(
        source: os.PathLike[str], target: os.PathLike[str], **kwargs: object
    ) -> None:
        source_fd = kwargs["src_dir_fd"]
        target_fd = kwargs["dst_dir_fd"]
        assert isinstance(source_fd, int)
        assert isinstance(target_fd, int)
        observed.append((Path(source), Path(target), source_fd, target_fd))
        real_link(source, target, **kwargs)

    monkeypatch.setattr(os, "link", record_link)

    _write(resolution, output_dir, systems_root)

    assert len(observed) == 1
    source, target, source_fd, target_fd = observed[0]
    assert source.parent == Path(".")
    assert source.name.startswith(f".{METADATA_FILENAME}.")
    assert source.name.endswith(".tmp")
    assert target == Path(METADATA_FILENAME)
    assert source_fd == target_fd
    assert list(output_dir.glob(f".{METADATA_FILENAME}.*.tmp")) == []


def test_atomic_install_fsyncs_file_before_publish_and_directory_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    events: list[tuple[str, bool | None]] = []
    real_fsync = os.fsync
    real_link = os.link

    def record_fsync(descriptor: int) -> None:
        events.append(("fsync", stat.S_ISDIR(os.fstat(descriptor).st_mode)))
        real_fsync(descriptor)

    def record_link(
        source: os.PathLike[str], target: os.PathLike[str], **kwargs: object
    ) -> None:
        events.append(("publish", None))
        real_link(source, target, **kwargs)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "link", record_link)

    _write(resolution, output_dir, systems_root)

    publish_index = events.index(("publish", None))
    assert ("fsync", False) in events[:publish_index]
    assert ("fsync", True) in events[publish_index + 1 :]


def test_atomic_install_cleans_temporary_file_when_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected publish failure")

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root)

    assert error.value.code == 2
    assert "injected publish failure" not in capsys.readouterr().err
    assert not (output_dir / METADATA_FILENAME).exists()
    assert list(output_dir.glob(f".{METADATA_FILENAME}.*.tmp")) == []


def test_stale_metadata_temporary_file_is_cleaned_before_retry(tmp_path: Path) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    stale = output_dir / f".{METADATA_FILENAME}.deadbeef.tmp"
    stale.write_text('{"partial": true', encoding="utf-8")

    result = _write(resolution, output_dir, systems_root)

    assert result.created is True
    assert not stale.exists()
    assert (output_dir / METADATA_FILENAME).is_file()


def test_directory_replacement_during_publish_fails_without_touching_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    moved_dir = tmp_path / "run-moved"
    real_link = os.link

    def replace_directory_then_link(
        source: os.PathLike[str], target: os.PathLike[str], **kwargs: object
    ) -> None:
        output_dir.rename(moved_dir)
        output_dir.mkdir()
        real_link(source, target, **kwargs)

    monkeypatch.setattr(os, "link", replace_directory_then_link)

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root)

    assert error.value.code == 2
    assert not (output_dir / METADATA_FILENAME).exists()
    assert (moved_dir / METADATA_FILENAME).is_file()


def test_directory_enumeration_error_is_reported_as_cli_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"

    def fail_listdir(_path: object) -> list[str]:
        raise OSError("injected list failure")

    monkeypatch.setattr(metadata_module.os, "listdir", fail_listdir)

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root)

    assert error.value.code == 2
    assert "injected list failure" not in capsys.readouterr().err


def test_directory_lock_error_is_reported_as_cli_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"

    def fail_lock(_descriptor: int, _operation: int) -> None:
        raise OSError("injected lock failure")

    monkeypatch.setattr(metadata_module.fcntl, "flock", fail_lock)

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root)

    assert error.value.code == 2
    assert "injected lock failure" not in capsys.readouterr().err
    assert not (output_dir / METADATA_FILENAME).exists()


def test_file_fsync_failure_is_cli_failure_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    real_fsync = os.fsync

    def fail_file_fsync(descriptor: int) -> None:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected file fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(metadata_module.os, "fsync", fail_file_fsync)

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root)

    assert error.value.code == 2
    assert "injected file fsync failure" not in capsys.readouterr().err
    assert not (output_dir / METADATA_FILENAME).exists()
    assert list(output_dir.glob(f".{METADATA_FILENAME}.*.tmp")) == []


def test_directory_fsync_failure_reports_published_but_uncertain_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    real_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("injected directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(metadata_module.os, "fsync", fail_directory_fsync)

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root)

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "published" in stderr
    assert "durability" in stderr
    assert "injected directory fsync failure" not in stderr
    assert (output_dir / METADATA_FILENAME).is_file()
    assert list(output_dir.glob(f".{METADATA_FILENAME}.*.tmp")) == []


def test_stale_temporary_symlink_is_not_removed_or_adopted(tmp_path: Path) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    external = tmp_path / "external.json"
    external.write_text('{"sentinel": true}\n', encoding="utf-8")
    stale = output_dir / f".{METADATA_FILENAME}.deadbeef.tmp"
    stale.symlink_to(external)

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root)

    assert error.value.code == 2
    assert stale.is_symlink()
    assert external.read_text(encoding="utf-8") == '{"sentinel": true}\n'
    assert not (output_dir / METADATA_FILENAME).exists()


def test_atomic_publish_never_overwrites_metadata_that_appears_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    sentinel = '{"schema":"different-provenance"}\n'

    def install_competing_metadata(
        _source: os.PathLike[str], target: os.PathLike[str], **kwargs: object
    ) -> None:
        target_fd = kwargs["dst_dir_fd"]
        assert isinstance(target_fd, int)
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=target_fd
        )
        try:
            os.write(descriptor, sentinel.encode("utf-8"))
        finally:
            os.close(descriptor)
        raise FileExistsError("competing metadata won publication")

    monkeypatch.setattr(os, "link", install_competing_metadata)

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root)

    assert error.value.code == 2
    assert (output_dir / METADATA_FILENAME).read_text(encoding="utf-8") == sentinel
    assert list(output_dir.glob(f".{METADATA_FILENAME}.*.tmp")) == []


def test_identical_metadata_is_reused_without_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    _write(resolution, output_dir, systems_root)
    before = (output_dir / METADATA_FILENAME).stat()

    def reject_link(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("matching metadata must not be rewritten")

    monkeypatch.setattr(os, "link", reject_link)
    result = _write(resolution, output_dir, systems_root)
    after = (output_dir / METADATA_FILENAME).stat()

    assert result.created is False
    assert result.adopted_legacy is False
    assert before.st_ino == after.st_ino
    assert before.st_mtime_ns == after.st_mtime_ns


@pytest.mark.parametrize("existing_kind", ["malformed", "symlink", "directory"])
def test_existing_metadata_that_is_not_a_regular_valid_document_fails_closed(
    tmp_path: Path, existing_kind: str
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    metadata_path = output_dir / METADATA_FILENAME
    external = tmp_path / "external.json"
    external.write_text('{"sentinel": true}\n', encoding="utf-8")
    if existing_kind == "malformed":
        metadata_path.write_text("{not-json\n", encoding="utf-8")
    elif existing_kind == "symlink":
        metadata_path.symlink_to(external)
    else:
        metadata_path.mkdir()

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root)

    assert error.value.code == 2
    assert external.read_text(encoding="utf-8") == '{"sentinel": true}\n'
    if existing_kind == "malformed":
        assert metadata_path.read_text(encoding="utf-8") == "{not-json\n"
    elif existing_kind == "symlink":
        assert metadata_path.is_symlink()
    else:
        assert metadata_path.is_dir()


@pytest.mark.parametrize(
    "difference",
    ["dataset", "alias_chain", "source_paths", "raw", "runtime", "context"],
)
def test_resume_rejects_any_provenance_or_semantic_mismatch(
    tmp_path: Path, difference: str
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    _write(resolution, output_dir, systems_root)
    dataset_id = "locomo"
    context = {"dataset_name": "locomo", "clean_groups": False}
    changed = resolution

    if difference == "dataset":
        dataset_id = "longmemeval"
        context = {"dataset_name": "longmemeval", "clean_groups": False}
    elif difference == "alias_chain":
        changed = replace(resolution, alias_chain=("openclaw-hybrid", "other"))
    elif difference == "source_paths":
        other = systems_root / "canonical" / "other.yaml"
        other.write_text("fixture\n", encoding="utf-8")
        changed = replace(resolution, source_paths=(other.resolve(),))
    elif difference == "raw":
        changed = replace(
            resolution, raw_config={**resolution.raw_config, "post_add_wait_seconds": 1}
        )
    elif difference == "runtime":
        changed = replace(
            resolution, config={**resolution.config, "post_add_wait_seconds": 2}
        )
    elif difference == "context":
        context = {"dataset_name": "locomo", "clean_groups": True}

    with pytest.raises(SystemExit) as error:
        write_resolved_system_metadata(
            changed,
            _final_runtime(changed),
            context,
            output_dir,
            dataset_id,
            systems_root=systems_root,
        )

    assert error.value.code == 2


def test_nonempty_legacy_directory_without_metadata_is_refused(tmp_path: Path) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "checkpoint_default.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root)

    assert error.value.code == 2
    assert not (output_dir / METADATA_FILENAME).exists()


def test_explicit_adoption_requires_recognized_artifact_and_is_one_time(
    tmp_path: Path,
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "search_results_checkpoint.json").write_text(
        '{"conversation": []}\n', encoding="utf-8"
    )

    adopted = _write(resolution, output_dir, systems_root, adopt_legacy=True)
    payload = json.loads((output_dir / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert adopted.created is True
    assert adopted.adopted_legacy is True
    assert adopted.warning is not None
    assert "UNVERIFIED" in adopted.warning
    assert payload["provenance_status"] == "adopted-legacy-unverified"

    before = (output_dir / METADATA_FILENAME).stat()
    resumed = _write(resolution, output_dir, systems_root)
    after = (output_dir / METADATA_FILENAME).stat()
    resumed_payload = json.loads(
        (output_dir / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert resumed.created is False
    assert resumed.adopted_legacy is True
    assert resumed_payload["provenance_status"] == "adopted-legacy-unverified"
    assert before.st_ino == after.st_ino
    assert before.st_mtime_ns == after.st_mtime_ns


def test_adoption_rejects_arbitrary_nonempty_directory(tmp_path: Path) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "notes.txt").write_text("not a checkpoint\n", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root, adopt_legacy=True)

    assert error.value.code == 2
    assert not (output_dir / METADATA_FILENAME).exists()


def test_adoption_rejects_fifo_artifact_without_blocking(tmp_path: Path) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    os.mkfifo(output_dir / "search_results_checkpoint.json")

    started = time.monotonic()
    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root, adopt_legacy=True)

    assert error.value.code == 2
    assert time.monotonic() - started < 1.0
    assert not (output_dir / METADATA_FILENAME).exists()


@pytest.mark.parametrize(
    ("relative_path", "contents"),
    [
        ("checkpoint_named-run.json", '{"completed_stages": []}\n'),
        ("responses_checkpoint_7.json", '{"question": {}}\n'),
        ("search_results.json", '[{"conversation": []}]\n'),
        ("answer_results.json", '[{"question": {}}]\n'),
        ("eval_results.json", '{"score": 1}\n'),
        ("memcells/memcell_list_conv_1.json", '[{"memory": "x"}]\n'),
    ],
)
def test_adoption_accepts_only_known_legacy_artifact_shapes(
    tmp_path: Path, relative_path: str, contents: str
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    artifact = output_dir / relative_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(contents, encoding="utf-8")

    result = _write(resolution, output_dir, systems_root, adopt_legacy=True)

    assert result.adopted_legacy is True


@pytest.mark.parametrize(
    "relative_path",
    [
        "answer_results_checkpoint.json",
        "checkpoint.json",
        "responses_checkpoint_latest.json",
        "my_checkpoint_default.json",
        "memcell_list_conv_1.json",
        "memcells/other.json",
        "resolved-system-config.json.bak",
    ],
)
def test_adoption_rejects_lookalike_artifact_names(
    tmp_path: Path, relative_path: str
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    artifact = output_dir / relative_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        _write(resolution, output_dir, systems_root, adopt_legacy=True)

    assert error.value.code == 2
    assert not (output_dir / METADATA_FILENAME).exists()


def test_adopt_flag_on_new_directory_still_records_verified_provenance(
    tmp_path: Path,
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "new-run"

    result = _write(resolution, output_dir, systems_root, adopt_legacy=True)
    payload = json.loads((output_dir / METADATA_FILENAME).read_text(encoding="utf-8"))

    assert result.adopted_legacy is False
    assert payload["provenance_status"] == "verified"


def test_runtime_digest_includes_runtime_context(tmp_path: Path) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    first_output = tmp_path / "first"
    second_output = tmp_path / "second"

    _write(
        resolution,
        first_output,
        systems_root,
        runtime_context={"dataset_name": "locomo", "clean_groups": False},
    )
    _write(
        resolution,
        second_output,
        systems_root,
        runtime_context={"dataset_name": "locomo", "clean_groups": True},
    )

    first = json.loads((first_output / METADATA_FILENAME).read_text(encoding="utf-8"))
    second = json.loads((second_output / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert first["raw_config_sha256"] == second["raw_config_sha256"]
    assert first["runtime_config_sha256"] != second["runtime_config_sha256"]


def test_concurrent_different_writers_are_serialized_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    systems_root = tmp_path / "systems"
    resolution = _resolution(systems_root)
    output_dir = tmp_path / "run"
    start = threading.Event()
    release_publish = threading.Event()
    publish_calls: queue.Queue[int] = queue.Queue()
    results: queue.Queue[tuple[str, object]] = queue.Queue()
    real_link = os.link

    def blocking_link(
        source: os.PathLike[str], target: os.PathLike[str], **kwargs: object
    ) -> None:
        publish_calls.put(os.getpid())
        if not release_publish.wait(timeout=10):
            raise TimeoutError("test did not release publication")
        real_link(source, target, **kwargs)

    monkeypatch.setattr(metadata_module.os, "link", blocking_link)
    writers = [
        threading.Thread(
            target=_concurrent_write,
            args=(
                resolution,
                _final_runtime(resolution),
                {"dataset_name": dataset_id, "clean_groups": False},
                output_dir,
                dataset_id,
                systems_root,
                start,
                results,
            ),
            daemon=True,
        )
        for dataset_id in ("locomo", "longmemeval")
    ]
    for writer in writers:
        writer.start()

    try:
        start.set()
        publish_calls.get(timeout=5)
        with pytest.raises(queue.Empty):
            publish_calls.get(timeout=0.5)
    finally:
        release_publish.set()
        for writer in writers:
            writer.join(timeout=10)

    assert not any(writer.is_alive() for writer in writers)
    outcomes = sorted(results.get(timeout=2) for _ in writers)
    assert outcomes == [("created", True), ("exit", 2)]
    with pytest.raises(queue.Empty):
        publish_calls.get_nowait()
    payload = json.loads((output_dir / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert payload["dataset_id"] in {"locomo", "longmemeval"}

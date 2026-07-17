from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from evaluation.src.config.cli_support import default_result_dir
from evaluation.src.config.system_index import load_system_index
from evaluation.src.config.system_loader import resolve_system_config

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
BASELINE_PATH = FIXTURES_DIR / "system_configs_before_cleanup.json"
APPROVED_DELTAS_PATH = FIXTURES_DIR / "system_config_approved_deltas.yaml"

EXPECTED_SYSTEM_IDS = {
    "evermemos",
    "evermemos_cloud_api",
    "evermemos_local_api",
    "hermes",
    "hermes-hindsight",
    "hermes-holographic",
    "hermes-honcho",
    "mem0",
    "memos",
    "memu",
    "openclaw",
    "openclaw-agent-local",
    "openclaw-docker",
    "openclaw-docker-evermemos",
    "openclaw-docker-hypercompositor",
    "openclaw-docker-mem0",
    "openclaw-docker-memclaw",
    "openclaw-docker-memcore-session-bundle",
    "openclaw-docker-memcore-session-bundle-emptytail",
    "openclaw-docker-memcore-session-bundle-weaktail",
    "openclaw-docker-openviking-session-bundle-memcore",
    "openclaw-docker-openviking-session-bundle-noop",
    "openclaw-docker-openviking-session-bundle-noop-fixpack",
    "openclaw-docker-openviking-session-bundle-noop-serial",
    "openclaw-docker-stub",
    "openclaw-fts",
    "openclaw-fts-noflush",
    "openclaw-hybrid",
    "openclaw-hybrid-noflush",
    "openclaw-hypercompositor",
    "openclaw-native-embed",
    "openclaw-native-noembed",
    "openclaw-noop",
    "openclaw-vector",
    "openclaw-vector-noflush",
    "zep",
}

PUBLIC_ONLINE_SYSTEM_IDS = (
    "evermemos",
    "evermemos_cloud_api",
    "evermemos_local_api",
    "mem0",
    "memos",
    "memu",
    "zep",
)

HERMES_SYSTEM_IDS = (
    "hermes",
    "hermes-holographic",
    "hermes-honcho",
    "hermes-hindsight",
)

HOST_OPENCLAW_CANONICAL_IDS = (
    "openclaw",
    "openclaw-fts",
    "openclaw-fts-noflush",
    "openclaw-hybrid-noflush",
    "openclaw-vector",
    "openclaw-vector-noflush",
)

HOST_OPENCLAW_EXPERIMENT_IDS = (
    "openclaw-agent-local",
    "openclaw-hypercompositor",
    "openclaw-native-embed",
    "openclaw-native-noembed",
    "openclaw-noop",
)

HOST_OPENCLAW_SYSTEM_IDS = (
    *HOST_OPENCLAW_CANONICAL_IDS,
    "openclaw-hybrid",
    *HOST_OPENCLAW_EXPERIMENT_IDS,
)

HOST_OPENCLAW_EMBEDDING_MIGRATION_IDS = {
    "openclaw",
    "openclaw-hybrid",
    "openclaw-hybrid-noflush",
    "openclaw-native-embed",
    "openclaw-vector",
    "openclaw-vector-noflush",
}

DOCKER_PLUGIN_CANONICAL_IDS = (
    "openclaw-docker",
    "openclaw-docker-evermemos",
    "openclaw-docker-mem0",
)

DOCKER_PLUGIN_EXPERIMENT_IDS = (
    "openclaw-docker-hypercompositor",
    "openclaw-docker-memclaw",
    "openclaw-docker-stub",
)

DOCKER_PLUGIN_SYSTEM_IDS = (*DOCKER_PLUGIN_CANONICAL_IDS, *DOCKER_PLUGIN_EXPERIMENT_IDS)

MEMCORE_SESSION_BUNDLE_IDS = (
    "openclaw-docker-memcore-session-bundle",
    "openclaw-docker-memcore-session-bundle-emptytail",
    "openclaw-docker-memcore-session-bundle-weaktail",
)

OPENVIKING_SESSION_BUNDLE_IDS = (
    "openclaw-docker-openviking-session-bundle-memcore",
    "openclaw-docker-openviking-session-bundle-noop",
    "openclaw-docker-openviking-session-bundle-noop-fixpack",
    "openclaw-docker-openviking-session-bundle-noop-serial",
)

SESSION_BUNDLE_SYSTEM_IDS = (
    *MEMCORE_SESSION_BUNDLE_IDS,
    *OPENVIKING_SESSION_BUNDLE_IDS,
)

ANSWER_RETRY_REMOVAL_IDS = frozenset(
    (
        *HERMES_SYSTEM_IDS,
        *HOST_OPENCLAW_SYSTEM_IDS,
        *DOCKER_PLUGIN_SYSTEM_IDS,
        *SESSION_BUNDLE_SYSTEM_IDS,
    )
)

CANONICAL_ID_OVERRIDES = {"hermes": "hermes-holographic", "openclaw-hybrid": "openclaw"}
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


def _load_test_module(name: str) -> ModuleType:
    path = TESTS_DIR / f"{name}.py"
    if not path.is_file():
        pytest.fail(f"required module does not exist: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _common_mapping(values: list[dict[str, Any]]) -> dict[str, Any]:
    """Return only recursively identical fields shared by every mapping."""
    common: dict[str, Any] = {}
    for key in sorted(set.intersection(*(set(value) for value in values))):
        candidates = [value[key] for value in values]
        if all(isinstance(candidate, dict) for candidate in candidates):
            nested = _common_mapping(candidates)
            if nested:
                common[key] = nested
        elif all(candidate == candidates[0] for candidate in candidates[1:]):
            common[key] = deepcopy(candidates[0])
    return common


def _subtract_mapping(config: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Return the exact recursive leaf override needed on top of ``base``."""
    remainder: dict[str, Any] = {}
    for key, value in config.items():
        if key not in base:
            remainder[key] = deepcopy(value)
        elif isinstance(value, dict) and isinstance(base[key], dict):
            nested = _subtract_mapping(value, base[key])
            if nested:
                remainder[key] = nested
        elif value != base[key]:
            remainder[key] = deepcopy(value)
    return remainder


def _has_path(config: dict[str, Any], path: tuple[str, ...]) -> bool:
    current: object = config
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def test_legacy_system_id_surface_is_locked() -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    index = load_system_index()

    assert set(baseline) == EXPECTED_SYSTEM_IDS
    assert set(index.systems) == EXPECTED_SYSTEM_IDS


def test_normalized_raw_config_preserves_environment_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = _load_test_module("system_config_legacy")
    config_path = tmp_path / "system.yaml"
    config_path.write_text(
        'adapter: example\napi_key: "${BASELINE_TEST_SECRET}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("BASELINE_TEST_SECRET", "machine-local-secret")

    result = legacy.normalized_raw_config(config_path)

    assert result == {"adapter": "example", "api_key": "${BASELINE_TEST_SECRET}"}
    assert "machine-local-secret" not in json.dumps(result)


def test_normalized_raw_config_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    legacy = _load_test_module("system_config_legacy")
    config_path = tmp_path / "system.yaml"
    config_path.write_text("- adapter\n- example\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mapping"):
        legacy.normalized_raw_config(config_path)


@pytest.mark.parametrize(
    ("yaml_text", "expected_path"),
    [
        ("adapter: example\n1: numeric-key\n", "<root>"),
        ("adapter: example\nnested:\n  valid: true\n  2: numeric-key\n", "/nested"),
    ],
)
def test_normalized_raw_config_rejects_non_string_mapping_keys(
    tmp_path: Path, yaml_text: str, expected_path: str
) -> None:
    legacy = _load_test_module("system_config_legacy")
    config_path = tmp_path / "system.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ValueError) as error:
        legacy.normalized_raw_config(config_path)

    assert expected_path in str(error.value)
    assert "mapping keys must be strings" in str(error.value)


@pytest.mark.parametrize("yaml_scalar", [".nan", ".inf", "-.inf"])
def test_normalized_raw_config_rejects_non_finite_floats(
    tmp_path: Path, yaml_scalar: str
) -> None:
    legacy = _load_test_module("system_config_legacy")
    config_path = tmp_path / "system.yaml"
    config_path.write_text(
        f"adapter: example\nvalues:\n  - {yaml_scalar}\n", encoding="utf-8"
    )

    with pytest.raises(ValueError) as error:
        legacy.normalized_raw_config(config_path)

    assert "/values/0" in str(error.value)
    assert "finite" in str(error.value)


def test_normalized_raw_config_rejects_non_json_scalars(tmp_path: Path) -> None:
    legacy = _load_test_module("system_config_legacy")
    config_path = tmp_path / "system.yaml"
    config_path.write_text("adapter: example\nreleased: 2026-07-16\n", encoding="utf-8")

    with pytest.raises(ValueError) as error:
        legacy.normalized_raw_config(config_path)

    assert "/released" in str(error.value)
    assert "JSON-compatible" in str(error.value)


@pytest.mark.parametrize("adapter", ["mem0", "memos", "memu", "zep", "evermemos_api"])
def test_effective_config_keeps_online_adapter_answer_retries(adapter: str) -> None:
    legacy = _load_test_module("system_config_legacy")
    raw = {"adapter": adapter, "answer": {"max_retries": 7}}

    assert legacy.normalized_effective_config("example", raw) == raw


def test_effective_config_removes_only_proven_unused_fields() -> None:
    legacy = _load_test_module("system_config_legacy")
    raw = {
        "adapter": "openclaw",
        "max_retries": 11,
        "timeout_seconds": 29,
        "answer": {"max_retries": 7, "timeout_seconds": 13},
        "openclaw": {
            "prompts": {"answer": "unused"},
            "embedding": {"api_key": "${SOPH_API_KEY}", "model": "text-embeddings"},
        },
    }

    result = legacy.normalized_effective_config("openclaw", raw)

    assert result == {
        "adapter": "openclaw",
        "max_retries": 11,
        "timeout_seconds": 29,
        "answer": {"timeout_seconds": 13},
        "openclaw": {
            "embedding": {"api_key_env": "SOPH_API_KEY", "model": "text-embeddings"}
        },
    }
    assert raw["answer"]["max_retries"] == 7
    assert raw["openclaw"]["prompts"] == {"answer": "unused"}
    assert raw["openclaw"]["embedding"]["api_key"] == "${SOPH_API_KEY}"


@pytest.mark.parametrize(
    ("system_id", "raw", "expected"),
    [
        (
            "memos",
            {"adapter": "memos", "request_interval": 0.1, "timeout": 5},
            {"adapter": "memos", "timeout": 5},
        ),
        (
            "memu",
            {"adapter": "memu", "min_similarity": 0.3, "timeout": 5},
            {"adapter": "memu", "timeout": 5},
        ),
        (
            "evermemos_cloud_api",
            {
                "adapter": "evermemos_api",
                "search": {"timeout_seconds": 300, "top_k": 20},
                "answer": {"max_retries": 3},
            },
            {
                "adapter": "evermemos_api",
                "search": {"timeout_seconds": 300, "top_k": 20},
                "answer": {"max_retries": 3},
            },
        ),
    ],
)
def test_effective_config_normalizes_system_specific_unused_fields(
    system_id: str, raw: dict[str, object], expected: dict[str, object]
) -> None:
    legacy = _load_test_module("system_config_legacy")

    assert legacy.normalized_effective_config(system_id, raw) == expected


def test_effective_config_does_not_rewrite_other_secret_markers() -> None:
    legacy = _load_test_module("system_config_legacy")
    raw = {
        "adapter": "openclaw",
        "api_key": "${SOPH_API_KEY}",
        "openclaw": {"embedding": {"api_key": "${OTHER_API_KEY}"}},
    }

    assert legacy.normalized_effective_config("openclaw", raw) == raw


def test_semantic_sha256_is_deterministic_for_mapping_order() -> None:
    legacy = _load_test_module("system_config_legacy")
    left = {"z": [1, 2], "a": {"é": True}}
    right = {"a": {"é": True}, "z": [1, 2]}

    assert legacy.semantic_sha256(left) == legacy.semantic_sha256(right)
    assert len(legacy.semantic_sha256(left)) == 64
    assert legacy.semantic_sha256({"items": [1, 2]}) != legacy.semantic_sha256(
        {"items": [2, 1]}
    )


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_semantic_sha256_rejects_non_finite_values(non_finite: float) -> None:
    legacy = _load_test_module("system_config_legacy")

    with pytest.raises(ValueError) as error:
        legacy.semantic_sha256({"value": non_finite})

    assert "/value" in str(error.value)
    assert "finite" in str(error.value)


def test_semantic_sha256_rejects_non_string_mapping_keys() -> None:
    legacy = _load_test_module("system_config_legacy")

    with pytest.raises(ValueError) as error:
        legacy.semantic_sha256({"valid": True, 1: "numeric-key"})

    assert "<root>" in str(error.value)
    assert "mapping keys must be strings" in str(error.value)


def test_json_pointer_differences_are_exact_and_escaped() -> None:
    legacy = _load_test_module("system_config_legacy")
    before = {
        "same": True,
        "a/b": {"~key": 1},
        "items": ["same", {"old": 1}],
        "removed": {"nested": "value"},
    }
    after = {
        "same": True,
        "a/b": {"~key": 2},
        "items": ["same", {"new": 1}, "added"],
        "added": None,
    }

    assert legacy.json_pointer_differences(before, after) == {
        "/a~1b/~0key",
        "/added",
        "/items/1/new",
        "/items/1/old",
        "/items/2",
        "/removed",
    }
    assert legacy.json_pointer_differences(1, 2) == {""}


def test_fixture_captures_the_locked_surface_without_expanding_secrets() -> None:
    legacy = _load_test_module("system_config_legacy")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    approved_document = yaml.safe_load(APPROVED_DELTAS_PATH.read_text(encoding="utf-8"))

    assert set(baseline) == EXPECTED_SYSTEM_IDS
    assert isinstance(approved_document, dict)
    assert set(approved_document) == {"deltas"}
    assert isinstance(approved_document["deltas"], dict)
    for system_id, entries in approved_document["deltas"].items():
        assert system_id in EXPECTED_SYSTEM_IDS
        assert isinstance(entries, list)
        seen: set[tuple[str, str]] = set()
        for entry in entries:
            assert isinstance(entry, dict)
            assert set(entry) in (
                {"pointer", "classification", "rationale"},
                {"pointer", "classification", "rationale", "surface"},
            )
            assert isinstance(entry["pointer"], str)
            assert entry["pointer"] == "" or entry["pointer"].startswith("/")
            assert entry["classification"] in {
                "structure-only",
                "security-fix",
                "behavior-fix",
            }
            assert isinstance(entry["rationale"], str)
            assert entry["rationale"].strip()
            surface = entry.get("surface", "raw")
            assert surface in {"raw", "effective"}
            key = (surface, entry["pointer"])
            assert key not in seen
            seen.add(key)

    registered_adapters = set(
        __import__(
            "evaluation.src.adapters.registry", fromlist=["list_adapters"]
        ).list_adapters()
    )
    differing_canonical_ids = set()
    for system_id, entry in baseline.items():
        assert set(entry) == {
            "adapter",
            "canonical_id",
            "default_result_suffix",
            "effective_config",
            "effective_sha256",
            "raw_config",
            "raw_sha256",
        }
        assert entry["adapter"] in registered_adapters
        assert entry["default_result_suffix"] == f"locomo-{system_id}"
        if entry["canonical_id"] != system_id:
            differing_canonical_ids.add(system_id)
        assert entry["canonical_id"] == CANONICAL_ID_OVERRIDES.get(system_id, system_id)
        assert entry["raw_sha256"] == legacy.semantic_sha256(entry["raw_config"])
        assert entry["effective_sha256"] == legacy.semantic_sha256(
            entry["effective_config"]
        )

    assert differing_canonical_ids == set(CANONICAL_ID_OVERRIDES)
    serialized = json.dumps(baseline, sort_keys=True)
    assert "${" in serialized
    assert "machine-local-baseline-secret" not in serialized


def test_answer_retry_structure_only_deltas_cover_exactly_non_online_ids() -> None:
    approved_document = yaml.safe_load(APPROVED_DELTAS_PATH.read_text(encoding="utf-8"))
    actual = {
        system_id
        for system_id, entries in approved_document["deltas"].items()
        if any(
            entry["pointer"] == "/answer/max_retries"
            and entry["classification"] == "structure-only"
            and entry.get("surface", "raw") == "raw"
            for entry in entries
        )
    }

    assert len(ANSWER_RETRY_REMOVAL_IDS) == 29
    assert actual == ANSWER_RETRY_REMOVAL_IDS


def test_every_legacy_id_resolves_with_a_registered_adapter() -> None:
    legacy = _load_test_module("system_config_legacy")
    from evaluation.src.adapters.registry import list_adapters

    registered_adapters = set(list_adapters())
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    approved_document = yaml.safe_load(APPROVED_DELTAS_PATH.read_text(encoding="utf-8"))
    approved = approved_document["deltas"]

    for system_id, expected in baseline.items():
        resolved = resolve_system_config(system_id, environ=FAKE_ENVIRONMENT)
        assert resolved.adapter in registered_adapters
        assert resolved.adapter == expected["adapter"]
        assert resolved.canonical_id == expected["canonical_id"]
        assert (
            default_result_dir(
                REPO_ROOT / "evaluation",
                dataset_id="locomo",
                requested_system_id=resolved.requested_id,
                run_name=None,
            ).name
            == expected["default_result_suffix"]
        )

        raw_differences = legacy.json_pointer_differences(
            expected["raw_config"], resolved.raw_config
        )
        allowed_raw = {
            item["pointer"]
            for item in approved.get(system_id, [])
            if item["classification"]
            in {"structure-only", "security-fix", "behavior-fix"}
            and item.get("surface", "raw") == "raw"
        }
        assert raw_differences == allowed_raw, (
            system_id,
            sorted(raw_differences ^ allowed_raw),
        )
        if not raw_differences:
            assert legacy.semantic_sha256(resolved.raw_config) == expected["raw_sha256"]

        effective = legacy.normalized_effective_config(system_id, resolved.raw_config)
        effective_differences = legacy.json_pointer_differences(
            expected["effective_config"], effective
        )
        allowed_effective = {
            item["pointer"]
            for item in approved.get(system_id, [])
            if item["classification"] in {"security-fix", "behavior-fix"}
            and item.get("surface") == "effective"
        }
        assert effective_differences == allowed_effective, (
            system_id,
            sorted(effective_differences ^ allowed_effective),
        )
        if not effective_differences:
            assert legacy.semantic_sha256(effective) == expected["effective_sha256"]


@pytest.mark.parametrize("system_id", PUBLIC_ONLINE_SYSTEM_IDS)
def test_public_online_system_migration_preserves_immutable_baseline(
    system_id: str,
) -> None:
    legacy = _load_test_module("system_config_legacy")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    expected = baseline[system_id]

    resolved = resolve_system_config(system_id, environ=FAKE_ENVIRONMENT)
    effective = legacy.normalized_effective_config(system_id, resolved.raw_config)

    if system_id == "memos":
        assert legacy.json_pointer_differences(
            expected["raw_config"], resolved.raw_config
        ) == {"/request_interval", "/requests_per_second"}
        assert "request_interval" not in resolved.raw_config
        assert resolved.raw_config["requests_per_second"] == 10
        assert legacy.json_pointer_differences(
            expected["effective_config"], effective
        ) == {"/requests_per_second"}
        assert effective["requests_per_second"] == 10
    elif system_id == "memu":
        assert legacy.json_pointer_differences(
            expected["raw_config"], resolved.raw_config
        ) == {"/min_similarity", "/search/min_similarity"}
        assert "min_similarity" not in resolved.raw_config
        assert resolved.raw_config["search"]["min_similarity"] == 0.3
        assert legacy.json_pointer_differences(
            expected["effective_config"], effective
        ) == {"/search/min_similarity"}
        assert effective["search"]["min_similarity"] == 0.3
    elif system_id == "evermemos_cloud_api":
        assert resolved.raw_config == expected["raw_config"]
        assert legacy.semantic_sha256(resolved.raw_config) == expected["raw_sha256"]
        assert legacy.json_pointer_differences(
            expected["effective_config"], effective
        ) == {"/search/timeout_seconds"}
        assert effective["search"]["timeout_seconds"] == 300
    else:
        assert resolved.raw_config == expected["raw_config"]
        assert legacy.semantic_sha256(resolved.raw_config) == expected["raw_sha256"]
        assert effective == expected["effective_config"]
        assert legacy.semantic_sha256(effective) == expected["effective_sha256"]


def test_public_online_approved_deltas_are_exact() -> None:
    approved_document = yaml.safe_load(APPROVED_DELTAS_PATH.read_text(encoding="utf-8"))

    for system_id in PUBLIC_ONLINE_SYSTEM_IDS:
        expected = set()
        if system_id == "memos":
            expected = {
                ("/request_interval", "behavior-fix", "raw"),
                ("/requests_per_second", "behavior-fix", "raw"),
                ("/requests_per_second", "behavior-fix", "effective"),
            }
        elif system_id == "memu":
            expected = {
                ("/min_similarity", "behavior-fix", "raw"),
                ("/search/min_similarity", "behavior-fix", "raw"),
                ("/search/min_similarity", "behavior-fix", "effective"),
            }
        elif system_id == "evermemos_cloud_api":
            expected = {
                ("/search/timeout_seconds", "behavior-fix", "effective"),
            }
        actual = {
            (entry["pointer"], entry["classification"], entry.get("surface", "raw"))
            for entry in approved_document["deltas"].get(system_id, [])
        }
        assert actual == expected


@pytest.mark.parametrize("system_id", PUBLIC_ONLINE_SYSTEM_IDS)
def test_public_online_system_configs_live_only_in_canonical_directory(
    system_id: str,
) -> None:
    systems_root = REPO_ROOT / "evaluation" / "config" / "systems"
    index = load_system_index()

    assert index.systems[system_id].path is not None
    assert index.systems[system_id].path.as_posix() == f"canonical/{system_id}.yaml"
    assert (systems_root / "canonical" / f"{system_id}.yaml").is_file()
    assert not (systems_root / f"{system_id}.yaml").exists()


@pytest.mark.parametrize("system_id", HERMES_SYSTEM_IDS)
def test_hermes_family_migration_preserves_effective_immutable_baseline(
    system_id: str,
) -> None:
    legacy = _load_test_module("system_config_legacy")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    expected = baseline[system_id]

    resolved = resolve_system_config(system_id, environ=FAKE_ENVIRONMENT)
    effective = legacy.normalized_effective_config(system_id, resolved.raw_config)

    assert legacy.json_pointer_differences(
        expected["raw_config"], resolved.raw_config
    ) == {"/answer/max_retries"}
    assert effective == expected["effective_config"]
    assert legacy.semantic_sha256(effective) == expected["effective_sha256"]


def test_hermes_family_uses_canonical_leaves_and_alias_only() -> None:
    systems_root = REPO_ROOT / "evaluation" / "config" / "systems"
    index = load_system_index()

    assert (systems_root / "_bases" / "hermes.yaml").is_file()
    for system_id in HERMES_SYSTEM_IDS[1:]:
        expected_path = f"canonical/{system_id}.yaml"
        assert index.systems[system_id].path is not None
        assert index.systems[system_id].path.as_posix() == expected_path
        assert (systems_root / expected_path).is_file()

    assert index.systems["hermes"].path is None
    assert index.systems["hermes"].alias_of == "hermes-holographic"
    for system_id in HERMES_SYSTEM_IDS:
        assert not (systems_root / f"{system_id}.yaml").exists()


@pytest.mark.parametrize("system_id", HOST_OPENCLAW_SYSTEM_IDS)
def test_host_openclaw_migration_preserves_effective_legacy_semantics(
    system_id: str,
) -> None:
    legacy = _load_test_module("system_config_legacy")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    expected = baseline[system_id]

    resolved = resolve_system_config(system_id, environ=FAKE_ENVIRONMENT)
    expected_raw_differences = {"/answer/max_retries", "/openclaw/prompts"}
    if system_id in HOST_OPENCLAW_EMBEDDING_MIGRATION_IDS:
        expected_raw_differences.update(
            {"/openclaw/embedding/api_key", "/openclaw/embedding/api_key_env"}
        )

    assert resolved.adapter == expected["adapter"] == "openclaw"
    assert resolved.canonical_id == expected["canonical_id"]
    assert (
        legacy.json_pointer_differences(expected["raw_config"], resolved.raw_config)
        == expected_raw_differences
    )
    assert "prompts" not in resolved.raw_config["openclaw"]

    if system_id in HOST_OPENCLAW_EMBEDDING_MIGRATION_IDS:
        embedding = resolved.raw_config["openclaw"]["embedding"]
        assert "api_key" not in embedding
        assert embedding["api_key_env"] == "SOPH_API_KEY"

    effective = legacy.normalized_effective_config(system_id, resolved.raw_config)
    assert effective == expected["effective_config"]
    assert legacy.semantic_sha256(effective) == expected["effective_sha256"]


def test_host_openclaw_family_uses_one_base_categorized_leaves_and_alias() -> None:
    systems_root = REPO_ROOT / "evaluation" / "config" / "systems"
    resolved_root = systems_root.resolve()
    index = load_system_index()
    base_path = systems_root / "_bases" / "openclaw-native.yaml"

    assert yaml.safe_load(base_path.read_text(encoding="utf-8")) == {
        "adapter": "openclaw",
        "llm": {
            "provider": "openai",
            "api_key": "${LLM_API_KEY}",
            "base_url": "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}",
            "temperature": 0.0,
            "max_tokens": 1024,
        },
        "search": {
            "top_k": 6,
            "response_top_k": 5,
            "num_workers": 5,
            "max_inflight_queries_per_conversation": 1,
        },
        "answer": {},
        "openclaw": {
            "repo_path": "${OPENCLAW_REPO_PATH}",
            "visibility_mode": "settled",
        },
    }

    for system_id in HOST_OPENCLAW_CANONICAL_IDS:
        expected_path = f"canonical/{system_id}.yaml"
        assert index.systems[system_id].path is not None
        assert index.systems[system_id].path.as_posix() == expected_path
        resolved = resolve_system_config(system_id, environ=FAKE_ENVIRONMENT)
        assert tuple(
            path.relative_to(resolved_root).as_posix() for path in resolved.source_paths
        ) == ("_bases/openclaw-native.yaml", expected_path)

    for system_id in HOST_OPENCLAW_EXPERIMENT_IDS:
        expected_path = f"experiments/{system_id}.yaml"
        assert index.systems[system_id].path is not None
        assert index.systems[system_id].path.as_posix() == expected_path
        resolved = resolve_system_config(system_id, environ=FAKE_ENVIRONMENT)
        assert tuple(
            path.relative_to(resolved_root).as_posix() for path in resolved.source_paths
        ) == ("_bases/openclaw-native.yaml", expected_path)

    alias = resolve_system_config("openclaw-hybrid", environ=FAKE_ENVIRONMENT)
    assert index.systems["openclaw-hybrid"].path is None
    assert index.systems["openclaw-hybrid"].alias_of == "openclaw"
    assert alias.canonical_id == "openclaw"
    assert alias.alias_chain == ("openclaw-hybrid", "openclaw")
    assert tuple(
        path.relative_to(resolved_root).as_posix() for path in alias.source_paths
    ) == ("_bases/openclaw-native.yaml", "canonical/openclaw.yaml")

    for system_id in HOST_OPENCLAW_SYSTEM_IDS:
        assert not (systems_root / f"{system_id}.yaml").exists()


def test_host_openclaw_approved_raw_deltas_are_exact() -> None:
    approved_document = yaml.safe_load(APPROVED_DELTAS_PATH.read_text(encoding="utf-8"))

    for system_id in HOST_OPENCLAW_SYSTEM_IDS:
        expected = {
            ("/answer/max_retries", "structure-only", "raw"),
            ("/openclaw/prompts", "structure-only", "raw"),
        }
        if system_id in HOST_OPENCLAW_EMBEDDING_MIGRATION_IDS:
            expected.update(
                {
                    ("/openclaw/embedding/api_key", "security-fix", "raw"),
                    ("/openclaw/embedding/api_key_env", "security-fix", "raw"),
                }
            )

        actual = {
            (entry["pointer"], entry["classification"], entry.get("surface", "raw"))
            for entry in approved_document["deltas"].get(system_id, [])
        }
        assert actual == expected


@pytest.mark.parametrize("system_id", DOCKER_PLUGIN_SYSTEM_IDS)
def test_docker_plugin_migration_preserves_effective_legacy_semantics(
    system_id: str,
) -> None:
    legacy = _load_test_module("system_config_legacy")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    expected = baseline[system_id]

    resolved = resolve_system_config(system_id, environ=FAKE_ENVIRONMENT)

    assert resolved.adapter == expected["adapter"] == "openclaw-docker"
    assert resolved.canonical_id == expected["canonical_id"] == system_id
    expected_raw_differences = {"/answer/max_retries", "/openclaw/prompts"}
    if system_id == "openclaw-docker-stub":
        expected_raw_differences.add("/openclaw_docker/image")
    assert (
        legacy.json_pointer_differences(expected["raw_config"], resolved.raw_config)
        == expected_raw_differences
    )
    assert "prompts" not in resolved.raw_config["openclaw"]

    effective = legacy.normalized_effective_config(system_id, resolved.raw_config)
    expected_effective_differences = (
        {"/openclaw_docker/image"} if system_id == "openclaw-docker-stub" else set()
    )
    assert (
        legacy.json_pointer_differences(expected["effective_config"], effective)
        == expected_effective_differences
    )
    if not expected_effective_differences:
        assert legacy.semantic_sha256(effective) == expected["effective_sha256"]


def test_docker_plugin_family_uses_only_common_fields_in_shared_base() -> None:
    systems_root = REPO_ROOT / "evaluation" / "config" / "systems"
    base_path = systems_root / "_bases" / "openclaw-docker.yaml"

    assert yaml.safe_load(base_path.read_text(encoding="utf-8")) == {
        "adapter": "openclaw-docker",
        "llm": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "${LLM_API_KEY}",
            "base_url": ("${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"),
            "temperature": 0.0,
            "max_tokens": 1024,
        },
        "search": {"max_inflight_queries_per_conversation": 1},
        "openclaw": {
            "repo_path": "${OPENCLAW_REPO_PATH}",
            "visibility_mode": "settled",
            "retrieval_route": "search_then_get",
            "answer_mode": "agent_local",
            "agent_llm": {
                "provider_id": "sophnet",
                "base_url": (
                    "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"
                ),
                "api": "openai-completions",
                "api_key_env": "LLM_API_KEY",
                "model": {
                    "id": "gpt-4.1-mini",
                    "name": "GPT 4.1 Mini (sophnet)",
                    "reasoning": False,
                    "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "context_window": 128000,
                    "max_tokens": 4096,
                },
            },
        },
        "openclaw_docker": {"network": "bridge"},
    }


def test_docker_plugin_family_uses_categorized_leaves_and_no_flat_files() -> None:
    systems_root = REPO_ROOT / "evaluation" / "config" / "systems"
    resolved_root = systems_root.resolve()
    index = load_system_index()

    expected_paths = {
        **{
            system_id: f"canonical/{system_id}.yaml"
            for system_id in DOCKER_PLUGIN_CANONICAL_IDS
        },
        **{
            system_id: f"experiments/{system_id}.yaml"
            for system_id in DOCKER_PLUGIN_EXPERIMENT_IDS
        },
    }
    for system_id, expected_path in expected_paths.items():
        entry = index.systems[system_id]
        assert entry.path is not None
        assert entry.path.as_posix() == expected_path
        assert (systems_root / expected_path).is_file()

        resolved = resolve_system_config(system_id, environ=FAKE_ENVIRONMENT)
        assert tuple(
            path.relative_to(resolved_root).as_posix() for path in resolved.source_paths
        ) == ("_bases/openclaw-docker.yaml", expected_path)
        assert not (systems_root / f"{system_id}.yaml").exists()


def test_docker_plugin_approved_deltas_are_exact() -> None:
    approved_document = yaml.safe_load(APPROVED_DELTAS_PATH.read_text(encoding="utf-8"))

    for system_id in DOCKER_PLUGIN_SYSTEM_IDS:
        expected = {
            ("/answer/max_retries", "structure-only", "raw"),
            ("/openclaw/prompts", "structure-only", "raw"),
        }
        if system_id == "openclaw-docker-stub":
            expected.update(
                {
                    ("/openclaw_docker/image", "behavior-fix", "raw"),
                    ("/openclaw_docker/image", "behavior-fix", "effective"),
                }
            )
        actual = {
            (entry["pointer"], entry["classification"], entry.get("surface", "raw"))
            for entry in approved_document["deltas"].get(system_id, [])
        }
        assert actual == expected


@pytest.mark.parametrize("system_id", SESSION_BUNDLE_SYSTEM_IDS)
def test_session_bundle_migration_preserves_effective_immutable_baseline(
    system_id: str,
) -> None:
    legacy = _load_test_module("system_config_legacy")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    expected = baseline[system_id]

    resolved = resolve_system_config(system_id, environ=FAKE_ENVIRONMENT)
    effective = legacy.normalized_effective_config(system_id, resolved.raw_config)

    assert legacy.json_pointer_differences(
        expected["raw_config"], resolved.raw_config
    ) == {"/answer/max_retries", "/openclaw/prompts"}
    assert "prompts" not in resolved.raw_config["openclaw"]
    assert effective == expected["effective_config"]
    assert legacy.semantic_sha256(effective) == expected["effective_sha256"]


def test_session_bundle_approved_raw_deltas_are_exact() -> None:
    approved_document = yaml.safe_load(APPROVED_DELTAS_PATH.read_text(encoding="utf-8"))

    for system_id in SESSION_BUNDLE_SYSTEM_IDS:
        actual = {
            (entry["pointer"], entry["classification"], entry.get("surface", "raw"))
            for entry in approved_document["deltas"].get(system_id, [])
        }
        assert actual == {
            ("/answer/max_retries", "structure-only", "raw"),
            ("/openclaw/prompts", "structure-only", "raw"),
        }


def test_memcore_session_bundle_base_and_leaves_are_exactly_minimal() -> None:
    systems_root = REPO_ROOT / "evaluation" / "config" / "systems"
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    expected_base = deepcopy(baseline[MEMCORE_SESSION_BUNDLE_IDS[0]]["raw_config"])
    expected_base["answer"].pop("max_retries")
    expected_base["openclaw"].pop("ingest_session_tail")
    expected_base["openclaw"].pop("prompts")

    base_path = systems_root / "_bases" / "openclaw-memcore-session-bundle.yaml"
    assert yaml.safe_load(base_path.read_text(encoding="utf-8")) == expected_base

    expected_categories = ("canonical", "ablations", "ablations")
    for system_id, category in zip(
        MEMCORE_SESSION_BUNDLE_IDS, expected_categories, strict=True
    ):
        leaf_path = systems_root / category / f"{system_id}.yaml"
        raw_config = baseline[system_id]["raw_config"]
        assert yaml.safe_load(leaf_path.read_text(encoding="utf-8")) == {
            "extends": "_bases/openclaw-memcore-session-bundle.yaml",
            "openclaw": {
                "ingest_session_tail": raw_config["openclaw"]["ingest_session_tail"]
            },
        }


def test_openviking_session_bundle_base_and_leaves_are_exactly_minimal() -> None:
    systems_root = REPO_ROOT / "evaluation" / "config" / "systems"
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    raw_configs = [
        deepcopy(baseline[system_id]["raw_config"])
        for system_id in OPENVIKING_SESSION_BUNDLE_IDS
    ]
    for raw_config in raw_configs:
        raw_config["answer"].pop("max_retries")
        raw_config["openclaw"].pop("prompts")
    expected_base = _common_mapping(raw_configs)

    base_path = systems_root / "_bases" / "openclaw-openviking.yaml"
    assert yaml.safe_load(base_path.read_text(encoding="utf-8")) == expected_base

    categories = ("canonical", "canonical", "ablations", "tooling")
    for system_id, category, raw_config in zip(
        OPENVIKING_SESSION_BUNDLE_IDS, categories, raw_configs, strict=True
    ):
        leaf_path = systems_root / category / f"{system_id}.yaml"
        assert yaml.safe_load(leaf_path.read_text(encoding="utf-8")) == {
            "extends": "_bases/openclaw-openviking.yaml",
            **_subtract_mapping(raw_config, expected_base),
        }


def test_session_bundle_presets_use_categorized_paths_and_single_base_chain() -> None:
    systems_root = REPO_ROOT / "evaluation" / "config" / "systems"
    resolved_root = systems_root.resolve()
    index = load_system_index()
    expected = {
        "openclaw-docker-memcore-session-bundle": (
            "canonical",
            "active",
            "canonical/openclaw-docker-memcore-session-bundle.yaml",
            "_bases/openclaw-memcore-session-bundle.yaml",
        ),
        "openclaw-docker-memcore-session-bundle-emptytail": (
            "ablation",
            "experimental",
            "ablations/openclaw-docker-memcore-session-bundle-emptytail.yaml",
            "_bases/openclaw-memcore-session-bundle.yaml",
        ),
        "openclaw-docker-memcore-session-bundle-weaktail": (
            "ablation",
            "experimental",
            "ablations/openclaw-docker-memcore-session-bundle-weaktail.yaml",
            "_bases/openclaw-memcore-session-bundle.yaml",
        ),
        "openclaw-docker-openviking-session-bundle-memcore": (
            "canonical",
            "active",
            "canonical/openclaw-docker-openviking-session-bundle-memcore.yaml",
            "_bases/openclaw-openviking.yaml",
        ),
        "openclaw-docker-openviking-session-bundle-noop": (
            "canonical",
            "active",
            "canonical/openclaw-docker-openviking-session-bundle-noop.yaml",
            "_bases/openclaw-openviking.yaml",
        ),
        "openclaw-docker-openviking-session-bundle-noop-fixpack": (
            "ablation",
            "experimental",
            "ablations/openclaw-docker-openviking-session-bundle-noop-fixpack.yaml",
            "_bases/openclaw-openviking.yaml",
        ),
        "openclaw-docker-openviking-session-bundle-noop-serial": (
            "tooling",
            "experimental",
            "tooling/openclaw-docker-openviking-session-bundle-noop-serial.yaml",
            "_bases/openclaw-openviking.yaml",
        ),
    }

    for system_id, (category, status, leaf, base) in expected.items():
        entry = index.systems[system_id]
        assert entry.category == category
        assert entry.status == status
        assert entry.path is not None
        assert entry.path.as_posix() == leaf
        assert (systems_root / leaf).is_file()

        resolved = resolve_system_config(system_id, environ=FAKE_ENVIRONMENT)
        assert tuple(
            path.relative_to(resolved_root).as_posix() for path in resolved.source_paths
        ) == (base, leaf)
        assert not (systems_root / f"{system_id}.yaml").exists()


@pytest.mark.parametrize("system_id", OPENVIKING_SESSION_BUNDLE_IDS)
def test_openviking_optional_key_presence_matches_immutable_baseline(
    system_id: str,
) -> None:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    expected = baseline[system_id]["raw_config"]
    actual = resolve_system_config(system_id, environ=FAKE_ENVIRONMENT).raw_config
    optional_paths = (
        ("openclaw", "embedding"),
        ("openclaw", "agent_llm", "idle_timeout_seconds"),
        ("openclaw", "agent_llm", "model", "compat"),
        ("openclaw", "ov_ingest", "user_id"),
        ("openclaw", "ov_ingest", "user_id_template"),
        ("openclaw", "ov_ingest", "agent_id_template"),
        ("openclaw", "ov_ingest", "cleanup_max_retries"),
        ("post_add_wait_seconds",),
        ("openclaw_docker", "remove_container_on_stop"),
    )

    for path in optional_paths:
        assert _has_path(actual, path) is _has_path(expected, path), path


def test_session_bundle_documentation_covers_stable_operational_rules() -> None:
    docs_root = REPO_ROOT / "evaluation" / "docs" / "system-configs"
    catalog = (docs_root / "README.md").read_text(encoding="utf-8")
    openviking = (docs_root / "openviking.md").read_text(encoding="utf-8")
    normalized_openviking = " ".join(openviking.split())

    for token in (
        "index.yaml",
        "canonical",
        "alias",
        "experiment",
        "ablation",
        "tooling",
        "_bases",
        "extends",
        "secret",
        "path",
        "openviking.md",
    ):
        assert token in catalog

    for token in (
        "direct SDK ingest",
        "plugin hooks",
        "user_id_template",
        "agent_id_template",
        "user_id: eval-1",
        "OPENVIKING_AGENT_PREFIX",
        "240",
        "180",
        "serial",
        "QA-log",
        "diagnostic-only",
        "not suitable for benchmark scoring",
        "post_add_wait_seconds: 600",
        "OPENVIKING_AUTO_CAPTURE",
        "OPENVIKING_RECALL_SCORE_THRESHOLD",
        "OPENVIKING_RECALL_LIMIT",
        "OPENVIKING_RECALL_MAX_INJECTED_CHARS",
        "cleanup_max_retries",
        "cleanup_retry_delay_sec",
        "remove_container_on_stop",
        "rebuild",
        "pinning",
    ):
        assert token in normalized_openviking

    assert not re.search(r"outer[^\n]{0,40}\b90\s*s", openviking, re.IGNORECASE)
    assert "377GB" not in openviking
    assert "locomo_4_qa119" not in openviking
    assert "Fix #4" not in openviking
    assert "Phase 3" not in openviking
    assert "/Data3/" not in openviking


def test_active_session_bundle_references_use_registered_public_ids() -> None:
    index = load_system_index()
    active_paths = (
        REPO_ROOT / "evaluation" / "README.md",
        REPO_ROOT / "evaluation" / "docs" / "openclaw_adapter.md",
        REPO_ROOT / "docs" / "locomo-fair-baseline.md",
        REPO_ROOT / "env.template",
        REPO_ROOT / "openclaw-eval" / "scripts" / "run_openviking_local_eval.sh",
        REPO_ROOT / "evaluation" / "tools" / "qa_logs" / "cli.py",
        REPO_ROOT / "evaluation" / "tools" / "qa_logs" / "raw_dump.py",
        REPO_ROOT / "evaluation" / "tools" / "qa_logs" / "annotate.py",
    )
    id_pattern = re.compile(
        r"openclaw-docker-(?:memcore|openviking)-session-bundle"
        r"(?:-(?:emptytail|weaktail|memcore|noop(?:-fixpack|-serial)?))?"
    )

    for path in active_paths:
        text = path.read_text(encoding="utf-8")
        for system_id in id_pattern.findall(text):
            assert system_id in index.systems, (path, system_id)
        for system_id in SESSION_BUNDLE_SYSTEM_IDS:
            assert f"{system_id}.yaml" not in text, (path, system_id)

    for path in active_paths[:3]:
        text = path.read_text(encoding="utf-8")
        assert "system-configs/README.md" in text
        assert "system-configs/openviking.md" in text


def test_generator_build_is_deterministic_and_rejects_reserved_index(
    tmp_path: Path,
) -> None:
    generator = _load_test_module("generate_system_config_baseline")
    temporary_repo = tmp_path / "repo"
    temporary_systems = temporary_repo / "evaluation" / "config" / "systems"
    temporary_systems.mkdir(parents=True)
    (temporary_systems / "index.yaml").write_text("systems: {}\n", encoding="utf-8")
    (temporary_systems / "example.yaml").write_text(
        "adapter: evermemos\n", encoding="utf-8"
    )
    (temporary_systems / "alpha.yaml").write_text(
        "adapter: mem0\napi_key: ${MEM0_API_KEY}\n", encoding="utf-8"
    )

    first = generator.build_baseline(temporary_repo)
    second = generator.build_baseline(temporary_repo)

    assert first == second
    assert list(first) == ["alpha", "example"]
    assert "index" not in first
    assert first["alpha"]["raw_config"]["api_key"] == "${MEM0_API_KEY}"


def test_generator_default_creation_does_not_probe_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = _load_test_module("generate_system_config_baseline")
    output_path = tmp_path / "baseline.json"
    original_exists = Path.exists

    def reject_target_probe(path: Path) -> bool:
        if path == output_path:
            raise AssertionError("target existence must not be checked before creation")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", reject_target_probe)

    generator.write_baseline(output_path)

    assert json.loads(output_path.read_text(encoding="utf-8")) == (
        generator.build_baseline(REPO_ROOT)
    )


def test_generator_cleans_temporary_file_when_exclusive_install_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = _load_test_module("generate_system_config_baseline")
    output_path = tmp_path / "baseline.json"
    observed_sources: list[Path] = []

    def fail_link(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        observed_sources.append(Path(source))
        assert Path(target) == output_path
        raise OSError("exclusive install failed")

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(OSError, match="exclusive install failed"):
        generator.write_baseline(output_path)

    assert len(observed_sources) == 1
    assert observed_sources[0].parent == output_path.parent
    assert not output_path.exists()
    assert list(tmp_path.glob(f".{output_path.name}.*.tmp")) == []


def test_generator_force_uses_same_directory_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = _load_test_module("generate_system_config_baseline")
    output_path = tmp_path / "baseline.json"
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    observed_sources: list[Path] = []
    real_replace = os.replace

    def record_replace(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        observed_sources.append(Path(source))
        assert Path(target) == output_path
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", record_replace)

    generator.write_baseline(output_path, force=True)

    assert len(observed_sources) == 1
    assert observed_sources[0].parent == output_path.parent
    assert json.loads(output_path.read_text(encoding="utf-8")) == (
        generator.build_baseline(REPO_ROOT)
    )
    assert list(tmp_path.glob(f".{output_path.name}.*.tmp")) == []


def test_generator_cleans_temporary_file_when_forced_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = _load_test_module("generate_system_config_baseline")
    output_path = tmp_path / "baseline.json"
    sentinel = '{"sentinel": true}\n'
    output_path.write_text(sentinel, encoding="utf-8")

    def fail_replace(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        assert Path(source).parent == output_path.parent
        assert Path(target) == output_path
        raise OSError("forced replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="forced replace failed"):
        generator.write_baseline(output_path, force=True)

    assert output_path.read_text(encoding="utf-8") == sentinel
    assert list(tmp_path.glob(f".{output_path.name}.*.tmp")) == []


def test_generator_refuses_overwrite_and_does_not_read_environment(
    tmp_path: Path,
) -> None:
    generator = _load_test_module("generate_system_config_baseline")
    output_path = tmp_path / "baseline.json"
    command = [
        sys.executable,
        str(TESTS_DIR / "generate_system_config_baseline.py"),
        "--output",
        str(output_path),
    ]
    environment = dict(os.environ)
    environment["SOPH_API_KEY"] = "machine-local-baseline-secret"
    environment["LLM_API_KEY"] = "machine-local-baseline-secret"

    first = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    second = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    output_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    forced = subprocess.run(
        [*command, "--force"],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode != 0
    assert "already exists" in second.stderr
    assert forced.returncode == 0, forced.stderr
    serialized = output_path.read_text(encoding="utf-8")
    parsed = json.loads(serialized)
    assert serialized == (
        json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    assert parsed == generator.build_baseline(REPO_ROOT)
    assert "sentinel" not in parsed
    assert "machine-local-baseline-secret" not in serialized

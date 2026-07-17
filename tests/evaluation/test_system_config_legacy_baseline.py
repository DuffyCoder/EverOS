from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

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
                "search": {"top_k": 20},
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


def test_every_legacy_id_resolves_with_a_registered_adapter() -> None:
    legacy = _load_test_module("system_config_legacy")
    from evaluation.src.adapters.registry import list_adapters

    registered_adapters = set(list_adapters())
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    approved_document = yaml.safe_load(APPROVED_DELTAS_PATH.read_text(encoding="utf-8"))
    approved = approved_document["deltas"]

    for system_id, expected in baseline.items():
        resolved = resolve_system_config(
            system_id, environ=FAKE_ENVIRONMENT, allow_legacy=True
        )
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

    resolved = resolve_system_config(
        system_id, environ=FAKE_ENVIRONMENT, allow_legacy=True
    )
    effective = legacy.normalized_effective_config(system_id, resolved.raw_config)

    assert resolved.raw_config == expected["raw_config"]
    assert legacy.semantic_sha256(resolved.raw_config) == expected["raw_sha256"]
    assert effective == expected["effective_config"]
    assert legacy.semantic_sha256(effective) == expected["effective_sha256"]


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

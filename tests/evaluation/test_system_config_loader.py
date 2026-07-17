from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
import yaml

from evaluation.src.config.system_index import (
    DEFAULT_SYSTEM_INDEX_PATH,
    SystemIndexError,
)
from evaluation.src.config.system_loader import (
    SystemConfigError,
    deep_merge_config,
    resolve_system_config,
)

SYSTEMS_ROOT = DEFAULT_SYSTEM_INDEX_PATH.parent
HERMES_VARIANTS = {
    "hermes-holographic": {
        "plugin": "holographic",
        "ingest_strategy": "session_end",
        "plugin_config": {"auto_extract": True, "default_trust": 0.5, "hrr_dim": 1024},
    },
    "hermes-honcho": {
        "plugin": "honcho",
        "ingest_strategy": "sync_per_turn",
        "plugin_config": {},
    },
    "hermes-hindsight": {
        "plugin": "hindsight",
        "ingest_strategy": "sync_per_turn",
        "plugin_config": {},
    },
}
HERMES_ENVIRONMENT = {
    "HERMES_REPO_PATH": "/tmp/hermes",
    "LLM_API_KEY": "llm-key",
    "LLM_MODEL": "test-model",
}


def _index_document() -> dict[str, Any]:
    document = yaml.safe_load(DEFAULT_SYSTEM_INDEX_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _systems(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    systems = document["systems"]
    assert isinstance(systems, dict)
    return systems


def _write_index(systems_root: Path, document: dict[str, Any]) -> Path:
    systems_root.mkdir(parents=True, exist_ok=True)
    path = systems_root / "index.yaml"
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def _write_config(systems_root: Path, relative_path: str, document: object) -> Path:
    path = systems_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def _write_raw_config(systems_root: Path, relative_path: str, text: str) -> Path:
    path = systems_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _configure_path(
    document: dict[str, Any], system_id: str, relative_path: str
) -> None:
    entry = _systems(document)[system_id]
    entry.pop("alias_of", None)
    entry["path"] = relative_path


def _minimal_openclaw_config() -> dict[str, Any]:
    """Schema-valid raw config used by loader-only synthetic scenarios."""
    return {
        "adapter": "openclaw",
        "llm": {
            "provider": "openai",
            "model": "test-model",
            "api_key": "${LLM_API_KEY}",
            "base_url": "https://llm.example/v1",
            "temperature": 0,
            "max_tokens": 1024,
        },
        "search": {
            "top_k": 6,
            "response_top_k": 5,
            "num_workers": 2,
            "max_inflight_queries_per_conversation": 1,
        },
        "answer": {"max_retries": 3},
        "openclaw": {
            "repo_path": "/tmp/openclaw",
            "visibility_mode": "settled",
            "retrieval_route": "search_then_get",
            "backend_mode": "fts_only",
            "flush_mode": "shared_llm",
            "memory_mode": "memory-core",
            "agent_llm": {
                "provider_id": "test-provider",
                "base_url": "https://llm.example/v1",
                "api": "openai-completions",
                "api_key_env": "LLM_API_KEY",
                "env_vars": ["LLM_API_KEY"],
                "model": {
                    "id": "test-model",
                    "name": "Test model",
                    "reasoning": False,
                    "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "context_window": 128000,
                    "max_tokens": 4096,
                },
            },
        },
    }


def _compat_payload(config: dict[str, Any]) -> dict[str, Any]:
    return config["openclaw"]["agent_llm"]["model"]["compat"]


def _simple_openclaw_root(
    tmp_path: Path,
    config: object,
    *,
    relative_path: str = "test/leaf.yaml",
    document: dict[str, Any] | None = None,
) -> tuple[Path, Path, Path]:
    systems_root = tmp_path / "systems"
    index_document = _index_document() if document is None else document
    _configure_path(index_document, "openclaw", relative_path)
    document_to_write = (
        deep_merge_config(_minimal_openclaw_config(), config)
        if isinstance(config, dict)
        else config
    )
    config_path = _write_config(systems_root, relative_path, document_to_write)
    index_path = _write_index(systems_root, index_document)
    return systems_root, index_path, config_path


def test_deep_merge_recurses_and_replaces_scalars_and_lists_without_mutation() -> None:
    base = {
        "adapter": "openclaw",
        "nested": {
            "kept": {"value": 1},
            "changed": {"base": True, "winner": "base"},
            "items": ["base"],
        },
        "scalar": "base",
    }
    override = {
        "nested": {
            "changed": {"leaf": True, "winner": "leaf"},
            "items": ["leaf", "replacement"],
        },
        "scalar": {"now": "a mapping"},
    }
    original_base = deepcopy(base)
    original_override = deepcopy(override)

    merged = deep_merge_config(base, override)

    assert merged == {
        "adapter": "openclaw",
        "nested": {
            "kept": {"value": 1},
            "changed": {"base": True, "leaf": True, "winner": "leaf"},
            "items": ["leaf", "replacement"],
        },
        "scalar": {"now": "a mapping"},
    }
    assert base == original_base
    assert override == original_override

    merged["nested"]["kept"]["value"] = 99
    merged["nested"]["items"].append("mutated")
    assert base == original_base
    assert override == original_override


def test_one_parent_inheritance_merges_from_the_systems_root(tmp_path: Path) -> None:
    systems_root = tmp_path / "systems"
    document = _index_document()
    _configure_path(document, "openclaw", "children/leaf.yaml")
    base = _minimal_openclaw_config()
    base["openclaw"]["agent_llm"]["model"]["compat"] = {
        "kept": {"value": 1},
        "changed": {"base": True, "winner": "base"},
        "items": ["base"],
    }
    base["openclaw"]["status_timeout_seconds"] = 60
    base_path = _write_config(systems_root, "_bases/base.yaml", base)
    leaf_path = _write_config(
        systems_root,
        "children/leaf.yaml",
        {
            "extends": "_bases/base.yaml",
            "openclaw": {
                "agent_llm": {
                    "model": {
                        "compat": {
                            "changed": {"leaf": True, "winner": "leaf"},
                            "items": ["leaf"],
                        }
                    }
                },
                "status_timeout_seconds": 7,
            },
        },
    )
    index_path = _write_index(systems_root, document)

    resolved = resolve_system_config(
        "openclaw", index_path=index_path, systems_root=systems_root, environ={}
    )

    assert _compat_payload(resolved.raw_config) == {
        "kept": {"value": 1},
        "changed": {"base": True, "leaf": True, "winner": "leaf"},
        "items": ["leaf"],
    }
    assert resolved.raw_config["openclaw"]["status_timeout_seconds"] == 7
    assert "extends" not in resolved.raw_config
    assert resolved.source_paths == (base_path.resolve(), leaf_path.resolve())
    assert resolved.requested_id == "openclaw"
    assert resolved.canonical_id == "openclaw"
    assert resolved.alias_chain == ("openclaw",)
    assert resolved.category == "canonical"
    assert resolved.status == "active"
    assert resolved.adapter == "openclaw"
    assert resolved.warning is None


def test_multilevel_inheritance_has_deterministic_base_to_leaf_sources(
    tmp_path: Path,
) -> None:
    systems_root = tmp_path / "systems"
    document = _index_document()
    _configure_path(document, "openclaw", "leaf.yaml")
    base = _minimal_openclaw_config()
    base["openclaw"]["agent_llm"]["model"]["compat"] = {"level": "base", "base": True}
    base_path = _write_config(systems_root, "base.yaml", base)
    middle_path = _write_config(
        systems_root,
        "middle.yaml",
        {
            "extends": "base.yaml",
            "openclaw": {
                "agent_llm": {"model": {"compat": {"level": "middle", "middle": True}}}
            },
        },
    )
    leaf_path = _write_config(
        systems_root,
        "leaf.yaml",
        {
            "extends": "middle.yaml",
            "openclaw": {
                "agent_llm": {"model": {"compat": {"level": "leaf", "leaf": True}}}
            },
        },
    )
    index_path = _write_index(systems_root, document)

    resolved = resolve_system_config(
        "openclaw", index_path=index_path, systems_root=systems_root, environ={}
    )

    assert _compat_payload(resolved.raw_config) == {
        "level": "leaf",
        "base": True,
        "middle": True,
        "leaf": True,
    }
    assert resolved.source_paths == (
        base_path.resolve(),
        middle_path.resolve(),
        leaf_path.resolve(),
    )


def test_missing_parent_reports_the_problem_path(tmp_path: Path) -> None:
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path, {"extends": "_bases/missing.yaml", "adapter": "openclaw"}
    )

    with pytest.raises(
        SystemConfigError, match=r"_bases/missing\.yaml.*not found|not found.*missing"
    ):
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )


def test_inheritance_cycle_reports_the_complete_chain(tmp_path: Path) -> None:
    systems_root = tmp_path / "systems"
    document = _index_document()
    _configure_path(document, "openclaw", "a.yaml")
    _write_config(systems_root, "a.yaml", {"extends": "b.yaml", "adapter": "openclaw"})
    _write_config(systems_root, "b.yaml", {"extends": "a.yaml", "adapter": "openclaw"})
    index_path = _write_index(systems_root, document)

    with pytest.raises(
        SystemConfigError, match=r"inheritance cycle.*a\.yaml.*b\.yaml.*a\.yaml"
    ):
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )


@pytest.mark.parametrize(
    "unsafe_parent", ["../outside.yaml", "/absolute/outside.yaml", r"..\outside.yaml"]
)
def test_extends_rejects_non_posix_or_escaping_paths(
    tmp_path: Path, unsafe_parent: str
) -> None:
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path, {"extends": unsafe_parent, "adapter": "openclaw"}
    )

    with pytest.raises(SystemConfigError, match=r"extends.*relative|POSIX|escape"):
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )


def test_symlinked_config_file_is_rejected_even_when_target_stays_inside_root(
    tmp_path: Path,
) -> None:
    systems_root = tmp_path / "systems"
    document = _index_document()
    _configure_path(document, "openclaw", "leaf.yaml")
    real_path = _write_config(systems_root, "real.yaml", {"adapter": "openclaw"})
    symlink_path = systems_root / "leaf.yaml"
    symlink_path.symlink_to(real_path.name)
    index_path = _write_index(systems_root, document)

    with pytest.raises(SystemConfigError, match=r"leaf\.yaml.*symlink|symlink.*leaf"):
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )


def test_parent_symlink_cannot_bypass_alias_inheritance_restriction(
    tmp_path: Path,
) -> None:
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path, {"extends": "linked/openclaw-hybrid.yaml"}
    )
    _write_config(
        systems_root,
        "openclaw-hybrid.yaml",
        {"adapter": "openclaw", "bypassed_alias": True},
    )
    (systems_root / "linked").symlink_to(".", target_is_directory=True)

    with pytest.raises(SystemConfigError) as error:
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )

    message = str(error.value)
    assert "linked/openclaw-hybrid.yaml" in message
    assert "linked" in message
    assert "symlink" in message


def test_parent_symlink_to_an_internal_directory_is_rejected(tmp_path: Path) -> None:
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path, {"extends": "linked/base.yaml"}
    )
    _write_config(
        systems_root, "_bases/real/base.yaml", {"adapter": "openclaw", "base": True}
    )
    (systems_root / "linked").symlink_to("_bases/real", target_is_directory=True)

    with pytest.raises(SystemConfigError) as error:
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )

    message = str(error.value)
    assert "linked/base.yaml" in message
    assert "linked" in message
    assert "symlink" in message


def test_parent_directory_symlink_cannot_escape_the_systems_root(
    tmp_path: Path,
) -> None:
    systems_root = tmp_path / "systems"
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    _write_config(outside_root, "leaf.yaml", {"adapter": "openclaw"})
    systems_root.mkdir()
    (systems_root / "linked").symlink_to(outside_root, target_is_directory=True)
    document = _index_document()
    _configure_path(document, "openclaw", "linked/leaf.yaml")
    index_path = _write_index(systems_root, document)

    with pytest.raises(
        SystemConfigError, match=r"linked/leaf\.yaml.*outside|escape|symlink"
    ):
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )


def test_directory_cannot_be_loaded_as_a_config(tmp_path: Path) -> None:
    systems_root = tmp_path / "systems"
    document = _index_document()
    _configure_path(document, "openclaw", "not-a-file.yaml")
    (systems_root / "not-a-file.yaml").mkdir(parents=True)
    index_path = _write_index(systems_root, document)

    with pytest.raises(
        SystemConfigError, match=r"not-a-file\.yaml.*regular file|directory"
    ):
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )


@pytest.mark.parametrize("alias_id", ["hermes", "openclaw-hybrid"])
def test_alias_legacy_yaml_cannot_be_used_as_an_inheritance_target(
    tmp_path: Path, alias_id: str
) -> None:
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path, {"extends": f"{alias_id}.yaml", "adapter": "openclaw"}
    )
    _write_config(systems_root, f"{alias_id}.yaml", {"adapter": "openclaw"})

    with pytest.raises(
        SystemConfigError,
        match=rf"alias.*{re.escape(alias_id)}|{re.escape(alias_id)}.*alias",
    ):
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )


def test_nested_base_can_share_an_alias_basename(tmp_path: Path) -> None:
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path, {"extends": "_bases/hermes.yaml", "description": "leaf"}
    )
    base = _minimal_openclaw_config()
    base["description"] = "base"
    base["openclaw"]["status_timeout_seconds"] = 60
    base_path = _write_config(systems_root, "_bases/hermes.yaml", base)

    resolved = resolve_system_config(
        "openclaw", index_path=index_path, systems_root=systems_root, environ={}
    )

    assert resolved.raw_config["description"] == "leaf"
    assert resolved.raw_config["openclaw"]["status_timeout_seconds"] == 60
    assert resolved.source_paths[0] == base_path.resolve()


def test_alias_to_alias_chain_preserves_requested_metadata(tmp_path: Path) -> None:
    document = _index_document()
    systems = _systems(document)
    intermediate = systems["openclaw-agent-local"]
    intermediate.pop("path")
    intermediate["category"] = "alias"
    intermediate["status"] = "compatibility"
    intermediate["alias_of"] = "openclaw"
    systems["openclaw-hybrid"]["alias_of"] = "openclaw-agent-local"
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path, {"adapter": "openclaw", "description": "canonical"}, document=document
    )

    resolved = resolve_system_config(
        "openclaw-hybrid", index_path=index_path, systems_root=systems_root, environ={}
    )

    assert resolved.requested_id == "openclaw-hybrid"
    assert resolved.canonical_id == "openclaw"
    assert resolved.alias_chain == (
        "openclaw-hybrid",
        "openclaw-agent-local",
        "openclaw",
    )
    assert resolved.category == "alias"
    assert resolved.status == "compatibility"
    assert resolved.adapter == "openclaw"
    assert resolved.raw_config["description"] == "canonical"


def test_alias_cycle_from_the_strict_36_id_index_is_a_domain_error(
    tmp_path: Path,
) -> None:
    document = _index_document()
    systems = _systems(document)
    intermediate = systems["openclaw-agent-local"]
    intermediate.pop("path")
    intermediate["category"] = "alias"
    intermediate["status"] = "compatibility"
    intermediate["alias_of"] = "openclaw-hybrid"
    systems["openclaw-hybrid"]["alias_of"] = "openclaw-agent-local"
    index_path = _write_index(tmp_path / "systems", document)

    with pytest.raises(
        SystemIndexError, match=r"alias cycle.*openclaw-hybrid.*openclaw-agent-local"
    ):
        resolve_system_config(
            "openclaw-hybrid",
            index_path=index_path,
            systems_root=index_path.parent,
            environ={},
        )


def test_environment_substitution_matches_existing_compatibility_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SET_VALUE", "host-value-must-not-leak")
    monkeypatch.setenv("ONLY_IN_HOST", "host-value-must-not-leak")
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path,
        {
            "adapter": "openclaw",
            "openclaw": {
                "agent_llm": {
                    "model": {
                        "compat": {
                            "set": "${SET_VALUE:loser}",
                            "set_to_empty": "${EMPTY_VALUE:fallback}",
                            "default": "${MISSING:fallback}",
                            "empty_default": "${MISSING:}",
                            "required_unset": "${MISSING}",
                            "explicit_mapping_only": "${ONLY_IN_HOST:fallback}",
                            "multiple": "before-${SET_VALUE}-${MISSING:after}",
                            "nested": [
                                "${SET_VALUE}",
                                {"value": "${MISSING:fallback}"},
                            ],
                        }
                    }
                }
            },
        },
    )

    resolved = resolve_system_config(
        "openclaw",
        index_path=index_path,
        systems_root=systems_root,
        environ={"SET_VALUE": "provided", "EMPTY_VALUE": ""},
    )

    assert _compat_payload(resolved.raw_config) == {
        "set": "${SET_VALUE:loser}",
        "set_to_empty": "${EMPTY_VALUE:fallback}",
        "default": "${MISSING:fallback}",
        "empty_default": "${MISSING:}",
        "required_unset": "${MISSING}",
        "explicit_mapping_only": "${ONLY_IN_HOST:fallback}",
        "multiple": "before-${SET_VALUE}-${MISSING:after}",
        "nested": ["${SET_VALUE}", {"value": "${MISSING:fallback}"}],
    }
    assert _compat_payload(resolved.config) == {
        "set": "provided",
        "set_to_empty": "",
        "default": "fallback",
        "empty_default": "",
        "required_unset": "",
        "explicit_mapping_only": "fallback",
        "multiple": "before-provided-after",
        "nested": ["provided", {"value": "fallback"}],
    }

    _compat_payload(resolved.config)["nested"][1]["value"] = "changed"
    assert _compat_payload(resolved.raw_config)["nested"][1]["value"] == (
        "${MISSING:fallback}"
    )


def test_environ_none_reads_os_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOADER_TEST_VALUE", "from-os")
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path,
        {"adapter": "openclaw", "llm": {"model": "${LOADER_TEST_VALUE:fallback}"}},
    )

    resolved = resolve_system_config(
        "openclaw", index_path=index_path, systems_root=systems_root
    )

    assert resolved.config["llm"]["model"] == "from-os"
    assert resolved.raw_config["llm"]["model"] == "${LOADER_TEST_VALUE:fallback}"


@pytest.mark.parametrize("adapter", [None, "", "   ", 7, "mem0"])
def test_resolved_adapter_must_be_nonempty_and_match_the_canonical_index(
    tmp_path: Path, adapter: object
) -> None:
    systems_root, index_path, _ = _simple_openclaw_root(tmp_path, {"adapter": adapter})

    with pytest.raises(
        SystemConfigError, match=r"adapter.*openclaw|adapter.*non-empty"
    ):
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )


@pytest.mark.parametrize("yaml_text", ["- one\n- two\n", "null\n", "plain scalar\n"])
def test_config_yaml_must_contain_a_mapping(tmp_path: Path, yaml_text: str) -> None:
    systems_root = tmp_path / "systems"
    document = _index_document()
    _configure_path(document, "openclaw", "leaf.yaml")
    _write_raw_config(systems_root, "leaf.yaml", yaml_text)
    index_path = _write_index(systems_root, document)

    with pytest.raises(SystemConfigError, match=r"leaf\.yaml.*mapping|mapping.*leaf"):
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )


def test_invalid_config_yaml_is_wrapped_as_a_system_config_error(
    tmp_path: Path,
) -> None:
    systems_root = tmp_path / "systems"
    document = _index_document()
    _configure_path(document, "openclaw", "leaf.yaml")
    _write_raw_config(systems_root, "leaf.yaml", "adapter: [unterminated\n")
    index_path = _write_index(systems_root, document)

    with pytest.raises(SystemConfigError, match="invalid YAML") as error:
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )

    assert isinstance(error.value.__cause__, yaml.YAMLError)


@pytest.mark.parametrize(
    ("duplicate_key", "yaml_text"),
    [
        (
            "extends",
            """
extends: _bases/first.yaml
extends: _bases/second.yaml
leaf: true
""",
        ),
        (
            "adapter",
            """
adapter: mem0
adapter: openclaw
""",
        ),
        (
            "value",
            """
adapter: openclaw
nested:
  value: first
  value: second
""",
        ),
    ],
    ids=["duplicate-extends", "duplicate-adapter", "nested-duplicate"],
)
def test_config_duplicate_mapping_keys_are_rejected_at_every_depth(
    tmp_path: Path, duplicate_key: str, yaml_text: str
) -> None:
    systems_root = tmp_path / "systems"
    document = _index_document()
    _configure_path(document, "openclaw", "leaf.yaml")
    _write_raw_config(systems_root, "leaf.yaml", yaml_text)
    _write_config(
        systems_root, "_bases/first.yaml", {"adapter": "openclaw", "first": True}
    )
    _write_config(
        systems_root, "_bases/second.yaml", {"adapter": "openclaw", "second": True}
    )
    index_path = _write_index(systems_root, document)

    with pytest.raises(SystemConfigError) as error:
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )

    message = str(error.value)
    assert "duplicate key" in message
    assert duplicate_key in message
    assert "line" in message
    assert "column" in message


@pytest.mark.parametrize(
    "yaml_text",
    [
        """
defaults: &defaults
  adapter: openclaw
<<: *defaults
""",
        """
first: &first
  adapter: openclaw
second: &second
  inherited: true
<<: *first
<<: *second
""",
        """
defaults: &defaults
  adapter: mem0
<<: *defaults
adapter: openclaw
""",
    ],
    ids=["single-merge", "repeated-merges", "merge-with-explicit-override"],
)
def test_config_yaml_merge_keys_are_rejected(tmp_path: Path, yaml_text: str) -> None:
    systems_root = tmp_path / "systems"
    document = _index_document()
    _configure_path(document, "openclaw", "leaf.yaml")
    _write_raw_config(systems_root, "leaf.yaml", yaml_text)
    index_path = _write_index(systems_root, document)

    with pytest.raises(SystemConfigError) as error:
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )

    message = str(error.value)
    assert "merge key" in message
    assert "<<" in message
    assert "line" in message
    assert "column" in message


@pytest.mark.parametrize("extends", [None, "", "   ", 7, ["base.yaml"]])
def test_extends_must_be_a_nonempty_posix_string(
    tmp_path: Path, extends: object
) -> None:
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path, {"extends": extends, "adapter": "openclaw"}
    )

    with pytest.raises(SystemConfigError, match=r"extends.*non-empty.*POSIX|string"):
        resolve_system_config(
            "openclaw", index_path=index_path, systems_root=systems_root, environ={}
        )


def test_result_is_frozen_and_raw_and_expanded_configs_do_not_alias(
    tmp_path: Path,
) -> None:
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path,
        {
            "adapter": "openclaw",
            "openclaw": {
                "agent_llm": {
                    "model": {
                        "compat": {
                            "marker": "${VALUE:default}",
                            "items": [{"value": 1}],
                        }
                    }
                }
            },
        },
    )

    resolved = resolve_system_config(
        "openclaw",
        index_path=index_path,
        systems_root=systems_root,
        environ={"VALUE": "expanded"},
    )

    with pytest.raises(FrozenInstanceError):
        resolved.requested_id = "changed"  # type: ignore[misc]
    assert resolved.raw_config is not resolved.config
    assert _compat_payload(resolved.raw_config) is not _compat_payload(resolved.config)
    assert (
        _compat_payload(resolved.raw_config)["items"]
        is not _compat_payload(resolved.config)["items"]
    )

    _compat_payload(resolved.config)["items"][0]["value"] = 99
    assert _compat_payload(resolved.raw_config)["items"][0]["value"] == 1


@pytest.mark.parametrize(
    ("deprecated_id", "requested_id", "replacement"),
    [
        ("openclaw-hybrid", "openclaw-hybrid", "openclaw"),
        ("openclaw", "openclaw-hybrid", "mem0"),
    ],
)
def test_deprecated_requested_or_canonical_entry_emits_a_clear_warning(
    tmp_path: Path, deprecated_id: str, requested_id: str, replacement: str
) -> None:
    document = _index_document()
    deprecated_entry = _systems(document)[deprecated_id]
    deprecated_entry["status"] = "deprecated"
    deprecated_entry["replacement"] = replacement
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path, {"adapter": "openclaw"}, document=document
    )

    resolved = resolve_system_config(
        requested_id, index_path=index_path, systems_root=systems_root, environ={}
    )

    assert resolved.warning is not None
    assert requested_id in resolved.warning
    assert deprecated_id in resolved.warning
    assert replacement in resolved.warning
    assert resolved.category == "alias"
    expected_status = (
        deprecated_entry["status"] if deprecated_id == requested_id else "compatibility"
    )
    assert resolved.status == expected_status


def test_deprecation_warning_includes_an_intermediate_alias(tmp_path: Path) -> None:
    document = _index_document()
    systems = _systems(document)
    intermediate = systems["openclaw-agent-local"]
    intermediate.pop("path")
    intermediate["category"] = "alias"
    intermediate["status"] = "deprecated"
    intermediate["alias_of"] = "openclaw"
    intermediate["replacement"] = "mem0"
    systems["openclaw-hybrid"]["alias_of"] = "openclaw-agent-local"
    systems_root, index_path, _ = _simple_openclaw_root(
        tmp_path, {"adapter": "openclaw"}, document=document
    )

    resolved = resolve_system_config(
        "openclaw-hybrid", index_path=index_path, systems_root=systems_root, environ={}
    )

    assert resolved.warning is not None
    assert "openclaw-hybrid" in resolved.warning
    assert "openclaw-agent-local" in resolved.warning
    assert "replacement: 'mem0'" in resolved.warning
    assert resolved.category == "alias"
    assert resolved.status == "compatibility"


def test_real_aliases_resolve_to_their_canonical_raw_configs() -> None:
    environ = {
        "HERMES_REPO_PATH": "/tmp/hermes",
        "LLM_API_KEY": "llm-key",
        "LLM_MODEL": "test-model",
        "OPENCLAW_REPO_PATH": "/tmp/openclaw",
        "SOPH_API_KEY": "embed-key",
    }
    openclaw_alias = resolve_system_config("openclaw-hybrid", environ=environ)
    openclaw_canonical = resolve_system_config("openclaw", environ=environ)
    hermes_alias = resolve_system_config("hermes", environ=environ)
    hermes_canonical = resolve_system_config("hermes-holographic", environ=environ)

    assert openclaw_alias.raw_config == openclaw_canonical.raw_config
    assert openclaw_alias.canonical_id == "openclaw"
    assert openclaw_alias.alias_chain == ("openclaw-hybrid", "openclaw")
    assert openclaw_alias.category == "alias"
    assert openclaw_alias.status == "compatibility"
    assert hermes_alias.raw_config == hermes_canonical.raw_config
    assert hermes_alias.canonical_id == "hermes-holographic"
    assert hermes_alias.alias_chain == ("hermes", "hermes-holographic")
    assert tuple(
        path.relative_to(SYSTEMS_ROOT).as_posix() for path in hermes_alias.source_paths
    ) == ("_bases/hermes.yaml", "canonical/hermes-holographic.yaml")


def test_real_hermes_base_contains_only_shared_sections() -> None:
    base_path = SYSTEMS_ROOT / "_bases" / "hermes.yaml"

    assert base_path.is_file()
    base = yaml.safe_load(base_path.read_text(encoding="utf-8"))

    assert set(base) == {"adapter", "llm", "search", "answer", "hermes"}
    assert set(base["hermes"]) == {"repo_path", "prompts"}


@pytest.mark.parametrize(("system_id", "variant"), HERMES_VARIANTS.items())
def test_real_hermes_variant_files_contain_only_plugin_axis(
    system_id: str, variant: dict[str, Any]
) -> None:
    leaf_path = SYSTEMS_ROOT / "canonical" / f"{system_id}.yaml"

    assert leaf_path.is_file()
    leaf = yaml.safe_load(leaf_path.read_text(encoding="utf-8"))

    assert leaf == {"extends": "_bases/hermes.yaml", "hermes": variant}


@pytest.mark.parametrize("system_id", HERMES_VARIANTS)
def test_real_hermes_variants_source_from_shared_base(system_id: str) -> None:
    resolved = resolve_system_config(system_id, environ=HERMES_ENVIRONMENT)

    assert tuple(
        path.relative_to(SYSTEMS_ROOT).as_posix() for path in resolved.source_paths
    ) == ("_bases/hermes.yaml", f"canonical/{system_id}.yaml")


def test_real_hermes_variants_differ_only_on_plugin_axis() -> None:
    shared_configs = []

    for system_id, expected_variant in HERMES_VARIANTS.items():
        raw_config = resolve_system_config(
            system_id, environ=HERMES_ENVIRONMENT
        ).raw_config
        assert {
            key: raw_config["hermes"][key]
            for key in ("plugin", "ingest_strategy", "plugin_config")
        } == expected_variant

        shared_config = deepcopy(raw_config)
        for key in ("plugin", "ingest_strategy", "plugin_config"):
            shared_config["hermes"].pop(key)
        shared_configs.append(shared_config)

    assert shared_configs[1:] == [shared_configs[0]] * (len(shared_configs) - 1)

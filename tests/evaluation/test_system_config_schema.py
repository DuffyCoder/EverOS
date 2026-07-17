from __future__ import annotations

from copy import deepcopy
from datetime import date
from types import MappingProxyType
from typing import Any

import pytest

from evaluation.src.config.system_schema import (
    SystemSchemaError,
    validate_system_config,
)


def _llm() -> dict[str, Any]:
    return {
        "provider": "openai",
        "model": "test-model",
        "api_key": "runtime-secret",
        "base_url": "https://llm.example/v1",
        "temperature": 0,
        "max_tokens": 1024,
    }


def _answer() -> dict[str, int]:
    return {"max_concurrent": 2}


def _online_answer() -> dict[str, int]:
    return {"max_retries": 3, "max_concurrent": 2}


def _openclaw_search() -> dict[str, int]:
    return {
        "top_k": 6,
        "response_top_k": 5,
        "num_workers": 2,
        "max_inflight_queries_per_conversation": 1,
    }


def _agent_llm() -> dict[str, Any]:
    return {
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
            "cost": {"input": 0, "output": 0.0, "cacheRead": 0, "cacheWrite": 0},
            "context_window": 128000,
            "max_tokens": 4096,
        },
    }


def _embedding() -> dict[str, Any]:
    return {
        "provider": "test",
        "model": "embedding-model",
        "api_key_env": "EMBED_API_KEY",
        "base_url": "https://embed.example/v1",
        "easyllm_id": "deployment",
        "output_dimensionality": 1024,
    }


def _openclaw_block() -> dict[str, Any]:
    return {
        "repo_path": "/tmp/openclaw",
        "visibility_mode": "settled",
        "retrieval_route": "search_then_get",
        "backend_mode": "fts_only",
        "flush_mode": "shared_llm",
        "memory_mode": "memory-core",
    }


def _valid_configs() -> dict[str, dict[str, Any]]:
    common_online = {"llm": _llm(), "answer": _online_answer(), "num_workers": 2}
    openclaw_common = {
        "llm": _llm(),
        "search": _openclaw_search(),
        "answer": _answer(),
        "openclaw": _openclaw_block(),
    }
    configs = {
        "evermemos": {
            "adapter": "evermemos",
            "llm": _llm(),
            "search": {"mode": "agentic"},
        },
        "evermemos_api": {
            "adapter": "evermemos_api",
            "base_url": "https://memory.example/api/v1/memories",
            "api_key": "runtime-secret",
            "sync_mode": True,
            "max_retries": 3,
            "timeout_seconds": 60,
            "request_interval": 0.0,
            "search": {
                "scope": "group",
                "top_k": 10,
                "retrieve_method": "agentic",
                "memory_types": ["episodic_memory"],
                "num_workers": 2,
                "timeout_seconds": 300,
            },
            **deepcopy(common_online),
        },
        "mem0": {
            "adapter": "mem0",
            "api_key": "runtime-secret",
            "batch_size": 5,
            "max_retries": 5,
            "max_content_length": 8000,
            "add_interval": 0.5,
            "post_add_wait_seconds": 180,
            "search": {"top_k": 20, "search_interval": 0.5},
            **deepcopy(common_online),
        },
        "memos": {
            "adapter": "memos",
            "api_url": "https://memory.example/v1",
            "api_key": "runtime-secret",
            "batch_size": 10,
            "max_retries": 5,
            "requests_per_second": 10,
            "post_add_wait_seconds": 180,
            "search": {"top_k": 20},
            **deepcopy(common_online),
        },
        "memu": {
            "adapter": "memu",
            "api_key": "runtime-secret",
            "base_url": "https://memory.example",
            "max_retries": 5,
            "agent_id": "agent",
            "agent_name": "Assistant",
            "task_check_interval": 15,
            "task_timeout": 4800,
            "valid_users": ["user-1"],
            "mock_mode": False,
            "search": {"top_k": 20, "min_similarity": 0.3},
            **deepcopy(common_online),
        },
        "zep": {
            "adapter": "zep",
            "api_key": "runtime-secret",
            "max_retries": 5,
            "poll_interval": 5,
            "add_message_interval": 0.2,
            "post_add_wait_seconds": 0,
            "search": {
                "top_k": 20,
                "reranker_nodes": "rrf",
                "reranker_edges": "cross_encoder",
            },
            **deepcopy(common_online),
        },
        "hermes": {
            "adapter": "hermes",
            "llm": _llm(),
            "search": _openclaw_search(),
            "answer": _answer(),
            "hermes": {
                "repo_path": "/tmp/hermes",
                "plugin": "holographic",
                "ingest_strategy": "session_end",
                "plugin_config": {"auto_extract": True},
                "prompts": {"answer_mode": "shared"},
            },
        },
        "openclaw": {"adapter": "openclaw", **deepcopy(openclaw_common)},
        "openclaw-docker": {
            "adapter": "openclaw-docker",
            **deepcopy(openclaw_common),
            "openclaw_docker": {
                "image": "registry.example/openclaw:stable",
                "max_concurrent_containers": 2,
                "mem_limit": "2g",
                "network": "bridge",
                "per_rpc_timeout_seconds": 210,
                "capture_container_logs": True,
                "remove_container_on_stop": True,
                "add_host_gateway": False,
            },
        },
    }
    configs["openclaw-docker"]["openclaw"].update(
        {
            "answer_mode": "agent_local",
            "memory_mode": "memory-core",
            "agent_timeout_seconds": 180,
            "agent_llm": _agent_llm(),
        }
    )
    return configs


VALID_CONFIGS = _valid_configs()

ONLINE_ANSWER_RETRY_ADAPTERS = ("evermemos_api", "mem0", "memos", "memu", "zep")
NON_ONLINE_ANSWER_ADAPTERS = ("evermemos", "hermes", "openclaw", "openclaw-docker")


def _config_with_all_env_name_fields() -> dict[str, Any]:
    config = deepcopy(VALID_CONFIGS["openclaw-docker"])
    config["openclaw"]["embedding"] = _embedding()
    config["openclaw"]["context_engine_mode"] = "openviking"
    config["openclaw"]["ov_ingest"] = {
        "base_url": "http://127.0.0.1:1933",
        "api_key_env": "OPENVIKING_API_KEY",
        "account_id": "default",
    }
    return config


@pytest.mark.parametrize("adapter", sorted(VALID_CONFIGS))
def test_each_supported_adapter_accepts_a_valid_config_without_coercion(
    adapter: str,
) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    before = deepcopy(config)

    result = validate_system_config(adapter, config)

    assert result is config
    assert config == before
    assert isinstance(config["llm"]["temperature"], int)


@pytest.mark.parametrize("adapter", ONLINE_ANSWER_RETRY_ADAPTERS)
def test_online_adapters_accept_only_positive_answer_max_retries(adapter: str) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    config["answer"]["max_retries"] = 7
    assert validate_system_config(adapter, config) is config

    config = deepcopy(VALID_CONFIGS[adapter])
    config["answer"]["max_retries"] = 0
    with pytest.raises(SystemSchemaError, match=r"answer\.max_retries"):
        validate_system_config(adapter, config)


@pytest.mark.parametrize("adapter", ONLINE_ANSWER_RETRY_ADAPTERS)
def test_online_adapters_reject_explicit_null_answer_max_retries(adapter: str) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    config["answer"]["max_retries"] = None

    with pytest.raises(SystemSchemaError, match=r"answer\.max_retries"):
        validate_system_config(adapter, config)


@pytest.mark.parametrize("adapter", ONLINE_ANSWER_RETRY_ADAPTERS)
def test_online_adapters_reject_explicit_null_answer_block(adapter: str) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    config["answer"] = None

    with pytest.raises(SystemSchemaError, match=r"answer"):
        validate_system_config(adapter, config)


@pytest.mark.parametrize("adapter", ONLINE_ANSWER_RETRY_ADAPTERS)
def test_online_adapters_allow_the_answer_block_to_be_omitted(adapter: str) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    del config["answer"]

    assert validate_system_config(adapter, config) is config


@pytest.mark.parametrize("adapter", NON_ONLINE_ANSWER_ADAPTERS)
def test_non_online_adapters_reject_unused_answer_max_retries(adapter: str) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    config.setdefault("answer", {})["max_retries"] = 3

    with pytest.raises(SystemSchemaError, match=r"answer\.max_retries"):
        validate_system_config(adapter, config)


@pytest.mark.parametrize("adapter", NON_ONLINE_ANSWER_ADAPTERS)
def test_non_online_dataset_overrides_reject_unused_answer_max_retries(
    adapter: str,
) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    config["dataset_overrides"] = {"future-dataset": {"answer": {"max_retries": 3}}}

    with pytest.raises(
        SystemSchemaError,
        match=r"dataset_overrides\.future-dataset\.answer\.max_retries",
    ):
        validate_system_config(adapter, config)


def test_validation_accepts_a_mapping_and_returns_the_same_mapping() -> None:
    config = MappingProxyType(deepcopy(VALID_CONFIGS["openclaw"]))

    result = validate_system_config("openclaw", config)

    assert result is config


@pytest.mark.parametrize("adapter", sorted(VALID_CONFIGS))
def test_each_adapter_rejects_unknown_top_level_fields(adapter: str) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    config["typo_field"] = True

    with pytest.raises(SystemSchemaError, match="typo_field"):
        validate_system_config(adapter, config)


@pytest.mark.parametrize("adapter", sorted(VALID_CONFIGS))
def test_each_adapter_rejects_unknown_nested_fields(adapter: str) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    config["llm"]["typo_field"] = True

    with pytest.raises(SystemSchemaError, match=r"llm\.typo_field"):
        validate_system_config(adapter, config)


@pytest.mark.parametrize("adapter", sorted(VALID_CONFIGS))
@pytest.mark.parametrize(
    ("field", "value"), [("dataset_name", "locomo"), ("clean_groups", True)]
)
def test_source_schema_rejects_runtime_context_fields(
    adapter: str, field: str, value: object
) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    config[field] = value

    with pytest.raises(SystemSchemaError, match=field):
        validate_system_config(adapter, config)


@pytest.mark.parametrize(
    ("adapter", "path"),
    [
        ("evermemos", ("search", "mode")),
        ("evermemos_api", ("timeout_seconds",)),
        ("mem0", ("batch_size",)),
        ("memos", ("requests_per_second",)),
        ("memu", ("task_timeout",)),
        ("zep", ("poll_interval",)),
        ("hermes", ("hermes", "ingest_strategy")),
        ("openclaw", ("openclaw", "backend_mode")),
        ("openclaw-docker", ("openclaw_docker", "max_concurrent_containers")),
    ],
)
def test_each_adapter_rejects_a_wrong_adapter_specific_type(
    adapter: str, path: tuple[str, ...]
) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    target: dict[str, Any] = config
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = ["wrong-type"]

    with pytest.raises(SystemSchemaError, match=r"\.".join(path)):
        validate_system_config(adapter, config)


@pytest.mark.parametrize(
    ("adapter", "required_field"),
    [
        ("evermemos", "search"),
        ("evermemos_api", "base_url"),
        ("mem0", "api_key"),
        ("memos", "api_url"),
        ("memu", "api_key"),
        ("zep", "api_key"),
        ("hermes", "hermes"),
        ("openclaw", "openclaw"),
        ("openclaw-docker", "openclaw_docker"),
    ],
)
def test_each_adapter_rejects_a_missing_required_field(
    adapter: str, required_field: str
) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    del config[required_field]

    with pytest.raises(SystemSchemaError, match=required_field):
        validate_system_config(adapter, config)


def test_unknown_adapter_and_nonmapping_configs_are_domain_errors() -> None:
    with pytest.raises(SystemSchemaError, match="unknown adapter"):
        validate_system_config("future-adapter", {"adapter": "future-adapter"})

    with pytest.raises(SystemSchemaError, match="mapping"):
        validate_system_config("openclaw", ["not", "a", "mapping"])  # type: ignore[arg-type]


def test_adapter_literal_must_match_schema_selection() -> None:
    config = deepcopy(VALID_CONFIGS["openclaw"])
    config["adapter"] = "mem0"

    with pytest.raises(SystemSchemaError, match="adapter"):
        validate_system_config("openclaw", config)


@pytest.mark.parametrize("temperature", [0, 0.0, 0.25])
def test_legitimate_yaml_numeric_forms_are_accepted(temperature: int | float) -> None:
    config = deepcopy(VALID_CONFIGS["openclaw"])
    config["llm"]["temperature"] = temperature

    assert validate_system_config("openclaw", config) is config


@pytest.mark.parametrize("bad_temperature", ["0", True])
def test_numeric_strings_and_bool_as_number_are_rejected(
    bad_temperature: object,
) -> None:
    config = deepcopy(VALID_CONFIGS["openclaw"])
    config["llm"]["temperature"] = bad_temperature

    with pytest.raises(SystemSchemaError, match=r"llm\.temperature"):
        validate_system_config("openclaw", config)

    config = deepcopy(VALID_CONFIGS["openclaw"])
    config["search"]["top_k"] = True
    with pytest.raises(SystemSchemaError, match=r"search\.top_k"):
        validate_system_config("openclaw", config)


@pytest.mark.parametrize("blank", [" ", " \t\n "])
@pytest.mark.parametrize(
    "path", [("description",), ("llm", "model"), ("openclaw", "repo_path")]
)
def test_nonempty_strings_reject_whitespace_only_values(
    path: tuple[str, ...], blank: str
) -> None:
    config = deepcopy(VALID_CONFIGS["openclaw"])
    target: dict[str, Any] = config
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = blank

    with pytest.raises(SystemSchemaError, match=r"\.".join(path)):
        validate_system_config("openclaw", config)


def test_explicit_empty_string_sentinels_remain_valid() -> None:
    api_config = deepcopy(VALID_CONFIGS["evermemos_api"])
    api_config["api_key"] = ""
    assert validate_system_config("evermemos_api", api_config) is api_config

    docker_config = deepcopy(VALID_CONFIGS["openclaw-docker"])
    docker_config["openclaw"]["ingest_session_tail"] = ""
    docker_config["openclaw_docker"]["mem_limit"] = ""
    assert validate_system_config("openclaw-docker", docker_config) is docker_config


def test_llm_provider_matches_the_only_runtime_supported_provider() -> None:
    config = deepcopy(VALID_CONFIGS["openclaw"])
    config["llm"]["provider"] = "openai"
    assert validate_system_config("openclaw", config) is config

    config["llm"]["provider"] = "openai-typo"
    with pytest.raises(SystemSchemaError, match=r"llm\.provider"):
        validate_system_config("openclaw", config)


@pytest.mark.parametrize("mode", ["agentic", "lightweight"])
def test_evermemos_search_mode_accepts_known_retrieval_modes(mode: str) -> None:
    config = deepcopy(VALID_CONFIGS["evermemos"])
    config["search"]["mode"] = mode

    assert validate_system_config("evermemos", config) is config


@pytest.mark.parametrize("mode", ["hybrid", "mode-typo"])
def test_evermemos_search_mode_rejects_unknown_retrieval_modes(mode: str) -> None:
    config = deepcopy(VALID_CONFIGS["evermemos"])
    config["search"]["mode"] = mode

    with pytest.raises(SystemSchemaError, match=r"search\.mode"):
        validate_system_config("evermemos", config)


@pytest.mark.parametrize("field", ["retrieve_method", "mode"])
@pytest.mark.parametrize("method", ["keyword", "vector", "hybrid", "rrf", "agentic"])
def test_evermemos_api_retrieval_methods_match_the_service_enum(
    field: str, method: str
) -> None:
    config = deepcopy(VALID_CONFIGS["evermemos_api"])
    config["search"].pop("retrieve_method", None)
    config["search"].pop("mode", None)
    config["search"][field] = method

    assert validate_system_config("evermemos_api", config) is config

    config["search"][field] = "method-typo"
    with pytest.raises(SystemSchemaError, match=rf"search\.{field}"):
        validate_system_config("evermemos_api", config)


EVERMEMOS_MEMORY_TYPES = [
    "profile",
    "episodic_memory",
    "foresight",
    "event_log",
    "base_memory",
    "preference",
    "core",
    "entity",
    "relation",
    "behavior_history",
    "group_profile",
]


@pytest.mark.parametrize("memory_type", EVERMEMOS_MEMORY_TYPES)
def test_evermemos_api_memory_types_accept_each_service_enum_value(
    memory_type: str,
) -> None:
    config = deepcopy(VALID_CONFIGS["evermemos_api"])
    config["search"]["memory_types"] = memory_type

    assert validate_system_config("evermemos_api", config) is config


def test_evermemos_api_memory_types_preserve_string_and_list_semantics() -> None:
    config = deepcopy(VALID_CONFIGS["evermemos_api"])
    config["search"]["memory_types"] = EVERMEMOS_MEMORY_TYPES
    assert validate_system_config("evermemos_api", config) is config

    config["search"]["memory_types"] = "episodic_memory, profile"
    assert validate_system_config("evermemos_api", config) is config

    config["search"]["memory_types"] = "episodic_memory,memory-type-typo"
    with pytest.raises(SystemSchemaError, match=r"search\.memory_types"):
        validate_system_config("evermemos_api", config)

    config["search"]["memory_types"] = ["episodic_memory", "memory-type-typo"]
    with pytest.raises(SystemSchemaError, match=r"search\.memory_types"):
        validate_system_config("evermemos_api", config)

    config["search"]["memory_types"] = "memory-type-typo"
    with pytest.raises(SystemSchemaError, match=r"search\.memory_types"):
        validate_system_config("evermemos_api", config)


OPENCLAW_MODEL_APIS = [
    "openai-completions",
    "openai-responses",
    "openai-codex-responses",
    "anthropic-messages",
    "google-generative-ai",
    "github-copilot",
    "bedrock-converse-stream",
    "ollama",
    "azure-openai-responses",
]


@pytest.mark.parametrize("api", OPENCLAW_MODEL_APIS)
def test_agent_llm_api_matches_the_pinned_openclaw_model_api_enum(api: str) -> None:
    config = deepcopy(VALID_CONFIGS["openclaw-docker"])
    config["openclaw"]["agent_llm"]["api"] = api

    assert validate_system_config("openclaw-docker", config) is config


def test_agent_llm_api_rejects_unknown_values() -> None:
    config = deepcopy(VALID_CONFIGS["openclaw-docker"])
    config["openclaw"]["agent_llm"]["api"] = "openai-completion-typo"

    with pytest.raises(SystemSchemaError, match=r"agent_llm\.api"):
        validate_system_config("openclaw-docker", config)


@pytest.mark.parametrize(
    "inputs", [[], ["text"], ["image"], ["text", "image"], ["text", "text"]]
)
def test_agent_llm_model_input_matches_the_pinned_openclaw_enum(
    inputs: list[str],
) -> None:
    config = deepcopy(VALID_CONFIGS["openclaw-docker"])
    config["openclaw"]["agent_llm"]["model"]["input"] = inputs

    assert validate_system_config("openclaw-docker", config) is config


def test_agent_llm_model_input_rejects_unknown_values() -> None:
    config = deepcopy(VALID_CONFIGS["openclaw-docker"])
    config["openclaw"]["agent_llm"]["model"]["input"] = ["audio"]

    with pytest.raises(SystemSchemaError, match=r"agent_llm\.model\.input"):
        validate_system_config("openclaw-docker", config)


@pytest.mark.parametrize(
    "reranker", ["rrf", "mmr", "node_distance", "episode_mentions", "cross_encoder"]
)
@pytest.mark.parametrize("field", ["reranker_nodes", "reranker_edges"])
def test_zep_rerankers_match_the_locked_sdk_enum(field: str, reranker: str) -> None:
    config = deepcopy(VALID_CONFIGS["zep"])
    config["search"][field] = reranker

    assert validate_system_config("zep", config) is config

    config["search"][field] = "reranker-typo"
    with pytest.raises(SystemSchemaError, match=rf"search\.{field}"):
        validate_system_config("zep", config)


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("backend_mode", ["fts_only", "vector", "hybrid"]),
        ("retrieval_route", ["search_only", "search_then_get"]),
        ("visibility_mode", ["settled", "eventual"]),
        ("flush_mode", ["disabled", "shared_llm", "session_bundle"]),
        ("answer_mode", ["shared_llm", "agent_local"]),
    ],
)
def test_openclaw_runtime_enum_value_sets(field: str, values: list[str]) -> None:
    for value in values:
        config = deepcopy(VALID_CONFIGS["openclaw"])
        config["openclaw"][field] = value
        if value == "agent_local":
            config["openclaw"].update(
                {
                    "memory_mode": "memory-core",
                    "agent_timeout_seconds": 180,
                    "agent_llm": _agent_llm(),
                }
            )
        assert validate_system_config("openclaw", config) is config

    config = deepcopy(VALID_CONFIGS["openclaw"])
    config["openclaw"][field] = "typo"
    with pytest.raises(SystemSchemaError, match=field):
        validate_system_config("openclaw", config)


def test_openclaw_prompts_are_rejected_as_adapter_unused_configuration() -> None:
    config = deepcopy(VALID_CONFIGS["openclaw"])
    config["openclaw"]["prompts"] = {
        "memory_mode": "native_compiled",
        "flush_mode": "shared_llm",
        "answer_mode": "shared",
    }

    with pytest.raises(SystemSchemaError, match=r"openclaw\.prompts"):
        validate_system_config("openclaw", config)


@pytest.mark.parametrize("strategy", ["sync_per_turn", "session_end", "both"])
def test_hermes_ingest_strategy_enum(strategy: str) -> None:
    config = deepcopy(VALID_CONFIGS["hermes"])
    config["hermes"]["ingest_strategy"] = strategy

    assert validate_system_config("hermes", config) is config


@pytest.mark.parametrize(
    ("field", "plugin_id", "expected"),
    [
        ("memory_mode", "missing-plugin", "unknown plugin"),
        ("memory_mode", "hypercompositor", "memory"),
        ("context_engine_mode", "memory-core", "context-engine"),
    ],
)
def test_plugin_selectors_are_registry_backed_and_kind_aware(
    field: str, plugin_id: str, expected: str
) -> None:
    config = deepcopy(VALID_CONFIGS["openclaw"])
    config["openclaw"][field] = plugin_id

    with pytest.raises(SystemSchemaError, match=expected):
        validate_system_config("openclaw", config)


def test_future_registered_memory_plugin_does_not_require_a_schema_release() -> None:
    config = deepcopy(VALID_CONFIGS["openclaw"])
    config["openclaw"]["memory_mode"] = "hindsight-plugin"

    assert validate_system_config("openclaw", config) is config


@pytest.mark.parametrize(
    "missing_field", ["memory_mode", "agent_timeout_seconds", "agent_llm"]
)
def test_agent_local_requires_its_runtime_dependencies(missing_field: str) -> None:
    config = deepcopy(VALID_CONFIGS["openclaw"])
    config["openclaw"].update(
        {
            "answer_mode": "agent_local",
            "memory_mode": "memory-core",
            "agent_timeout_seconds": 180,
            "agent_llm": _agent_llm(),
        }
    )
    del config["openclaw"][missing_field]

    with pytest.raises(SystemSchemaError, match=missing_field):
        validate_system_config("openclaw", config)


def test_ov_ingest_requires_the_openviking_context_engine() -> None:
    config = deepcopy(VALID_CONFIGS["openclaw-docker"])
    config["openclaw"]["ov_ingest"] = {
        "base_url": "http://127.0.0.1:1933",
        "api_key_env": "OPENVIKING_API_KEY",
        "account_id": "default",
        "user_id_template": "{conv_id}",
        "task_timeout_sec": 600,
    }

    with pytest.raises(SystemSchemaError, match="context_engine_mode"):
        validate_system_config("openclaw-docker", config)

    config["openclaw"]["context_engine_mode"] = "openviking"
    assert validate_system_config("openclaw-docker", config) is config


def test_embedding_requires_api_key_env_and_rejects_api_key() -> None:
    config = deepcopy(VALID_CONFIGS["openclaw"])
    config["openclaw"]["backend_mode"] = "vector"
    config["openclaw"]["embedding"] = _embedding()
    del config["openclaw"]["embedding"]["api_key_env"]

    with pytest.raises(SystemSchemaError, match="api_key_env"):
        validate_system_config("openclaw", config)

    config["openclaw"]["embedding"]["api_key_env"] = "SOPH_API_KEY"
    config["openclaw"]["embedding"]["api_key"] = "runtime-secret"
    with pytest.raises(SystemSchemaError, match=r"embedding\.api_key"):
        validate_system_config("openclaw", config)


@pytest.mark.parametrize(
    ("path", "error_path"),
    [
        (("openclaw", "agent_llm", "api_key_env"), r"agent_llm\.api_key_env"),
        (("openclaw", "agent_llm", "env_vars", 0), r"agent_llm\.env_vars\.0"),
        (("openclaw", "embedding", "api_key_env"), r"embedding\.api_key_env"),
        (("openclaw", "ov_ingest", "api_key_env"), r"ov_ingest\.api_key_env"),
    ],
)
@pytest.mark.parametrize("bad_name", ["${ENV_NAME}", "bad-name", " ", ""])
def test_env_name_schema_fields_reject_non_bare_names(
    path: tuple[str | int, ...], error_path: str, bad_name: str
) -> None:
    config = _config_with_all_env_name_fields()
    target: Any = config
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = bad_name

    with pytest.raises(SystemSchemaError, match=error_path):
        validate_system_config("openclaw-docker", config)


def test_env_name_schema_fields_accept_bare_names() -> None:
    config = _config_with_all_env_name_fields()
    config["openclaw"]["agent_llm"]["api_key_env"] = "LLM_API_KEY_2"
    config["openclaw"]["agent_llm"]["env_vars"] = ["LLM_API_KEY", "SECONDARY_KEY_2"]
    config["openclaw"]["embedding"]["api_key_env"] = "EMBED_API_KEY_2"
    config["openclaw"]["ov_ingest"]["api_key_env"] = "_OPENVIKING_API_KEY"

    assert validate_system_config("openclaw-docker", config) is config


@pytest.mark.parametrize(
    ("path", "error_path"),
    [
        (("openclaw", "agent_llm", "api_key_env"), r"agent_llm\.api_key_env"),
        (("openclaw", "agent_llm", "env_vars", 0), r"agent_llm\.env_vars\.0"),
        (("openclaw", "embedding", "api_key_env"), r"embedding\.api_key_env"),
    ],
)
@pytest.mark.parametrize("bad_name", ["lowercase", "_LEADING_UNDERSCORE", "A" * 129])
def test_bridge_env_name_schema_fields_reject_names_the_js_bridge_will_drop(
    path: tuple[str | int, ...], error_path: str, bad_name: str
) -> None:
    config = _config_with_all_env_name_fields()
    target: Any = config
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = bad_name

    with pytest.raises(SystemSchemaError, match=error_path) as error:
        validate_system_config("openclaw-docker", config)

    assert "^[A-Z][A-Z0-9_]{0,127}$" in str(error.value)


@pytest.mark.parametrize("name", ["lowercase_key", "_LEADING_UNDERSCORE"])
def test_ov_ingest_schema_keeps_generic_python_env_names(name: str) -> None:
    config = _config_with_all_env_name_fields()
    config["openclaw"]["ov_ingest"]["api_key_env"] = name

    assert validate_system_config("openclaw-docker", config) is config


def test_dataset_override_keys_are_dynamic_but_inner_fields_are_strict() -> None:
    config = deepcopy(VALID_CONFIGS["memos"])
    # dataset_overrides is applied to the generic top-level system config by
    # evaluation/cli.py, so num_workers is intentionally shared across the
    # online adapters rather than being a MemU-only nested field.
    config["dataset_overrides"] = {"future-dataset": {"batch_size": 6}}
    assert validate_system_config("memos", config) is config

    config["dataset_overrides"]["future-dataset"]["unknown"] = True
    with pytest.raises(
        SystemSchemaError, match=r"dataset_overrides\.future-dataset\.unknown"
    ):
        validate_system_config("memos", config)

    config = deepcopy(VALID_CONFIGS["memos"])
    config["dataset_overrides"] = {"future-dataset": {"batch_size": "6"}}
    with pytest.raises(
        SystemSchemaError, match=r"dataset_overrides\.future-dataset\.batch_size"
    ):
        validate_system_config("memos", config)


def test_memos_rejects_unused_request_interval_at_top_level() -> None:
    config = deepcopy(VALID_CONFIGS["memos"])
    config["request_interval"] = 0.1

    with pytest.raises(SystemSchemaError, match="request_interval"):
        validate_system_config("memos", config)


def test_memu_rejects_legacy_root_similarity() -> None:
    config = deepcopy(VALID_CONFIGS["memu"])
    config["min_similarity"] = 0.3

    with pytest.raises(SystemSchemaError, match="min_similarity"):
        validate_system_config("memu", config)


def test_memu_dataset_overrides_reject_legacy_root_similarity() -> None:
    config = deepcopy(VALID_CONFIGS["memu"])
    config["dataset_overrides"] = {
        "future-dataset": {"min_similarity": 0.3}
    }

    with pytest.raises(
        SystemSchemaError,
        match=r"dataset_overrides\.future-dataset\.min_similarity",
    ):
        validate_system_config("memu", config)


@pytest.mark.parametrize("min_similarity", [0, 0.65, 1])
def test_memu_search_accepts_probability_similarity(
    min_similarity: int | float,
) -> None:
    config = deepcopy(VALID_CONFIGS["memu"])
    config["search"]["min_similarity"] = min_similarity

    assert validate_system_config("memu", config) is config


@pytest.mark.parametrize("min_similarity", [-0.01, 1.01, "0.65", None])
def test_memu_search_rejects_invalid_similarity(min_similarity: object) -> None:
    config = deepcopy(VALID_CONFIGS["memu"])
    config["search"]["min_similarity"] = min_similarity

    with pytest.raises(SystemSchemaError, match=r"search\.min_similarity"):
        validate_system_config("memu", config)


def test_memu_dataset_override_accepts_nested_search_similarity() -> None:
    config = deepcopy(VALID_CONFIGS["memu"])
    config["dataset_overrides"] = {
        "future-dataset": {"search": {"min_similarity": 0.8}}
    }

    assert validate_system_config("memu", config) is config


def test_memos_dataset_overrides_reject_unused_request_interval() -> None:
    config = deepcopy(VALID_CONFIGS["memos"])
    config["dataset_overrides"] = {
        "future-dataset": {"request_interval": 0.1}
    }

    with pytest.raises(
        SystemSchemaError,
        match=r"dataset_overrides\.future-dataset\.request_interval",
    ):
        validate_system_config("memos", config)


@pytest.mark.parametrize("requests_per_second", [1, 0.5, 10])
def test_memos_accepts_positive_requests_per_second(
    requests_per_second: int | float,
) -> None:
    config = deepcopy(VALID_CONFIGS["memos"])
    config["requests_per_second"] = requests_per_second

    assert validate_system_config("memos", config) is config


def test_memos_allows_requests_per_second_to_use_the_schema_default() -> None:
    config = deepcopy(VALID_CONFIGS["memos"])
    del config["requests_per_second"]

    assert validate_system_config("memos", config) is config


@pytest.mark.parametrize("requests_per_second", [0, -1, "10", None])
def test_memos_rejects_invalid_requests_per_second(
    requests_per_second: object,
) -> None:
    config = deepcopy(VALID_CONFIGS["memos"])
    config["requests_per_second"] = requests_per_second

    with pytest.raises(SystemSchemaError, match="requests_per_second"):
        validate_system_config("memos", config)


@pytest.mark.parametrize(
    ("adapter", "override"),
    [
        ("memos", {"batch_size": 6}),
        ("memos", {"requests_per_second": 4}),
        ("memu", {"num_workers": 1}),
        ("memu", {"task_timeout": 120}),
    ],
)
def test_dataset_overrides_accept_fields_supported_by_their_adapter(
    adapter: str, override: dict[str, object]
) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    config["dataset_overrides"] = {"future-dataset": override}

    assert validate_system_config(adapter, config) is config


@pytest.mark.parametrize("adapter", sorted(VALID_CONFIGS))
def test_dataset_overrides_accept_common_harness_search_fields(adapter: str) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    config["dataset_overrides"] = {
        "future-dataset": {"search": {"num_workers": 2, "response_top_k": 4}}
    }

    assert validate_system_config(adapter, config) is config


@pytest.mark.parametrize(
    ("adapter", "override", "field"),
    [
        ("memos", {"task_timeout": 120}, "task_timeout"),
        ("memos", {"search": {"search_interval": 0.1}}, "search_interval"),
        ("memu", {"requests_per_second": 4}, "requests_per_second"),
        ("memos", {"adapter": "memos"}, "adapter"),
        ("memos", {"dataset_overrides": {}}, "dataset_overrides"),
    ],
)
def test_dataset_overrides_reject_fields_from_other_adapter_schemas(
    adapter: str, override: dict[str, object], field: str
) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    config["dataset_overrides"] = {"future-dataset": override}

    with pytest.raises(SystemSchemaError, match=field):
        validate_system_config(adapter, config)


def test_dataset_override_plugin_errors_include_the_dataset_path() -> None:
    config = deepcopy(VALID_CONFIGS["openclaw"])
    config["dataset_overrides"] = {
        "future-dataset": {"openclaw": {"memory_mode": "missing-plugin"}}
    }

    with pytest.raises(
        SystemSchemaError,
        match=r"dataset_overrides\.future-dataset\.openclaw\.memory_mode",
    ):
        validate_system_config("openclaw", config)


@pytest.mark.parametrize(
    ("adapter", "container_path"),
    [
        ("hermes", ("hermes", "plugin_config")),
        ("openclaw", ("openclaw", "agent_llm", "model", "compat")),
    ],
)
def test_documented_external_json_payload_maps_accept_extensions(
    adapter: str, container_path: tuple[str, ...]
) -> None:
    config = deepcopy(VALID_CONFIGS[adapter])
    if adapter == "openclaw":
        config["openclaw"].update(
            {
                "answer_mode": "agent_local",
                "agent_timeout_seconds": 180,
                "agent_llm": _agent_llm(),
            }
        )
    target: dict[str, Any] = config
    for segment in container_path[:-1]:
        target = target[segment]
    target[container_path[-1]] = {
        "vendorOption": {"enabled": True, "threshold": 0.5, "items": [None, "text", 3]}
    }

    assert validate_system_config(adapter, config) is config


@pytest.mark.parametrize(
    "bad_payload",
    [
        {1: "non-string-key"},
        {"date": date(2026, 7, 16)},
        {"nan": float("nan")},
        {"set": {"unsupported"}},
    ],
)
def test_external_json_payload_maps_reject_non_json_values(
    bad_payload: dict[object, object],
) -> None:
    config = deepcopy(VALID_CONFIGS["hermes"])
    config["hermes"]["plugin_config"] = bad_payload

    with pytest.raises(SystemSchemaError, match=r"hermes\.plugin_config"):
        validate_system_config("hermes", config)


def test_openclaw_docker_environment_memory_mode_is_not_a_consumed_field() -> None:
    config = deepcopy(VALID_CONFIGS["openclaw-docker"])
    config["openclaw_docker"]["environment"] = {"memory_mode": "native_compiled"}

    with pytest.raises(SystemSchemaError, match=r"openclaw_docker\.environment"):
        validate_system_config("openclaw-docker", config)


@pytest.mark.parametrize(
    "required_field",
    ["answer_mode", "memory_mode", "agent_timeout_seconds", "agent_llm"],
)
def test_docker_openclaw_runtime_fields_are_required(required_field: str) -> None:
    config = deepcopy(VALID_CONFIGS["openclaw-docker"])
    del config["openclaw"][required_field]

    with pytest.raises(SystemSchemaError, match=required_field):
        validate_system_config("openclaw-docker", config)

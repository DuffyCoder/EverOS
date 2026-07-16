"""Strict adapter-specific schemas for resolved evaluation system configs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from evaluation.src.plugins.registry import DEFAULT_REGISTRY_PATH, RegistryError
from evaluation.src.plugins.registry import get as get_plugin
from evaluation.src.plugins.registry import load_registry

NonEmptyStr = Annotated[str, Field(min_length=1, pattern=r"\S")]
EnvName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveNumber = Annotated[int | float, Field(gt=0)]
NonNegativeNumber = Annotated[int | float, Field(ge=0)]
Probability = Annotated[int | float, Field(ge=0, le=1)]
RetrieveMethod = Literal["keyword", "vector", "hybrid", "rrf", "agentic"]
MemoryType = Literal[
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
MEMORY_TYPE_VALUES = frozenset(get_args(MemoryType))
OpenClawModelAPI = Literal[
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
OpenClawModelInput = Literal["text", "image"]
ZepReranker = Literal[
    "rrf", "mmr", "node_distance", "episode_mentions", "cross_encoder"
]


class SystemSchemaError(ValueError):
    """Raised when a resolved system config violates its adapter schema."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class LLMConfig(_StrictModel):
    provider: Literal["openai"]
    model: NonEmptyStr
    api_key: str
    base_url: NonEmptyStr
    temperature: NonNegativeNumber | None = None
    max_tokens: PositiveInt


class AnswerConfig(_StrictModel):
    max_retries: PositiveInt | None = None
    max_concurrent: PositiveInt | Literal["auto"] | None = None


class HarnessSearchConfig(_StrictModel):
    response_top_k: PositiveInt | None = None
    num_workers: PositiveInt | None = None


class _SystemBase(_StrictModel):
    adapter: str
    name: NonEmptyStr | None = None
    version: NonEmptyStr | None = None
    description: NonEmptyStr | None = None
    llm: LLMConfig
    answer: AnswerConfig | None = None
    num_workers: PositiveInt | None = None
    post_add_wait_seconds: NonNegativeNumber | None = None
    dataset_overrides: dict[NonEmptyStr, dict[str, JsonValue]] | None = None


class EverMemOSAddConfig(_StrictModel):
    enable_foresight_extraction: bool | None = None
    enable_clustering: bool | None = None
    enable_profile_extraction: bool | None = None


class EverMemOSSearchConfig(HarnessSearchConfig):
    mode: Literal["agentic", "lightweight"]
    use_hybrid_search: bool | None = None
    lightweight_search_mode: Literal["bm25_only", "hybrid", "emb_only"] | None = None
    hybrid_emb_candidates: PositiveInt | None = None
    hybrid_bm25_candidates: PositiveInt | None = None
    hybrid_rrf_k: PositiveInt | None = None


class EverMemOSSystem(_SystemBase):
    adapter: Literal["evermemos"]
    search: EverMemOSSearchConfig
    add: EverMemOSAddConfig | None = None


class EverMemOSAPISearchConfig(HarnessSearchConfig):
    scope: Literal["personal", "group"]
    top_k: PositiveInt
    retrieve_method: RetrieveMethod | None = None
    mode: RetrieveMethod | None = None
    memory_types: str | list[MemoryType] | None = None
    timeout_seconds: PositiveNumber | None = None

    @field_validator("memory_types")
    @classmethod
    def _validate_memory_type_string(
        cls, value: str | list[MemoryType] | None
    ) -> str | list[MemoryType] | None:
        if not isinstance(value, str):
            return value
        items = [item.strip() for item in value.split(",") if item.strip()]
        if not items or any(item not in MEMORY_TYPE_VALUES for item in items):
            raise ValueError("memory_types contains an unknown memory type")
        return value


class EverMemOSAPISystem(_SystemBase):
    adapter: Literal["evermemos_api"]
    base_url: NonEmptyStr
    api_key: str
    sync_mode: bool
    max_retries: PositiveInt
    timeout_seconds: PositiveNumber
    request_interval: NonNegativeNumber
    search: EverMemOSAPISearchConfig


class Mem0SearchConfig(HarnessSearchConfig):
    top_k: PositiveInt
    search_interval: NonNegativeNumber | None = None


class Mem0System(_SystemBase):
    adapter: Literal["mem0"]
    api_key: NonEmptyStr
    batch_size: PositiveInt
    max_retries: PositiveInt
    max_content_length: PositiveInt
    add_interval: NonNegativeNumber
    search: Mem0SearchConfig
    custom_instructions: NonEmptyStr | None = None
    clean_before_add: bool | None = None


class OnlineSearchConfig(HarnessSearchConfig):
    top_k: PositiveInt


class MemosSystem(_SystemBase):
    adapter: Literal["memos"]
    api_url: NonEmptyStr
    api_key: NonEmptyStr
    batch_size: PositiveInt
    max_retries: PositiveInt
    request_interval: NonNegativeNumber | None = None
    requests_per_second: PositiveNumber | None = None
    search: OnlineSearchConfig


class MemuSystem(_SystemBase):
    adapter: Literal["memu"]
    api_key: NonEmptyStr
    base_url: NonEmptyStr
    max_retries: PositiveInt
    agent_id: NonEmptyStr
    agent_name: NonEmptyStr
    task_check_interval: PositiveInt
    task_timeout: PositiveInt
    valid_users: list[NonEmptyStr] | None = None
    mock_mode: bool | None = None
    min_similarity: Probability | None = None
    search: OnlineSearchConfig


class ZepSearchConfig(OnlineSearchConfig):
    reranker_nodes: ZepReranker | None = None
    reranker_edges: ZepReranker | None = None


class ZepSystem(_SystemBase):
    adapter: Literal["zep"]
    api_key: NonEmptyStr
    max_retries: PositiveInt | None = None
    poll_interval: PositiveInt
    add_message_interval: NonNegativeNumber | None = None
    search: ZepSearchConfig


class HermesPromptsConfig(_StrictModel):
    answer_mode: Literal["shared"]


class HermesConfig(_StrictModel):
    repo_path: NonEmptyStr
    plugin: NonEmptyStr
    ingest_strategy: Literal["sync_per_turn", "session_end", "both"]
    plugin_config: dict[str, JsonValue]
    prompts: HermesPromptsConfig


class HermesSearchConfig(HarnessSearchConfig):
    top_k: PositiveInt
    response_top_k: PositiveInt
    num_workers: PositiveInt
    max_inflight_queries_per_conversation: PositiveInt


class HermesSystem(_SystemBase):
    adapter: Literal["hermes"]
    search: HermesSearchConfig
    hermes: HermesConfig


class AgentLLMCost(_StrictModel):
    input: NonNegativeNumber
    output: NonNegativeNumber
    cacheRead: NonNegativeNumber
    cacheWrite: NonNegativeNumber


class AgentLLMModel(_StrictModel):
    id: NonEmptyStr
    name: NonEmptyStr
    reasoning: bool
    input: list[OpenClawModelInput]
    cost: AgentLLMCost
    context_window: PositiveInt
    max_tokens: PositiveInt
    compat: dict[str, JsonValue] | None = None


class AgentLLMConfig(_StrictModel):
    provider_id: NonEmptyStr
    base_url: NonEmptyStr
    api: OpenClawModelAPI
    api_key_env: EnvName
    env_vars: list[EnvName] | None = None
    model: AgentLLMModel
    idle_timeout_seconds: NonNegativeInt | None = None


class EmbeddingConfig(_StrictModel):
    provider: NonEmptyStr
    model: NonEmptyStr
    api_key: NonEmptyStr | None = None
    api_key_env: EnvName | None = None
    base_url: NonEmptyStr
    easyllm_id: NonEmptyStr | None = None
    output_dimensionality: PositiveInt

    @model_validator(mode="after")
    def _require_credential_reference(self) -> "EmbeddingConfig":
        if self.api_key is None and self.api_key_env is None:
            raise ValueError("embedding requires api_key or api_key_env")
        return self


class OVIngestConfig(_StrictModel):
    base_url: NonEmptyStr
    api_key_env: EnvName
    account_id: NonEmptyStr
    user_id: str | None = None
    user_id_template: NonEmptyStr | None = None
    agent_id: str | None = None
    agent_id_template: NonEmptyStr | None = None
    task_timeout_sec: PositiveNumber | None = None
    cleanup_timeout_sec: PositiveNumber | None = None
    cleanup_max_retries: PositiveInt | None = None
    cleanup_retry_delay_sec: NonNegativeNumber | None = None
    isolate_user_scope_by_agent: bool | None = None
    isolateUserScopeByAgent: bool | None = None
    isolate_agent_scope_by_user: bool | None = None
    isolateAgentScopeByUser: bool | None = None


class OpenClawPromptsConfig(_StrictModel):
    memory_mode: Literal["native_compiled"]
    flush_mode: Literal["disabled", "shared_llm", "session_bundle", "native"]
    answer_mode: Literal["shared"]


class OpenClawConfig(_StrictModel):
    repo_path: NonEmptyStr
    visibility_mode: Literal["settled", "eventual"]
    retrieval_route: Literal["search_only", "search_then_get"]
    backend_mode: Literal["fts_only", "vector", "hybrid"]
    flush_mode: Literal["disabled", "shared_llm", "session_bundle"]
    answer_mode: Literal["shared_llm", "agent_local"] | None = None
    memory_mode: NonEmptyStr | None = None
    context_engine_mode: NonEmptyStr | None = None
    agent_timeout_seconds: PositiveInt | None = None
    agent_llm: AgentLLMConfig | None = None
    embedding: EmbeddingConfig | None = None
    prompts: OpenClawPromptsConfig | None = None
    ov_ingest: OVIngestConfig | None = None
    honor_silent_token: bool | None = None
    ingest_session_tail: str | None = None
    add_max_concurrent_convs: PositiveInt | None = None
    flush_max_retries: PositiveInt | None = None
    flush_retry_base_seconds: NonNegativeNumber | None = None
    index_timeout_seconds: PositiveNumber | None = None
    status_timeout_seconds: PositiveNumber | None = None

    @model_validator(mode="after")
    def _validate_conditional_fields(self) -> "OpenClawConfig":
        if self.answer_mode == "agent_local":
            missing = [
                field
                for field in ("memory_mode", "agent_timeout_seconds", "agent_llm")
                if getattr(self, field) is None
            ]
            if missing:
                raise ValueError(
                    "answer_mode=agent_local requires " + ", ".join(missing)
                )
        if self.ov_ingest is not None and self.context_engine_mode != "openviking":
            raise ValueError("ov_ingest requires context_engine_mode='openviking'")
        return self


class OpenClawSearchConfig(HarnessSearchConfig):
    top_k: PositiveInt
    response_top_k: PositiveInt
    num_workers: PositiveInt
    max_inflight_queries_per_conversation: PositiveInt


class OpenClawSystem(_SystemBase):
    adapter: Literal["openclaw"]
    search: OpenClawSearchConfig
    openclaw: OpenClawConfig


class OpenClawDockerConfig(_StrictModel):
    image: NonEmptyStr
    max_concurrent_containers: PositiveInt | None = None
    mem_limit: str | None = None
    network: NonEmptyStr | None = None
    per_rpc_timeout_seconds: PositiveInt | None = None
    capture_container_logs: bool | None = None
    remove_container_on_stop: bool | None = None
    add_host_gateway: bool | None = None
    container_tmp_host_path: NonEmptyStr | None = None


class OpenClawDockerRuntimeConfig(OpenClawConfig):
    answer_mode: Literal["shared_llm", "agent_local"]
    memory_mode: NonEmptyStr
    agent_timeout_seconds: PositiveInt
    agent_llm: AgentLLMConfig


class OpenClawDockerSystem(_SystemBase):
    adapter: Literal["openclaw-docker"]
    search: OpenClawSearchConfig
    openclaw: OpenClawDockerRuntimeConfig
    openclaw_docker: OpenClawDockerConfig


_MODELS: dict[str, type[_SystemBase]] = {
    "evermemos": EverMemOSSystem,
    "evermemos_api": EverMemOSAPISystem,
    "mem0": Mem0System,
    "memos": MemosSystem,
    "memu": MemuSystem,
    "zep": ZepSystem,
    "hermes": HermesSystem,
    "openclaw": OpenClawSystem,
    "openclaw-docker": OpenClawDockerSystem,
}


def validate_system_config(
    adapter: str, config: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Validate a runtime config and return the exact original mapping."""
    if not isinstance(config, Mapping):
        raise SystemSchemaError(
            f"system config for adapter {adapter!r} must be a mapping"
        )
    model = _MODELS.get(adapter)
    if model is None:
        raise SystemSchemaError(f"unknown adapter schema: {adapter!r}")

    try:
        validated = model.model_validate(dict(config))
        _validate_model_plugin_selectors(validated)
        _validate_dataset_overrides(adapter, config, model)
    except ValidationError as exc:
        raise SystemSchemaError(_format_validation_error(adapter, exc)) from exc
    except (RegistryError, ValueError) as exc:
        raise SystemSchemaError(
            f"system config for adapter {adapter!r}: {exc}"
        ) from exc
    return config


def _validate_dataset_overrides(
    adapter: str, config: Mapping[str, Any], model: type[_SystemBase]
) -> None:
    overrides = config.get("dataset_overrides")
    if not isinstance(overrides, Mapping):
        return

    base_config = {
        key: value for key, value in config.items() if key != "dataset_overrides"
    }
    for dataset_id, override in overrides.items():
        if not isinstance(override, Mapping):
            continue
        structural_fields = {
            "adapter",
            "dataset_overrides",
            "dataset_name",
            "clean_groups",
        }
        forbidden_fields = structural_fields.intersection(override)
        if forbidden_fields:
            field = sorted(forbidden_fields)[0]
            raise ValueError(
                f"dataset_overrides.{dataset_id}.{field}: "
                "dataset overrides cannot set structural or runtime-context fields"
            )
        effective_config = _deep_merge_for_validation(base_config, override)
        try:
            validated = model.model_validate(effective_config)
            _validate_model_plugin_selectors(validated)
        except ValidationError as exc:
            raise ValueError(
                _format_validation_details(
                    exc, prefix=f"dataset_overrides.{dataset_id}."
                )
            ) from exc
        except (RegistryError, ValueError) as exc:
            raise ValueError(f"dataset_overrides.{dataset_id}.{exc}") from exc


def _deep_merge_for_validation(
    base: Mapping[str, Any], override: Mapping[str, Any]
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge_for_validation(current, value)
        else:
            merged[key] = value
    return merged


def _validate_model_plugin_selectors(validated: _SystemBase) -> None:
    if isinstance(validated, (OpenClawSystem, OpenClawDockerSystem)):
        _validate_plugin_selectors(validated.openclaw)


def _validate_plugin_selectors(openclaw: OpenClawConfig) -> None:
    registry = load_registry(Path(DEFAULT_REGISTRY_PATH))
    selectors = (
        ("memory_mode", openclaw.memory_mode, "memory"),
        ("context_engine_mode", openclaw.context_engine_mode, "context-engine"),
    )
    for field, plugin_id, expected_kind in selectors:
        if plugin_id is None:
            continue
        try:
            entry = get_plugin(registry, plugin_id)
        except RegistryError as exc:
            raise ValueError(f"openclaw.{field}: {exc}") from exc
        if not entry.supports_kind(expected_kind):
            raise ValueError(
                f"openclaw.{field}: plugin {plugin_id!r} does not support "
                f"kind {expected_kind!r}"
            )


def _format_validation_error(adapter: str, error: ValidationError) -> str:
    return (
        f"system config for adapter {adapter!r} is invalid: "
        f"{_format_validation_details(error)}"
    )


def _format_validation_details(error: ValidationError, *, prefix: str = "") -> str:
    details: list[str] = []
    for item in error.errors(include_url=False, include_context=False):
        location = ".".join(str(segment) for segment in item["loc"]) or "<root>"
        details.append(f"{prefix}{location}: {item['msg']}")
    return "; ".join(details)

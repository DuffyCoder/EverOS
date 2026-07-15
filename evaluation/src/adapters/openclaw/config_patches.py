"""In-container OpenClaw config patches applied by the docker adapter.

The eval image renders ``openclaw.docker.json`` from a baked template; the
harness writes a richer ``openclaw.json`` on the mounted workspace. Bridge
``agent_run`` reads ``openclaw.docker.json``, so runtime jq patches align
container config with yaml intent without rebuilding the image.
"""

# Enables pi-ai ``stream_options.include_usage`` for every provider model.
# Pair with ``agent_llm.model.compat.supportsUsageInStreaming`` in system yaml.
STREAMING_USAGE_COMPAT_JQ = (
    ".models.providers |= with_entries("
    ".value.models |= map(.compat = ((.compat // {}) + "
    "{supportsUsageInStreaming: true})))"
)

OPENCLAW_CONFIG_PATHS = (
    "/workspace/openclaw.docker.json",
    "/workspace/openclaw.json",
)


def _positive_int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _non_negative_int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def build_agent_llm_runtime_jq(
    *,
    model_max_tokens: object = None,
    idle_timeout_seconds: object = None,
) -> str:
    """Build jq for runtime LLM settings the baked docker template omits."""
    filters: list[str] = []
    max_tokens = _positive_int_or_none(model_max_tokens)
    if max_tokens is not None:
        filters.append(
            ".models.providers |= with_entries("
            f".value.models |= map(.maxTokens = {max_tokens}))"
        )
    idle_timeout = _non_negative_int_or_none(idle_timeout_seconds)
    if idle_timeout is not None:
        filters.append(f".agents.defaults.llm.idleTimeoutSeconds = {idle_timeout}")
    if not filters:
        return "."
    return " | ".join(filters)


def shell_patch_openclaw_configs(jq_filter: str) -> str:
    """Return a shell snippet that jq-patches each config file if present."""
    paths = " ".join(f'"{p}"' for p in OPENCLAW_CONFIG_PATHS)
    return (
        f"for f in {paths}; do "
        '  if [ -f "$f" ]; then '
        f'    jq \'{jq_filter}\' "$f" > "$f.tmp" && mv "$f.tmp" "$f"; '
        "  fi; "
        "done"
    )

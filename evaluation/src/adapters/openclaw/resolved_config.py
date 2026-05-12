"""
OpenClaw-schema config builder for the benchmark adapter.

Produces the dict shape that OpenClaw's CLI reads via OPENCLAW_CONFIG_PATH.

We emit only the fields we actually override; everything else is left out so
OpenClaw's own defaults from src/agents/memory-search.ts (tokenizer,
chunking, cache, sync debounce, hybrid weights, mmr/temporal-decay toggles,
etc.) apply. Native defaults we explicitly mirror here have citations.

Compaction policy:
    Native ``runMemoryFlushIfNeeded`` is force-OFF in every mode emitted
    by this builder. The agent-backbone evals run per-session bundle
    ingest with explicit transcript archive between sessions; native
    flush would either fire mid-replay (writing partial memory) or never
    fire at all (token budget never crosses). Either way the eval semantic
    is owned by the adapter, not openclaw's runtime.

    LLM-backbone evals (flush_mode == shared_llm | disabled) bypass
    in-turn flush by design — the framework writes memory/*.md before
    the agent runs.

    Yaml ``openclaw.compaction_overrides`` is removed; passing it raises.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)


# Citations from /Data3/shutong.shan/openclaw/repo for reviewers:
#   memorySearch.maxResults default ............ memory-search.ts:103 -> 6
#   memorySearch.minScore default .............. memory-search.ts:104 -> 0.35
#   memorySearch.sync.onSearch default ......... memory-search.ts:234 -> true
#   memorySearch.sync.onSessionStart default ... memory-search.ts:233 -> true
#   memorySearch.sync.watch default ............ memory-search.ts:235 -> true
#   memorySearch.store.vector.enabled default .. memory-search.ts:215 -> true
#   memorySearch.enabled default ............... schema.base.generated.ts:24241 -> true
#   hybrid.vectorWeight / textWeight ........... memory-search.ts:106/107
#   hybrid.candidateMultiplier ................. memory-search.ts:108 -> 4
#   chunking.tokens / overlap .................. memory-search.ts:98/99
#   cache.enabled .............................. memory-search.ts:113 -> true
#   memory.backend ............................. backend-config.ts:79 -> "builtin"
#   plugins.allow / plugins.slots / plugins.entries ... types.plugins.ts


def build_openclaw_resolved_config(
    *,
    workspace_dir: str,
    native_store_dir: str,
    backend_mode: str,
    memory_mode: str = "memory-core",
    context_engine_mode: Optional[str] = None,
    agent_llm: Optional[dict] = None,
    embedding: Optional[dict] = None,
) -> dict:
    """Return the dict that OpenClaw CLI expects at OPENCLAW_CONFIG_PATH.

    Args:
        workspace_dir: per-conversation isolated workspace root
        native_store_dir: per-conversation isolated state dir (memory db lives here)
        backend_mode: ``fts_only`` | ``vector`` | ``hybrid``
        memory_mode: ``memory-core`` (baseline), ``noop`` (memorySearch disabled),
            or any other plugin id
        agent_llm: optional agent LLM provider config. If provided, emits
            ``models.providers.<id>`` and ``agents.defaults.model``. Required
            for ``answer_mode=agent_local``.
        embedding: optional embedding provider config (sophnet etc.)

    Secret handling:
        - ``agent_llm.api_key_env`` and ``embedding.api_key_env`` are bare env
          variable names (e.g. ``"LLM_API_KEY"``). The function constructs
          ``${VAR}`` template strings so OpenClaw resolves them at startup
          via SecretInput. Real secret values never pass through this
          function or land on disk.
        - Non-secret fields (base_url / easyllm_id / model id / etc.) carry
          plain expanded strings; they are not credentials.

    backend_mode behavior:
        ``fts_only``: provider=auto + vector disabled (no embedding)
        ``vector``:   provider=sophnet + vector enabled
        ``hybrid``:   provider=sophnet + vector enabled (BM25 + embeddings,
                      OpenClaw's production retrieval path)

    memory_mode behavior:
        ``noop``:        memorySearch.enabled=false (agent has no memory tools)
        ``memory-core``: bundled plugin enabled, memorySearch active
        other plugin id: that plugin enabled, memory-core disabled
    """
    sqlite_path = str(Path(native_store_dir) / "memory" / "default.sqlite")

    # === memorySearch (incl. sophnet embedding) ==========================
    # When memory_mode == "noop", omit the embedding provider/model/remote
    # block entirely. OpenClaw's env substitution evaluates ALL ${VAR}
    # placeholders at startup before runtime can decide to ignore them
    # based on enabled=false, so leaving ${SOPH_API_KEY} in a "disabled"
    # block makes noop runs fail in any environment without sophnet creds.
    is_noop = memory_mode == "noop"

    memory_search: dict[str, Any] = {
        "enabled": not is_noop,
        "store": {
            "path": sqlite_path,
            "vector": {"enabled": backend_mode != "fts_only" and not is_noop},
        },
        "sources": ["memory"],
    }

    if is_noop:
        # No embedding required; agent has no memory tools anyway.
        memory_search["provider"] = "auto"
    elif backend_mode == "fts_only":
        memory_search["provider"] = "auto"
    else:
        memory_search["provider"] = (embedding or {}).get("provider", "sophnet")
        memory_search["model"] = (embedding or {}).get("model", "text-embeddings")
        memory_search["outputDimensionality"] = int(
            (embedding or {}).get("output_dimensionality", 1024)
        )
        memory_search["remote"] = _build_embedding_remote(embedding or {})

    # compaction.memoryFlush is force-OFF. The new session-bundle ingest
    # owns memory writes via the agent's in-turn file_write tool calls
    # (driven by the tail directive); native flush would either fire
    # too early (mid-replay) or never (token budget never crosses).
    compaction_block: dict[str, Any] = {"memoryFlush": {"enabled": False}}

    resolved: dict[str, Any] = {
        "memory": {"backend": "builtin"},
        "agents": {
            "defaults": {
                "workspace": workspace_dir,
                "userTimezone": "UTC",
                "memorySearch": memory_search,
                "compaction": compaction_block,
            }
        },
    }

    # === agent LLM provider ==============================================
    if agent_llm:
        provider_id, provider_cfg, model_ref = _build_agent_provider(agent_llm)
        models_cfg = resolved.setdefault("models", {})
        models_cfg.setdefault("mode", "replace")
        providers = models_cfg.setdefault("providers", {})
        providers[provider_id] = provider_cfg
        resolved["agents"]["defaults"]["model"] = model_ref

    # === plugins (allow + slots + entries) ===============================
    resolved["plugins"] = _build_plugins_section(memory_mode, context_engine_mode)

    return resolved


def _build_embedding_remote(embedding: dict) -> dict[str, Any]:
    """Build the memorySearch.remote block.

    apiKey: prefer ``api_key_env`` marker (rebuilds ``${VAR}`` template so
    OpenClaw resolves at runtime, secret never lands on disk). Falls back
    to ``api_key`` plain field for backward compat with a warning.

    base_url / easyllm_id: plain expanded values. These are not secrets;
    the yaml ``${VAR:default}`` template is expanded by evermemos's
    ``_replace_env_vars`` and we just pass the resulting string.
    """
    remote: dict[str, Any] = {}

    if "api_key_env" in embedding and embedding["api_key_env"]:
        remote["apiKey"] = "${" + str(embedding["api_key_env"]) + "}"
    elif embedding.get("api_key"):
        # Backward-compat: plain key from old yaml. Logs a warning because
        # this path leaks secret to disk.
        logger.warning(
            "embedding.api_key is set as a plain value; prefer "
            "embedding.api_key_env to keep secrets out of resolved_config"
        )
        remote["apiKey"] = embedding["api_key"]
    else:
        remote["apiKey"] = ""

    remote["baseUrl"] = embedding.get("base_url", "") or ""
    remote["easyllmId"] = embedding.get("easyllm_id", "") or ""
    return remote


def _build_agent_provider(agent_llm: dict) -> tuple[str, dict[str, Any], str]:
    """Build (provider_id, provider_config, model_ref) from agent_llm yaml.

    ``api_key_env`` (bare env var name) -> ``apiKey: "${VAR}"`` template.
    Other fields (base_url, model id/name/...) are plain values.

    Required keys: provider_id, base_url, api_key_env, model.{id, name,
    context_window, max_tokens}.
    """
    pid = agent_llm["provider_id"]
    md = agent_llm["model"]

    if not agent_llm.get("api_key_env"):
        raise ValueError(
            "agent_llm.api_key_env is required (bare env var name, e.g. 'LLM_API_KEY')"
        )

    provider_cfg: dict[str, Any] = {
        "baseUrl": agent_llm["base_url"],
        "apiKey": "${" + str(agent_llm["api_key_env"]) + "}",
        "api": agent_llm.get("api", "openai-completions"),
        "models": [{
            "id": md["id"],
            "name": md["name"],
            "reasoning": md.get("reasoning", False),
            "input": md.get("input", ["text"]),
            "cost": md.get("cost", {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
            }),
            "contextWindow": md["context_window"],
            "maxTokens": md["max_tokens"],
        }],
    }
    model_ref = f"{pid}/{md['id']}"
    return pid, provider_cfg, model_ref


def _build_plugins_section(
    memory_mode: str,
    context_engine_mode: Optional[str] = None,
) -> dict[str, Any]:
    """Build plugins.allow / slots / entries from memory + context-engine modes.

    Memory side:
    - memory-core or noop: only memory-core in allow + slot, enabled
      (noop disables memorySearch via its enabled flag, not via plugin
      removal, so memory-core stays loaded but has nothing to do)
    - other plugin id: memory-core in allow (still required as plugin
      slot fallback for some openclaw paths) but its entry disabled;
      target plugin allowed + slot owner + entry enabled

    Context-engine side:
    - When ``context_engine_mode`` is a non-empty string, splice the engine
      id into ``allow`` + register an enabled ``entries[<id>]`` + emit
      ``slots.contextEngine``. Empty/None leaves the slot absent so
      openclaw resolves to its default ``"legacy"`` engine.
    - The two modes compose freely: a plugin can be in the memory slot
      while a different plugin sits in the context-engine slot.
    """
    if memory_mode in ("memory-core", "noop"):
        section: dict[str, Any] = {
            "allow": ["memory-core"],
            "slots": {"memory": "memory-core"},
            "entries": {"memory-core": {"enabled": True}},
        }
    else:
        plugin_id = memory_mode
        section = {
            "allow": ["memory-core", plugin_id],
            "slots": {"memory": plugin_id},
            "entries": {
                "memory-core": {"enabled": False},
                plugin_id: {"enabled": True},
            },
        }

    # Compose context-engine plugin into the same section. Empty string is
    # treated as unset (defensive — yaml ${VAR:default} expansion can yield "").
    if context_engine_mode and context_engine_mode.strip():
        ce_id = context_engine_mode.strip()
        if ce_id not in section["allow"]:
            section["allow"].append(ce_id)
        section["entries"][ce_id] = {"enabled": True}
        section["slots"]["contextEngine"] = ce_id

    return section

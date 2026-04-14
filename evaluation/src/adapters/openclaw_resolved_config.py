"""
OpenClaw-schema config builder for the benchmark adapter.

Produces the dict shape that OpenClaw's CLI reads via OPENCLAW_CONFIG_PATH:

    {
      "memory": {"backend": "builtin"},
      "agents": {"defaults": {
        "workspace": <abs>,
        "userTimezone": "UTC",
        "memorySearch": {... provider + store + sync ...},
        "compaction": {"memoryFlush": {"enabled": ...}}
      }}
    }

Kept in its own module so the schema can evolve without editing the adapter.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


def build_openclaw_resolved_config(
    *,
    workspace_dir: str,
    native_store_dir: str,
    backend_mode: str,
    flush_mode: str,
    embedding: Optional[dict] = None,
) -> dict:
    """Return the dict that OpenClaw CLI expects at OPENCLAW_CONFIG_PATH.

    backend_mode:
        ``fts_only``: provider=auto + vector disabled (matches v0.1)
        ``vector``:   provider=sophnet + vector enabled (embedding only)
        ``hybrid``:   provider=sophnet + vector enabled (BM25 + embeddings,
                      OpenClaw's production retrieval path)

    flush_mode:
        ``disabled``: OpenClaw's own compaction.memoryFlush stays off; the
                      adapter writes raw transcripts (benchmark-A ablation).
        ``native``:   compaction.memoryFlush stays off too (because the
                      adapter already performed LLM-driven flush at ingest
                      time, so OpenClaw should not double-flush during
                      search). The label stays in the resolved config for
                      diagnostics.
    """
    sqlite_path = str(Path(native_store_dir) / "memory" / "default.sqlite")

    memory_search: dict[str, Any] = {
        "store": {
            "path": sqlite_path,
            "fts": {"tokenizer": "unicode61"},
            "vector": {"enabled": backend_mode != "fts_only"},
        },
        "sources": ["memory"],
        "sync": {
            "onSearch": True,
            "onSessionStart": False,
            "watch": False,
        },
    }

    if backend_mode == "fts_only":
        memory_search["provider"] = "auto"
    else:
        memory_search["provider"] = (embedding or {}).get("provider", "sophnet")
        memory_search["model"] = (embedding or {}).get("model", "text-embeddings")
        memory_search["outputDimensionality"] = int(
            (embedding or {}).get("output_dimensionality", 1024)
        )
        remote = {
            "baseUrl": (embedding or {}).get("base_url", ""),
            "easyllmId": (embedding or {}).get("easyllm_id", ""),
            "apiKey": (embedding or {}).get("api_key", ""),
        }
        memory_search["remote"] = remote

    # Faithful modes: the adapter controls flush timing (pre-ingest LLM
    # distil). OpenClaw's own compaction stays off so search never triggers
    # a second LLM call mid-benchmark.
    memory_flush_enabled = False

    return {
        "memory": {"backend": "builtin"},
        "agents": {
            "defaults": {
                "workspace": workspace_dir,
                "userTimezone": "UTC",
                "memorySearch": memory_search,
                "compaction": {
                    "memoryFlush": {"enabled": memory_flush_enabled},
                },
            }
        },
    }

"""mem0 sidecar — FastAPI server exposing the 8-endpoint contract from
Spike #2 Decision 4. Wraps the mem0ai SDK.

Configuration (read from env):
    MEM0_PORT             default 8765
    MEM0_HOST             default 127.0.0.1
    MEM0_DATA_DIR         default /workspace/state/mem0
    MEM0_INFER            "true" | "false" (default "false")  — when false,
                          memory.add stores raw text without LLM extraction.
                          Default false so the sidecar is usable even if
                          LLM credentials are missing during smoke runs.
    MEM0_EMBEDDER         "huggingface" | "openai"  default huggingface
    MEM0_EMBED_MODEL      huggingface model id, default
                          sentence-transformers/all-MiniLM-L6-v2
    MEM0_LLM_MODEL        OpenAI-compat model id, defaults to env LLM_MODEL
    LLM_API_KEY           OpenAI-compat API key (sophnet token in our setup)
    LLM_BASE_URL          OpenAI-compat base URL (sophnet endpoint)

Endpoint contract:
    POST /index    {documents: [{id, content, metadata}, ...]} -> {ok, ingested}
    POST /search   {query, max_results?, min_score?, session_key?}
                                                              -> {hits: [...], provider, model}
    GET  /stats                                               -> {provider, files, chunks, dirty, model}
    GET  /probe_embedding                                     -> {ok, model?, error?}
    GET  /probe_vector                                        -> {enabled}
    POST /sync     {reason?, force?, session_files?: [str]}   -> {ok, ingested?}
    GET  /healthz                                             -> {ok, ready}
    POST /close                                               -> {ok}
"""
from __future__ import annotations

import logging
import os
import pathlib
import threading
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

logger = logging.getLogger("mem0_sidecar")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# --- mem0 lazy init -----------------------------------------------------

_memory_lock = threading.Lock()
_memory = None
_init_error: Optional[str] = None


def _build_mem0_config() -> dict[str, Any]:
    data_dir = os.getenv("MEM0_DATA_DIR", "/workspace/state/mem0")
    pathlib.Path(data_dir).mkdir(parents=True, exist_ok=True)

    embedder_provider = os.getenv("MEM0_EMBEDDER", "huggingface").strip().lower()
    embed_model = os.getenv(
        "MEM0_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
    )
    if embedder_provider == "openai":
        embedder = {
            "provider": "openai",
            "config": {
                "model": embed_model,
                "api_key": os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", ""),
                "openai_base_url": os.getenv("LLM_BASE_URL") or None,
            },
        }
    else:
        embedder = {
            "provider": "huggingface",
            "config": {"model": embed_model},
        }

    llm = {
        "provider": "openai",
        "config": {
            "model": os.getenv("MEM0_LLM_MODEL") or os.getenv("LLM_MODEL", "gpt-4.1-mini"),
            "api_key": os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", ""),
            "openai_base_url": os.getenv("LLM_BASE_URL") or None,
        },
    }

    config = {
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": os.getenv("MEM0_COLLECTION", "mem0"),
                "path": str(pathlib.Path(data_dir) / "chroma"),
            },
        },
        "embedder": embedder,
        "llm": llm,
    }
    return config


def _get_memory():
    global _memory, _init_error
    if _memory is not None:
        return _memory
    with _memory_lock:
        if _memory is not None:
            return _memory
        try:
            from mem0 import Memory  # noqa: PLC0415  (deferred import)

            cfg = _build_mem0_config()
            logger.info("mem0 config (sanitized): provider=chroma embedder=%s",
                        cfg["embedder"]["provider"])
            _memory = Memory.from_config(cfg)
            logger.info("mem0 initialized")
        except Exception as exc:  # pragma: no cover (init path)
            _init_error = f"{type(exc).__name__}: {exc}"
            logger.exception("mem0 init failed")
            raise
    return _memory


def _infer_enabled() -> bool:
    raw = os.getenv("MEM0_INFER", "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


# --- FastAPI app --------------------------------------------------------

app = FastAPI(title="mem0 sidecar", version="0.1.0")


class Document(BaseModel):
    id: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class IndexRequest(BaseModel):
    documents: list[Document]


class SearchRequest(BaseModel):
    query: str
    max_results: Optional[int] = None
    min_score: Optional[float] = None
    session_key: Optional[str] = None


class SyncRequest(BaseModel):
    reason: Optional[str] = None
    force: Optional[bool] = False
    session_files: Optional[list[str]] = None


# --------------------- endpoints ---------------------

@app.get("/healthz")
def healthz():
    # /healthz must NOT trigger mem0 init (which is heavy and can fail
    # if LLM creds missing). Just confirm the FastAPI process is alive.
    return {"ok": True, "ready": _memory is not None}


@app.post("/index")
def index(req: IndexRequest):
    memory = _get_memory()
    ingested = 0
    infer = _infer_enabled()
    user_id = os.getenv("MEM0_DEFAULT_USER_ID", "openclaw")
    for doc in req.documents:
        try:
            memory.add(
                doc.content,
                user_id=user_id,
                metadata={"id": doc.id, **(doc.metadata or {})},
                infer=infer,
            )
            ingested += 1
        except Exception as exc:  # pragma: no cover
            logger.exception("mem0.add failed for doc id=%s", doc.id)
            return {"ok": False, "ingested": ingested, "error": str(exc)}
    return {"ok": True, "ingested": ingested}


@app.post("/search")
def search(req: SearchRequest):
    memory = _get_memory()
    user_id = req.session_key or os.getenv("MEM0_DEFAULT_USER_ID", "openclaw")
    limit = req.max_results or 10
    try:
        result = memory.search(query=req.query, user_id=user_id, limit=limit)
    except Exception as exc:
        logger.exception("mem0.search failed")
        return {"hits": [], "provider": "mem0", "error": str(exc)}

    raw_results = result.get("results", []) if isinstance(result, dict) else result
    hits = []
    min_score = req.min_score if req.min_score is not None else 0.0
    for entry in raw_results:
        score = float(entry.get("score") or 0.0)
        if score < min_score:
            continue
        text = entry.get("memory") or entry.get("text") or entry.get("content") or ""
        meta = entry.get("metadata") or {}
        hits.append({
            "score": score,
            "snippet": text,
            "path": meta.get("id") or entry.get("id") or "mem0/hit.md",
            "source": "memory",
            "metadata": meta,
        })
    return {"hits": hits, "provider": "mem0", "model": os.getenv("MEM0_EMBED_MODEL")}


@app.get("/stats")
def stats():
    memory = _memory
    if memory is None:
        return {
            "provider": "mem0",
            "files": 0,
            "chunks": 0,
            "dirty": True,
            "model": os.getenv("MEM0_EMBED_MODEL"),
        }
    # mem0 doesn't expose a chunk count directly. Approximate via collection size.
    chunks = 0
    try:
        # Internal poke: chroma collection exposed on mem0 vector_store.
        vs = getattr(memory, "vector_store", None)
        if vs is not None and hasattr(vs, "collection") and vs.collection is not None:
            chunks = int(vs.collection.count())
    except Exception:  # pragma: no cover (defensive)
        pass
    return {
        "provider": "mem0",
        "files": chunks,
        "chunks": chunks,
        "dirty": False,
        "model": os.getenv("MEM0_EMBED_MODEL"),
    }


@app.get("/probe_embedding")
def probe_embedding():
    try:
        _get_memory()
    except Exception as exc:
        return {"ok": False, "error": f"mem0 init failed: {exc}"}
    model = os.getenv("MEM0_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    return {"ok": True, "model": model}


@app.get("/probe_vector")
def probe_vector():
    return {"enabled": True}


@app.post("/sync")
def sync(req: SyncRequest):
    memory = _get_memory()
    files = req.session_files or []
    ingested = 0
    infer = _infer_enabled()
    user_id = os.getenv("MEM0_DEFAULT_USER_ID", "openclaw")
    workspace_dir = os.getenv("WORKSPACE_DIR", "/workspace")
    base = pathlib.Path(workspace_dir)
    for rel in files:
        try:
            abs_path = (base / rel).resolve()
            if not abs_path.exists():
                continue
            text = abs_path.read_text(encoding="utf-8", errors="replace")
            memory.add(
                text,
                user_id=user_id,
                metadata={"id": rel, "source_file": str(abs_path)},
                infer=infer,
            )
            ingested += 1
        except Exception:
            logger.exception("mem0.add failed for file=%s", rel)
    return {"ok": True, "ingested": ingested}


@app.post("/close")
def close():
    global _memory
    _memory = None
    return {"ok": True}

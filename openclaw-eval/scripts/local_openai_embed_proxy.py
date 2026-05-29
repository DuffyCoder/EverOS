#!/usr/bin/env python3
"""Local sentence-transformers → OpenAI-format embedding proxy.

Replaces sophnet_openai_proxy.py for the OpenViking eval workload —
the Sophnet account on this machine lacks embedding permission
(persistent HTTP 403 in EverMemOS logs), so OV's embedding provider
needs a self-contained alternative.

Listens on $PROXY_PORT (default 1934) and exposes:
    POST /v1/embeddings   OpenAI-compatible endpoint
    GET  /v1/models       lists the loaded model
    GET  /healthz

The model loads once at startup; requests are served synchronously
on the asyncio loop's default executor.

Default model is sentence-transformers/all-MiniLM-L6-v2 (384 dims,
~80MB, CPU-friendly). Override via SENTENCE_TRANSFORMERS_MODEL.

Usage:
    uv run python openclaw-eval/scripts/local_openai_embed_proxy.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from aiohttp import web

logger = logging.getLogger("local_openai_embed_proxy")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


MODEL_NAME = os.environ.get(
    "SENTENCE_TRANSFORMERS_MODEL",
    "sentence-transformers/all-MiniLM-L6-v2",
)
DEFAULT_PROXY_PORT = int(os.environ.get("PROXY_PORT", "1934"))
DEFAULT_PROXY_HOST = os.environ.get("PROXY_HOST", "0.0.0.0")


# Module-level lazy-init slot; populated on app startup.
_model: Any = None
_model_dim: int = 0


def _normalize_inputs(payload: dict[str, Any]) -> list[str]:
    inp = payload.get("input")
    if isinstance(inp, str):
        return [inp]
    if isinstance(inp, list):
        return [str(x) for x in inp]
    if inp is None:
        raise web.HTTPBadRequest(reason="missing 'input' field")
    return [str(inp)]


async def _embed_async(texts: list[str]) -> list[list[float]]:
    loop = asyncio.get_running_loop()
    def _do() -> list[list[float]]:
        # fastembed.TextEmbedding.embed is a generator; materialize.
        return [list(map(float, v)) for v in _model.embed(texts)]
    return await loop.run_in_executor(None, _do)


async def handle_embeddings(request: web.Request) -> web.Response:
    if _model is None:
        return web.json_response(
            {"error": {"message": "model not yet loaded",
                       "type": "service_unavailable"}},
            status=503,
        )
    try:
        payload = await request.json()
    except Exception as err:
        return web.json_response(
            {"error": {"message": f"invalid JSON: {err}",
                       "type": "bad_request"}},
            status=400,
        )

    try:
        inputs = _normalize_inputs(payload)
    except web.HTTPBadRequest as err:
        return web.json_response(
            {"error": {"message": err.reason or "bad input",
                       "type": "bad_request"}},
            status=400,
        )

    if not inputs:
        return web.json_response(
            {"error": {"message": "input array is empty",
                       "type": "bad_request"}},
            status=400,
        )

    requested_model = payload.get("model") or MODEL_NAME

    try:
        vectors = await _embed_async(inputs)
    except Exception as err:
        logger.exception("embedding failed")
        return web.json_response(
            {"error": {"message": f"embedding failed: {err}",
                       "type": "internal_error"}},
            status=500,
        )

    out_data = [
        {"object": "embedding", "embedding": vec, "index": i}
        for i, vec in enumerate(vectors)
    ]
    out = {
        "object": "list",
        "data": out_data,
        "model": requested_model,
        "usage": {
            "prompt_tokens": sum(len(t) for t in inputs),
            "total_tokens": sum(len(t) for t in inputs),
        },
    }
    logger.info(
        "embedded %d input(s) (model=%s, dim=%d)",
        len(inputs), MODEL_NAME, _model_dim,
    )
    return web.json_response(out)


async def handle_models(_request: web.Request) -> web.Response:
    return web.json_response(
        {"object": "list", "data": [
            {"id": MODEL_NAME, "object": "model", "owned_by": "local"},
        ]},
    )


async def handle_health(_request: web.Request) -> web.Response:
    if _model is None:
        return web.json_response(
            {"status": "starting", "model": MODEL_NAME},
            status=503,
        )
    return web.json_response(
        {"status": "ok", "model": MODEL_NAME, "dim": _model_dim},
    )


async def _on_startup(_app: web.Application) -> None:
    global _model, _model_dim
    # fastembed (ONNX) instead of sentence-transformers — avoids the
    # multi-GB torch+CUDA dependency tree on this CPU-only host.
    from fastembed import TextEmbedding
    logger.info("loading fastembed model: %s", MODEL_NAME)
    _model = TextEmbedding(MODEL_NAME)
    probe = list(_model.embed(["dim probe"]))
    _model_dim = int(len(probe[0]))
    logger.info("model ready (dim=%d)", _model_dim)


def make_app() -> web.Application:
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.router.add_post("/v1/embeddings", handle_embeddings)
    app.router.add_post("/embeddings", handle_embeddings)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/", handle_health)
    app.on_startup.append(_on_startup)
    return app


def main() -> None:
    logger.info(
        "starting local sentence-transformers embedding proxy on %s:%d",
        DEFAULT_PROXY_HOST, DEFAULT_PROXY_PORT,
    )
    web.run_app(
        make_app(),
        host=DEFAULT_PROXY_HOST,
        port=DEFAULT_PROXY_PORT,
        print=lambda _: None,
    )


if __name__ == "__main__":
    main()

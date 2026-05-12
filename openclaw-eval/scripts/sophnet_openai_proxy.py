#!/usr/bin/env python3
"""Sophnet → OpenAI embedding-format proxy.

Sophnet's /easyllms/embeddings endpoint expects:
    POST <SOPH_EMBED_URL>
    {"easyllm_id": "...", "model": "...", "input_texts": [...], "dimensions": 1024}
    Response: {"data": [{"embedding": [...], "index": 0}, ...], "usage": {...}}

OpenViking expects an OpenAI-shaped /v1/embeddings:
    POST <api_base>/v1/embeddings
    {"model": "...", "input": "..." | [...]}
    Response: {"object": "list", "data": [{"object": "embedding", "embedding": [...], "index": 0}], "model": "...", "usage": {...}}

This proxy listens locally and translates between the two so OV's
"OpenAI" embedding provider can talk to a Sophnet account.

Usage:
    SOPH_API_KEY=... \\
    SOPH_EMBED_URL=https://www.sophnet.com/api/open-apis/projects/<pid>/easyllms/embeddings \\
    SOPH_EMBED_EASYLLM_ID=<easyllm_id> \\
    PROXY_PORT=1934 \\
    uv run python openclaw-eval/scripts/sophnet_openai_proxy.py
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

import aiohttp
from aiohttp import web

logger = logging.getLogger("sophnet_openai_proxy")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


SOPH_API_KEY = os.environ.get("SOPH_API_KEY", "")
SOPH_EMBED_URL = os.environ.get("SOPH_EMBED_URL", "")
SOPH_EMBED_EASYLLM_ID = os.environ.get("SOPH_EMBED_EASYLLM_ID", "")
DEFAULT_DIMENSIONS = int(os.environ.get("SOPH_EMBED_DIMENSIONS", "1024"))


def _normalize_inputs(payload: dict[str, Any]) -> list[str]:
    """OpenAI accepts string OR list[str]; Sophnet wants list[str]."""
    inp = payload.get("input")
    if isinstance(inp, str):
        return [inp]
    if isinstance(inp, list):
        return [str(x) for x in inp]
    if inp is None:
        raise web.HTTPBadRequest(reason="missing 'input' field")
    return [str(inp)]


async def handle_embeddings(request: web.Request) -> web.Response:
    if not SOPH_API_KEY or not SOPH_EMBED_URL or not SOPH_EMBED_EASYLLM_ID:
        return web.json_response(
            {"error": {
                "message": (
                    "Sophnet credentials missing: set SOPH_API_KEY, "
                    "SOPH_EMBED_URL, SOPH_EMBED_EASYLLM_ID."
                ),
                "type": "configuration_error",
            }},
            status=500,
        )

    try:
        payload = await request.json()
    except Exception as err:
        return web.json_response(
            {"error": {"message": f"invalid JSON: {err}", "type": "bad_request"}},
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

    model = payload.get("model") or "text-embeddings"
    dimensions = int(payload.get("dimensions") or DEFAULT_DIMENSIONS)

    soph_payload = {
        "easyllm_id": SOPH_EMBED_EASYLLM_ID,
        "model": model,
        "input_texts": inputs,
        "dimensions": dimensions,
    }
    headers = {
        "Authorization": f"Bearer {SOPH_API_KEY}",
        "Content-Type": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(
                SOPH_EMBED_URL, json=soph_payload, headers=headers,
            ) as resp:
                body_text = await resp.text()
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = None

                if resp.status != 200 or not isinstance(body, dict):
                    logger.warning(
                        "sophnet returned http=%s body=%s",
                        resp.status, body_text[:200],
                    )
                    return web.json_response(
                        {"error": {
                            "message": f"sophnet upstream error: {body_text[:300]}",
                            "type": "upstream_error",
                        }},
                        status=502,
                    )

                if body.get("status") not in (None, 0):
                    logger.warning(
                        "sophnet wrapper error: status=%s message=%s",
                        body.get("status"), body.get("message"),
                    )
                    return web.json_response(
                        {"error": {
                            "message": f"sophnet api error: {body.get('message')}",
                            "type": "upstream_error",
                        }},
                        status=502,
                    )

                data = body.get("data") or []
                if not data:
                    return web.json_response(
                        {"error": {
                            "message": "sophnet returned empty data array",
                            "type": "upstream_error",
                        }},
                        status=502,
                    )
        except Exception as err:
            logger.exception("sophnet request failed")
            return web.json_response(
                {"error": {
                    "message": f"sophnet request failed: {err}",
                    "type": "upstream_error",
                }},
                status=502,
            )

    out_data = [
        {
            "object": "embedding",
            "embedding": item.get("embedding") or [],
            "index": item.get("index", idx),
        }
        for idx, item in enumerate(data)
    ]
    usage = body.get("usage") or {}
    out = {
        "object": "list",
        "data": out_data,
        "model": model,
        "usage": {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        },
    }
    logger.info(
        "embedded %d input(s) (model=%s, dim=%d)",
        len(inputs), model, dimensions,
    )
    return web.json_response(out)


async def handle_health(_request: web.Request) -> web.Response:
    ready = bool(SOPH_API_KEY and SOPH_EMBED_URL and SOPH_EMBED_EASYLLM_ID)
    return web.json_response(
        {
            "status": "ok" if ready else "missing_config",
            "soph_embed_url_set": bool(SOPH_EMBED_URL),
            "soph_easyllm_id_set": bool(SOPH_EMBED_EASYLLM_ID),
            "soph_api_key_set": bool(SOPH_API_KEY),
            "default_dimensions": DEFAULT_DIMENSIONS,
        },
        status=200 if ready else 503,
    )


async def handle_models(_request: web.Request) -> web.Response:
    """Minimal OpenAI-compatible /v1/models stub. Some clients call it
    during health checks; respond with an empty list so they continue."""
    return web.json_response(
        {"object": "list", "data": [
            {"id": "text-embeddings", "object": "model",
             "owned_by": "sophnet"},
        ]},
    )


def make_app() -> web.Application:
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.router.add_post("/v1/embeddings", handle_embeddings)
    app.router.add_post("/embeddings", handle_embeddings)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/", handle_health)
    return app


def main() -> None:
    port = int(os.environ.get("PROXY_PORT", "1934"))
    host = os.environ.get("PROXY_HOST", "0.0.0.0")
    if not (SOPH_API_KEY and SOPH_EMBED_URL and SOPH_EMBED_EASYLLM_ID):
        logger.error(
            "missing required env: SOPH_API_KEY / SOPH_EMBED_URL / "
            "SOPH_EMBED_EASYLLM_ID. Aborting."
        )
        sys.exit(1)
    logger.info(
        "starting sophnet→openai proxy on %s:%d (default dim=%d)",
        host, port, DEFAULT_DIMENSIONS,
    )
    web.run_app(make_app(), host=host, port=port, print=lambda _: None)


if __name__ == "__main__":
    main()

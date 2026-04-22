"""
Sophnet Vectorize Service Implementation

Calls Sophnet's easyllms/embeddings endpoint. The schema differs from
OpenAI's /v1/embeddings enough that we do NOT go through AsyncOpenAI.

Request body fields (confirmed against the OpenClaw TS reference at
openclaw/src/memory-host-sdk/host/embeddings-sophnet.ts):
    { easyllm_id, model, input_texts: [...], dimensions }

Response:
    { data: [{embedding: [...], index: 0}, ...], usage: {...} }

Errors: non-2xx OR {"status": <non-zero>, "message": ...}.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import aiohttp
import numpy as np

from agentic_layer.vectorize_interface import (
    UsageInfo,
    VectorizeError,
    VectorizeServiceInterface,
)

logger = logging.getLogger(__name__)


@dataclass
class SophnetVectorizeConfig:
    api_key: str = ""
    base_url: str = ""  # full URL, e.g. https://.../projects/{pid}/easyllms/embeddings
    easyllm_id: str = ""
    model: str = "text-embeddings"
    timeout: int = 60
    max_retries: int = 6
    batch_size: int = 10
    max_concurrent_requests: int = 2
    encoding_format: str = "float"  # unused on Sophnet, kept for interface parity
    dimensions: int = 1024


class SophnetVectorizeService(VectorizeServiceInterface):
    def __init__(self, config: Optional[SophnetVectorizeConfig] = None):
        if config is None:
            config = SophnetVectorizeConfig()
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        logger.info(
            f"Initialized SophnetVectorizeService | model={config.model} "
            f"| base_url={config.base_url} | dim={config.dimensions}"
        )

    async def _ensure_session(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _post(self, input_texts: List[str]) -> dict:
        await self._ensure_session()
        payload = {
            "easyllm_id": self.config.easyllm_id,
            "model": self.config.model,
            "input_texts": input_texts,
            "dimensions": self.config.dimensions,
        }
        async with self._semaphore:
            for attempt in range(self.config.max_retries):
                try:
                    async with self.session.post(
                        self.config.base_url, json=payload
                    ) as resp:
                        body_text = await resp.text()
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            body = None

                        # Sophnet wraps errors in status != 0 even on 2xx paths.
                        if resp.status == 200 and isinstance(body, dict) and \
                                body.get("status", 0) in (0, None):
                            if "data" in body:
                                return body
                            # No data means treat as transient error.
                            logger.error(
                                f"Sophnet embedding: 200 without data: "
                                f"{body_text[:400]}"
                            )

                        # Error path.
                        message = (
                            body.get("message") if isinstance(body, dict) else None
                        ) or body_text[:400]
                        is_rate_limited = (
                            resp.status == 429
                            or (isinstance(body, dict) and body.get("status") in (429,))
                            or "rate" in message.lower()
                            or "limit" in message.lower()
                        )
                        logger.error(
                            f"Sophnet embedding API error "
                            f"HTTP {resp.status} (attempt {attempt + 1}/"
                            f"{self.config.max_retries}): {message}"
                        )
                        if attempt < self.config.max_retries - 1:
                            retry_after = resp.headers.get("Retry-After")
                            if retry_after:
                                try:
                                    delay = float(retry_after)
                                except ValueError:
                                    delay = 30.0
                            elif is_rate_limited or resp.status in (429, 503):
                                delay = 30.0 * (attempt + 1)
                            else:
                                delay = 2 ** attempt
                            await asyncio.sleep(delay)
                            continue
                        raise VectorizeError(
                            f"Sophnet embedding API failed: HTTP {resp.status} - {message}"
                        )
                except VectorizeError:
                    raise
                except Exception as e:
                    logger.error(
                        f"Sophnet embedding exception "
                        f"(attempt {attempt + 1}/{self.config.max_retries}): {e}"
                    )
                    if attempt < self.config.max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise VectorizeError(f"Sophnet embedding exception: {e}")
        raise VectorizeError("Sophnet embedding: exhausted retries")

    @staticmethod
    def _parse_embeddings(body: dict) -> List[np.ndarray]:
        data = body.get("data") or []
        if not data:
            raise VectorizeError("Sophnet embedding: empty data array")
        out: List[np.ndarray] = []
        for item in data:
            emb = item.get("embedding") or []
            out.append(np.array(emb, dtype=np.float32))
        return out

    @staticmethod
    def _parse_usage(body: dict) -> Optional[UsageInfo]:
        usage = body.get("usage") or {}
        if not usage:
            return None
        try:
            return UsageInfo(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
            )
        except Exception:
            return None

    async def get_embedding(
        self, text: str, instruction: Optional[str] = None, is_query: bool = False
    ) -> np.ndarray:
        body = await self._post([text])
        return self._parse_embeddings(body)[0]

    async def get_embedding_with_usage(
        self, text: str, instruction: Optional[str] = None, is_query: bool = False
    ) -> Tuple[np.ndarray, Optional[UsageInfo]]:
        body = await self._post([text])
        embeddings = self._parse_embeddings(body)
        return embeddings[0], self._parse_usage(body)

    async def get_embeddings(
        self,
        texts: List[str],
        instruction: Optional[str] = None,
        is_query: bool = False,
    ) -> List[np.ndarray]:
        if not texts:
            return []
        if len(texts) <= self.config.batch_size:
            body = await self._post(texts)
            return self._parse_embeddings(body)
        embeddings: List[np.ndarray] = []
        for i in range(0, len(texts), self.config.batch_size):
            batch = texts[i : i + self.config.batch_size]
            body = await self._post(batch)
            embeddings.extend(self._parse_embeddings(body))
            if i + self.config.batch_size < len(texts):
                await asyncio.sleep(0.1)
        return embeddings

    async def get_embeddings_batch(
        self,
        text_batches: List[List[str]],
        instruction: Optional[str] = None,
        is_query: bool = False,
    ) -> List[List[np.ndarray]]:
        tasks = [
            self.get_embeddings(batch, instruction, is_query) for batch in text_batches
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: List[List[np.ndarray]] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Sophnet embedding batch {i} failed: {result}")
                out.append([])
            else:
                out.append(result)
        return out

    def get_model_name(self) -> str:
        return self.config.model

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

"""
SiliconFlow Vectorize Service Implementation

Uses SiliconFlow's OpenAI-compatible embedding endpoint
(https://api.siliconflow.cn/v1/embeddings). Default model BAAI/bge-m3
produces fixed 1024-D vectors symmetrically — unlike Qwen3-Embedding, BGE
does not expect the "Instruct: ... / Query: ..." prefix, so we override
_make_request to send raw text.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from agentic_layer.vectorize_base import BaseVectorizeService
from agentic_layer.vectorize_interface import VectorizeError

logger = logging.getLogger(__name__)


@dataclass
class SiliconFlowVectorizeConfig:
    api_key: str = ""
    base_url: str = "https://api.siliconflow.cn/v1"
    model: str = "BAAI/bge-m3"
    timeout: int = 30
    max_retries: int = 3
    batch_size: int = 10
    max_concurrent_requests: int = 5
    encoding_format: str = "float"
    dimensions: int = 1024


class SiliconFlowVectorizeService(BaseVectorizeService):
    def __init__(self, config: Optional[SiliconFlowVectorizeConfig] = None):
        if config is None:
            config = SiliconFlowVectorizeConfig()
        super().__init__(config)

    def _get_config_params(self) -> Tuple[str, str, str]:
        return self.config.api_key, self.config.base_url, self.config.model

    def _should_pass_dimensions(self) -> bool:
        return False

    def _should_truncate_client_side(self) -> bool:
        return False

    async def _make_request(
        self,
        texts: List[str],
        instruction: Optional[str] = None,
        is_query: bool = False,
    ):
        await self._ensure_client()
        if not self.config.model:
            raise VectorizeError("Embedding model is not configured.")
        import asyncio

        try:
            from openai import RateLimitError  # type: ignore
        except ImportError:  # pragma: no cover
            RateLimitError = tuple()  # type: ignore

        async with self._semaphore:
            for attempt in range(self.config.max_retries):
                try:
                    return await self.client.embeddings.create(
                        model=self.config.model,
                        input=texts,
                        encoding_format=self.config.encoding_format,
                    )
                except Exception as e:
                    is_rate_limited = isinstance(e, RateLimitError) or (
                        "429" in str(e) or "rate limit" in str(e).lower()
                    )
                    logger.error(
                        f"SiliconFlowVectorizeService API error "
                        f"(attempt {attempt + 1}/{self.config.max_retries}): {e}"
                    )
                    if attempt < self.config.max_retries - 1:
                        delay = 30.0 * (attempt + 1) if is_rate_limited else 2 ** attempt
                        await asyncio.sleep(delay)
                        continue
                    raise VectorizeError(
                        f"SiliconFlowVectorizeService API request failed after "
                        f"{self.config.max_retries} attempts: {e}"
                    )

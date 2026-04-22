"""
SiliconFlow Rerank Service Implementation

Uses SiliconFlow's dedicated /v1/rerank endpoint with cross-encoder models
like BAAI/bge-reranker-v2-m3. Request shape:
  {"model": ..., "query": ..., "documents": [...]}
Response shape:
  {"results": [{"index": int, "relevance_score": float, ...}], ...}
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp

from agentic_layer.rerank_interface import RerankError, RerankServiceInterface
from api_specs.memory_models import MemoryType

logger = logging.getLogger(__name__)


@dataclass
class SiliconFlowRerankConfig:
    api_key: str = ""
    base_url: str = "https://api.siliconflow.cn/v1/rerank"
    model: str = "BAAI/bge-reranker-v2-m3"
    timeout: int = 30
    max_retries: int = 3
    batch_size: int = 32
    max_concurrent_requests: int = 5


class SiliconFlowRerankService(RerankServiceInterface):
    def __init__(self, config: Optional[SiliconFlowRerankConfig] = None):
        if config is None:
            config = SiliconFlowRerankConfig()
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
        self._semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        logger.info(
            f"Initialized SiliconFlowRerankService | model={config.model} "
            f"| url={config.base_url}"
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

    async def _send_batch(
        self, query: str, documents: List[str]
    ) -> List[float]:
        await self._ensure_session()
        payload = {
            "model": self.config.model,
            "query": query,
            "documents": documents,
        }
        async with self._semaphore:
            for attempt in range(self.config.max_retries):
                try:
                    async with self.session.post(
                        self.config.base_url, json=payload
                    ) as resp:
                        if resp.status == 200:
                            body = await resp.json()
                            results = body.get("results", [])
                            # Reorder by original index to align with `documents`.
                            results.sort(key=lambda x: x.get("index", 0))
                            return [
                                float(r.get("relevance_score", 0.0)) for r in results
                            ]
                        error_text = await resp.text()
                        logger.error(
                            f"SiliconFlow rerank API error {resp.status}: {error_text}"
                        )
                        if attempt < self.config.max_retries - 1:
                            # Respect Retry-After on 429 / 503, else long backoff
                            # for rate-limit-class errors to avoid burning the TPM
                            # budget with ineffective fast retries.
                            retry_after = resp.headers.get("Retry-After")
                            if retry_after:
                                try:
                                    delay = float(retry_after)
                                except ValueError:
                                    delay = 30.0
                            elif resp.status in (429, 503):
                                delay = 30.0 * (attempt + 1)
                            else:
                                delay = 2 ** attempt
                            await asyncio.sleep(delay)
                            continue
                        raise RerankError(
                            f"API failed: {resp.status} - {error_text}"
                        )
                except RerankError:
                    raise
                except Exception as e:
                    logger.error(f"SiliconFlow rerank exception: {e}")
                    if attempt < self.config.max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise RerankError(f"Exception: {e}")
        return [0.0] * len(documents)

    async def rerank_documents(
        self, query: str, documents: List[str], instruction: Optional[str] = None
    ) -> Dict[str, Any]:
        if not documents:
            return {"results": []}

        batch_size = self.config.batch_size if self.config.batch_size > 0 else 32
        batches = [
            documents[i : i + batch_size]
            for i in range(0, len(documents), batch_size)
        ]

        batch_results = await asyncio.gather(
            *[self._send_batch(query, b) for b in batches], return_exceptions=True
        )

        all_scores: List[float] = []
        for i, result in enumerate(batch_results):
            if isinstance(result, Exception):
                logger.error(f"Rerank batch {i} failed: {result}")
                all_scores.extend([-100.0] * len(batches[i]))
            else:
                all_scores.extend(result)

        # Pad/truncate to match documents length.
        if len(all_scores) < len(documents):
            all_scores.extend([0.0] * (len(documents) - len(all_scores)))
        all_scores = all_scores[: len(documents)]

        indexed = sorted(enumerate(all_scores), key=lambda x: x[1], reverse=True)
        return {
            "results": [
                {"index": idx, "score": score, "rank": rank}
                for rank, (idx, score) in enumerate(indexed)
            ]
        }

    def _extract_text_from_hit(self, hit: Dict[str, Any]) -> str:
        source = hit.get("_source", hit)
        memory_type = hit.get("memory_type", "")
        match memory_type:
            case MemoryType.EPISODIC_MEMORY.value:
                episode = source.get("episode", "")
                if episode:
                    return f"Episode Memory: {episode}"
            case MemoryType.FORESIGHT.value:
                foresight = source.get("foresight", "") or source.get("content", "")
                evidence = source.get("evidence", "")
                if foresight:
                    return (
                        f"Foresight: {foresight} (Evidence: {evidence})"
                        if evidence
                        else f"Foresight: {foresight}"
                    )
            case MemoryType.EVENT_LOG.value:
                atomic_fact = source.get("atomic_fact", "")
                if atomic_fact:
                    return f"Atomic Fact: {atomic_fact}"
        for key in ("episode", "atomic_fact", "foresight", "content", "summary", "subject"):
            val = source.get(key)
            if val:
                return str(val)
        return str(hit)

    async def rerank_memories(
        self,
        query: str,
        hits: List[Dict[str, Any]],
        top_k: Optional[int] = None,
        instruction: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not hits:
            return []
        texts = [self._extract_text_from_hit(h) for h in hits]
        try:
            rerank_result = await self.rerank_documents(query, texts, instruction)
            reranked: List[Dict[str, Any]] = []
            for item in rerank_result.get("results", []):
                idx = item.get("index", 0)
                score = item.get("score", 0.0)
                if 0 <= idx < len(hits):
                    hit = hits[idx].copy()
                    hit["score"] = score
                    reranked.append(hit)
            if top_k is not None and top_k > 0:
                reranked = reranked[:top_k]
            if reranked:
                top_scores = [f"{h.get('score', 0):.4f}" for h in reranked[:3]]
                logger.info(
                    f"Reranking completed: {len(reranked)} results, top scores: {top_scores}"
                )
            return reranked
        except Exception as e:
            logger.error(f"Error during reranking: {e}")
            sorted_hits = sorted(hits, key=lambda x: x.get("score", 0), reverse=True)
            return sorted_hits[:top_k] if top_k and top_k > 0 else sorted_hits

    def get_model_name(self) -> str:
        return self.config.model

"""
Cognee Adapter — local black-box integration, fairness-baseline LLM (Sophnet).

Auto-bench routine notes:
- Inherits OnlineAPIAdapter template method.
- Cognee is a knowledge-engine that builds a graph + vector index over ingested text.
  V1 API: cognee.add() / cognee.cognify() / cognee.search() — fully async.
- Per-user isolation: each user_id maps to a Cognee `dataset_name`.
- LLM rewrite: Cognee reads `LLM_API_KEY`, `LLM_PROVIDER`, `LLM_MODEL`,
  `LLM_ENDPOINT` from env via dotenv at import. We force these to the Sophnet
  baseline (`LLM_BASE_URL` / `LLM_API_KEY`) before importing the package.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from evaluation.src.adapters.online_base import OnlineAPIAdapter
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult


@register_adapter("cognee")
class CogneeAdapter(OnlineAPIAdapter):
    """
    Cognee adapter (local SDK, fully async).

    Per-user_id isolation via Cognee `dataset_name = user_id`. The cognify step
    is run lazily once per user_id after the first ingest batch.
    """

    def __init__(self, config: dict, output_dir: Optional[Path] = None):
        super().__init__(config, output_dir)

        # Rule B: force Cognee's openai-compatible client to Sophnet BEFORE import.
        # Cognee's `__init__.py` calls dotenv.load_dotenv(override=True) and reads
        # LLM_PROVIDER / LLM_MODEL / LLM_ENDPOINT / LLM_API_KEY at config time.
        llm_cfg = config.get("llm") or {}
        if os.environ.get("LLM_BASE_URL"):
            os.environ["LLM_ENDPOINT"] = os.environ["LLM_BASE_URL"]
            os.environ["OPENAI_BASE_URL"] = os.environ["LLM_BASE_URL"]
        if os.environ.get("LLM_API_KEY"):
            os.environ["OPENAI_API_KEY"] = os.environ["LLM_API_KEY"]
        os.environ["LLM_PROVIDER"] = "openai"
        os.environ["LLM_MODEL"] = str(llm_cfg.get("model") or "openai/gpt-4.1-mini")

        # Rule C: cognee is NOT in evaluation-full. Surface install-failed cleanly.
        try:
            import cognee  # type: ignore
            from cognee.api.v1.search import SearchType  # type: ignore
        except ImportError as e:
            raise ImportError(
                f"cognee not installed: {e}. Not in pyproject.toml [dependency-groups] "
                "evaluation-full; per Rule C the routine cannot add it. Upstream install: "
                "`uv pip install cognee` (also requires Neo4j or NetworkX backend; defaults "
                "to in-process NetworkX which keeps Rule 3 RAM under 4 GB)."
            ) from e

        self._cognee = cognee
        self._SearchType = SearchType
        self._search_type_name = str(config.get("search_type", "GRAPH_COMPLETION")).upper()

        self.max_retries = int(config.get("max_retries", 3))
        self.request_interval = float(config.get("request_interval", 0.0))

        # Track which datasets have been cognified so we don't re-cognify on every search.
        self._cognified: set[str] = set()
        self._cognify_locks: Dict[str, asyncio.Lock] = {}
        print(f"   Cognee SDK loaded; search_type={self._search_type_name}")

    @staticmethod
    def _safe_dataset(name: str) -> str:
        # Cognee dataset names should be filesystem-safe and short.
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:80]

    def _need_dual_perspective(self, speaker_a: str, speaker_b: str) -> bool:
        return False

    # ---- ingest (Stage 1 — add) ----
    async def _add_user_messages(
        self,
        conv: Conversation,
        messages: List[Dict[str, Any]],
        speaker: str,
        **kwargs: Any,
    ) -> Any:
        user_id = self._extract_user_id(conv, speaker=speaker)
        dataset_name = self._safe_dataset(user_id)

        progress = kwargs.get("progress")
        task_id = kwargs.get("task_id")

        for m in messages:
            content = m.get("content") or m.get("text") or ""
            if not content:
                continue
            speaker_name = m.get("speaker_name") or m.get("speaker") or ""
            ts = m.get("create_time") or m.get("timestamp") or ""
            prefix = " ".join(p for p in (ts, speaker_name) if p)
            text = f"[{prefix}] {content}" if prefix else content

            for attempt in range(self.max_retries):
                try:
                    await self._cognee.add(text, dataset_name=dataset_name)
                    break
                except Exception:  # noqa: BLE001
                    if attempt == self.max_retries - 1:
                        raise
                    await asyncio.sleep(min(2**attempt, 8))

            if progress is not None and task_id is not None:
                progress.update(task_id, advance=1)
            if self.request_interval > 0:
                await asyncio.sleep(self.request_interval)

        await self._ensure_cognified(dataset_name)
        return None

    async def _ensure_cognified(self, dataset_name: str) -> None:
        if dataset_name in self._cognified:
            return
        lock = self._cognify_locks.setdefault(dataset_name, asyncio.Lock())
        async with lock:
            if dataset_name in self._cognified:
                return
            await self._cognee.cognify(datasets=[dataset_name])
            self._cognified.add(dataset_name)

    # ---- retrieve (Stage 2 — search) ----
    async def _search_single_user(
        self,
        query: str,
        conversation_id: str,
        user_id: str,
        top_k: int,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        del kwargs
        dataset_name = self._safe_dataset(user_id)
        await self._ensure_cognified(dataset_name)

        search_type = getattr(self._SearchType, self._search_type_name, None) or self._SearchType.GRAPH_COMPLETION
        raw = await self._cognee.search(
            query_type=search_type,
            query_text=query,
            datasets=[dataset_name],
            top_k=int(top_k),
        )

        out: List[Dict[str, Any]] = []
        for item in raw or []:
            if isinstance(item, dict):
                content = (
                    item.get("text")
                    or item.get("content")
                    or item.get("answer")
                    or item.get("summary")
                    or ""
                )
                score = float(item.get("score", item.get("relevance", 0.0)) or 0.0)
            else:
                content = str(item)
                score = 0.0
            out.append(
                {
                    "content": content,
                    "score": score,
                    "user_id": user_id,
                    "metadata": {"raw": str(item)[:500]},
                }
            )
        out.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return out[: int(top_k)]

    def _build_single_search_result(
        self,
        query: str,
        conversation_id: str,
        results: List[Dict[str, Any]],
        user_id: str,
        top_k: int,
        **kwargs: Any,
    ) -> SearchResult:
        del kwargs
        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=results[: int(top_k)],
            retrieval_metadata={
                "system": "cognee",
                "top_k": int(top_k),
                "dual_perspective": False,
                "user_ids": [user_id],
                "search_type": self._search_type_name,
            },
        )

    def _build_dual_search_result(
        self,
        query: str,
        conversation_id: str,
        all_results: List[Dict[str, Any]],
        results_a: List[Dict[str, Any]],
        results_b: List[Dict[str, Any]],
        speaker_a: str,
        speaker_b: str,
        speaker_a_user_id: str,
        speaker_b_user_id: str,
        top_k: int,
        **kwargs: Any,
    ) -> SearchResult:
        del all_results, results_a, results_b, speaker_a, speaker_b, speaker_b_user_id, kwargs
        return self._build_single_search_result(
            query=query,
            conversation_id=conversation_id,
            results=[],
            user_id=speaker_a_user_id,
            top_k=top_k,
        )

    def get_system_info(self) -> Dict[str, Any]:
        return {
            "name": "Cognee",
            "type": "online_api",
            "adapter": "CogneeAdapter",
            "package": "cognee",
        }

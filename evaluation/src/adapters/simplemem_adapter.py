"""
SimpleMem Adapter — local black-box integration, fairness-baseline LLM (Sophnet).

Auto-bench routine notes:
- Inherits OnlineAPIAdapter template method.
- SimpleMem is an efficient-lifelong-memory framework based on semantic lossless
  compression; the text path uses LanceDB plus SQLite locally and exposes a
  small Python factory `simplemem.create()` returning a stateful memory.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from common_utils.datetime_utils import to_iso_format
from evaluation.src.adapters.online_base import OnlineAPIAdapter
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult


@register_adapter("simplemem")
class SimpleMemAdapter(OnlineAPIAdapter):
    """
    SimpleMem adapter (local SDK, LanceDB + SQLite under the hood).

    Config example:
    ```yaml
    adapter: "simplemem"
    mode: "auto"
    num_workers: 5
    llm:
      model: "openai/gpt-4.1-mini"
      api_key: "${LLM_API_KEY}"
      base_url: "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"
    embedding:
      model: "openai/text-embedding-3-small"
    ```
    """

    def __init__(self, config: dict, output_dir: Optional[Path] = None):
        super().__init__(config, output_dir)

        # Rule B: SimpleMem reads OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL /
        # EMBEDDING_MODEL from env at import time. Force them to the fairness
        # baseline before importing the package.
        llm_cfg = config.get("llm") or {}
        embed_cfg = config.get("embedding") or {}

        os.environ["OPENAI_API_KEY"] = os.environ.get("LLM_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        os.environ["OPENAI_BASE_URL"] = os.environ.get("LLM_BASE_URL", "") or os.environ.get("OPENAI_BASE_URL", "")
        os.environ["LLM_MODEL"] = str(llm_cfg.get("model") or os.environ.get("LLM_MODEL") or "openai/gpt-4.1-mini")
        os.environ["EMBEDDING_MODEL"] = str(
            embed_cfg.get("model") or os.environ.get("EMBEDDING_MODEL") or "openai/text-embedding-3-small"
        )

        try:
            import simplemem_router as _simplemem  # type: ignore
        except ImportError as e:
            raise ImportError(
                f"simplemem not installed: {e}. Not in pyproject.toml [evaluation-full]; "
                "add `simplemem` to the dep group in a separate PR before running this adapter. "
                "Upstream install: `pip install simplemem` (also clones LanceDB + SQLite local storage)."
            ) from e

        self._simplemem = _simplemem
        self.mode = str(config.get("mode") or "auto")
        self.max_retries = int(config.get("max_retries", 3))
        self.request_interval = float(config.get("request_interval", 0.0))

        # TODO(auto-bench): SimpleMem's basic API does not expose persist_dir.
        # We construct one mem instance per user_id under the assumption that
        # `simplemem.create()` returns an isolated backend per call. If the
        # underlying storage is shared globally across instances this will need
        # the candidate to expose an isolation parameter — surface that in the
        # PR if smoke shows cross-user leakage.
        self._mem_by_user: Dict[str, Any] = {}
        self._mem_lock = asyncio.Lock()
        print(f"   SimpleMem client constructed (mode={self.mode})")

    async def _get_mem_for_user(self, user_id: str) -> Any:
        if user_id in self._mem_by_user:
            return self._mem_by_user[user_id]
        async with self._mem_lock:
            if user_id in self._mem_by_user:
                return self._mem_by_user[user_id]
            mem = await asyncio.to_thread(self._simplemem.create, mode=self.mode)
            self._mem_by_user[user_id] = mem
            return mem

    # SimpleMem isolates per memory instance (one per user_id in this adapter).
    def _need_dual_perspective(self, speaker_a: str, speaker_b: str) -> bool:
        return bool(speaker_a) and bool(speaker_b) and speaker_a != speaker_b

    async def _add_user_messages(
        self,
        conv: Conversation,
        messages: List[Dict[str, Any]],
        speaker: str,
        **kwargs: Any,
    ) -> Any:
        del messages  # SimpleMem expects (speaker, text, timestamp) per turn — use raw conv.messages.
        user_id = self._extract_user_id(conv, speaker=speaker)
        mem = await self._get_mem_for_user(user_id)

        progress = kwargs.get("progress")
        task_id = kwargs.get("task_id")

        def _ingest_all() -> None:
            for msg in conv.messages:
                ts = to_iso_format(msg.timestamp) if msg.timestamp else ""
                mem.add_dialogue(msg.speaker_name or "user", msg.content or "", ts)
            mem.finalize()

        for attempt in range(self.max_retries):
            try:
                await asyncio.to_thread(_ingest_all)
                break
            except Exception:  # noqa: BLE001 — propagate after retries
                if attempt == self.max_retries - 1:
                    raise
                await asyncio.sleep(min(2 ** attempt, 8))

        if progress is not None and task_id is not None:
            progress.update(task_id, advance=len(conv.messages))
        if self.request_interval > 0:
            await asyncio.sleep(self.request_interval)
        return None

    async def _search_single_user(
        self,
        query: str,
        conversation_id: str,
        user_id: str,
        top_k: int,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        del kwargs
        mem = await self._get_mem_for_user(user_id)
        raw = await asyncio.to_thread(mem.query, query, top_k=int(top_k))

        out: List[Dict[str, Any]] = []
        for item in raw or []:
            if isinstance(item, dict):
                content = item.get("text") or item.get("content") or item.get("memory") or ""
                ts = item.get("timestamp") or item.get("created_at") or ""
                score = item.get("score", 0.0)
            else:
                content = (
                    getattr(item, "text", None)
                    or getattr(item, "content", None)
                    or getattr(item, "memory", None)
                    or str(item)
                )
                ts = getattr(item, "timestamp", None) or getattr(item, "created_at", None) or ""
                score = getattr(item, "score", 0.0)
            text = f"{ts}: {content}".strip(": ").strip() if ts else str(content)
            out.append(
                {
                    "content": text,
                    "score": float(score or 0.0),
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
                "system": "simplemem",
                "top_k": int(top_k),
                "dual_perspective": False,
                "user_ids": [user_id],
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
        del kwargs
        speaker_a_text = (
            "\n".join(r["content"] for r in results_a) if results_a else "(No memories found)"
        )
        speaker_b_text = (
            "\n".join(r["content"] for r in results_b) if results_b else "(No memories found)"
        )
        template = self._prompts["online_api"].get("templates", {}).get("default", "")
        formatted = (
            template.format(
                speaker_1=speaker_a,
                speaker_1_memories=speaker_a_text,
                speaker_2=speaker_b,
                speaker_2_memories=speaker_b_text,
            )
            if template
            else f"{speaker_a}:\n{speaker_a_text}\n\n{speaker_b}:\n{speaker_b_text}"
        )
        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=all_results,
            retrieval_metadata={
                "system": "simplemem",
                "top_k": int(top_k),
                "dual_perspective": True,
                "user_ids": [speaker_a_user_id, speaker_b_user_id],
                "formatted_context": formatted,
            },
        )

    def get_system_info(self) -> Dict[str, Any]:
        return {
            "name": "SimpleMem",
            "type": "online_api",
            "adapter": "SimpleMemAdapter",
        }

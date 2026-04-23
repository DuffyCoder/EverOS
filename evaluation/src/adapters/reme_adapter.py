"""
ReMe Adapter — local black-box integration, fairness-baseline LLM (Sophnet).

Auto-bench routine notes:
- Inherits OnlineAPIAdapter template method.
- ReMe is a memory management kit that treats memory as files plus an optional
  vector backend. It already reads LLM_API_KEY / LLM_BASE_URL natively, so the
  fairness-baseline rewrite is a no-op for the LLM and only defaults the
  EMBEDDING_* vars to mirror the LLM endpoint when not separately set.
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


@register_adapter("reme")
class ReMeAdapter(OnlineAPIAdapter):
    """
    ReMe adapter (local SDK, vector-backed by default).

    Config example:
    ```yaml
    adapter: "reme"
    working_dir: ".reme"
    num_workers: 5
    llm:
      model: "openai/gpt-4.1-mini"
      api_key: "${LLM_API_KEY}"
      base_url: "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"
    embedding:
      model: "openai/text-embedding-3-small"
      dimensions: 1536
    vector_store:
      backend: "local"
    ```
    """

    def __init__(self, config: dict, output_dir: Optional[Path] = None):
        super().__init__(config, output_dir)

        # Rule B: force fairness-baseline endpoints. ReMe reads these env vars natively.
        if not os.environ.get("EMBEDDING_API_KEY"):
            os.environ["EMBEDDING_API_KEY"] = os.environ.get("LLM_API_KEY", "")
        if not os.environ.get("EMBEDDING_BASE_URL"):
            os.environ["EMBEDDING_BASE_URL"] = os.environ.get("LLM_BASE_URL", "")

        try:
            from reme import ReMe  # type: ignore
        except ImportError as e:
            raise ImportError(
                f"reme not installed: {e}. Not in pyproject.toml [evaluation-full]; "
                "add dependency in a separate PR before running this adapter. "
                "Upstream install path: clone agentscope-ai/ReMe and `pip install -e '.[light]'`."
            ) from e

        llm_cfg = config.get("llm") or {}
        embed_cfg = config.get("embedding") or {}
        store_cfg = config.get("vector_store") or {}

        self.working_dir = str(config.get("working_dir") or ".reme")
        self.max_retries = int(config.get("max_retries", 3))
        self.request_interval = float(config.get("request_interval", 0.0))

        self._client = ReMe(
            working_dir=self.working_dir,
            default_llm_config={
                "backend": "openai",
                "model_name": str(llm_cfg.get("model") or "openai/gpt-4.1-mini"),
            },
            default_embedding_model_config={
                "backend": "openai",
                "model_name": str(embed_cfg.get("model") or "openai/text-embedding-3-small"),
                "dimensions": int(embed_cfg.get("dimensions", 1536)),
            },
            default_vector_store_config={
                "backend": str(store_cfg.get("backend") or "local"),
            },
        )
        self._started = False
        self._start_lock = asyncio.Lock()
        print(f"   ReMe client constructed (working_dir={self.working_dir})")

    async def _ensure_started(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            await self._client.start()
            self._started = True

    async def close(self) -> None:
        if self._started:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001 — close errors must not mask eval results
                pass

    # ReMe supports per-user isolation natively via user_name.
    def _need_dual_perspective(self, speaker_a: str, speaker_b: str) -> bool:
        return bool(speaker_a) and bool(speaker_b) and speaker_a != speaker_b

    def _conversation_to_messages(
        self,
        conversation: Conversation,
        format_type: str = "basic",
        perspective: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        del format_type
        messages = super()._conversation_to_messages(conversation, "basic", perspective)
        for msg, src in zip(messages, conversation.messages):
            if src.timestamp:
                msg["time_created"] = to_iso_format(src.timestamp)
        return messages

    async def _add_user_messages(
        self,
        conv: Conversation,
        messages: List[Dict[str, Any]],
        speaker: str,
        **kwargs: Any,
    ) -> Any:
        await self._ensure_started()
        user_id = self._extract_user_id(conv, speaker=speaker)
        progress = kwargs.get("progress")
        task_id = kwargs.get("task_id")

        for attempt in range(self.max_retries):
            try:
                # TODO(auto-bench): summarize_memory is the high-level entry per ReMe README;
                # if the candidate's eval prefers per-message add_memory(), swap here.
                await self._client.summarize_memory(messages=messages, user_name=user_id)
                break
            except Exception:  # noqa: BLE001 — propagate after retries
                if attempt == self.max_retries - 1:
                    raise
                await asyncio.sleep(min(2 ** attempt, 8))

        if progress is not None and task_id is not None:
            progress.update(task_id, advance=len(messages))
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
        await self._ensure_started()

        raw = await self._client.retrieve_memory(query=query, user_name=user_id)

        out: List[Dict[str, Any]] = []
        for item in raw or []:
            content = (
                getattr(item, "memory_content", None)
                or getattr(item, "content", None)
                or (item.get("memory_content") if isinstance(item, dict) else None)
                or (item.get("content") if isinstance(item, dict) else None)
                or ""
            )
            score_raw = (
                getattr(item, "score", None)
                or (item.get("score") if isinstance(item, dict) else 0.0)
                or 0.0
            )
            ts = (
                getattr(item, "time_created", None)
                or (item.get("time_created") if isinstance(item, dict) else "")
                or ""
            )
            text = f"{ts}: {content}".strip(": ").strip() if ts else str(content)
            out.append(
                {
                    "content": text,
                    "score": float(score_raw),
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
                "system": "reme",
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
                "system": "reme",
                "top_k": int(top_k),
                "dual_perspective": True,
                "user_ids": [speaker_a_user_id, speaker_b_user_id],
                "formatted_context": formatted,
            },
        )

    def get_system_info(self) -> Dict[str, Any]:
        return {
            "name": "ReMe",
            "type": "online_api",
            "adapter": "ReMeAdapter",
        }

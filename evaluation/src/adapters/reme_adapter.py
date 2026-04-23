"""
ReMe Adapter — local black-box integration, fairness-baseline LLM (Sophnet).

Auto-bench routine notes:
- Inherits OnlineAPIAdapter template method.
- ReMe (agentscope-ai/ReMe) is a memory management kit for agents that stores
  personal memories in a local vector store and retrieves them via a
  flow-based pipeline (summarize -> extract -> rerank).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console

from evaluation.src.adapters.online_base import OnlineAPIAdapter
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult


@register_adapter("reme")
class ReMeAdapter(OnlineAPIAdapter):
    """
    ReMe adapter (local deployment, black-box integration via reme_ai Python SDK).

    Uses ReMeApp's `summary_personal_memory` flow for ingest and
    `retrieve_personal_memory` flow for search. Vector store defaults to
    "memory" (in-process) so no external services are required.

    Config example:
    ```yaml
    adapter: "reme"
    vector_store_backend: "memory"   # memory | local | qdrant | elasticsearch
    working_dir: ".reme_eval"
    embedding_model: "text-embedding-3-small"
    num_workers: 3
    llm:
      model: "openai/gpt-4.1-mini"
      api_key: "${LLM_API_KEY}"
      base_url: "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"
    ```
    """

    def __init__(self, config: dict, output_dir: Path = None):
        super().__init__(config, output_dir)

        # --- Rule B: force candidate LLM/embedding to fairness-baseline env ---
        llm_cfg = config.get("llm", {}) or {}
        llm_api_key = llm_cfg.get("api_key") or os.environ.get("LLM_API_KEY", "")
        llm_base_url = (
            llm_cfg.get("base_url")
            or os.environ.get("LLM_BASE_URL", "")
            or "https://www.sophnet.com/api/open-apis/v1"
        )
        llm_model = llm_cfg.get("model") or "gpt-4.1-mini"

        # ReMe reads these from env if kwargs omitted — set both for safety.
        os.environ["FLOW_LLM_API_KEY"] = llm_api_key
        os.environ["FLOW_LLM_BASE_URL"] = llm_base_url
        os.environ["FLOW_EMBEDDING_API_KEY"] = llm_api_key
        os.environ["FLOW_EMBEDDING_BASE_URL"] = llm_base_url

        embedding_model = str(
            config.get("embedding_model")
            or llm_cfg.get("embedding_model")
            or "text-embedding-3-small"
        )
        vector_store_backend = str(config.get("vector_store_backend", "memory"))
        working_dir = str(config.get("working_dir", ".reme_eval"))

        self.max_retries = int(config.get("max_retries", 3))
        self.request_interval = float(config.get("request_interval", 0.0))

        # Plain import — Rule C: reme_ai is declared in python_deps of the
        # system YAML and installed ephemerally via `uv run --with`.
        from reme_ai import ReMeApp  # type: ignore

        overrides = [
            f"llm.default.model_name={llm_model}",
            "llm.default.backend=openai_compatible",
            f"embedding_model.default.model_name={embedding_model}",
            "embedding_model.default.backend=openai_compatible",
            f"vector_store.default.backend={vector_store_backend}",
            "vector_store.default.embedding_model=default",
        ]

        self._app = ReMeApp(
            *overrides,
            llm_api_key=llm_api_key,
            llm_api_base=llm_base_url,
            embedding_api_key=llm_api_key,
            embedding_api_base=llm_base_url,
        )
        self._started = False
        self._start_lock = asyncio.Lock()

        self.console = Console()
        print(
            f"   ReMe configured: llm={llm_model}, emb={embedding_model}, "
            f"vector_store={vector_store_backend}, working_dir={working_dir}"
        )

    async def _ensure_started(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            start = getattr(self._app, "async_start", None)
            if start is not None:
                await start()
            self._started = True

    async def close(self) -> None:
        if not self._started:
            return
        stop = getattr(self._app, "async_stop", None)
        if stop is not None:
            try:
                await stop()
            except Exception:
                pass
        self._started = False

    # ReMe stores per-workspace memory; dual perspective is redundant for
    # a single shared memory store, so we key by speaker user_id.
    def _need_dual_perspective(self, speaker_a: str, speaker_b: str) -> bool:
        return super()._need_dual_perspective(speaker_a, speaker_b)

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

        if not messages:
            if progress is not None and task_id is not None:
                progress.update(task_id, advance=0)
            return None

        trajectories = [{"messages": messages, "score": 1.0}]
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                await self._app.async_execute(
                    name="summary_personal_memory",
                    workspace_id=user_id,
                    trajectories=trajectories,
                )
                last_exc = None
                break
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise
        if last_exc is not None:
            raise last_exc

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
        del conversation_id, kwargs
        await self._ensure_started()

        raw: Dict[str, Any] = {}
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                raw = await self._app.async_execute(
                    name="retrieve_personal_memory",
                    workspace_id=user_id,
                    query=query,
                    top_k=int(top_k),
                )
                last_exc = None
                break
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise
        if last_exc is not None:
            raise last_exc

        memory_list = (raw.get("metadata") or {}).get("memory_list") or []

        out: List[Dict[str, Any]] = []
        for item in memory_list:
            mem = self._memory_to_dict(item)
            content = str(mem.get("content") or "")
            timestamp = str(
                mem.get("time_created")
                or mem.get("created_at")
                or (mem.get("metadata") or {}).get("event_datetime")
                or ""
            )
            score = float(mem.get("score") or 0.0)
            display = f"{timestamp}: {content}".strip(": ").strip() if timestamp else content
            out.append(
                {
                    "content": display,
                    "score": score,
                    "user_id": user_id,
                    "metadata": {"raw": mem},
                }
            )

        # Fallback to the flow's pre-formatted answer if memory_list is missing.
        if not out and raw.get("answer"):
            out.append(
                {
                    "content": str(raw["answer"]),
                    "score": 0.0,
                    "user_id": user_id,
                    "metadata": {"source": "answer"},
                }
            )

        out.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return out[: int(top_k)]

    @staticmethod
    def _memory_to_dict(item: Any) -> Dict[str, Any]:
        if isinstance(item, dict):
            return item
        dump = getattr(item, "model_dump", None)
        if callable(dump):
            try:
                return dump()
            except Exception:
                pass
        as_dict = getattr(item, "__dict__", None)
        if isinstance(as_dict, dict):
            return dict(as_dict)
        return {"content": str(item)}

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
        formatted = ""
        if template:
            formatted = template.format(
                speaker_1=speaker_a,
                speaker_1_memories=speaker_a_text,
                speaker_2=speaker_b,
                speaker_2_memories=speaker_b_text,
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

"""
ReMe Adapter — local black-box integration, fairness-baseline LLM (Sophnet).

Auto-bench routine notes:
- Inherits OnlineAPIAdapter template method.
- ReMe (agentscope-ai/ReMe, Apache-2.0) is a memory management framework for
  agents, exposing personal / procedural / tool memory with a local vector
  store (local / Chroma / Qdrant / Elasticsearch / ObVec). This adapter uses
  the local vector store backend and personal memory, mirroring the shape of
  ReMe's own LoCoMo benchmark harness.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console

from common_utils.datetime_utils import to_iso_format
from evaluation.src.adapters.online_base import OnlineAPIAdapter
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult


@register_adapter("reme")
class ReMeAdapter(OnlineAPIAdapter):
    """
    ReMe adapter (local vector store, black-box integration).
    """

    def __init__(self, config: dict, output_dir: Path = None):
        # Sophnet's /v1/chat/completions rejects the "openai/" provider prefix
        # that OpenRouter uses. Strip it on the way in so both ReMe's internal
        # chat client AND the answer-stage LLMProvider see the plain model id.
        llm_base_url_probe = (
            (config.get("llm") or {}).get("base_url")
            or os.environ.get("LLM_BASE_URL", "")
        )
        if (
            isinstance(config, dict)
            and "sophnet" in str(llm_base_url_probe or "").lower()
        ):
            raw_model = (config.get("llm") or {}).get("model", "")
            if isinstance(raw_model, str) and raw_model.startswith("openai/"):
                # Shallow-copy llm block so we don't mutate the caller's dict.
                config = {**config, "llm": {**(config.get("llm") or {}),
                                            "model": raw_model.split("/", 1)[1]}}

        super().__init__(config, output_dir)

        # Rule B: force candidate's LLM/embedding env to the fairness baseline
        # BEFORE importing ReMe (it reads env on construction).
        llm_cfg = config.get("llm", {}) or {}
        llm_api_key = llm_cfg.get("api_key") or os.environ.get("LLM_API_KEY", "")
        llm_base_url = llm_cfg.get("base_url") or os.environ.get(
            "LLM_BASE_URL", "https://www.sophnet.com/api/open-apis/v1"
        )
        os.environ["LLM_API_KEY"] = llm_api_key
        os.environ["LLM_BASE_URL"] = llm_base_url

        embed_cfg = config.get("embedding", {}) or {}
        embed_api_key = embed_cfg.get("api_key") or os.environ.get(
            "EMBEDDING_API_KEY", os.environ.get("VECTORIZE_API_KEY", "")
        )
        embed_base_url = embed_cfg.get("base_url") or os.environ.get(
            "EMBEDDING_BASE_URL", os.environ.get("VECTORIZE_BASE_URL", "")
        )
        if embed_api_key:
            os.environ["EMBEDDING_API_KEY"] = embed_api_key
        if embed_base_url:
            os.environ["EMBEDDING_BASE_URL"] = embed_base_url

        from reme.reme import ReMe  # type: ignore
        from reme.core.embedding.base_embedding_model import (  # type: ignore
            BaseEmbeddingModel,
        )
        from reme.core.registry_factory import R  # type: ignore

        # Sophnet's embedding endpoint is NOT OpenAI-compatible (body uses
        # `input_texts` and an optional `easyllm_id`, response shape matches
        # OpenAI's data array). Register a custom backend so ReMe can talk to
        # it via the fairness-baseline Sophnet Vectorize endpoint.
        class _SophnetEmbeddingModel(BaseEmbeddingModel):
            """Sophnet native embedding backend (non-OpenAI-compatible body)."""

            def __init__(self, easyllm_id: str = "", **kw):
                super().__init__(**kw)
                self._easyllm_id = easyllm_id
                self._http = None

            async def start(self):
                import aiohttp

                await super().start()
                self._http = aiohttp.ClientSession(
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    }
                )

            async def close(self):
                if self._http is not None and not self._http.closed:
                    await self._http.close()
                    self._http = None
                await super().close()

            async def _get_embeddings(self, input_text, **kwargs):
                import aiohttp

                if self._http is None or self._http.closed:
                    self._http = aiohttp.ClientSession(
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        }
                    )
                payload = {
                    "easyllm_id": self._easyllm_id,
                    "model": self.model_name,
                    "input_texts": list(input_text),
                    "dimensions": int(self.dimensions),
                }
                async with self._http.post(self.base_url, json=payload) as resp:
                    body = await resp.json(content_type=None)
                if not isinstance(body, dict) or not body.get("data"):
                    raise RuntimeError(
                        f"Sophnet embedding bad response: HTTP {resp.status} {body}"
                    )
                out = [[] for _ in range(len(input_text))]
                for item in body["data"]:
                    idx = int(item.get("index", 0))
                    out[idx] = list(item.get("embedding") or [])
                return out

        if "sophnet_embeddings" not in R.embedding_models:
            R.embedding_models.register("sophnet_embeddings")(_SophnetEmbeddingModel)

        reme_cfg = config.get("reme", {}) or {}
        self.algo_version: str = str(reme_cfg.get("algo_version", "default"))
        self.enable_thinking_params: bool = bool(
            reme_cfg.get("enable_thinking_params", False)
        )
        self.retrieve_top_k: int = int(
            reme_cfg.get("retrieve_top_k", config.get("search", {}).get("top_k", 20))
        )
        self.summarize_batch_size: int = int(reme_cfg.get("summarize_batch_size", 40))

        working_dir = reme_cfg.get("working_dir") or str(
            (Path(self.output_dir) / ".reme").resolve()
            if str(self.output_dir) != "."
            else Path(tempfile.mkdtemp(prefix="reme_")).resolve()
        )
        Path(working_dir).mkdir(parents=True, exist_ok=True)

        default_llm_config = {
            "backend": reme_cfg.get("llm_backend", "openai"),
            "model_name": llm_cfg.get("model", "gpt-4.1-mini"),
        }
        embedding_backend = reme_cfg.get("embedding_backend", "sophnet_embeddings")
        default_embedding_model_config: Dict[str, Any] = {
            "backend": embedding_backend,
            "model_name": reme_cfg.get(
                "embedding_model", os.environ.get("VECTORIZE_MODEL", "text-embeddings")
            ),
            "dimensions": int(
                reme_cfg.get(
                    "embedding_dimensions",
                    os.environ.get("VECTORIZE_DIMENSIONS", 1024),
                )
            ),
        }
        if embedding_backend == "sophnet_embeddings":
            default_embedding_model_config["easyllm_id"] = os.environ.get(
                "SOPH_EMBED_EASYLLM_ID", ""
            )
        default_vector_store_config = {
            "backend": reme_cfg.get("vector_store_backend", "local"),
        }

        self._reme = ReMe(
            working_dir=working_dir,
            default_llm_config=default_llm_config,
            default_embedding_model_config=default_embedding_model_config,
            default_vector_store_config=default_vector_store_config,
            enable_profile=bool(reme_cfg.get("enable_profile", False)),
            enable_logo=False,
            log_to_file=False,
            log_to_console=bool(reme_cfg.get("log_to_console", False)),
        )
        self._raise_reme_exception: bool = bool(
            reme_cfg.get("raise_exception", False)
        )
        self._started = False
        self.console = Console()
        print(f"   ReMe working_dir: {working_dir}")
        print(f"   ReMe algo_version: {self.algo_version}")

    async def _ensure_started(self) -> None:
        if not self._started:
            await self._reme.start()
            self._started = True

    async def close(self) -> None:
        if self._started:
            try:
                await self._reme.close()
            finally:
                self._started = False

    # ReMe stores group chat stream under one user_name; do not duplicate.
    def _need_dual_perspective(self, speaker_a: str, speaker_b: str) -> bool:
        return False

    @staticmethod
    def _format_time_created(iso_or_dt: Any, fallback_idx: int) -> str:
        """ReMe requires time_created as 'YYYY-MM-DD HH:MM:SS' string."""
        if iso_or_dt is None:
            base = datetime(2023, 1, 1, 0, 0, 0) + timedelta(seconds=fallback_idx * 30)
            return base.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(iso_or_dt, datetime):
            return iso_or_dt.strftime("%Y-%m-%d %H:%M:%S")
        iso = to_iso_format(iso_or_dt)
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except Exception:
            return iso[:19].replace("T", " ")

    def _conversation_to_messages(
        self,
        conversation: Conversation,
        format_type: str = "basic",
        perspective: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        del format_type, perspective
        speaker_a = conversation.metadata.get("speaker_a", "")
        out: List[Dict[str, Any]] = []
        for idx, msg in enumerate(conversation.messages):
            role = "user" if msg.speaker_name == speaker_a else "assistant"
            out.append(
                {
                    "role": role,
                    "name": msg.speaker_name or role,
                    "content": msg.content,
                    "time_created": self._format_time_created(msg.timestamp, idx),
                }
            )
        return out

    async def _add_user_messages(
        self,
        conv: Conversation,
        messages: List[Dict[str, Any]],
        speaker: str,
        **kwargs: Any,
    ) -> Any:
        del speaker
        await self._ensure_started()
        user_id = self._extract_user_id(conv, speaker="speaker_a")

        progress = kwargs.get("progress")
        task_id = kwargs.get("task_id")

        for i in range(0, len(messages), self.summarize_batch_size):
            batch = messages[i : i + self.summarize_batch_size]
            await self._reme.summarize_memory(
                messages=batch,
                user_name=user_id,
                version=self.algo_version,
                return_dict=True,
                enable_time_filter=True,
                enable_thinking_params=self.enable_thinking_params,
                raise_exception=self._raise_reme_exception,
            )
            if progress is not None and task_id is not None:
                progress.update(task_id, advance=len(batch))
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

        result = await self._reme.retrieve_memory(
            query=query,
            user_name=user_id,
            retrieve_top_k=int(top_k),
            version=self.algo_version,
            return_dict=True,
            enable_time_filter=True,
            enable_thinking_params=self.enable_thinking_params,
            raise_exception=False,
        )

        retrieved = result.get("retrieved_nodes") or []
        out: List[Dict[str, Any]] = []
        for node in retrieved:
            node_dict = (
                node.model_dump(exclude_none=True)
                if hasattr(node, "model_dump")
                else (node if isinstance(node, dict) else {})
            )
            content = (
                node_dict.get("content")
                or node_dict.get("memory_content")
                or node_dict.get("memory")
                or ""
            )
            ts = (
                node_dict.get("message_time")
                or node_dict.get("time_created")
                or ""
            )
            display = f"{ts}: {content}".strip().strip(":").strip() if ts else content
            out.append(
                {
                    "content": display,
                    "score": float(node_dict.get("score", 0.0) or 0.0),
                    "user_id": user_id,
                    "metadata": {"raw": node_dict},
                }
            )

        if not out:
            answer_text = result.get("answer")
            if isinstance(answer_text, str) and answer_text:
                out.append(
                    {
                        "content": answer_text,
                        "score": 0.0,
                        "user_id": user_id,
                        "metadata": {"source": "reme.retrieve_memory.answer"},
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
        clipped = results[: int(top_k)]
        context_lines = [r["content"] for r in clipped if r.get("content")]
        formatted_context = "\n".join(context_lines) if context_lines else "(No memories found)"
        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=clipped,
            retrieval_metadata={
                "system": "reme",
                "top_k": int(top_k),
                "dual_perspective": False,
                "user_ids": [user_id],
                "algo_version": self.algo_version,
                "formatted_context": formatted_context,
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
        del all_results, results_b, speaker_a, speaker_b, speaker_b_user_id
        return self._build_single_search_result(
            query=query,
            conversation_id=conversation_id,
            results=results_a,
            user_id=speaker_a_user_id,
            top_k=top_k,
            **kwargs,
        )

    def get_system_info(self) -> Dict[str, Any]:
        return {
            "name": "ReMe",
            "type": "online_api",
            "adapter": "ReMeAdapter",
            "algo_version": self.algo_version,
        }

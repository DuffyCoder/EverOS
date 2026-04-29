"""
SimpleMem Adapter — local black-box integration, fairness-baseline LLM (Sophnet).

Auto-bench routine notes:
- Inherits OnlineAPIAdapter template method.
- SimpleMem stores dialogues in a local LanceDB+SQLite memory and answers via an
  OpenAI-compatible LLM. Default embedding (Qwen3-Embedding-0.6B, <1B params)
  is kept per Rule 2 exception; LLM is force-rewritten to Sophnet.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console

from common_utils.datetime_utils import to_iso_format
from evaluation.src.adapters.online_base import OnlineAPIAdapter
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult


@register_adapter("simplemem")
class SimpleMemAdapter(OnlineAPIAdapter):
    """
    SimpleMem adapter (local SDK, in-process LanceDB+SQLite).

    Config example:
    ```yaml
    adapter: "simplemem"
    num_workers: 5
    llm:
      model: "openai/gpt-4.1-mini"
      api_key: "${LLM_API_KEY}"
      base_url: "${LLM_BASE_URL:https://www.sophnet.com/api/open-apis/v1}"
    ```
    """

    def __init__(self, config: dict, output_dir: Path = None):
        super().__init__(config, output_dir)

        # --- Rule B: force candidate's LLM/embedding env to fairness baseline ---
        llm_cfg = config.get("llm", {}) or {}
        forced_base = llm_cfg.get("base_url") or os.environ.get("LLM_BASE_URL", "")
        forced_key = llm_cfg.get("api_key") or os.environ.get("LLM_API_KEY", "")
        forced_model = llm_cfg.get("model") or os.environ.get("LLM_MODEL", "openai/gpt-4.1-mini")
        if forced_base:
            os.environ["OPENAI_BASE_URL"] = forced_base
            os.environ["LLM_BASE_URL"] = forced_base
        if forced_key:
            os.environ["OPENAI_API_KEY"] = forced_key
            os.environ["LLM_API_KEY"] = forced_key
        if forced_model:
            os.environ["LLM_MODEL"] = forced_model

        # --- import + construct candidate client (Rule A, Rule C) ---
        # `simplemem_router` ships in the `simplemem` PyPI package — installed
        # ephemerally via `uv run --with` from system YAML's `python_deps:`.
        import simplemem_router as simplemem  # type: ignore

        self.max_retries = int(config.get("max_retries", 3))
        self.request_interval = float(config.get("request_interval", 0.0))
        self.post_add_wait_seconds = float(config.get("post_add_wait_seconds", 0.0))
        self.search_overfetch = int(config.get("search_overfetch", 5))

        clear_db = bool(config.get("clear_db", True))
        self.simplemem = simplemem
        self.mem = simplemem.create(clear_db=clear_db)
        self._finalized_users: set[str] = set()
        self._user_lock = asyncio.Lock()
        self.console = Console()
        print(f"   SimpleMem mem constructed (clear_db={clear_db})")

    async def close(self) -> None:
        try:
            close_fn = getattr(self.mem, "close", None)
            if callable(close_fn):
                await asyncio.to_thread(close_fn)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  SimpleMem close error (ignored): {e}")

    # SimpleMem stores a single dialogue stream. We isolate per (conv, speaker)
    # using a tag, but always ingest from speaker_a's perspective only — there
    # is no need to duplicate the whole transcript twice.
    def _need_dual_perspective(self, speaker_a: str, speaker_b: str) -> bool:
        return False

    @staticmethod
    def _user_tag(user_id: str) -> str:
        return f"user_id:{user_id}"

    async def _add_user_messages(
        self,
        conv: Conversation,
        messages: List[Dict[str, Any]],
        speaker: str,
        **kwargs: Any,
    ) -> Any:
        del messages  # we read directly from conv.messages to retain timestamps
        user_id = self._extract_user_id(conv, speaker="speaker_a")
        tag = self._user_tag(user_id)

        progress = kwargs.get("progress")
        task_id = kwargs.get("task_id")

        def _ingest_one(speaker_name: str, content: str, ts_iso: str) -> None:
            # Prefer add_text with tags (gives us per-user isolation at query time).
            # Encode dialogue structure into the content so embeddings still see it.
            line = f"[{ts_iso}] {speaker_name}: {content}" if ts_iso else f"{speaker_name}: {content}"
            for attempt in range(self.max_retries):
                try:
                    self.mem.add_text(line, tags=[tag])
                    return
                except Exception:
                    if attempt < self.max_retries - 1:
                        continue
                    raise

        for msg in conv.messages:
            ts_iso = to_iso_format(msg.timestamp) if msg.timestamp else ""
            await asyncio.to_thread(
                _ingest_one,
                msg.speaker_name or "user",
                msg.content,
                ts_iso,
            )
            if progress is not None and task_id is not None:
                progress.update(task_id, advance=1)
            if self.request_interval > 0:
                await asyncio.sleep(self.request_interval)

        # Persist this user's memory once after ingest. finalize() may be
        # global on the underlying mem; protect with a lock so concurrent
        # conversation workers don't race into double-finalize.
        async with self._user_lock:
            if user_id not in self._finalized_users:
                try:
                    await asyncio.to_thread(self.mem.finalize)
                except Exception as e:  # noqa: BLE001
                    print(f"⚠️  SimpleMem finalize warning for {user_id}: {e}")
                self._finalized_users.add(user_id)

        if self.post_add_wait_seconds > 0:
            await asyncio.sleep(self.post_add_wait_seconds)
        return None

    async def _post_add_process(self, add_results: List[Any], **kwargs) -> None:
        del add_results, kwargs
        try:
            await asyncio.to_thread(self.mem.finalize)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  SimpleMem global finalize warning: {e}")

    @staticmethod
    def _extract_items(raw: Any) -> List[Dict[str, Any]]:
        if raw is None:
            return []
        items = getattr(raw, "items", None)
        if items is None and isinstance(raw, dict):
            items = raw.get("items") or raw.get("results")
        if items is None and isinstance(raw, list):
            items = raw
        if items is None:
            return []
        out: List[Dict[str, Any]] = []
        for it in items:
            if hasattr(it, "__dict__"):
                out.append({k: v for k, v in it.__dict__.items()})
            elif isinstance(it, dict):
                out.append(it)
            else:
                out.append({"content": str(it)})
        return out

    async def _search_single_user(
        self,
        query: str,
        conversation_id: str,
        user_id: str,
        top_k: int,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        del conversation_id, kwargs
        tag = self._user_tag(user_id)
        overfetch = max(int(top_k) * self.search_overfetch, int(top_k))

        def _do_query() -> Any:
            # Prefer tag-filtered query if the SDK supports it; fall back to
            # plain query and post-filter.
            try:
                return self.mem.query(query, top_k=overfetch, tags=[tag])
            except TypeError:
                return self.mem.query(query, top_k=overfetch)

        raw = await asyncio.to_thread(_do_query)
        items = self._extract_items(raw)

        out: List[Dict[str, Any]] = []
        for it in items:
            tags = it.get("tags") or it.get("metadata", {}).get("tags") or []
            # Post-filter: only keep items tagged for this user_id.
            if tags and tag not in tags:
                continue
            content = (
                it.get("text")
                or it.get("content")
                or it.get("summary")
                or it.get("memory")
                or ""
            )
            score = float(
                it.get("score")
                or it.get("similarity")
                or it.get("relevance")
                or 0.0
            )
            out.append(
                {
                    "content": content if isinstance(content, str) else str(content),
                    "score": score,
                    "user_id": user_id,
                    "metadata": {"tags": tags, "raw": it},
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
        # Forced-single via _need_dual_perspective; provide a minimal stub.
        del all_results, results_b, speaker_a, speaker_b, speaker_b_user_id, kwargs
        return self._build_single_search_result(
            query=query,
            conversation_id=conversation_id,
            results=results_a or [],
            user_id=speaker_a_user_id,
            top_k=top_k,
        )

    def get_system_info(self) -> Dict[str, Any]:
        return {
            "name": "SimpleMem",
            "type": "online_api",
            "adapter": "SimpleMemAdapter",
        }

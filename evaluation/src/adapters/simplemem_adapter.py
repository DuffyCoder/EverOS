"""
SimpleMem Adapter — local black-box integration with Sophnet LLM.

Auto-bench routine notes:
- Inherits OnlineAPIAdapter template method.
- SimpleMem performs three-stage semantic-lossless compression and stores
  entries in LanceDB; retrieval is hybrid (BM25 + semantic + structured)
  via SimpleMemSystem.hybrid_retriever.
- LLM is forced to the fairness-baseline provider via constructor args.
  Default embedding (Qwen3-Embedding-0.6B, < 1B params) is left local —
  Rule 2 size exception applies.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from common_utils.datetime_utils import to_iso_format
from evaluation.src.adapters.online_base import OnlineAPIAdapter
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult


_USER_ID_SAFE = re.compile(r"[^a-zA-Z0-9_]")


def _safe_table(user_id: str) -> str:
    return _USER_ID_SAFE.sub("_", user_id)[:48] or "default"


@register_adapter("simplemem")
class SimpleMemAdapter(OnlineAPIAdapter):
    """SimpleMem (local LanceDB-backed) adapter."""

    def __init__(self, config: dict, output_dir: Path = None):
        super().__init__(config, output_dir)

        # SimpleMem's bundled OpenAI client reads OPENAI_* env vars at fallback;
        # FORCE-mirror LLM_* into them so any internal call inherits the
        # baseline even if a stale OPENAI_* is already in the environment.
        llm_cfg = config.get("llm", {}) or {}
        baseline_url = (
            llm_cfg.get("base_url")
            or os.environ.get("LLM_BASE_URL")
            or "https://www.sophnet.com/api/open-apis/v1"
        )
        baseline_key = llm_cfg.get("api_key") or os.environ.get("LLM_API_KEY", "")
        # Sophnet rejects the namespaced "openai/gpt-4.1-mini" id; default to bare.
        baseline_model = llm_cfg.get("model") or "gpt-4.1-mini"

        os.environ["OPENAI_API_KEY"] = baseline_key
        os.environ["OPENAI_BASE_URL"] = baseline_url
        os.environ["LLM_MODEL"] = baseline_model

        # SimpleMem defaults to Qwen3-Embedding-0.6B which loads in bfloat16
        # and balloons to ~1.5 GB per instance once cast to float32 for CPU
        # inference. With one SimpleMemSystem per LoCoMo speaker (×10 convs)
        # that exceeds the 16 GB cloud budget. Override to the lightweight
        # MiniLM fallback (~80 MB, 384-dim, float32 native).
        embedding_override = config.get(
            "embedding_model", "sentence-transformers/all-MiniLM-L6-v2"
        )
        os.environ["SIMPLEMEM_EMBEDDING_MODEL"] = embedding_override

        self._llm_api_key = baseline_key
        self._llm_base_url = baseline_url
        self._llm_model = baseline_model

        self.max_retries = int(config.get("max_retries", 3))
        self.request_interval = float(config.get("request_interval", 0.0))

        self._db_root = Path(self.output_dir) / "simplemem_data"
        self._db_root.mkdir(parents=True, exist_ok=True)

        self._mem_instances: Dict[str, Any] = {}
        self._mem_locks: Dict[str, asyncio.Lock] = {}
        self._instances_lock = asyncio.Lock()

        print(f"   SimpleMem db_root={self._db_root}")
        print(f"   SimpleMem LLM base_url={self._llm_base_url} model={self._llm_model}")

    async def _get_or_create_mem(self, user_id: str):
        async with self._instances_lock:
            existing = self._mem_instances.get(user_id)
            if existing is not None:
                return existing, self._mem_locks[user_id]
            lock = self._mem_locks.setdefault(user_id, asyncio.Lock())

        async with lock:
            existing = self._mem_instances.get(user_id)
            if existing is not None:
                return existing, lock

            def _construct():
                # `pip install simplemem` exposes simplemem.system.SimpleMemSystem.
                from simplemem.system import SimpleMemSystem  # type: ignore

                table = _safe_table(user_id)
                db_path = str(self._db_root / f"{table}.lance")
                inst = SimpleMemSystem(
                    api_key=self._llm_api_key,
                    base_url=self._llm_base_url,
                    model=self._llm_model,
                    db_path=db_path,
                    table_name=table,
                    clear_db=True,
                )
                # Qwen3 weights load as bfloat16 but sentence-transformers
                # tokenizes inputs as float32; on CPU the linear layer rejects
                # the mismatch. Cast the embedding stack to float32 explicitly.
                try:
                    import torch  # type: ignore

                    emb = getattr(inst, "embedding_model", None)
                    st = getattr(emb, "model", None)
                    if st is not None:
                        st.to(torch.float32)
                except Exception as e:  # noqa: BLE001
                    print(f"   SimpleMem dtype cast skipped: {e}")
                return inst

            mem = await asyncio.to_thread(_construct)
            self._mem_instances[user_id] = mem
            return mem, lock

    def _need_dual_perspective(self, speaker_a: str, speaker_b: str) -> bool:
        # SimpleMem stores per-user_id; isolate at the instance boundary, not
        # by per-perspective splitting of the same conversation.
        return False

    async def _add_user_messages(
        self,
        conv: Conversation,
        messages: List[Dict[str, Any]],
        speaker: str,
        **kwargs: Any,
    ) -> Any:
        del messages  # we use raw conv.messages to preserve speaker_name + timestamp
        user_id = self._extract_user_id(conv, speaker=speaker)
        progress = kwargs.get("progress")
        task_id = kwargs.get("task_id")

        mem, lock = await self._get_or_create_mem(user_id)

        async with lock:
            for raw_msg in conv.messages:
                content = raw_msg.content or ""
                speaker_name = raw_msg.speaker_name or speaker
                ts = to_iso_format(raw_msg.timestamp) if raw_msg.timestamp else None

                last_exc: Optional[Exception] = None
                for attempt in range(self.max_retries):
                    try:
                        await asyncio.to_thread(
                            mem.add_dialogue, speaker_name, content, ts
                        )
                        last_exc = None
                        break
                    except Exception as e:  # noqa: BLE001
                        last_exc = e
                        if attempt < self.max_retries - 1:
                            await asyncio.sleep(min(2 ** attempt, 8))
                            continue
                if last_exc is not None:
                    raise last_exc

                if progress is not None and task_id is not None:
                    progress.update(task_id, advance=1)
                if self.request_interval > 0:
                    await asyncio.sleep(self.request_interval)

            # Flush buffered dialogues so memories become searchable.
            await asyncio.to_thread(mem.finalize)

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
        mem = self._mem_instances.get(user_id)
        if mem is None:
            return []
        lock = self._mem_locks[user_id]

        async with lock:
            entries = await asyncio.to_thread(mem.hybrid_retriever.retrieve, query)

        out: List[Dict[str, Any]] = []
        entries = entries or []
        for rank, entry in enumerate(entries[: int(top_k)]):
            text = (
                getattr(entry, "lossless_restatement", None)
                or getattr(entry, "summary", None)
                or ""
            )
            ts = getattr(entry, "timestamp", "") or ""
            content = f"{ts}: {text}".strip(": ").strip() if ts else text
            out.append(
                {
                    "content": content,
                    "score": 1.0 / (rank + 1),
                    "user_id": user_id,
                    "metadata": {
                        "entry_id": getattr(entry, "entry_id", None),
                        "topic": getattr(entry, "topic", None),
                        "persons": getattr(entry, "persons", None),
                        "entities": getattr(entry, "entities", None),
                        "keywords": getattr(entry, "keywords", None),
                        "location": getattr(entry, "location", None),
                    },
                }
            )
        return out

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
        # Not used (single perspective forced), but provide a minimal impl.
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
            "name": "SimpleMem",
            "type": "online_api",
            "adapter": "SimpleMemAdapter",
        }

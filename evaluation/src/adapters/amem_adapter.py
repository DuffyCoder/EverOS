"""
A-Mem Adapter - integrates A-MEM (Agentic Memory for LLM Agents) into the
evaluation framework.

Reference: https://github.com/agiresearch/A-mem  (NeurIPS 2025; arXiv 2502.12110)

Decision-rule classification (see CLAUDE.md / auto-bench-routine):
- Memory backend: LOCAL — ChromaDB EphemeralClient inside the Python process.
- LLM: REWRITTEN to the Sophnet fairness baseline. A-MEM's `OpenAIController`
  uses the OpenAI Python SDK without a base_url override, so we set
  `OPENAI_BASE_URL` / `OPENAI_API_KEY` from `LLM_BASE_URL` / `LLM_API_KEY`
  before the first AgenticMemorySystem is constructed. The SDK reads
  `OPENAI_BASE_URL` when no URL is passed in, so this redirects every
  metadata/evolution call through Sophnet without patching A-MEM source.
- Embedding: A-MEM's default `all-MiniLM-L6-v2` (~22M params) — well below
  the 1B threshold the routine rule allows for in-process embedding.

Isolation note:
A-MEM's `AgenticMemorySystem.__init__` calls `chromadb.Client.reset()` on
the global client, which would wipe sibling instances. We monkey-patch
`ChromaRetriever.__init__` once to use a fresh `EphemeralClient` per
retriever, so each per-user system has an isolated in-memory ChromaDB.
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from evaluation.src.adapters.online_base import OnlineAPIAdapter
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult


def _redirect_openai_to_sophnet(config: dict) -> None:
    """Force A-MEM's OpenAI client to talk to the configured baseline.

    A-MEM constructs `OpenAI(api_key=...)` without a base_url; the SDK then
    reads OPENAI_BASE_URL from the env. Setting both keys here guarantees
    every LLM call A-MEM makes (analyze_content + memory evolution) hits
    Sophnet, not api.openai.com.
    """
    llm_cfg = config.get("llm", {}) or {}
    base_url = llm_cfg.get("base_url") or os.getenv("LLM_BASE_URL")
    api_key = llm_cfg.get("api_key") or os.getenv("LLM_API_KEY")
    if base_url:
        os.environ["OPENAI_BASE_URL"] = base_url
    if api_key:
        os.environ["OPENAI_API_KEY"] = api_key


def _patch_chroma_isolation() -> None:
    """Make every `ChromaRetriever` instance use its own PersistentClient.

    chromadb.EphemeralClient is NOT isolated — multiple instances share an
    in-process global, so A-MEM's `reset()` call inside __init__ would wipe
    every previously-indexed (conversation_id, speaker) collection.

    Fix: each ChromaRetriever instance gets a fresh `PersistentClient` rooted
    at a unique tmpdir, so its `client.reset()` only affects its own DB.
    The temp dirs are auto-cleaned at process exit.
    """
    from agentic_memory import retrievers as _retrievers
    import chromadb
    from chromadb.config import Settings
    from chromadb.utils.embedding_functions import (
        SentenceTransformerEmbeddingFunction,
    )

    if getattr(_retrievers.ChromaRetriever, "_amem_adapter_patched", False):
        return

    import atexit
    import shutil
    import tempfile

    _persist_dirs: List[str] = []

    def _cleanup() -> None:
        for d in _persist_dirs:
            shutil.rmtree(d, ignore_errors=True)

    atexit.register(_cleanup)

    def _isolated_init(
        self,
        collection_name: str = "memories",
        model_name: str = "all-MiniLM-L6-v2",
    ) -> None:
        persist_dir = tempfile.mkdtemp(prefix="amem-chroma-")
        _persist_dirs.append(persist_dir)
        self.client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(allow_reset=True, anonymized_telemetry=False),
        )
        self.embedding_function = SentenceTransformerEmbeddingFunction(
            model_name=model_name
        )
        self.collection = self.client.get_or_create_collection(
            name=collection_name, embedding_function=self.embedding_function
        )

    _retrievers.ChromaRetriever.__init__ = _isolated_init
    _retrievers.ChromaRetriever._amem_adapter_patched = True


@register_adapter("amem")
class AMemAdapter(OnlineAPIAdapter):
    """A-MEM adapter (in-process, ChromaDB-backed).

    Memory shape:
    - One `AgenticMemorySystem` instance per (conversation_id, speaker)
      pair, keyed by the same `user_id` scheme other adapters use.
    - `add_note()` per conversation message, in conversation order, so
      A-MEM's evolution graph reflects the original temporal flow.
    - `search_agentic()` for retrieval (the README's recommended path —
      pulls neighbors via the evolved link graph).
    """

    def __init__(self, config: dict, output_dir: Path = None):
        super().__init__(config, output_dir)

        try:
            _patch_chroma_isolation()
            from agentic_memory.memory_system import AgenticMemorySystem  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "agentic-memory package not installed. "
                "Declare it in evaluation/config/systems/amem.yaml under "
                "python_deps: and rerun via `uv run --with`."
            ) from exc

        _redirect_openai_to_sophnet(config)

        amem_cfg = config.get("amem", {}) or {}
        self.embedding_model = amem_cfg.get("embedding_model", "all-MiniLM-L6-v2")
        self.llm_backend = amem_cfg.get("llm_backend", "openai")
        # A-MEM's OpenAIController will call OpenAI(model=...). We pass the
        # bare upstream model id (Sophnet expects "openai/gpt-4.1-mini").
        self.llm_model_for_amem = amem_cfg.get(
            "llm_model", config.get("llm", {}).get("model", "openai/gpt-4.1-mini")
        )
        self.evo_threshold = int(amem_cfg.get("evo_threshold", 100))
        self.search_top_k = int(config.get("search", {}).get("top_k", 10))
        self.max_content_length = int(amem_cfg.get("max_content_length", 12000))

        # user_id -> AgenticMemorySystem (lazy)
        self._systems: Dict[str, Any] = {}
        # user_id -> stored note ids (for diagnostics + ordering)
        self._note_ids: Dict[str, List[str]] = {}

        self.console = Console()
        self.console.print(
            f"   A-MEM embedding: {self.embedding_model}", style="dim"
        )
        self.console.print(
            f"   A-MEM LLM backend: {self.llm_backend} / {self.llm_model_for_amem}",
            style="dim",
        )
        self.console.print(
            f"   OpenAI base_url override: {os.getenv('OPENAI_BASE_URL')}",
            style="dim",
        )

    def _get_or_create_system(self, user_id: str) -> Any:
        from agentic_memory.memory_system import AgenticMemorySystem

        sys = self._systems.get(user_id)
        if sys is None:
            sys = AgenticMemorySystem(
                model_name=self.embedding_model,
                llm_backend=self.llm_backend,
                llm_model=self.llm_model_for_amem,
                evo_threshold=self.evo_threshold,
                api_key=os.getenv("OPENAI_API_KEY"),
            )
            self._systems[user_id] = sys
            self._note_ids[user_id] = []
        return sys

    def _get_format_type(self) -> str:
        # We don't need any system-specific formatter; default 'basic' is enough
        # — we handle message rendering ourselves in _add_user_messages.
        return "basic"

    @staticmethod
    def _format_amem_timestamp(ts) -> Optional[str]:
        """A-MEM expects YYYYMMDDHHMM strings."""
        if ts is None:
            return None
        try:
            return ts.strftime("%Y%m%d%H%M")
        except Exception:
            return None

    async def _add_user_messages(
        self,
        conv: Conversation,
        messages: List[Dict[str, Any]],
        speaker: str,
        **kwargs,
    ) -> Any:
        user_id = self._extract_user_id(conv, speaker=speaker)
        speaker_name = conv.metadata.get(speaker, speaker)
        self.console.print(
            f"   📤 A-MEM add for {speaker_name} ({user_id}): {len(messages)} msgs",
            style="dim",
        )

        system = self._get_or_create_system(user_id)

        loop = asyncio.get_running_loop()
        ids: List[str] = []
        # Walk messages in original order so A-MEM's evolution sees the
        # conversation as a temporal sequence, the way it was designed.
        for idx, msg in enumerate(messages):
            content = msg["content"]
            if len(content) > self.max_content_length:
                content = content[: self.max_content_length]
            # Pair each formatted message with its original timestamp.
            ts = None
            if idx < len(conv.messages):
                ts = self._format_amem_timestamp(conv.messages[idx].timestamp)
            try:
                note_id = await loop.run_in_executor(
                    None,
                    lambda c=content, t=ts: system.add_note(c, time=t),
                )
                ids.append(note_id)
            except Exception as exc:  # bounded: A-MEM raises freely
                self.console.print(
                    f"   ⚠️  A-MEM add_note failed at idx={idx} for "
                    f"{user_id}: {exc}",
                    style="yellow",
                )

        self._note_ids[user_id].extend(ids)
        return None

    async def _wait_for_conversation_tasks(
        self, results: List[Any], conversation_id: str, **kwargs
    ) -> None:
        # All A-MEM operations are synchronous (run_in_executor) — nothing
        # to await past the per-message loop.
        return None

    async def _search_single_user(
        self,
        query: str,
        conversation_id: str,
        user_id: str,
        top_k: int,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        system = self._systems.get(user_id)
        if system is None:
            # No memories ingested for this user — empty result is fine, the
            # answer stage will fall back to "no information" handling.
            return []

        loop = asyncio.get_running_loop()
        try:
            raw = await loop.run_in_executor(
                None,
                lambda: system.search_agentic(query, k=top_k),
            )
        except Exception as exc:
            self.console.print(
                f"   ⚠️  A-MEM search failed for {user_id}: {exc}", style="yellow"
            )
            return []

        results: List[Dict[str, Any]] = []
        for item in raw:
            content = item.get("content") or ""
            ts = item.get("timestamp") or ""
            display = (
                f"[{ts}] {content}"
                if ts
                else content
            )
            results.append(
                {
                    "content": display,
                    "score": float(item.get("score", 0.0)),
                    "user_id": user_id,
                    "metadata": {
                        "id": item.get("id"),
                        "context": item.get("context", ""),
                        "keywords": item.get("keywords", []),
                        "tags": item.get("tags", []),
                        "category": item.get("category", ""),
                        "is_neighbor": item.get("is_neighbor", False),
                        "timestamp": ts,
                    },
                }
            )
        return results

    @staticmethod
    def _format_context(results: List[Dict[str, Any]], speaker_label: str) -> str:
        if not results:
            return ""
        lines = []
        for r in results:
            lines.append(f"- {speaker_label}: {r['content']}")
        return "\n".join(lines)

    def _build_single_search_result(
        self,
        query: str,
        conversation_id: str,
        results: List[Dict[str, Any]],
        user_id: str,
        top_k: int,
        **kwargs,
    ) -> SearchResult:
        formatted = self._format_context(results, speaker_label=user_id)
        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=results,
            retrieval_metadata={
                "system": "amem",
                "top_k": top_k,
                "formatted_context": formatted,
                "perspective": "single",
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
        **kwargs,
    ) -> SearchResult:
        formatted_a = self._format_context(results_a, speaker_label=speaker_a)
        formatted_b = self._format_context(results_b, speaker_label=speaker_b)
        formatted_context = "\n".join(p for p in (formatted_a, formatted_b) if p)
        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=all_results,
            retrieval_metadata={
                "system": "amem",
                "top_k": top_k,
                "formatted_context": formatted_context,
                "perspective": "dual",
                "speaker_a_count": len(results_a),
                "speaker_b_count": len(results_b),
            },
        )

    def get_system_info(self) -> Dict[str, Any]:
        return {
            "name": "A-MEM",
            "version": "0.0.1",
            "description": (
                "Agentic Memory system (NeurIPS 2025) — ChromaDB-backed local "
                "memory with Zettelkasten-style evolution. LLM rewired to "
                "Sophnet baseline."
            ),
            "adapter": "AMemAdapter",
        }

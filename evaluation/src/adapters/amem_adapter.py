"""
A-MEM Adapter — local ChromaDB-backed agentic memory system from the NeurIPS
2025 paper "A-Mem: Agentic Memory for LLM Agents" (WujiangXu/A-mem-sys).

Rule compliance:
- Rule 1 (local): memories are stored in a local ChromaDB collection; embeddings
  via sentence-transformers (all-MiniLM-L6-v2, ~22 M params — well under 1 B).
- Rule 2 (configurable LLM/embedding): A-MEM's OpenAIController accepts api_key
  but does not expose base_url, so we set OPENAI_BASE_URL via env before
  import. The OpenAI Python SDK honors this env var when constructing clients.
  Embedding model name is kept at all-MiniLM-L6-v2 (<1 B threshold exception).
- Rule 3 (RAM): ~1.5 GB (ChromaDB local + MiniLM weights on CPU).

The A-MEM repo is cloned into /tmp/candidate/amem/ and injected into sys.path
at adapter init time.
"""

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from evaluation.src.adapters.base import BaseAdapter
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult
from common_utils.datetime_utils import to_iso_format

AMEM_REPO_DIR = Path(os.environ.get("AMEM_REPO_DIR", "/tmp/candidate/amem"))


def _ensure_amem_importable(repo_dir: Path) -> None:
    if not repo_dir.exists():
        raise RuntimeError(
            f"A-MEM repo not found at {repo_dir}. Clone via "
            f"`git clone https://github.com/WujiangXu/A-mem-sys.git {repo_dir}` "
            f"or set AMEM_REPO_DIR."
        )
    repo_str = str(repo_dir.resolve())
    sys.path[:] = [repo_str] + [p for p in sys.path if p != repo_str]


@register_adapter("amem")
class AMEMAdapter(BaseAdapter):
    """Adapter for WujiangXu/A-mem-sys's AgenticMemorySystem."""

    def __init__(self, config: dict, output_dir: Path = None):
        super().__init__(config)
        self.output_dir = Path(output_dir) if output_dir else Path(".")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.llm_config = config.get("llm", {}) or {}
        self.search_config = config.get("search", {}) or {}

        # Route A-MEM's OpenAI client through Sophnet. A-MEM's OpenAIController
        # does not expose base_url; the openai SDK honors OPENAI_BASE_URL when
        # the client is constructed without an explicit base_url arg.
        api_key = self.llm_config.get("api_key") or os.getenv("LLM_API_KEY", "")
        base_url = self.llm_config.get("base_url") or os.getenv(
            "LLM_BASE_URL", "https://www.sophnet.com/api/open-apis/v1"
        )
        os.environ["OPENAI_API_KEY"] = api_key
        os.environ["OPENAI_BASE_URL"] = base_url

        _ensure_amem_importable(AMEM_REPO_DIR)

        self._llm_backend = self.llm_config.get("backend", "openai")
        self._llm_model = self.llm_config.get("model", "openai/gpt-4.1-mini")
        self._embed_model = self.search_config.get(
            "embedding_model", "all-MiniLM-L6-v2"
        )
        self._api_key = api_key
        self._systems: Dict[str, Any] = {}

        print(f"✅ AMEMAdapter initialized")
        print(f"   LLM Model: {self._llm_model}")
        print(f"   Base URL:  {base_url}")
        print(f"   Embedding: {self._embed_model}")
        print(f"   Output Dir: {self.output_dir}")

    def _build_system(self):
        from agentic_memory.memory_system import AgenticMemorySystem  # type: ignore

        return AgenticMemorySystem(
            model_name=self._embed_model,
            llm_backend=self._llm_backend,
            llm_model=self._llm_model,
            api_key=self._api_key,
        )

    async def add(
        self,
        conversations: List[Conversation],
        output_dir: Path = None,
        **kwargs,
    ) -> Dict[str, Any]:
        output_dir = Path(output_dir) if output_dir else self.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        ingested: List[str] = []

        def _ingest_one(conv: Conversation) -> str:
            # A-MEM's ChromaDB backend is a shared singleton — we use one system
            # per routine call but namespace via conv_id in note tags so search
            # can filter by conversation. ChromaDB is reset on each fresh
            # SimpleMemSystem() call (see memory_system.py), which is why we
            # build a single system and reuse it across convs, not one per conv.
            if "system" not in self._systems:
                self._systems["system"] = self._build_system()
            system = self._systems["system"]

            for msg in conv.messages:
                ts = to_iso_format(msg.timestamp) if msg.timestamp else None
                speaker = msg.speaker_name or msg.speaker_id
                content = f"{speaker}: {msg.content}"
                system.add_note(
                    content=content,
                    time=ts,
                    tags=[f"conv:{conv.conversation_id}"],
                )
            return conv.conversation_id

        loop = asyncio.get_running_loop()
        for conv in conversations:
            conv_id = await loop.run_in_executor(None, _ingest_one, conv)
            ingested.append(conv_id)
            print(f"  ✅ Ingested {conv_id}")

        return {
            "type": "local",
            "system": "amem",
            "conversation_ids": ingested,
        }

    async def search(
        self, query: str, conversation_id: str, index: Any, **kwargs
    ) -> SearchResult:
        t0 = time.perf_counter()
        system = self._systems.get("system")
        if system is None:
            # Edge case: search without preceding add (stage filter / checkpoint
            # resume). A-MEM has no on-disk persistence outside the ChromaDB
            # in-memory client here, so we can't rehydrate — return empty.
            return SearchResult(
                query=query,
                conversation_id=conversation_id,
                results=[],
                retrieval_metadata={
                    "system": "amem",
                    "error": "no active AMEM system; add stage must run first",
                    "retrieval_latency_ms": (time.perf_counter() - t0) * 1000.0,
                },
            )

        top_k = kwargs.get("top_k", self.search_config.get("top_k", 10))

        def _retrieve():
            # search() returns List[Dict[str, Any]]; each item has content/id/
            # context/keywords/tags/score fields per A-MEM's ChromaRetriever.
            raw = system.search(query, k=top_k * 3)  # over-fetch to filter
            filtered = [
                r for r in raw
                if any(
                    str(t).startswith(f"conv:{conversation_id}")
                    for t in (r.get("tags") or [])
                )
            ]
            # If tag filter yields nothing (tag storage quirk), fall back to
            # top-k raw — still scoped by ChromaDB relevance.
            return (filtered or raw)[:top_k]

        loop = asyncio.get_running_loop()
        raw_hits = await loop.run_in_executor(None, _retrieve)

        results: List[Dict[str, Any]] = []
        for r in raw_hits:
            content = r.get("content") or ""
            score = float(r.get("score", 0.0) or 0.0)
            meta = {
                "memory_id": r.get("id"),
                "keywords": r.get("keywords") or [],
                "context": r.get("context"),
                "tags": r.get("tags") or [],
                "timestamp": r.get("timestamp"),
            }
            results.append({"content": content, "score": score, "metadata": meta})

        conversation = kwargs.get("conversation")
        speaker_a = (
            conversation.metadata.get("speaker_a", "Speaker A") if conversation else "Speaker A"
        )
        speaker_b = (
            conversation.metadata.get("speaker_b", "Speaker B") if conversation else "Speaker B"
        )
        body = "\n---\n".join(r["content"] for r in results if r["content"])
        formatted_context = (
            f"Memories between {speaker_a} and {speaker_b} from A-MEM:\n\n{body}\n"
            if body
            else ""
        )

        retrieval_metadata = {
            "system": "amem",
            "retrieval_latency_ms": (time.perf_counter() - t0) * 1000.0,
            "backend_mode": "chromadb_agentic",
            "top_k": top_k,
            "formatted_context": formatted_context,
        }

        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=results,
            retrieval_metadata=retrieval_metadata,
        )

    async def answer(self, query: str, context: str, **kwargs) -> str:
        from memory_layer.llm.llm_provider import LLMProvider
        from evaluation.src.utils.config import load_yaml

        if not hasattr(self, "_llm_provider"):
            self._llm_provider = LLMProvider(
                provider_type=self.llm_config.get("provider", "openai"),
                model=self.llm_config.get("model", "openai/gpt-4.1-mini"),
                api_key=self.llm_config.get("api_key") or os.getenv("LLM_API_KEY", ""),
                base_url=self.llm_config.get("base_url")
                or os.getenv("LLM_BASE_URL", "https://www.sophnet.com/api/open-apis/v1"),
                temperature=self.llm_config.get("temperature", 0.0),
                max_tokens=self.llm_config.get("max_tokens", 4096),
            )
            evaluation_root = Path(__file__).parent.parent.parent
            self._prompts = load_yaml(str(evaluation_root / "config" / "prompts.yaml"))

        prompt_tmpl = self._prompts["online_api"]["default"]["answer_prompt_memos"]
        prompt = prompt_tmpl.format(context=context, question=query)
        result = await self._llm_provider.generate(prompt=prompt, temperature=0)
        if "FINAL ANSWER:" in result:
            result = result.split("FINAL ANSWER:")[-1].strip()
        return result.strip()

    def get_system_info(self) -> Dict[str, Any]:
        return {
            "name": "A-MEM",
            "version": "main",
            "description": "Agentic memory with ChromaDB + sentence-transformers (WujiangXu/A-mem-sys)",
            "adapter": "auto-bench adapter for A-MEM",
            "repo_dir": str(AMEM_REPO_DIR),
        }

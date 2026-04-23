"""
SimpleMem Adapter — local memory backend (LanceDB + SQLite) with Qwen3 embedding
and OpenAI-compatible LLM. Written by the auto-bench routine.

SimpleMem is a research implementation from aiming-lab that exposes a
three-stage pipeline: add_dialogue -> finalize -> ask. We only need the first
two stages during add(), then call hybrid_retriever.retrieve(query) directly in
search() to stay within the BaseAdapter add/search contract and let the
framework's own answer step handle answer generation against the fairness
baseline prompt.

Rule compliance:
- Rule 1 (local backend): LanceDB + SQLite, fully local — no SaaS call.
- Rule 2 (configurable LLM/embedding): LLM rewritten to Sophnet via SimpleMem's
  api_key / base_url / model kwargs. Embedding stays on Qwen3-Embedding-0.6B
  (0.6B parameters < 1B) which the routine rules explicitly permit.
- Rule 3 (RAM): ~2 GB (Qwen3-0.6B weights + LanceDB). Fits alongside EverOS-off.

The SimpleMem package is cloned at /tmp/candidate/simplemem/ and injected into
sys.path at adapter init time. A minimal config.py shim is materialized from
config.py.example before first import so the `import config` at the top of
main.py resolves.
"""

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from evaluation.src.adapters.base import BaseAdapter
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult
from common_utils.datetime_utils import to_iso_format

SIMPLEMEM_REPO_DIR = Path(
    os.environ.get("SIMPLEMEM_REPO_DIR", "/tmp/candidate/simplemem")
)


def _ensure_simplemem_importable(repo_dir: Path) -> None:
    """Prepare config.py from template and inject repo into sys.path.

    SimpleMem's top-level modules (`main`, `core`, `database`, `utils`,
    `models`, `config`) clash with EverOS src/ namespace packages. We put the
    repo path first on sys.path AND evict any already-imported conflicting
    names from sys.modules so SimpleMem resolves to its own code.
    """
    if not repo_dir.exists():
        raise RuntimeError(
            f"SimpleMem repo not found at {repo_dir}. Clone it via "
            f"`git clone https://github.com/aiming-lab/SimpleMem.git {repo_dir}` "
            f"or set SIMPLEMEM_REPO_DIR to the existing path."
        )

    config_path = repo_dir / "config.py"
    template_path = repo_dir / "config.py.example"
    if not config_path.exists() and template_path.exists():
        shutil.copy(template_path, config_path)

    repo_str = str(repo_dir.resolve())
    # Force the SimpleMem repo to the front of sys.path (re-insert if already
    # present further down). The top-level `core`, `utils`, `models` packages
    # collide with EverOS's own src/ layout and Python caches the first win.
    sys.path[:] = [repo_str] + [p for p in sys.path if p != repo_str]

    conflicting = ("config", "main", "core", "database", "utils", "models")
    for mod_name in list(sys.modules):
        if mod_name == "config" or mod_name.startswith("config."):
            del sys.modules[mod_name]
            continue
        if mod_name in conflicting:
            del sys.modules[mod_name]
            continue
        for prefix in conflicting:
            if mod_name.startswith(prefix + "."):
                del sys.modules[mod_name]
                break


@register_adapter("simplemem")
class SimpleMemAdapter(BaseAdapter):
    """Adapter wrapping aiming-lab/SimpleMem's SimpleMemSystem.

    One SimpleMemSystem per conversation, persisted under output_dir/lancedb/
    with a table name derived from the conversation id. Add path streams
    conversation messages through add_dialogue(); search path calls the
    hybrid retriever directly and yields formatted context for the shared
    answer stage.
    """

    def __init__(self, config: dict, output_dir: Path = None):
        super().__init__(config)
        self.output_dir = Path(output_dir) if output_dir else Path(".")
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.llm_config = config.get("llm", {}) or {}
        self.search_config = config.get("search", {}) or {}

        _ensure_simplemem_importable(SIMPLEMEM_REPO_DIR)

        # Overwrite config module constants to force Sophnet + eval-friendly knobs
        # BEFORE any SimpleMem component imports. SimpleMem's LLMClient reads
        # these at instantiation time, but EmbeddingModel and retriever tuning
        # pick them up only from the module-level constants.
        import config as simplemem_config  # type: ignore

        simplemem_config.OPENAI_API_KEY = self.llm_config.get("api_key") or os.getenv(
            "LLM_API_KEY", ""
        )
        simplemem_config.OPENAI_BASE_URL = self.llm_config.get("base_url") or os.getenv(
            "LLM_BASE_URL", "https://www.sophnet.com/api/open-apis/v1"
        )
        simplemem_config.LLM_MODEL = self.llm_config.get("model", "openai/gpt-4.1-mini")
        simplemem_config.ENABLE_THINKING = False
        simplemem_config.USE_STREAMING = False
        simplemem_config.USE_JSON_FORMAT = self.llm_config.get("use_json_format", False)

        self._simplemem_config = simplemem_config
        self._systems: Dict[str, Any] = {}

        # Import lazily-built LLMProvider only if the answer stage runs against
        # SimpleMem's retrieved contexts — we delegate answer() to the base
        # pipeline, so skip that cost here.
        print(f"✅ SimpleMemAdapter initialized")
        print(f"   LLM Model: {simplemem_config.LLM_MODEL}")
        print(f"   Base URL:  {simplemem_config.OPENAI_BASE_URL}")
        print(f"   Repo Dir:  {SIMPLEMEM_REPO_DIR}")
        print(f"   Output Dir: {self.output_dir}")

    def _db_path(self) -> str:
        return str((self.output_dir / "lancedb").resolve())

    @staticmethod
    def _safe_table_name(conv_id: str) -> str:
        return "sm_" + "".join(c if c.isalnum() else "_" for c in conv_id)

    def _build_system(self, conv_id: str):
        """Instantiate a fresh SimpleMemSystem for one conversation."""
        from main import SimpleMemSystem  # type: ignore

        return SimpleMemSystem(
            api_key=self._simplemem_config.OPENAI_API_KEY,
            base_url=self._simplemem_config.OPENAI_BASE_URL,
            model=self._simplemem_config.LLM_MODEL,
            db_path=self._db_path(),
            table_name=self._safe_table_name(conv_id),
            clear_db=False,
            enable_thinking=False,
            use_streaming=False,
        )

    async def add(
        self,
        conversations: List[Conversation],
        output_dir: Path = None,
        **kwargs,
    ) -> Dict[str, Any]:
        output_dir = Path(output_dir) if output_dir else self.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        ingested = []

        def _ingest_one(conv: Conversation) -> str:
            system = self._build_system(conv.conversation_id)
            for msg in conv.messages:
                ts = to_iso_format(msg.timestamp) if msg.timestamp else None
                speaker = msg.speaker_name or msg.speaker_id
                system.add_dialogue(speaker=speaker, content=msg.content, timestamp=ts)
            system.finalize()
            self._systems[conv.conversation_id] = system
            return conv.conversation_id

        loop = asyncio.get_running_loop()
        # Add is CPU/LLM-bound inside SimpleMem; keep it single-worker to avoid
        # exceeding Sophnet rate limits during the routine's cloud window.
        for conv in conversations:
            conv_id = await loop.run_in_executor(None, _ingest_one, conv)
            ingested.append(conv_id)
            print(f"  ✅ Ingested {conv_id}")

        return {
            "type": "local",
            "system": "simplemem",
            "conversation_ids": ingested,
            "db_path": self._db_path(),
        }

    async def search(
        self, query: str, conversation_id: str, index: Any, **kwargs
    ) -> SearchResult:
        t0 = time.perf_counter()

        system = self._systems.get(conversation_id)
        if system is None:
            # Index metadata path: rebuild a read-only handle pointing at the
            # on-disk LanceDB table written by add(). SimpleMem reads state
            # from its persisted table, so re-instantiation is safe.
            system = self._build_system(conversation_id)
            self._systems[conversation_id] = system

        top_k = kwargs.get("top_k", self.search_config.get("top_k", 10))

        def _retrieve():
            return system.hybrid_retriever.retrieve(query)

        loop = asyncio.get_running_loop()
        entries = await loop.run_in_executor(None, _retrieve)

        results: List[Dict[str, Any]] = []
        for entry in entries[:top_k]:
            content = getattr(entry, "lossless_restatement", "") or ""
            score = float(getattr(entry, "score", 0.0) or 0.0)
            meta: Dict[str, Any] = {
                "entry_id": getattr(entry, "entry_id", None),
                "timestamp": getattr(entry, "timestamp", None),
                "topic": getattr(entry, "topic", None),
                "keywords": list(getattr(entry, "keywords", []) or []),
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
            f"Memories between {speaker_a} and {speaker_b} from SimpleMem:\n\n{body}\n"
            if body
            else ""
        )

        retrieval_metadata = {
            "system": "simplemem",
            "retrieval_latency_ms": (time.perf_counter() - t0) * 1000.0,
            "backend_mode": "hybrid_lancedb_bm25",
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
        """Defer to the shared answer prompt via LLMProvider.

        Kept on the adapter so the Pipeline can invoke answer() directly when
        --stages includes it. Matches the behavior of OnlineAPIAdapter.answer.
        """
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
            "name": "SimpleMem",
            "version": "0.2.0",
            "description": "Local LanceDB+SQLite memory with hybrid retrieval (aiming-lab/SimpleMem)",
            "adapter": "auto-bench adapter for SimpleMem",
            "repo_dir": str(SIMPLEMEM_REPO_DIR),
        }

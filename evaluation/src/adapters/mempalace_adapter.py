"""
MemPalace adapter - integrate local MemPalace retrieval into the evaluation framework.

This adapter intentionally keeps its MemPalace integration thin and internal:
conversation add uses the vendored ``evaluation.src.adapters.mempalace.convo_miner``
implementation and retrieval uses the vendored
``evaluation.src.adapters.mempalace.searcher`` implementation so evaluation
behavior stays aligned with the MemPalace pipeline used in this repository.
"""

import asyncio
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

from rich.console import Console

from evaluation.src.adapters.mempalace.convo_miner import mine_convos
from evaluation.src.adapters.mempalace.convo_closet_miner import (
    mine_convos_with_closets,
)
from evaluation.src.adapters.mempalace.searcher import search_memories
from evaluation.src.adapters.online_base import OnlineAPIAdapter
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, Message, SearchResult


@register_adapter("mempalace")
class MemPalaceAdapter(OnlineAPIAdapter):
    """MemPalace local adapter."""

    def __init__(self, config: dict, output_dir: Path = None):
        super().__init__(config, output_dir)

        palace_path = config.get("palace_path")
        if palace_path:
            self.palace_path = Path(palace_path).expanduser()
        else:
            self.palace_path = self.output_dir / "mempalace_palace"

        self.agent_name = config.get("agent_name", "evaluation_mempalace")
        self.extract_mode = config.get("extract_mode", "exchange")
        self.clean_before_add = config.get("clean_before_add", False)
        self.use_closet_boost = config.get("search", {}).get("use_closet_boost", True)
        self.use_keyword_predicate_boost = config.get("search", {}).get(
            "use_keyword_predicate_boost", False
        )
        self.build_closets = config.get("add", {}).get(
            "build_closets", self.use_closet_boost
        )
        self._mine_convos = (
            mine_convos_with_closets if self.build_closets else mine_convos
        )
        self._search_memories = search_memories
        self.transcript_root = self.output_dir / "mempalace_convo_inputs"
        self._search_observations: Dict[str, Dict[str, Any]] = {}
        self.console = Console()

        self.console.print(f"   Palace Path: {self.palace_path}", style="dim")
        self.console.print(f"   Transcript Staging: {self.transcript_root}", style="dim")
        self.console.print(f"   Agent Name: {self.agent_name}", style="dim")
        self.console.print(f"   Extract Mode: {self.extract_mode}", style="dim")
        self.console.print(f"   Closet Boost: {'enabled' if self.use_closet_boost else 'disabled'}", style="dim")
        self.console.print(
            f"   Keyword/Predicate Boost: {'enabled' if self.use_keyword_predicate_boost else 'disabled'}",
            style="dim",
        )
        self.console.print(
            f"   Build Closets On Add: {'enabled' if self.build_closets else 'disabled'}",
            style="dim",
        )
        add_pipeline_name = (
            "convo_closet_miner.mine_convos_with_closets()"
            if self.build_closets
            else "convo_miner.mine_convos()"
        )
        self.console.print(f"   Add Pipeline: {add_pipeline_name}", style="dim")
        self.console.print("   Search Pipeline: searcher.search_memories()", style="dim")

    async def prepare(self, conversations: List[Conversation], **kwargs) -> None:
        """Optionally clear the local palace before re-indexing."""
        _ = conversations, kwargs
        self.console.print(
            f"   📦 MemPalace will index {len(conversations)} conversation(s) via internal convo_miner",
            style="dim",
        )
        if self.clean_before_add and self.palace_path.exists():
            self.console.print(
                f"   🧹 Removing existing MemPalace data: {self.palace_path}",
                style="yellow",
            )
            shutil.rmtree(self.palace_path, ignore_errors=True)
        elif not self.clean_before_add:
            self.console.print(
                "   ⏭️  Skipping palace cleanup (clean_before_add=false)",
                style="dim",
            )
        if self.clean_before_add and self.transcript_root.exists():
            shutil.rmtree(self.transcript_root, ignore_errors=True)
        self.palace_path.parent.mkdir(parents=True, exist_ok=True)
        self.transcript_root.mkdir(parents=True, exist_ok=True)
        self.console.print(
            "   💡 Fidelity mode: add/search both use the internal vendored MemPalace package",
            style="cyan",
        )

    def _need_dual_perspective(self, speaker_a: str, speaker_b: str) -> bool:
        """
        MemPalace indexes the full conversation together, not separate per-speaker views.
        """
        _ = speaker_a, speaker_b
        return False

    async def _add_user_messages(
        self,
        conv: Conversation,
        messages: List[Dict[str, Any]],
        speaker: str,
        **kwargs,
    ) -> Any:
        """
        Index a full conversation into a conversation-scoped MemPalace wing.

        The `messages` and `speaker` parameters are part of the OnlineAPIAdapter
        interface; MemPalace instead uses the original conversation object.
        """
        _ = messages, speaker, kwargs
        await asyncio.to_thread(self._index_conversation_sync, conv)
        return None

    def _index_conversation_sync(self, conv: Conversation) -> None:
        wing = self._conversation_to_wing(conv.conversation_id)
        transcript_path = self._conversation_transcript_path(conv.conversation_id)
        self._write_conversation_transcript(conv, transcript_path)
        self.console.print(
            f"   📥 MemPalace add: conv={conv.conversation_id} wing={wing} file={transcript_path.name}",
            style="dim",
        )

        try:
            self._mine_convos(
                convo_dir=str(transcript_path.parent),
                palace_path=str(self.palace_path),
                wing=wing,
                agent=self.agent_name,
                extract_mode=self.extract_mode,
            )
        except Exception as exc:
            error_text = str(exc)
            if "onnx" in error_text.lower() or "readtimeout" in error_text.lower():
                raise RuntimeError(
                    "MemPalace/ChromaDB failed while downloading its default local "
                    "embedding model. Retry once after the model download completes, "
                    "or pre-warm the Chroma ONNX model cache before running evaluation."
                ) from exc
            raise
        self.console.print(
            f"   ✅ MemPalace add complete: conv={conv.conversation_id} via mine_convos(extract_mode={self.extract_mode})",
            style="green",
        )

    async def _search_single_user(
        self,
        query: str,
        conversation_id: str,
        user_id: str,
        top_k: int,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """
        Search conversation-scoped MemPalace memories.
        """
        _ = user_id, kwargs
        wing = self._conversation_to_wing(conversation_id)
        room = self.config.get("search", {}).get("room")
        max_distance = self.config.get("search", {}).get("max_distance", 0.0)
        question_id = str(kwargs.get("question_id") or f"{conversation_id}:{query}")
        self.console.print(
            f"   🔎 MemPalace search: conv={conversation_id} wing={wing} top_k={top_k} room={room or '*'}",
            style="dim",
        )

        t0 = time.perf_counter()
        raw_results = await asyncio.to_thread(
            self._search_memories,
            query,
            str(self.palace_path),
            wing,
            room,
            top_k,
            max_distance,
            self.use_closet_boost,
            self.use_keyword_predicate_boost,
        )
        retrieval_latency_ms = (time.perf_counter() - t0) * 1000.0

        if raw_results.get("error"):
            self.console.print(
                f"❌ MemPalace search error for {conversation_id}: {raw_results['error']}",
                style="red",
            )
            self._search_observations[question_id] = {
                "retrieval_latency_ms": retrieval_latency_ms,
                "scheduler_wait_ms": 0.0,
                "retrieval_route": "search_memories",
                "backend_mode": self._backend_mode_label(),
            }
            return []

        converted = []
        for hit in raw_results.get("results", []):
            room_name = hit.get("room", "general")
            source = hit.get("source_file", "?")
            content = f"[{room_name}] {hit.get('text', '').strip()}\nSource: {source}"
            converted.append(
                {
                    "content": content,
                    "score": hit.get("similarity", 0.0),
                    "user_id": conversation_id,
                    "metadata": hit,
                }
            )
        self._search_observations[question_id] = {
            "retrieval_latency_ms": retrieval_latency_ms,
            "scheduler_wait_ms": 0.0,
            "retrieval_route": "search_memories",
            "backend_mode": self._backend_mode_label(),
        }
        self.console.print(
            f"   ✅ MemPalace search returned {len(converted)} result(s) via search_memories",
            style="green",
        )
        return converted

    def _build_single_search_result(
        self,
        query: str,
        conversation_id: str,
        results: List[Dict[str, Any]],
        user_id: str,
        top_k: int,
        **kwargs,
    ) -> SearchResult:
        _ = user_id
        question_id = str(kwargs.get("question_id") or f"{conversation_id}:{query}")
        search_obs = self._search_observations.pop(question_id, {})
        formatted_context = self._format_results_context(results[:top_k])
        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=results,
            retrieval_metadata={
                "system": "mempalace",
                "top_k": top_k,
                "dual_perspective": False,
                "wing": self._conversation_to_wing(conversation_id),
                "formatted_context": formatted_context,
                "retrieval_latency_ms": search_obs.get("retrieval_latency_ms"),
                "scheduler_wait_ms": search_obs.get("scheduler_wait_ms"),
                "retrieval_route": search_obs.get("retrieval_route"),
                "backend_mode": search_obs.get("backend_mode"),
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
        _ = (
            results_a,
            results_b,
            speaker_a,
            speaker_b,
            speaker_a_user_id,
            speaker_b_user_id,
        )
        question_id = str(kwargs.get("question_id") or f"{conversation_id}:{query}")
        search_obs = self._search_observations.pop(question_id, {})
        formatted_context = self._format_results_context(all_results[:top_k])
        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=all_results,
            retrieval_metadata={
                "system": "mempalace",
                "top_k": top_k,
                "dual_perspective": False,
                "wing": self._conversation_to_wing(conversation_id),
                "formatted_context": formatted_context,
                "retrieval_latency_ms": search_obs.get("retrieval_latency_ms"),
                "scheduler_wait_ms": search_obs.get("scheduler_wait_ms"),
                "retrieval_route": search_obs.get("retrieval_route"),
                "backend_mode": search_obs.get("backend_mode"),
            },
        )

    def _get_answer_prompt(self) -> str:
        return self._prompts["online_api"]["default"]["answer_prompt_memos"]

    def get_system_info(self) -> Dict[str, Any]:
        return {
            "name": "MemPalace",
            "type": "local",
            "description": "MemPalace local ChromaDB-backed memory system",
            "adapter": "MemPalaceAdapter",
            "palace_path": str(self.palace_path),
            "build_closets": self.build_closets,
            "use_closet_boost": self.use_closet_boost,
            "use_keyword_predicate_boost": self.use_keyword_predicate_boost,
        }

    def _backend_mode_label(self) -> str:
        base = "drawer+closet+bm25" if self.use_closet_boost else "drawer+bm25"
        if self.use_keyword_predicate_boost:
            return f"{base}+kw_predicate_v5"
        return base

    def _conversation_to_wing(self, conversation_id: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_. -]+", "_", conversation_id).strip("_. -")
        if not normalized:
            normalized = "conversation"
        if normalized[0] == "_":
            normalized = f"conv{normalized}"
        if normalized[-1] == "_":
            normalized = normalized.rstrip("_") or "conversation"
        return normalized[:120]

    def _conversation_input_dir(self, conversation_id: str) -> Path:
        return self.transcript_root / self._conversation_to_wing(conversation_id)

    def _conversation_transcript_path(self, conversation_id: str) -> Path:
        wing = self._conversation_to_wing(conversation_id)
        return self._conversation_input_dir(conversation_id) / f"{wing}.txt"

    def _write_conversation_transcript(self, conv: Conversation, transcript_path: Path) -> None:
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript = self._build_transcript(conv)
        transcript_path.write_text(transcript, encoding="utf-8")

    def _build_transcript(self, conv: Conversation) -> str:
        turns = self._render_turns(conv)
        if not turns:
            return ""

        lines: List[str] = []
        for idx, turn in enumerate(turns):
            text = turn.strip()
            if not text:
                continue
            if idx % 2 == 0:
                lines.append(f"> {text}")
            else:
                lines.append(text)
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _render_turns(self, conv: Conversation) -> List[str]:
        if not conv.messages:
            return []

        turns: List[str] = []
        current_speaker = None
        current_lines: List[str] = []

        for msg in conv.messages:
            rendered = self._render_message(msg)
            if current_speaker is None or msg.speaker_name == current_speaker:
                current_speaker = msg.speaker_name
                current_lines.append(rendered)
                continue

            turns.append(" | ".join(current_lines))
            current_speaker = msg.speaker_name
            current_lines = [rendered]

        if current_lines:
            turns.append(" | ".join(current_lines))

        return turns

    def _render_message(self, msg: Message) -> str:
        speaker = msg.speaker_name or msg.speaker_id or "Unknown"
        content = " ".join((msg.content or "").split())
        if msg.timestamp:
            timestamp = msg.timestamp.isoformat()
            return f"[{timestamp}] {speaker}: {content}"
        return f"{speaker}: {content}"

    def _format_results_context(self, results: List[Dict[str, Any]]) -> str:
        if not results:
            return "(No memories found)"

        parts = []
        for idx, result in enumerate(results, 1):
            metadata = result.get("metadata", {})
            room = metadata.get("room", "general")
            similarity = metadata.get("similarity", result.get("score", 0.0))
            content = result.get("content", "")
            parts.append(
                f"Memory {idx} [room={room}, similarity={similarity}]:\n{content}"
            )
        return "\n\n".join(parts)

"""
MemPalace adapter - integrate local MemPalace retrieval into the evaluation framework.

This adapter intentionally keeps its MemPalace integration thin and internal:
conversation add uses the vendored ``evaluation.src.adapters.mempalace.convo_miner``
implementation and retrieval uses the vendored
``evaluation.src.adapters.mempalace.runtime_searcher`` implementation so evaluation
behavior stays aligned with the MemPalace pipeline used in this repository.
"""

import asyncio
import hashlib
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

from rich.console import Console

from evaluation.src.adapters.mempalace.convo_miner import detect_convo_room, mine_convos
from evaluation.src.adapters.mempalace.convo_closet_miner import (
    mine_convos_with_closets,
)
from evaluation.src.adapters.mempalace.palace import get_room_summaries_collection
from evaluation.src.adapters.mempalace.runtime_searcher import search_memories
from evaluation.src.adapters.online_base import OnlineAPIAdapter
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, Message, SearchResult


@register_adapter("mempalace")
class MemPalaceAdapter(OnlineAPIAdapter):
    """MemPalace local adapter."""

    PALACE_ROOMS = [
        "identity_sexuality",
        "career_education",
        "relationships_romance",
        "family_children",
        "health_wellness",
        "hobbies_creativity",
        "social_community",
        "home_living",
        "travel_places",
        "food_cooking",
        "money_finance",
        "emotions_mood",
        "media_entertainment",
        "general",
    ]

    def __init__(self, config: dict, output_dir: Path = None):
        super().__init__(config, output_dir)
        search_cfg = config.get("search", {})
        add_cfg = config.get("add", {})

        palace_path = config.get("palace_path")
        if palace_path:
            self.palace_path = Path(palace_path).expanduser()
        else:
            self.palace_path = self.output_dir / "mempalace_palace"

        self.agent_name = config.get("agent_name", "evaluation_mempalace")
        self.extract_mode = config.get("extract_mode", "exchange")
        self.clean_before_add = config.get("clean_before_add", False)
        raw_mode = search_cfg.get("retrieval_mode", search_cfg.get("locomo_search_mode", "raw"))
        self.retrieval_mode = str(raw_mode or "raw").strip().lower()
        if self.retrieval_mode not in {"raw", "hybrid", "palace"}:
            raise ValueError(
                "search.retrieval_mode must be one of: raw, hybrid, palace"
            )
        self.use_closet_boost = bool(search_cfg.get("use_closet_boost", False))
        self.use_bm25_rerank = bool(search_cfg.get("use_bm25_rerank", False))
        self.palace_room_top_k = int(search_cfg.get("palace_room_top_k", 3) or 3)
        self.build_closets = add_cfg.get(
            "build_closets", self.use_closet_boost
        )
        self.build_palace_room_summaries = bool(
            add_cfg.get("build_palace_room_summaries", False) or self.retrieval_mode == "palace"
        )
        self.palace_use_llm_summary = add_cfg.get("palace_use_llm_summary", True)
        self.palace_use_llm_room_assignment = add_cfg.get(
            "palace_use_llm_room_assignment",
            True,
        )
        self._mine_convos = (
            mine_convos_with_closets if self.build_closets else mine_convos
        )
        self._search_memories = search_memories
        self.transcript_root = self.output_dir / "mempalace_convo_inputs"
        self._search_observations: Dict[str, Dict[str, Any]] = {}
        self.console = Console()
        self._search_options = {
            "retrieval_mode": self.retrieval_mode,
            "use_bm25_rerank": self.use_bm25_rerank,
            "use_closet_boost": self.use_closet_boost,
            "max_distance": search_cfg.get("max_distance", 0.0),
            "palace_room_top_k": self.palace_room_top_k,
        }

        self.console.print(f"   Palace Path: {self.palace_path}", style="dim")
        self.console.print(f"   Transcript Staging: {self.transcript_root}", style="dim")
        self.console.print(f"   Agent Name: {self.agent_name}", style="dim")
        self.console.print(f"   Extract Mode: {self.extract_mode}", style="dim")
        self.console.print(f"   Retrieval Mode: {self.retrieval_mode}", style="dim")
        self.console.print(f"   Closet Boost: {'enabled' if self.use_closet_boost else 'disabled'}", style="dim")
        self.console.print(
            f"   BM25 Re-rank: {'enabled' if self.use_bm25_rerank else 'disabled'}",
            style="dim",
        )
        self.console.print(
            f"   Build Closets On Add: {'enabled' if self.build_closets else 'disabled'}",
            style="dim",
        )
        self.console.print(
            f"   Build Palace Room Summaries: {'enabled' if self.build_palace_room_summaries else 'disabled'}",
            style="dim",
        )
        if self.build_palace_room_summaries:
            self.console.print(
                f"   Palace LLM Summary: {'enabled' if self.palace_use_llm_summary else 'disabled'}",
                style="dim",
            )
            self.console.print(
                "   Palace LLM Room Assignment: "
                f"{'enabled' if self.palace_use_llm_room_assignment else 'disabled'}",
                style="dim",
            )
        add_pipeline_name = (
            "convo_closet_miner.mine_convos_with_closets()"
            if self.build_closets
            else "convo_miner.mine_convos()"
        )
        self.console.print(f"   Add Pipeline: {add_pipeline_name}", style="dim")
        self.console.print("   Search Pipeline: runtime_searcher.search_memories()", style="dim")

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
        session_records: List[Dict[str, Any]] = []
        if self.retrieval_mode == "palace":
            session_records = self._build_session_records(conv)
            if self.build_palace_room_summaries:
                await self._enrich_session_records_for_palace(session_records)
        await asyncio.to_thread(self._index_conversation_sync, conv, session_records)
        return None

    def _index_conversation_sync(self, conv: Conversation, session_records: List[Dict[str, Any]]) -> None:
        wing = self._conversation_to_wing(conv.conversation_id)
        transcript_dir = self._conversation_input_dir(conv.conversation_id)
        if self.retrieval_mode == "palace":
            self._write_session_transcripts(session_records, transcript_dir)
        else:
            transcript_path = self._conversation_transcript_path(conv.conversation_id)
            self._write_conversation_transcript_file(conv, transcript_path)
        self.console.print(
            (
                f"   📥 MemPalace add: conv={conv.conversation_id} wing={wing} sessions={len(session_records)}"
                if self.retrieval_mode == "palace"
                else f"   📥 MemPalace add: conv={conv.conversation_id} wing={wing} file={self._conversation_transcript_path(conv.conversation_id).name}"
            ),
            style="dim",
        )

        try:
            self._mine_convos(
                convo_dir=str(transcript_dir),
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
        if self.retrieval_mode == "palace" and self.build_palace_room_summaries:
            self._upsert_palace_room_docs_sync(wing, session_records)
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
        search_cfg = self.config.get("search", {})
        room = search_cfg.get("room")
        question_id = str(kwargs.get("question_id") or f"{conversation_id}:{query}")
        self.console.print(
            f"   🔎 MemPalace search: conv={conversation_id} wing={wing} top_k={top_k} "
            f"room={room or '*'} mode={self.retrieval_mode}",
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
            self._search_options,
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
                "retrieval_route": raw_results.get("retrieval_route", "search_memories"),
                "backend_mode": raw_results.get("backend_mode", self._backend_mode_label()),
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
            "retrieval_route": raw_results.get("retrieval_route", "search_memories"),
            "backend_mode": raw_results.get("backend_mode", self._backend_mode_label()),
        }
        self.console.print(
            f"   ✅ MemPalace search returned {len(converted)} result(s) via search_memories",
            style="green",
        )
        return converted

    def _build_session_records(self, conv: Conversation) -> List[Dict[str, Any]]:
        session_records: List[Dict[str, Any]] = []
        for session_id, session_messages in self._group_messages_by_session(conv):
            transcript_path = self._session_transcript_path(conv.conversation_id, session_id)
            transcript_text = self._build_transcript_from_messages(session_messages)
            routing_text = self._render_session_text(session_messages)
            if not transcript_text.strip():
                continue
            session_records.append(
                {
                    "session_id": session_id,
                    "messages": session_messages,
                    "transcript_path": transcript_path,
                    "transcript_text": transcript_text,
                    "routing_text": routing_text,
                    "message_count": len(session_messages),
                }
            )
        return session_records

    async def _enrich_session_records_for_palace(self, session_records: List[Dict[str, Any]]) -> None:
        for record in session_records:
            session_text = record.get("routing_text", "")
            if not session_text.strip():
                record["summary"] = ""
                record["room"] = "general"
                continue
            summary = await self._summarize_session_for_palace(session_text)
            room = await self._assign_palace_room(summary, session_text)
            record["summary"] = summary
            record["room"] = room

    def _group_messages_by_session(self, conv: Conversation) -> List[tuple[str, List[Message]]]:
        grouped: Dict[str, List[Message]] = {}
        order: List[str] = []
        for idx, msg in enumerate(conv.messages):
            session_id = str(msg.metadata.get("session") or f"session_{idx:04d}")
            if session_id not in grouped:
                grouped[session_id] = []
                order.append(session_id)
            grouped[session_id].append(msg)
        return [(session_id, grouped[session_id]) for session_id in order]

    def _render_session_text(self, messages: List[Message]) -> str:
        lines = []
        for msg in messages:
            speaker = msg.speaker_name or msg.speaker_id or "Unknown"
            content = " ".join((msg.content or "").split())
            if content:
                lines.append(f'{speaker} said, "{content}"')
        return "\n".join(lines)

    async def _summarize_session_for_palace(self, session_text: str) -> str:
        fallback = self._fallback_session_summary(session_text)
        if not self.palace_use_llm_summary:
            return fallback
        prompt = (
            "Summarize the following conversation session for retrieval routing. "
            "Focus on concrete facts, people, events, plans, preferences, places, and time cues. "
            "Return one compact paragraph under 120 words.\n\n"
            f"Conversation:\n{session_text[:5000]}"
        )
        try:
            summary = (await self.llm_provider.generate(prompt=prompt, temperature=0)).strip()
            return summary or fallback
        except Exception:
            return fallback

    async def _assign_palace_room(self, summary: str, session_text: str) -> str:
        fallback = detect_convo_room(session_text)
        if not self.palace_use_llm_room_assignment:
            return fallback
        room_list = "\n".join(f"- {room}" for room in self.PALACE_ROOMS)
        prompt = (
            "Read this conversation summary and assign it to exactly one room from the list below. "
            "Reply with only the room name.\n\n"
            f"Rooms:\n{room_list}\n\n"
            f"Summary:\n{summary[:2000]}"
        )
        try:
            raw = (await self.llm_provider.generate(prompt=prompt, temperature=0)).strip().lower()
        except Exception:
            return fallback
        for room in self.PALACE_ROOMS:
            if raw == room or room in raw:
                return room
        return fallback

    def _fallback_session_summary(self, session_text: str) -> str:
        compact = " ".join(session_text.split())
        return compact[:600]

    def _upsert_palace_room_docs_sync(self, wing: str, session_records: List[Dict[str, Any]]) -> None:
        summaries_col = get_room_summaries_collection(str(self.palace_path))
        try:
            summaries_col.delete(where={"wing": wing})
        except Exception:
            pass

        documents = []
        ids = []
        metadatas = []
        for entry in session_records:
            summary = str(entry.get("summary") or "").strip()
            if not summary:
                continue
            documents.append(summary)
            session_id = str(entry["session_id"])
            ids.append(
                "room_summary_"
                + hashlib.sha256(f"{wing}:{session_id}".encode()).hexdigest()[:24]
            )
            metadatas.append(
                {
                    "wing": wing,
                    "room": entry.get("room", "general"),
                    "session_id": session_id,
                    "message_count": entry["message_count"],
                    "source_file": str(entry["transcript_path"]),
                }
            )
        if documents:
            summaries_col.upsert(documents=documents, ids=ids, metadatas=metadatas)

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
            "retrieval_mode": self.retrieval_mode,
            "use_closet_boost": self.use_closet_boost,
            "use_bm25_rerank": self.use_bm25_rerank,
            "build_palace_room_summaries": self.build_palace_room_summaries,
        }

    def _backend_mode_label(self) -> str:
        base = self.retrieval_mode
        if self.retrieval_mode == "raw" and self.use_closet_boost:
            base = f"{base}+closet"
        if self.retrieval_mode in {"hybrid", "palace"}:
            base = f"{base}+v5"
        if self.retrieval_mode == "raw" and self.use_bm25_rerank:
            base = f"{base}+bm25"
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

    def _session_transcript_path(self, conversation_id: str, session_id: str) -> Path:
        safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id)).strip("_.-") or "session"
        return self._conversation_input_dir(conversation_id) / f"{safe_session}.txt"

    def _write_session_transcripts(self, session_records: List[Dict[str, Any]], transcript_dir: Path) -> None:
        transcript_dir.mkdir(parents=True, exist_ok=True)
        for stale_file in transcript_dir.glob("*.txt"):
            stale_file.unlink(missing_ok=True)
        for record in session_records:
            transcript_path = record["transcript_path"]
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text(record["transcript_text"], encoding="utf-8")

    def _write_conversation_transcript_file(self, conv: Conversation, transcript_path: Path) -> None:
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        for stale_file in transcript_path.parent.glob("*.txt"):
            stale_file.unlink(missing_ok=True)
        transcript_path.write_text(self._build_transcript(conv), encoding="utf-8")

    def _build_transcript(self, conv: Conversation) -> str:
        return self._build_transcript_from_messages(conv.messages)

    def _build_transcript_from_messages(self, messages: List[Message]) -> str:
        turns = self._render_turns(messages)
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

    def _render_turns(self, messages: List[Message]) -> List[str]:
        if not messages:
            return []

        turns: List[str] = []
        current_speaker = None
        current_lines: List[str] = []

        for msg in messages:
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

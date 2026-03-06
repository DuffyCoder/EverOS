"""
Agent Experience Extractor for EverMemOS

Extracts AgentCase from agent MemCells (OpenAI chat completion format).

Pipeline:
1. Pre-compress: Build a structured list from raw messages. If total tool content
   exceeds a threshold, use LLM to compress tool call inputs/outputs in chunks.
2. Single LLM call: Extract one experience with task_intent, approach, quality_score.
3. Compute embedding on task_intent for retrieval.

OpenAI message format:
- role="user": User input (content only)
- role="assistant" with tool_calls: Agent decides to call tools
- role="tool" with tool_call_id: Tool execution result
- role="assistant" without tool_calls: Agent final response
"""

import copy
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from common_utils.json_utils import parse_json_response

from memory_layer.llm.llm_provider import LLMProvider
from memory_layer.memory_extractor.base_memory_extractor import (
    MemoryExtractor,
    MemoryExtractRequest,
)
from memory_layer.prompts import get_prompt_by
from api_specs.memory_types import (
    MemCell,
    RawDataType,
    AgentCase,
)
from api_specs.memory_models import MemoryType
from agentic_layer.vectorize_service import get_vectorize_service
from core.di.utils import get_bean_by_type
from core.component.llm.tokenizer.tokenizer_factory import TokenizerFactory
from core.observation.logger import get_logger

logger = get_logger(__name__)

# LLM pre-compression chunk size (tokens)
PRE_COMPRESS_CHUNK_SIZE = 16000


@dataclass
class AgentCaseExtractRequest(MemoryExtractRequest):
    """Request for extracting AgentCase from a MemCell."""

    pass


class AgentCaseExtractor(MemoryExtractor):
    """
    Extracts AgentCase from an agent MemCell.

    Each MemCell produces at most one AgentCase.
    Multiple conversation turns solving the same problem are synthesized into one record.

    Pipeline:
    1. Pre-compress: Build structured list, LLM-compress tool content if over threshold
    2. Single LLM call: extract one experience record
    3. Compute embedding on task_intent for retrieval
    """

    def __init__(
        self,
        llm_provider: Optional[LLMProvider] = None,
        experience_compress_prompt: Optional[str] = None,
        tool_pre_compress_prompt: Optional[str] = None,
        pre_compress_chunk_size: int = PRE_COMPRESS_CHUNK_SIZE,
    ):
        super().__init__(MemoryType.AGENT_CASE)
        self.llm_provider = llm_provider
        self.experience_compress_prompt = experience_compress_prompt or get_prompt_by(
            "AGENT_CASE_COMPRESS_PROMPT"
        )
        self.tool_pre_compress_prompt = tool_pre_compress_prompt or get_prompt_by(
            "AGENT_TOOL_PRE_COMPRESS_PROMPT"
        )
        self.pre_compress_chunk_size = pre_compress_chunk_size

    @staticmethod
    def _json_default(obj: Any) -> Any:
        """JSON encoder default for non-serializable types."""
        if isinstance(obj, datetime):
            return obj.isoformat()
        return str(obj)

    @classmethod
    def _get_tokenizer(cls):
        """Get the shared tokenizer from tokenizer factory."""
        tokenizer_factory: TokenizerFactory = get_bean_by_type(TokenizerFactory)
        return tokenizer_factory.get_tokenizer_from_tiktoken("o200k_base")

    @classmethod
    def _count_tokens(cls, text: str) -> int:
        """Count tokens in a string."""
        if not text:
            return 0
        tokenizer = cls._get_tokenizer()
        return len(tokenizer.encode(text))

    @classmethod
    def _calc_tool_content_size(cls, msg: Dict[str, Any]) -> int:
        """Calculate the tool-related content size of a message (in tokens)."""
        role = msg.get("role", "")
        if role == "tool":
            return cls._count_tokens(msg.get("content", ""))
        if role == "assistant" and msg.get("tool_calls"):
            return sum(
                cls._count_tokens(tc.get("function", {}).get("arguments", ""))
                for tc in msg["tool_calls"]
            )
        return 0

    def _collect_tool_call_groups(
        self, items: List[Dict[str, Any]]
    ) -> List[List[int]]:
        """Collect atomic tool call groups from the message list.

        Each group is an assistant message with tool_calls + its corresponding
        tool response messages. These must not be split across chunks.

        Returns:
            List of groups, where each group is a list of message indices.
        """
        groups: List[List[int]] = []
        i = 0
        while i < len(items):
            msg = items[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                # Start a new group: assistant with tool_calls
                group = [i]
                # Collect all following tool responses
                j = i + 1
                while j < len(items) and items[j].get("role") == "tool":
                    group.append(j)
                    j += 1
                groups.append(group)
                i = j
            else:
                i += 1
        return groups

    def _calc_group_size(self, items: List[Dict[str, Any]], group: List[int]) -> int:
        """Calculate total tool content tokens of a group."""
        return sum(self._calc_tool_content_size(items[idx]) for idx in group)

    async def _pre_compress_to_list(
        self, original_data: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Pre-compress tool content using recursive LLM compression.

        If total tool content <= pre_compress_chunk_size, return as-is.
        Otherwise, split tool call groups into chunks and compress recursively:
        each round uses previously compressed output as context for the next chunk.
        """
        items = copy.deepcopy(original_data)

        # Collect atomic groups: [assistant_with_tool_calls, tool_response, ...]
        tool_call_groups = self._collect_tool_call_groups(items)
        if not tool_call_groups:
            return items

        total_size = sum(self._calc_group_size(items, g) for g in tool_call_groups)
        if total_size <= self.pre_compress_chunk_size:
            logger.debug(
                f"[AgentCaseExtractor] Tool content {total_size} tokens "
                f"<= {self.pre_compress_chunk_size}, no compression needed"
            )
            return items

        # Split groups into chunks of pre_compress_chunk_size
        chunks: List[List[List[int]]] = []
        current_chunk: List[List[int]] = []
        current_size = 0

        for group in tool_call_groups:
            group_size = self._calc_group_size(items, group)
            if current_chunk and current_size + group_size > self.pre_compress_chunk_size:
                chunks.append(current_chunk)
                current_chunk = [group]
                current_size = group_size
            else:
                current_chunk.append(group)
                current_size += group_size

        if current_chunk:
            chunks.append(current_chunk)

        logger.debug(
            f"[AgentCaseExtractor] Recursive compression: "
            f"{len(chunks)} chunks, {total_size} total tokens"
        )

        # Recursive compression: each round uses previous output as context
        compressed_context: List[Dict[str, Any]] = []
        all_compressed: List[Dict[str, Any]] = []

        for round_idx, chunk_groups in enumerate(chunks):
            chunk_indices = [idx for group in chunk_groups for idx in group]
            chunk_msgs = [items[idx] for idx in chunk_indices]

            compressed = await self._compress_tool_chunk(compressed_context, chunk_msgs)

            if compressed is not None:
                compressed_context.extend(compressed)
                all_compressed.extend(compressed)
            else:
                logger.warning(
                    f"[AgentCaseExtractor] Round {round_idx + 1} compression failed, "
                    "keeping original messages"
                )
                compressed_context.extend(chunk_msgs)
                all_compressed.extend(chunk_msgs)

        # Replace tool-group messages with compressed results
        all_tool_indices = sorted(idx for group in tool_call_groups for idx in group)

        if len(all_compressed) == len(all_tool_indices):
            for i, idx in enumerate(all_tool_indices):
                items[idx] = all_compressed[i]
        else:
            logger.warning(
                f"[AgentCaseExtractor] Compressed count {len(all_compressed)} "
                f"!= tool message count {len(all_tool_indices)}, keeping originals"
            )

        return items

    async def _compress_tool_chunk(
        self,
        context: List[Dict[str, Any]],
        new_messages: List[Dict[str, Any]],
    ) -> Optional[List[Dict[str, Any]]]:
        """Compress a chunk of tool-related messages via LLM, with context.

        Args:
            context: Previously compressed messages (read-only context).
            new_messages: New messages to compress.

        Returns:
            Compressed version of new_messages (same count), or None on failure.
        """
        prompt = self.tool_pre_compress_prompt.format(
            context_json=json.dumps(context, ensure_ascii=False, indent=2, default=self._json_default),
            messages_json=json.dumps(new_messages, ensure_ascii=False, indent=2, default=self._json_default),
            new_count=len(new_messages),
        )

        for i in range(3):
            try:
                resp = await self.llm_provider.generate(prompt)
                data = parse_json_response(resp)
                if (
                    data
                    and "compressed_messages" in data
                    and isinstance(data["compressed_messages"], list)
                    and len(data["compressed_messages"]) == len(new_messages)
                ):
                    return data["compressed_messages"]
                logger.warning(
                    f"[AgentCaseExtractor] Tool pre-compress retry {i+1}/3: "
                    f"invalid response format"
                )
            except Exception as e:
                logger.warning(
                    f"[AgentCaseExtractor] Tool pre-compress retry {i+1}/3: {e}"
                )

        return None

    async def _compress_experience(
        self, messages_json: str
    ) -> Optional[Dict[str, Any]]:
        """Single LLM call to extract one experience with task_intent + approach + quality_score.

        Returns:
            The experience dict, or None if the LLM determined no experience is worth extracting.
        """
        prompt = self.experience_compress_prompt.format(messages=messages_json)

        for i in range(5):
            try:
                resp = await self.llm_provider.generate(prompt)
                data = parse_json_response(resp)
                if data and "task_intent" in data:
                    # LLM returns {"task_intent": null} when no experience is worth extracting
                    if data["task_intent"] is None:
                        return None
                    if data.get("task_intent"):
                        return data
                logger.warning(
                    f"[AgentCaseExtractor] Compress retry {i+1}/5: "
                    f"missing or invalid 'task_intent' field"
                )
            except Exception as e:
                logger.warning(
                    f"[AgentCaseExtractor] Compress retry {i+1}/5: {e}"
                )

        raise Exception("Agent experience extraction failed after 5 retries")

    @staticmethod
    def _clamp_quality_score(value: Any) -> Optional[float]:
        """Clamp quality_score to [0.0, 1.0], return None if invalid."""
        if value is None:
            return None
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_single_turn_no_tools(messages: List[Dict[str, Any]]) -> bool:
        """Check if the conversation is a single-turn exchange without tool calls.

        A single turn means only one user message exists. If the conversation
        contains any tool_calls or tool responses, it is NOT single-turn-no-tools
        (tool-use conversations are always worth extracting).

        Returns:
            True if the conversation should be skipped (single turn, no tools).
        """
        has_tool_calls = any(
            msg.get("tool_calls") or msg.get("role") == "tool"
            for msg in messages
        )
        if has_tool_calls:
            return False

        user_count = sum(1 for msg in messages if msg.get("role") == "user")
        return user_count < 2

    async def _compute_embedding(self, text: str) -> Optional[Dict[str, Any]]:
        """Compute embedding for the task intent."""
        try:
            if not text:
                return None
            vs = get_vectorize_service()
            vec = await vs.get_embedding(text)
            return {
                "embedding": vec.tolist() if hasattr(vec, "tolist") else list(vec),
                "vector_model": vs.get_model_name(),
            }
        except Exception as e:
            logger.error(f"[AgentCaseExtractor] Embedding failed: {e}")
            return None

    async def extract_memory(
        self, request: MemoryExtractRequest
    ) -> Optional[AgentCase]:
        """
        Extract AgentCase from a MemCell.

        Pipeline:
        1. Pre-compress: build structured list, LLM-compress tool content if over threshold
        2. Single LLM call: extract one experience record
        3. Compute embedding on task_intent

        Args:
            request: Memory extraction request containing an agent MemCell.

        Returns:
            AgentCase object, or None if extraction fails or is skipped.
        """
        memcell = request.memcell
        if not memcell:
            return None

        if memcell.type != RawDataType.AGENTCONVERSATION:
            logger.warning(
                f"[AgentCaseExtractor] Expected AGENT_CONVERSATION, got {memcell.type}"
            )
            return None

        try:
            original_data = memcell.original_data or []

            # Pre-filter: skip single-turn conversations without tool calls.
            # These rarely capture reusable problem-solving processes.
            if self._is_single_turn_no_tools(original_data):
                logger.info(
                    "[AgentCaseExtractor] Single-turn conversation without tool calls, skipping"
                )
                return None

            # Step 1: Pre-compress to JSON list (LLM-based if tool content is large)
            pre_compressed_list = await self._pre_compress_to_list(original_data)
            messages_json = json.dumps(pre_compressed_list, ensure_ascii=False, indent=2, default=self._json_default)

            logger.debug(
                f"[AgentCaseExtractor] Pre-compressed: "
                f"{len(pre_compressed_list)} items, {len(messages_json)} chars"
            )

            # Step 2: Single LLM call — returns experience dict or None
            exp_dict = await self._compress_experience(messages_json)

            if not exp_dict:
                logger.info(
                    "[AgentCaseExtractor] No actionable experience extracted, skipping"
                )
                return None

            # Build AgentCase
            experience = AgentCase(
                task_intent=exp_dict.get("task_intent", ""),
                approach=exp_dict.get("approach", ""),
                quality_score=self._clamp_quality_score(exp_dict.get("quality_score")),
            )

            # Step 3: Compute embedding on task_intent for retrieval
            embedding_data = await self._compute_embedding(experience.task_intent)

            # Store embedding in memcell extend field for downstream persistence
            if embedding_data:
                if memcell.extend is None:
                    memcell.extend = {}
                memcell.extend["agent_case_embedding"] = embedding_data

            # Attach to memcell
            memcell.agent_case = experience

            logger.debug(
                f"[AgentCaseExtractor] Extracted: "
                f"intent='{experience.task_intent[:80]}'"
            )

            return experience

        except Exception as e:
            logger.error(f"[AgentCaseExtractor] Extraction failed: {e}")
            return None

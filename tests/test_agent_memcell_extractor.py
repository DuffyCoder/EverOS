"""
AgentMemCellExtractor Unit Tests

Tests agent turn-boundary-aware MemCell splitting:
- Skipping intermediate agent steps (tool_call, tool_response)
- Only splitting at complete agent responses
- Flush mode packing all messages
- History boundary validation
- Helper method correctness

Usage:
    PYTHONPATH=src pytest tests/test_agent_memcell_extractor.py -v
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta
from typing import List, Dict, Any

from common_utils.datetime_utils import get_now_with_timezone
from api_specs.dtos import RawData
from api_specs.memory_types import RawDataType

from memory_layer.memcell_extractor.agent_memcell_extractor import (
    AgentMemCellExtractor,
    AgentMemCellExtractRequest,
)
from memory_layer.memcell_extractor.base_memcell_extractor import (
    MemCellExtractRequest,
    StatusResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_TIME = get_now_with_timezone() - timedelta(hours=1)


def _ts(offset_minutes: int) -> str:
    return (BASE_TIME + timedelta(minutes=offset_minutes)).isoformat()


def _user_msg(content: str, offset: int = 0) -> Dict[str, Any]:
    return {
        "role": "user",
        "content": content,
        "speaker_name": "User",
        "timestamp": _ts(offset),
    }


def _assistant_msg(content: str, offset: int = 0) -> Dict[str, Any]:
    """Complete assistant response (no tool_calls)."""
    return {
        "role": "assistant",
        "content": content,
        "speaker_name": "Assistant",
        "timestamp": _ts(offset),
    }


def _tool_call_msg(content: str = "", offset: int = 0) -> Dict[str, Any]:
    """Intermediate assistant message WITH tool_calls."""
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [{"id": "call_1", "function": {"name": "search", "arguments": "{}"}}],
        "speaker_name": "Assistant",
        "timestamp": _ts(offset),
    }


def _tool_response_msg(content: str = "tool result", offset: int = 0) -> Dict[str, Any]:
    """Tool execution result."""
    return {
        "role": "tool",
        "content": content,
        "tool_call_id": "call_1",
        "speaker_name": "Tool",
        "timestamp": _ts(offset),
    }


def _raw(msg: Dict[str, Any], data_id: str = "d") -> RawData:
    return RawData(content=msg, data_id=data_id)


def _raw_list(msgs: List[Dict[str, Any]], prefix: str = "d") -> List[RawData]:
    return [RawData(content=m, data_id=f"{prefix}_{i}") for i, m in enumerate(msgs)]


def _make_request(
    history_msgs: List[Dict[str, Any]],
    new_msgs: List[Dict[str, Any]],
    flush: bool = False,
) -> MemCellExtractRequest:
    return MemCellExtractRequest(
        history_raw_data_list=_raw_list(history_msgs, "h"),
        new_raw_data_list=_raw_list(new_msgs, "n"),
        user_id_list=["user1"],
        group_id="test_group",
        flush=flush,
    )


def _build_extractor() -> AgentMemCellExtractor:
    """Build extractor with a mocked LLM provider (boundary detection returns should_wait)."""
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(
        return_value='{"should_end": false, "should_wait": true, "reasoning": "mock", "confidence": 1.0}'
    )
    return AgentMemCellExtractor(llm_provider=mock_llm)


def _build_extractor_should_end() -> AgentMemCellExtractor:
    """Build extractor with LLM that signals boundary (should_end=True)."""
    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(
        return_value='{"should_end": true, "should_wait": false, "reasoning": "topic changed", "confidence": 0.9, "topic_summary": "agent task"}'
    )
    return AgentMemCellExtractor(llm_provider=mock_llm)


# ---------------------------------------------------------------------------
# Tests: Helper Methods
# ---------------------------------------------------------------------------


class TestHelperMethods:
    """Test _is_intermediate_agent_step, _is_complete_agent_response, _filter_for_boundary_detection."""

    def setup_method(self):
        self.extractor = _build_extractor()

    def test_tool_response_is_intermediate(self):
        assert self.extractor._is_intermediate_agent_step(_tool_response_msg()) is True

    def test_tool_call_is_intermediate(self):
        assert self.extractor._is_intermediate_agent_step(_tool_call_msg()) is True

    def test_user_msg_not_intermediate(self):
        assert self.extractor._is_intermediate_agent_step(_user_msg("hi")) is False

    def test_final_assistant_not_intermediate(self):
        assert self.extractor._is_intermediate_agent_step(_assistant_msg("done")) is False

    def test_complete_agent_response_true(self):
        assert self.extractor._is_complete_agent_response(_assistant_msg("done")) is True

    def test_complete_agent_response_false_for_tool_call(self):
        assert self.extractor._is_complete_agent_response(_tool_call_msg()) is False

    def test_complete_agent_response_false_for_user(self):
        assert self.extractor._is_complete_agent_response(_user_msg("hi")) is False

    def test_complete_agent_response_false_for_tool_response(self):
        assert self.extractor._is_complete_agent_response(_tool_response_msg()) is False

    def test_filter_for_boundary_detection(self):
        msgs = [
            _user_msg("q1"),
            _tool_call_msg("thinking"),
            _tool_response_msg("result"),
            _assistant_msg("answer"),
        ]
        filtered = self.extractor._filter_for_boundary_detection(msgs)
        assert len(filtered) == 2
        assert filtered[0]["role"] == "user"
        assert filtered[1]["role"] == "assistant"
        assert "tool_calls" not in filtered[1]


# ---------------------------------------------------------------------------
# Tests: Guard 1 - Skip intermediate agent steps
# ---------------------------------------------------------------------------


class TestGuard1SkipIntermediate:
    """New message is intermediate -> return (None, should_wait=True)."""

    def setup_method(self):
        self.extractor = _build_extractor()

    @pytest.mark.asyncio
    async def test_skip_tool_call_message(self):
        request = _make_request(
            history_msgs=[_user_msg("hello", 0)],
            new_msgs=[_tool_call_msg("calling tool", 1)],
        )
        memcell, status = await self.extractor.extract_memcell(request)
        assert memcell is None
        assert status.should_wait is True

    @pytest.mark.asyncio
    async def test_skip_tool_response_message(self):
        request = _make_request(
            history_msgs=[_user_msg("hello", 0)],
            new_msgs=[_tool_response_msg("result", 1)],
        )
        memcell, status = await self.extractor.extract_memcell(request)
        assert memcell is None
        assert status.should_wait is True

    @pytest.mark.asyncio
    async def test_skip_multiple_new_last_is_tool(self):
        """Multiple new messages, last one is tool response -> skip."""
        request = _make_request(
            history_msgs=[_user_msg("hello", 0)],
            new_msgs=[_user_msg("more", 1), _tool_response_msg("result", 2)],
        )
        memcell, status = await self.extractor.extract_memcell(request)
        assert memcell is None
        assert status.should_wait is True


# ---------------------------------------------------------------------------
# Tests: Guard 2 - Flush
# ---------------------------------------------------------------------------


class TestGuard2Flush:
    """Flush mode packs all messages into one MemCell."""

    def setup_method(self):
        self.extractor = _build_extractor()

    @pytest.mark.asyncio
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.record_boundary_detection")
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.record_memcell_extracted")
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.get_space_id_for_metrics", return_value="test")
    async def test_flush_packs_all_messages(self, mock_space, mock_record, mock_bd):
        """Flush creates MemCell from all history + new messages."""
        request = _make_request(
            history_msgs=[
                _user_msg("hello", 0),
                _tool_call_msg("thinking", 1),
                _tool_response_msg("result", 2),
                _assistant_msg("answer", 3),
            ],
            new_msgs=[_user_msg("follow up", 4)],
            flush=True,
        )
        memcell, status = await self.extractor.extract_memcell(request)
        assert memcell is not None
        assert status.should_wait is False
        # All 5 messages should be packed
        assert len(memcell.original_data) == 5
        assert memcell.type == RawDataType.AGENTCONVERSATION

    @pytest.mark.asyncio
    async def test_flush_empty_messages(self):
        """Flush with no messages returns None."""
        request = _make_request(history_msgs=[], new_msgs=[], flush=True)
        memcell, status = await self.extractor.extract_memcell(request)
        assert memcell is None
        assert status.should_wait is True

    @pytest.mark.asyncio
    async def test_flush_skips_guard1(self):
        """Even if last new message is intermediate, flush still works
        because guard 1 runs before guard 2 and would skip.
        But if new_raw_data_list last is intermediate AND flush=True,
        guard 1 triggers first -> returns (None, should_wait).
        """
        request = _make_request(
            history_msgs=[_user_msg("hello", 0), _assistant_msg("answer", 1)],
            new_msgs=[_tool_call_msg("thinking", 2)],
            flush=True,
        )
        # Guard 1 fires first because last new message is intermediate
        memcell, status = await self.extractor.extract_memcell(request)
        assert memcell is None
        assert status.should_wait is True


# ---------------------------------------------------------------------------
# Tests: Guard 3 - History must end at complete agent response
# ---------------------------------------------------------------------------


class TestGuard3HistoryBoundary:
    """History doesn't end at complete agent response -> wait."""

    def setup_method(self):
        self.extractor = _build_extractor()

    @pytest.mark.asyncio
    async def test_history_ends_at_user_msg_waits(self):
        request = _make_request(
            history_msgs=[_user_msg("hello", 0)],
            new_msgs=[_user_msg("more", 1)],
        )
        memcell, status = await self.extractor.extract_memcell(request)
        assert memcell is None
        assert status.should_wait is True

    @pytest.mark.asyncio
    async def test_history_ends_at_tool_call_waits(self):
        request = _make_request(
            history_msgs=[_user_msg("hello", 0), _tool_call_msg("thinking", 1)],
            new_msgs=[_assistant_msg("done", 2)],
        )
        memcell, status = await self.extractor.extract_memcell(request)
        assert memcell is None
        assert status.should_wait is True

    @pytest.mark.asyncio
    async def test_history_ends_at_tool_response_waits(self):
        request = _make_request(
            history_msgs=[
                _user_msg("hello", 0),
                _tool_call_msg("thinking", 1),
                _tool_response_msg("result", 2),
            ],
            new_msgs=[_assistant_msg("done", 3)],
        )
        memcell, status = await self.extractor.extract_memcell(request)
        assert memcell is None
        assert status.should_wait is True


# ---------------------------------------------------------------------------
# Tests: Delegation to parent (all guards pass)
# ---------------------------------------------------------------------------


class TestDelegationToParent:
    """When all guards pass, parent's extract_memcell is called."""

    @pytest.mark.asyncio
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.record_boundary_detection")
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.record_memcell_extracted")
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.get_space_id_for_metrics", return_value="test")
    async def test_valid_boundary_delegates_to_parent_llm_no_boundary(
        self, mock_space, mock_record, mock_bd
    ):
        """History ends at assistant, new msg is user -> delegates to parent.
        Mock LLM returns should_wait -> no MemCell."""
        extractor = _build_extractor()
        request = _make_request(
            history_msgs=[_user_msg("hello", 0), _assistant_msg("hi there", 1)],
            new_msgs=[_user_msg("what is 2+2?", 5)],
        )
        memcell, status = await extractor.extract_memcell(request)
        # LLM says no boundary -> should_wait
        assert memcell is None
        assert status.should_wait is True

    @pytest.mark.asyncio
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.record_boundary_detection")
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.record_memcell_extracted")
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.get_space_id_for_metrics", return_value="test")
    async def test_valid_boundary_llm_detects_end(self, mock_space, mock_record, mock_bd):
        """LLM detects boundary -> MemCell is created from history."""
        extractor = _build_extractor_should_end()
        request = _make_request(
            history_msgs=[_user_msg("hello", 0), _assistant_msg("hi there", 1)],
            new_msgs=[_user_msg("new topic entirely", 30)],
        )
        memcell, status = await extractor.extract_memcell(request)
        assert memcell is not None
        assert status.should_wait is False
        # MemCell should contain only history messages
        assert len(memcell.original_data) == 2
        assert memcell.type == RawDataType.AGENTCONVERSATION

    @pytest.mark.asyncio
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.record_boundary_detection")
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.record_memcell_extracted")
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.get_space_id_for_metrics", return_value="test")
    async def test_empty_history_delegates(self, mock_space, mock_record, mock_bd):
        """Empty history + user new msg -> delegates to parent (guard 3 skips for empty history).
        Parent's _detect_boundary returns should_wait=False for first messages (no history)."""
        extractor = _build_extractor()
        request = _make_request(
            history_msgs=[],
            new_msgs=[_user_msg("hello", 0)],
        )
        memcell, status = await extractor.extract_memcell(request)
        # No history -> _detect_boundary returns should_end=False, should_wait=False
        assert memcell is None
        assert status.should_wait is False


# ---------------------------------------------------------------------------
# Tests: Force split respects turn boundaries
# ---------------------------------------------------------------------------


class TestForceSplit:
    """Force split via hard limits only happens when history ends at valid boundary."""

    @pytest.mark.asyncio
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.record_boundary_detection")
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.record_memcell_extracted")
    @patch("memory_layer.memcell_extractor.conv_memcell_extractor.get_space_id_for_metrics", return_value="test")
    async def test_force_split_at_valid_boundary(self, mock_space, mock_record, mock_bd):
        """When history is valid and exceeds message limit, force split creates MemCell."""
        extractor = _build_extractor()
        extractor.hard_message_limit = 3  # Low limit to trigger force split

        request = _make_request(
            history_msgs=[_user_msg("q1", 0), _assistant_msg("a1", 1)],
            new_msgs=[_user_msg("q2", 2)],
        )
        # 2 history + 1 new = 3 >= hard_message_limit
        memcell, status = await extractor.extract_memcell(request)
        assert memcell is not None
        assert status.should_wait is False
        assert len(memcell.original_data) == 2  # Only history

    @pytest.mark.asyncio
    async def test_force_split_deferred_invalid_boundary(self):
        """When history ends at user msg and exceeds limit, guard 3 defers the split."""
        extractor = _build_extractor()
        extractor.hard_message_limit = 3

        request = _make_request(
            history_msgs=[_user_msg("q1", 0), _user_msg("q2", 1)],
            new_msgs=[_assistant_msg("a1", 2)],
        )
        # History ends at user msg -> guard 3 blocks -> (None, should_wait)
        memcell, status = await extractor.extract_memcell(request)
        assert memcell is None
        assert status.should_wait is True


# ---------------------------------------------------------------------------
# Tests: Multi-turn agent conversation flow simulation
# ---------------------------------------------------------------------------


class TestMultiTurnFlow:
    """Simulate a complete agent turn: user -> tool_call -> tool_response -> assistant."""

    @pytest.mark.asyncio
    async def test_full_agent_turn_sequence(self):
        """Simulate messages arriving one by one; only the final assistant triggers processing."""
        extractor = _build_extractor()
        results = []

        # Message 1: user asks a question
        req1 = _make_request(history_msgs=[], new_msgs=[_user_msg("search for X", 0)])
        # Empty history -> guard 3 skips, delegates to parent
        # Parent: no history, first messages -> LLM says should_wait
        with patch(
            "memory_layer.memcell_extractor.conv_memcell_extractor.record_boundary_detection"
        ), patch(
            "memory_layer.memcell_extractor.conv_memcell_extractor.record_memcell_extracted"
        ), patch(
            "memory_layer.memcell_extractor.conv_memcell_extractor.get_space_id_for_metrics",
            return_value="test",
        ):
            r1 = await extractor.extract_memcell(req1)
        results.append(r1)

        # Message 2: agent makes tool call (intermediate)
        req2 = _make_request(
            history_msgs=[_user_msg("search for X", 0)],
            new_msgs=[_tool_call_msg("calling search", 1)],
        )
        r2 = await extractor.extract_memcell(req2)
        results.append(r2)

        # Message 3: tool response (intermediate)
        req3 = _make_request(
            history_msgs=[_user_msg("search for X", 0), _tool_call_msg("calling search", 1)],
            new_msgs=[_tool_response_msg("search results", 2)],
        )
        r3 = await extractor.extract_memcell(req3)
        results.append(r3)

        # Message 4: final assistant response
        req4 = _make_request(
            history_msgs=[
                _user_msg("search for X", 0),
                _tool_call_msg("calling search", 1),
                _tool_response_msg("search results", 2),
            ],
            new_msgs=[_assistant_msg("Here are the results for X", 3)],
        )
        # History ends at tool_response -> guard 3 blocks
        r4 = await extractor.extract_memcell(req4)
        results.append(r4)

        # Verify: messages 2, 3, 4 all returned (None, should_wait=True)
        for i in range(1, 4):
            memcell, status = results[i]
            assert memcell is None, f"Step {i+1} should not create MemCell"
            assert status.should_wait is True, f"Step {i+1} should wait"

        # Message 5: next user message arrives, now history ends at assistant response
        req5 = _make_request(
            history_msgs=[
                _user_msg("search for X", 0),
                _tool_call_msg("calling search", 1),
                _tool_response_msg("search results", 2),
                _assistant_msg("Here are the results for X", 3),
            ],
            new_msgs=[_user_msg("thanks, now search Y", 10)],
        )
        with patch(
            "memory_layer.memcell_extractor.conv_memcell_extractor.record_boundary_detection"
        ), patch(
            "memory_layer.memcell_extractor.conv_memcell_extractor.record_memcell_extracted"
        ), patch(
            "memory_layer.memcell_extractor.conv_memcell_extractor.get_space_id_for_metrics",
            return_value="test",
        ):
            r5 = await extractor.extract_memcell(req5)
        # All guards pass -> delegates to parent LLM boundary detection
        memcell, status = r5
        # LLM mock says should_wait -> no memcell, which is correct behavior
        assert status.should_wait is True


# ---------------------------------------------------------------------------
# Tests: raw_data_type
# ---------------------------------------------------------------------------


class TestRawDataType:
    def test_raw_data_type_is_agent_conversation(self):
        extractor = _build_extractor()
        assert extractor.raw_data_type == RawDataType.AGENTCONVERSATION

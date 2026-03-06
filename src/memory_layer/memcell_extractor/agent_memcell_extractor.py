"""
Agent MemCell Extractor for EverMemOS

Extends ConvMemCellExtractor for agent conversations in OpenAI chat completion format.

Key differences from ConvMemCellExtractor:
- Boundary detection prompt only sees user inputs and agent final responses
  (intermediate tool calls and tool responses are filtered out)
- MemCells are only split at complete agent turn boundaries:
  * Skips boundary detection during intermediate agent steps (tool calls, tool responses)
  * Only allows boundary/force-split when history ends at a complete agent response
  * For flush mode, packs all messages together (forced session-end scenario)
"""

from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass

from memory_layer.memcell_extractor.conv_memcell_extractor import (
    ConvMemCellExtractor,
)
from memory_layer.memcell_extractor.base_memcell_extractor import (
    MemCellExtractRequest,
    StatusResult,
)
from memory_layer.llm.llm_provider import LLMProvider
from api_specs.memory_types import MemCell, RawDataType
from core.observation.logger import get_logger

logger = get_logger(__name__)


@dataclass
class AgentMemCellExtractRequest(MemCellExtractRequest):
    """Agent-specific MemCell extraction request."""

    pass


class AgentMemCellExtractor(ConvMemCellExtractor):
    """
    Agent MemCell Extractor - Extends ConvMemCellExtractor for agent conversations.

    Key differences from ConvMemCellExtractor:
    - Boundary detection only sees user inputs and agent final responses
      (intermediate tool calls and tool responses are filtered out)
    - MemCells are only split at complete agent turn boundaries:
      * Skips processing when new messages are intermediate agent steps
      * Only allows boundary/force-split when history ends at complete agent response
      * For flush mode, packs all messages (session-end scenario)
    """

    def __init__(
        self,
        llm_provider=LLMProvider,
        boundary_detection_prompt: Optional[str] = None,
        hard_token_limit: Optional[int] = None,
        hard_message_limit: Optional[int] = None,
    ):
        super().__init__(
            llm_provider=llm_provider,
            boundary_detection_prompt=boundary_detection_prompt,
            hard_token_limit=hard_token_limit,
            hard_message_limit=hard_message_limit,
        )
        self.raw_data_type = RawDataType.AGENTCONVERSATION

    def _filter_for_boundary_detection(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Filter to only user inputs and assistant final responses.

        Removes intermediate agent loop steps:
        - role="tool": Tool execution results
        - role="assistant" WITH tool_calls: Intermediate tool invocations
        """
        filtered = []
        for msg in messages:
            role = msg.get("role", "")
            if role == "user":
                filtered.append(msg)
            elif role == "assistant" and not msg.get("tool_calls"):
                filtered.append(msg)
        return filtered

    def _is_intermediate_agent_step(self, msg: Dict[str, Any]) -> bool:
        """Check if a message is an intermediate agent step.

        Intermediate steps are:
        - role="tool": Tool execution results
        - role="assistant" WITH tool_calls: Intermediate tool invocations
        """
        role = msg.get("role", "")
        if role == "tool":
            return True
        if role == "assistant" and msg.get("tool_calls"):
            return True
        return False

    def _is_complete_agent_response(self, msg: Dict[str, Any]) -> bool:
        """Check if a message is a complete agent response (assistant without tool_calls)."""
        return msg.get("role") == "assistant" and not msg.get("tool_calls")

    async def extract_memcell(
        self, request: MemCellExtractRequest
    ) -> Tuple[Optional[MemCell], Optional[StatusResult]]:
        """Extract MemCell with agent turn-boundary guards, then delegate to parent.

        Three guards run before the parent's logic:
        1. Skip if new message is an intermediate agent step (tool call/response)
        2. Flush: pack all messages into one MemCell, skip LLM detection
        3. Skip if history doesn't end at a complete agent response

        After these guards pass, the parent's extract_memcell handles force split,
        LLM boundary detection, and MemCell creation — all operating on data that
        is guaranteed to have a valid agent turn boundary in history.
        """
        # Guard 1: Skip intermediate agent steps (tool calls / tool responses).
        # The agent turn is still in progress — no boundary detection needed.
        if request.new_raw_data_list:
            last_new_content = request.new_raw_data_list[-1].content
            if isinstance(last_new_content, dict) and self._is_intermediate_agent_step(
                last_new_content
            ):
                logger.debug(
                    f"[AgentMemCellExtractor] Skipping: new message is intermediate "
                    f"(role={last_new_content.get('role')})"
                )
                return (None, StatusResult(should_wait=True))

        # Guard 2: Flush — pack all messages together, skip LLM detection.
        # Flush is a forced session-end; we don't need turn-boundary enforcement.
        if request.flush:
            return self._flush_all_messages(request)

        # Guard 3: History must end at a complete agent response.
        # If it doesn't (e.g. ends at user msg, tool call, or tool response),
        # we cannot create a valid MemCell from it regardless of LLM output.
        # Skip the expensive LLM call and wait for more messages.
        if request.history_raw_data_list:
            last_hist_content = request.history_raw_data_list[-1].content
            if isinstance(
                last_hist_content, dict
            ) and not self._is_complete_agent_response(last_hist_content):
                logger.debug(
                    "[AgentMemCellExtractor] History doesn't end at complete agent response, "
                    "waiting for more messages"
                )
                return (None, StatusResult(should_wait=True))

        # All guards passed — delegate to parent.
        # At this point: new message is user/assistant_final, and history
        # either is empty or ends at a valid agent response.
        return await super().extract_memcell(request)

    def _flush_all_messages(
        self, request: MemCellExtractRequest
    ) -> Tuple[Optional[MemCell], StatusResult]:
        """Flush: process and pack all messages into one MemCell."""
        all_msgs = []
        for raw_data in list(request.history_raw_data_list) + list(
            request.new_raw_data_list
        ):
            processed = self._data_process(raw_data)
            if processed is not None:
                all_msgs.append(processed)

        if all_msgs:
            logger.info(
                f"[AgentMemCellExtractor] Flush: packing {len(all_msgs)} messages"
            )
            return self._create_memcell_directly(all_msgs, request, 'flush')

        logger.warning("[AgentMemCellExtractor] Flush: no messages to process")
        return (None, StatusResult(should_wait=True))

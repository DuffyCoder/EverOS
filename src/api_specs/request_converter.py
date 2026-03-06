"""
Request converter module

This module contains various functions to convert external request formats to internal Request objects.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Union, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from api_specs.memory_models import MemoryType
from api_specs.dtos import RetrieveMemRequest, FetchMemRequest, MemorizeRequest, RawData
from api_specs.memory_types import RawDataType
from core.oxm.constants import MAGIC_ALL

from typing import Dict, Any, Optional
from common_utils.datetime_utils import from_iso_format
from zoneinfo import ZoneInfo
from core.observation.logger import get_logger
from api_specs.memory_models import RetrieveMethod, MemoryType

logger = get_logger(__name__)


def to_bool(value: Any, default: bool = False) -> bool:
    """
    Convert various input types to boolean.

    Handles common representations from HTTP JSON body:
    - bool: True/False (pass through)
    - str: "true"/"false", "1"/"0", "yes"/"no" (case-insensitive)
    - int: 1/0
    - None: returns default

    Args:
        value: Input value to convert
        default: Default value if input is None or unrecognized

    Returns:
        bool: Converted boolean value
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return default


def generate_single_user_group_id(sender: str) -> str:
    """
    Generate a group_id for single-user mode based on sender (user_id) hash.

    This function creates a deterministic group_id by hashing the sender
    and appending '_group' suffix. This is used when group_id is not provided,
    representing single-user mode where each user's messages are extracted
    into separate memory spaces.

    Args:
        sender: The sender user ID (equivalent to user_id internally)

    Returns:
        str: Generated group_id in format: {hash(sender)[:16]}_group
    """
    # Use MD5 hash for deterministic and compact result
    hash_value = hashlib.md5(sender.encode('utf-8')).hexdigest()[:16]
    return f"{hash_value}_group"


class DataFields:
    """Data field constants"""

    MESSAGES = "messages"
    RAW_DATA_TYPE = "raw_data_type"
    GROUP_ID = "group_id"


def convert_dict_to_fetch_mem_request(data: Dict[str, Any]) -> FetchMemRequest:
    """
    Convert dictionary to FetchMemRequest object

    Args:
        data: Dictionary containing FetchMemRequest fields

    Returns:
        FetchMemRequest object

    Raises:
        ValueError: When required fields are missing or have incorrect types
    """
    try:
        # Convert memory_type, use default if not provided
        memory_type = MemoryType(
            data.get("memory_type", MemoryType.EPISODIC_MEMORY.value)
        )

        # Convert page and page_size to integer type (all obtained from query_params are strings)
        page = data.get("page", 1)
        page_size = data.get("page_size", 20)
        if isinstance(page, str):
            page = int(page)
        if isinstance(page_size, str):
            page_size = int(page_size)

        # Handle group_ids parameter (supports List[str] only, no MAGIC_ALL)
        # Semantics:
        #   - None: Skip group filtering
        #   - []: Empty array, skip filtering (upper layer validation will reject)
        #   - ["g1", "g2"]: Filter by specified group_ids
        group_ids_raw = data.get("group_ids")
        if group_ids_raw is None:
            # User didn't pass group_ids parameter -> skip filtering
            group_ids = None
        elif isinstance(group_ids_raw, str):
            # Support comma-separated string to array (Query Param scenario)
            group_ids = [g.strip() for g in group_ids_raw.split(",") if g.strip()]
            # If parsed result is empty array, set to None
            if not group_ids:
                group_ids = None
        elif isinstance(group_ids_raw, list):
            group_ids = group_ids_raw if group_ids_raw else None
        else:
            raise ValueError(f"Invalid group_ids type: {type(group_ids_raw)}")

        # Build FetchMemRequest object
        return FetchMemRequest(
            user_id=data.get(
                "user_id", MAGIC_ALL
            ),  # User ID, use MAGIC_ALL to skip user filtering
            group_ids=group_ids,  # Group IDs list, None to skip group filtering
            memory_type=memory_type,
            page=page,
            page_size=page_size,
            start_time=data.get("start_time"),
            end_time=data.get("end_time"),
        )
    except Exception as e:
        raise ValueError(f"FetchMemRequest conversion failed: {e}")


def convert_dict_to_retrieve_mem_request(
    data: Dict[str, Any], query: Optional[str] = None
) -> RetrieveMemRequest:
    """
    Convert dictionary to RetrieveMemRequest object

    Args:
        data: Dictionary containing RetrieveMemRequest fields
        query: Query text (optional)

    Returns:
        RetrieveMemRequest object

    Raises:
        ValueError: When required fields are missing or have incorrect types
    """
    try:
        # Validate required fields: user_id or group_id at least one is required
        # if not data.get("user_id") and not data.get("group_id"):
        #     raise ValueError("user_id or group_id at least one is required")

        # Handle retrieve_method, use default keyword if not provided

        retrieve_method_str = data.get("retrieve_method", RetrieveMethod.KEYWORD.value)
        logger.debug(f"[DEBUG] retrieve_method_str from data: {retrieve_method_str!r}")

        # Convert string to RetrieveMethod enum
        try:
            retrieve_method = RetrieveMethod(retrieve_method_str)
            logger.debug(f"[DEBUG] converted to: {retrieve_method}")
        except ValueError:
            raise ValueError(
                f"Invalid retrieve_method: {retrieve_method_str}. "
                f"Supported methods: {[m.value for m in RetrieveMethod]}"
            )

        # Convert top_k to integer type (all obtained from query_params are strings)
        # Default to -1 means return all results that meet the threshold
        top_k = data.get("top_k", -1)
        if isinstance(top_k, str):
            top_k = int(top_k)

        # Convert include_metadata to boolean type
        include_metadata = data.get("include_metadata", True)
        if isinstance(include_metadata, str):
            include_metadata = include_metadata.lower() in ("true", "1", "yes")

        # Convert radius to float type (if exists)
        radius = data.get("radius", None)
        if radius is not None and isinstance(radius, str):
            radius = float(radius)

        # Convert memory_types string list to MemoryType enum list
        raw_memory_types = data.get("memory_types", [])
        # Handle comma-separated string (from query_params)
        if isinstance(raw_memory_types, str):
            raw_memory_types = [
                mt.strip() for mt in raw_memory_types.split(",") if mt.strip()
            ]
        memory_types = []
        for mt in raw_memory_types:
            if isinstance(mt, str):
                try:
                    memory_types.append(MemoryType(mt))
                except ValueError:
                    logger.error(f"Invalid memory_type: {mt}, skipping")
            elif isinstance(mt, MemoryType):
                memory_types.append(mt)

        # Default: profile + episodic_memory if not specified
        if not memory_types:
            memory_types = [MemoryType.PROFILE, MemoryType.EPISODIC_MEMORY]

        # Handle group_ids: support both string and array for backward compatibility
        # Priority: group_ids (new) > group_id (old, for backward compatibility)
        group_ids_raw = data.get("group_ids", None)
        if group_ids_raw is None:
            # Try legacy group_id parameter for backward compatibility
            group_id_legacy = data.get("group_id", None)
            if isinstance(group_id_legacy, str):
                group_ids = [group_id_legacy]  # Convert string to array
            elif isinstance(group_id_legacy, list):
                group_ids = group_id_legacy
            else:
                group_ids = None
        elif isinstance(group_ids_raw, str):
            # Support comma-separated string to array (Query Param scenario)
            group_ids = [g.strip() for g in group_ids_raw.split(",") if g.strip()]
            # If parsed result is empty array, set to None
            if not group_ids:
                group_ids = None
        elif isinstance(group_ids_raw, list):
            group_ids = group_ids_raw if group_ids_raw else None
        else:
            group_ids = None

        return RetrieveMemRequest(
            retrieve_method=retrieve_method,
            user_id=data.get(
                "user_id", MAGIC_ALL
            ),  # User ID, use MAGIC_ALL to skip user filtering
            group_ids=group_ids,  # Group IDs array (List[str] or None)
            query=query or data.get("query", None),
            memory_types=memory_types,
            top_k=top_k,
            include_metadata=include_metadata,
            start_time=data.get("start_time", None),
            end_time=data.get("end_time", None),
            radius=radius,  # COSINE similarity threshold
        )
    except Exception as e:
        raise ValueError(f"RetrieveMemRequest conversion failed: {e}")


# =========================================


def normalize_refer_list(refer_list: List[Any]) -> List[str]:
    """
    Normalize refer_list format to a list of message IDs

    Supports two formats:
    1. String list: ["msg_id_1", "msg_id_2"]
    2. MessageReference object list: [{"message_id": "msg_id_1", ...}, ...]

    Args:
        refer_list: Original reference list

    Returns:
        List[str]: Normalized list of message IDs
    """
    if not refer_list:
        return []

    normalized: List[str] = []
    for refer in refer_list:
        if isinstance(refer, str):
            normalized.append(refer)
        elif isinstance(refer, dict):
            ref_msg_id = refer.get("message_id")
            if ref_msg_id:
                normalized.append(str(ref_msg_id))
    return normalized


def build_raw_data_from_simple_message(
    message_id: str,
    sender: str,
    content: str,
    timestamp: datetime,
    sender_name: Optional[str] = None,
    role: Optional[str] = None,
    group_id: Optional[str] = None,
    group_name: Optional[str] = None,
    refer_list: Optional[List[str]] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    tool_call_id: Optional[str] = None,
) -> RawData:
    """
    Build RawData object from simple message fields.

    This is the canonical function for creating RawData from simple message format.
    All code that needs to create RawData from simple messages should use this function
    to ensure consistency.

    Args:
        message_id: Message ID (required)
        sender: Sender user ID (required)
        content: Message content (required)
        timestamp: Message timestamp as datetime object (required)
        sender_name: Sender display name (defaults to sender if not provided)
        role: Message sender role — "user", "assistant", or "tool" (optional)
        group_id: Group ID (optional)
        group_name: Group name (optional)
        refer_list: Normalized list of referenced message IDs (optional)
        extra_metadata: Additional metadata to merge (optional)
        tool_calls: Tool calls from assistant (OpenAI format, optional)
        tool_call_id: Tool call ID this message responds to (for role=tool, optional)

    Returns:
        RawData: Fully constructed RawData object
    """
    # Use sender as sender_name if not provided
    if sender_name is None:
        sender_name = sender

    # Ensure refer_list is a list
    if refer_list is None:
        refer_list = []

    # Build content dictionary with all required fields
    raw_content = {
        "speaker_name": sender_name,
        "role": role,  # Message sender role: "user", "assistant", or "tool"
        "receiverId": None,
        "roomId": group_id,
        "groupName": group_name,
        "userIdList": [],
        "referList": refer_list,
        "content": content,
        "timestamp": timestamp,
        "createBy": sender,
        "updateTime": timestamp,
        "orgId": None,
        "speaker_id": sender,
        "msgType": 1,  # TEXT
        "data_id": message_id,
    }

    # Add OpenAI-format agent fields if present
    if tool_calls:
        raw_content["tool_calls"] = tool_calls
    if tool_call_id:
        raw_content["tool_call_id"] = tool_call_id

    # Build metadata
    metadata = {
        "original_id": message_id,
        "createTime": timestamp,
        "updateTime": timestamp,
        "createBy": sender,
        "orgId": None,
    }

    # Merge extra metadata if provided
    if extra_metadata:
        metadata.update(extra_metadata)

    return RawData(content=raw_content, data_id=message_id, metadata=metadata)


async def convert_simple_message_to_memorize_request(
    message_data: Dict[str, Any]
) -> MemorizeRequest:
    """
    Convert simple direct single message format directly to MemorizeRequest

    This is a unified conversion function that combines the previous two-step conversion
    (convert_simple_message_to_memorize_input + handle_conversation_format) into one.

    Args:
        message_data: Simple single message data, containing:
            - sender (required): Sender user ID (also used as user_id internally)
            - group_id (optional): Group ID. If not provided, will auto-generate based on
              hash(sender) + '_group' suffix for single-user mode
            - group_name (optional): Group name
            - message_id (required): Message ID
            - create_time (required): Creation time (ISO 8601 format)
            - sender_name (optional): Sender name
            - role (optional): Message sender role ("user" for human, "assistant" for AI)
            - content (required): Message content
            - refer_list (optional): List of referenced message IDs

    Returns:
        MemorizeRequest: Ready-to-use memorize request object

    Raises:
        ValueError: When required fields are missing
    """
    # Extract fields
    group_id = message_data.get("group_id")
    group_name = message_data.get("group_name")
    message_id = message_data.get("message_id")
    create_time_str = message_data.get("create_time")
    sender = message_data.get("sender")
    sender_name = message_data.get("sender_name", sender)
    role = message_data.get("role")  # "user", "assistant", or "tool"
    content = message_data.get("content", "")
    refer_list = message_data.get("refer_list", [])
    flush = to_bool(message_data.get("flush"), default=False)  # Force boundary trigger
    tool_calls = message_data.get("tool_calls")  # OpenAI tool_calls (assistant)
    tool_call_id = message_data.get("tool_call_id")  # OpenAI tool_call_id (tool)
    raw_data_type_str = message_data.get("raw_data_type")  # "Conversation" or "AgentConversation"

    # Validate required fields
    if not sender:
        raise ValueError("Missing required field: sender")
    if not message_id:
        raise ValueError("Missing required field: message_id")
    if not create_time_str:
        raise ValueError("Missing required field: create_time")
    if not content:
        raise ValueError("Missing required field: content")

    # Auto-generate group_id if not provided (single-user mode)
    if not group_id:
        group_id = generate_single_user_group_id(sender)
        logger.debug(
            f"Auto-generated group_id for single-user mode: {group_id} (sender: {sender})"
        )

    # Normalize refer_list
    normalized_refer_list = normalize_refer_list(refer_list)

    # Parse timestamp
    timestamp = from_iso_format(create_time_str, ZoneInfo("UTC"))

    # Determine raw_data_type
    raw_data_type = RawDataType.CONVERSATION
    if raw_data_type_str:
        parsed_type = RawDataType.from_string(raw_data_type_str)
        if parsed_type:
            raw_data_type = parsed_type

    # AgentConversation requires a role field (user / assistant / tool)
    if raw_data_type == RawDataType.AGENTCONVERSATION and not role:
        raise ValueError(
            "AgentConversation requires 'role' field (user, assistant, or tool)"
        )

    # Build RawData using the canonical function
    raw_data = build_raw_data_from_simple_message(
        message_id=message_id,
        sender=sender,
        content=content,
        timestamp=timestamp,
        sender_name=sender_name,
        role=role,
        group_id=group_id,
        group_name=group_name,
        refer_list=normalized_refer_list,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
    )

    # Create and return MemorizeRequest
    return MemorizeRequest(
        history_raw_data_list=[],
        new_raw_data_list=[raw_data],
        raw_data_type=raw_data_type,
        user_id_list=[],
        group_id=group_id,
        group_name=group_name,
        current_time=timestamp,
        flush=flush,
    )

"""
Memory Controller - Unified memory management controller

Provides RESTful API routes for:
- Memory ingestion (POST /memories): accept a single-message payload and create memories
- Memory fetch (GET /memories): fetch by memory_type with optional user/group/time filters (query params or JSON body)
- Memory search (GET /memories/search): keyword/vector/hybrid/rrf/agentic retrieval with grouped results
- Memory deletion (DELETE /memories): soft delete by combined filters
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from contextlib import suppress
from fastapi import HTTPException, Request as FastAPIRequest

from core.di.decorators import controller
from core.di import get_bean_by_type
from core.interface.controller.base_controller import BaseController, get, post, delete
from core.constants.errors import ErrorCode, ErrorStatus
from agentic_layer.memory_manager import MemoryManager
from api_specs.request_converter import (
    convert_simple_message_to_memorize_request,
    convert_dict_to_fetch_mem_request,
    convert_dict_to_retrieve_mem_request,
)
from infra_layer.adapters.input.api.dto.memory_dto import (
    # Request DTOs
    MemorizeMessageRequest,
    FetchMemRequest,
    RetrieveMemRequest,
    DeleteMemoriesRequestDTO,
    # Response DTOs
    MemorizeResponse,
    FetchMemoriesResponse,
    SearchMemoriesResponse,
    DeleteMemoriesResponse,
)
from core.request.timeout_background import timeout_to_background
from core.request import log_request
from core.component.redis_provider import RedisProvider
from service.memory_request_log_service import MemoryRequestLogService
from service.memcell_delete_service import MemCellDeleteService
from service.conversation_meta_service import ConversationMetaService
from infra_layer.adapters.input.api.dto.memory_dto import ConversationMetaCreateRequest
from api_specs.memory_types import RawDataType
from agentic_layer.metrics.memorize_metrics import (
    record_memorize_request,
    record_memorize_error,
    record_memorize_message,
    classify_memorize_error,
    get_space_id_for_metrics,
    get_raw_data_type_label,
)

logger = logging.getLogger(__name__)


@controller("memory_controller", primary=True)
class MemoryController(BaseController):
    """
    Memory Controller
    """

    def __init__(self):
        """Initialize controller"""
        super().__init__(
            prefix="/api/v0/memories",
            tags=["Memory Controller"],
            default_auth="none",  # Adjust authentication strategy based on actual needs
        )
        self.memory_manager = MemoryManager()
        # Get RedisProvider
        self.redis_provider = get_bean_by_type(RedisProvider)
        logger.info("MemoryController initialized with MemoryManager")

    @post(
        "",
        response_model=MemorizeResponse,
        summary="Store single message",
        description="""
        Store a single message into memory.
        
        ## Fields:
        - **message_id** (required): Unique identifier for the message
        - **create_time** (required): Message creation time (ISO 8601 format)
        - **sender** (required): Sender user ID
        - **content** (required): Message content
        - **group_id** (optional): Group ID
        - **group_name** (optional): Group name
        - **sender_name** (optional): Sender display name (defaults to sender if empty)
        - **role** (optional): Sender role ("user" or "assistant")
        - **refer_list** (optional): List of referenced message IDs
        
        ## Functionality:
        - Accepts raw single-message data
        - Automatically creates memories when sufficient context is available
        - Returns extraction count and status
        """,
        responses={
            400: {
                "description": "Request parameter error",
                "content": {
                    "application/json": {
                        "example": {
                            "status": ErrorStatus.FAILED.value,
                            "code": ErrorCode.INVALID_PARAMETER.value,
                            "message": "Data format error: Required field message_id is missing",
                            "timestamp": "2025-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories",
                        }
                    }
                },
            },
            500: {
                "description": "Internal server error",
                "content": {
                    "application/json": {
                        "example": {
                            "status": ErrorStatus.FAILED.value,
                            "code": ErrorCode.SYSTEM_ERROR.value,
                            "message": "Failed to store memory, please try again later",
                            "timestamp": "2025-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories",
                        }
                    }
                },
            },
        },
    )
    @log_request()
    @timeout_to_background()
    async def memorize_single_message(
        self,
        request: FastAPIRequest,
        request_body: MemorizeMessageRequest = None,  # OpenAPI documentation only
    ) -> MemorizeResponse:
        """
        Store single message memory data

        Convert a single-message payload to a memory request and persist it.
        If no memory is extracted, the message remains pending for later processing.

        Args:
            request: FastAPI request object
            request_body: Message request body (used for OpenAPI documentation only)

        Returns:
            Dict[str, Any]: Memory storage response with extraction count and status info

        Raises:
            HTTPException: When request processing fails
        """
        del request_body  # Used for OpenAPI documentation only
        start_time = time.perf_counter()
        memory_count = 0
        # Get space_id for metrics (available from tenant context)
        space_id = get_space_id_for_metrics()
        # Default raw_data_type, will be updated after conversion
        raw_data_type = get_raw_data_type_label(None)

        try:
            # 1. Get JSON body from request (simple direct format)
            message_data = await request.json()
            logger.info("Received memorize request (single message)")

            # 2. Convert directly to MemorizeRequest (unified single-step conversion)
            logger.info(
                "Starting conversion from simple message format to MemorizeRequest"
            )
            memorize_request = await convert_simple_message_to_memorize_request(
                message_data
            )

            # Update raw_data_type from request (for subsequent metrics)
            if memorize_request.raw_data_type:
                raw_data_type = get_raw_data_type_label(
                    memorize_request.raw_data_type.value
                )

            record_memorize_message(
                space_id=space_id,
                raw_data_type=raw_data_type,
                status='received',
                count=1,
            )

            # Extract metadata for logging
            group_name = memorize_request.group_name
            group_id = memorize_request.group_id

            logger.info(
                "Conversion completed: group_id=%s, group_name=%s", group_id, group_name
            )

            # Create async task to ensure conversation meta exists for this group
            if group_id is not None:
                asyncio.create_task(
                    self._ensure_conversation_meta_exists(
                        group_id=group_id, group_name=group_name
                    )
                )

            # 2.5 Check for duplicate message before saving
            # Extract user_id (sender) and message_id from original message_data
            sender = message_data.get("sender")
            message_id = message_data.get("message_id")

            if group_id and sender and message_id:
                log_service = get_bean_by_type(MemoryRequestLogService)
                is_duplicate = await log_service.check_duplicate_message(
                    group_id=group_id, user_id=sender, message_id=message_id
                )
                if is_duplicate:
                    logger.warning(
                        "Duplicate message detected, returning early: "
                        "group_id=%s, user_id=%s, message_id=%s",
                        group_id,
                        sender,
                        message_id,
                    )
                    return {
                        "status": ErrorStatus.DUPLICATE.value,
                        "message": "Duplicate message: this message has already been processed",
                        "result": {
                            "saved_memories": [],
                            "count": 0,
                            "status_info": "duplicate",
                            "group_id": group_id,
                            "message_id": message_id,
                        },
                    }

            # 3. Save request logs first (sync_status=-1) for better timing control
            if (
                memorize_request.raw_data_type in (RawDataType.CONVERSATION, RawDataType.AGENTCONVERSATION)
                and memorize_request.new_raw_data_list
            ):
                log_service = get_bean_by_type(MemoryRequestLogService)
                await log_service.save_request_logs(
                    request=memorize_request,
                    version="1.0.0",
                    endpoint_name="memorize_single_message",
                    method=request.method,
                    url=str(request.url),
                    raw_input_dict=message_data,
                )
                logger.info(
                    "Saved %d request logs: group_id=%s",
                    len(memorize_request.new_raw_data_list),
                    group_id,
                )

            # 4. Call memory_manager to process the request
            logger.info("Starting to process memory request")
            # memorize returns count of extracted memories (int)
            memory_count = await self.memory_manager.memorize(memorize_request)

            # 5. Return unified response format
            logger.info(
                "Memory request processing completed, extracted %s memories",
                memory_count,
            )

            # Optimize return message to help users understand runtime status
            if memory_count > 0:
                message = f"Extracted {memory_count} memories"
                status = 'extracted'
            else:
                message = "Message queued, awaiting boundary detection"
                status = 'accumulated'

            # Record success metrics
            record_memorize_request(
                space_id=space_id,
                raw_data_type=raw_data_type,
                status=status,
                duration_seconds=time.perf_counter() - start_time,
            )

            return {
                "status": ErrorStatus.OK.value,
                "message": message,
                "result": {
                    "saved_memories": [],  # Memories saved to DB, fetch via API
                    "count": memory_count,
                    "status_info": "accumulated" if memory_count == 0 else "extracted",
                },
            }

        except ValueError as e:
            logger.error("memorize request parameter error: %s", e)
            record_memorize_error(
                space_id=space_id,
                raw_data_type=raw_data_type,
                stage='conversion',
                error_type='validation_error',
            )
            record_memorize_request(
                space_id=space_id,
                raw_data_type=raw_data_type,
                status='error',
                duration_seconds=time.perf_counter() - start_time,
            )
            raise HTTPException(status_code=400, detail=str(e)) from e
        except HTTPException:
            # Re-raise HTTPException (already handled errors)
            record_memorize_request(
                space_id=space_id,
                raw_data_type=raw_data_type,
                status='error',
                duration_seconds=time.perf_counter() - start_time,
            )
            raise
        except Exception as e:
            logger.error("memorize request processing failed: %s", e, exc_info=True)
            error_type = classify_memorize_error(e)
            record_memorize_error(
                space_id=space_id,
                raw_data_type=raw_data_type,
                stage='memorize_process',
                error_type=error_type,
            )
            record_memorize_request(
                space_id=space_id,
                raw_data_type=raw_data_type,
                status='error',
                duration_seconds=time.perf_counter() - start_time,
            )
            raise HTTPException(
                status_code=500, detail="Failed to store memory, please try again later"
            ) from e

    async def _ensure_conversation_meta_exists(
        self, group_id: str, group_name: str = None
    ) -> None:
        """
        Background task to ensure conversation meta exists for a group.

        If the group_id doesn't have an associated conversation meta,
        creates one with the provided group_name.

        Args:
            group_id: Group unique identifier
            group_name: Group name (optional, used when creating new meta)
        """
        try:
            meta_service = get_bean_by_type(ConversationMetaService)
            repo = meta_service._get_repository()

            # Check if meta exists using simple query (no fallback)
            existing_meta = await repo.get_by_group_id(group_id)

            if existing_meta is not None:
                logger.debug(
                    "Conversation meta already exists for group_id=%s", group_id
                )
                return

            # Meta doesn't exist, create a new one
            logger.info(
                "Creating conversation meta for group_id=%s, group_name=%s",
                group_id,
                group_name,
            )

            # Build create request for group config
            # Group config only needs: name, created_at (scene/scene_desc inherited from global)
            create_request = ConversationMetaCreateRequest(
                group_id=group_id,
                name=group_name or group_id,  # Use group_name or fallback to group_id
                created_at=datetime.now(timezone.utc).isoformat(),
            )

            result = await meta_service.save(create_request)

            if result:
                logger.info(
                    "Successfully created conversation meta for group_id=%s", group_id
                )
            else:
                logger.warning(
                    "Failed to create conversation meta for group_id=%s", group_id
                )

        except Exception as e:
            # Log error but don't propagate - this is a background task
            logger.warning(
                "Error ensuring conversation meta exists for group_id=%s: %s",
                group_id,
                e,
            )

    @get(
        "",
        response_model=FetchMemoriesResponse,
        summary="Fetch user memories",
        description="""
        Retrieve memory records by memory_type with optional filters

        ## Fields:
        - **user_id** (optional): User ID (at least one of user_id or group_ids must be specified)
        - **group_ids** (optional): List of Group IDs for batch query (max 50)
            - Single group: ["group_1"]
            - Multiple groups: ["group_1", "group_2", "group_3"]
            - Query param format: group_ids=group_1,group_2,group_3
        - **memory_type** (optional): Memory type (default: episodic_memory)
            - profile: user profile
            - episodic_memory: episodic memory
            - foresight: prospective memory
            - event_log: event log (atomic facts)
        - **page** (optional): Page number, starts from 1 (default: 1)
        - **page_size** (optional): Records per page (default: 20, max: 100)


        ## Response:
        - **total_count**: Total records matching query conditions (for pagination)
        - **count**: Number of records in current page

        ## Use cases:
        - User profile display
        - Personalized recommendation systems
        - Conversation history review
        - Cross-group memory aggregation
        """,
        responses={
            400: {
                "description": "Request parameter error",
                "content": {
                    "application/json": {
                        "example": {
                            "status": ErrorStatus.FAILED.value,
                            "code": ErrorCode.INVALID_PARAMETER.value,
                            "message": "user_id cannot be empty",
                            "timestamp": "2024-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories",
                        }
                    }
                },
            },
            500: {
                "description": "Internal server error",
                "content": {
                    "application/json": {
                        "example": {
                            "status": ErrorStatus.FAILED.value,
                            "code": ErrorCode.SYSTEM_ERROR.value,
                            "message": "Failed to retrieve memory, please try again later",
                            "timestamp": "2024-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories",
                        }
                    }
                },
            },
        },
    )
    async def fetch_memories(
        self,
        fastapi_request: FastAPIRequest,
        request_body: FetchMemRequest = None,  # For OpenAPI request body documentation
    ) -> FetchMemoriesResponse:
        """
        Retrieve user memory data

        Fetch memory records by memory_type with optional user/group/time filters.
        Parameters are accepted from query params or request body (GET with body is supported).

        Args:
            fastapi_request: FastAPI request object
            request_body: Request body parameters (used for OpenAPI documentation only)

        Returns:
            Dict[str, Any]: Memory retrieval response

        Raises:
            HTTPException: When request processing fails
        """
        del request_body  # Used for OpenAPI documentation only
        try:
            # Get params from query params first
            # Handle array parameters (like group_ids) that may appear multiple times
            params = dict(fastapi_request.query_params)

            # Special handling for array parameters
            # If group_ids appears multiple times (?group_ids=a&group_ids=b), collect all values
            if "group_ids" in fastapi_request.query_params:
                all_group_ids = fastapi_request.query_params.getlist("group_ids")
                if all_group_ids:
                    params["group_ids"] = all_group_ids

            # Also try to get params from body (for GET + body requests)
            if body := await fastapi_request.body():
                with suppress(json.JSONDecodeError, TypeError):
                    if isinstance(body_data := json.loads(body), dict):
                        params.update(body_data)

            logger.info(
                "Received fetch request: user_id=%s, memory_type=%s",
                params.get("user_id"),
                params.get("memory_type"),
            )

            # Directly use converter to transform
            fetch_request = convert_dict_to_fetch_mem_request(params)

            # Call memory_manager's fetch_mem method
            response = await self.memory_manager.fetch_mem(fetch_request)

            # Return unified response format
            memory_count = len(response.memories) if response.memories else 0
            logger.info(
                "Fetch request processing completed: user_id=%s, returned %s memories",
                params.get("user_id"),
                memory_count,
            )
            return {
                "status": ErrorStatus.OK.value,
                "message": f"Memory retrieval successful, retrieved {memory_count} memories",
                "result": response,
            }

        except ValueError as e:
            logger.error("Fetch request parameter error: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except HTTPException:
            # Re-raise HTTPException
            raise
        except Exception as e:
            logger.error("Fetch request processing failed: %s", e, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to retrieve memory, please try again later",
            ) from e

    @get(
        "/search",
        response_model=SearchMemoriesResponse,
        summary="Search relevant memories (keyword/vector/hybrid/rrf/agentic)",
        description="""
        Retrieve relevant memory data based on query text using multiple retrieval methods
        
        ## Fields:
        - **query** (optional): Search query text
        - **user_id** (optional): User ID (at least one of user_id/group_id required)
        - **group_id** (optional): Group ID(s) - supports single string or array of strings.
            If not provided, searches all groups for the user.
        - **retrieve_method** (optional): Retrieval method (default: keyword)
            - keyword: keyword retrieval (BM25)
            - vector: vector semantic retrieval
            - hybrid: hybrid retrieval (keyword + vector)
            - rrf: RRF fusion retrieval
            - agentic: LLM-guided multi-round retrieval
        - **radius** (optional): Similarity threshold (0.0-1.0) for vector/profile search
        - **memory_types** (optional): List of memory types to search (default: profile + episodic_memory)
            - profile: user profile (Milvus vector search, no rerank)
            - episodic_memory: episodic memory (ES + Milvus with rerank)
            - Note: Only profile and episodic_memory are supported for search retrieval
        - **start_time** (optional): Start time (ISO 8601). Only applies to episodic_memory, ignored for profile
        - **end_time** (optional): End time (ISO 8601). Only applies to episodic_memory, ignored for profile
        - **top_k** (optional): Max results (default: -1, max: 100). -1 means return all results that meet the threshold
        - **include_metadata** (optional): Whether to include metadata (default: true)
        
        ## Result description:
        - **profiles**: Profile search results (explicit_info and implicit_traits from Milvus)
        - **memories**: Episodic memory search results (from ES + Milvus with rerank)
        - Each result has a relevance score indicating match degree with query
        """,
        responses={
            400: {
                "description": "Request parameter error",
                "content": {
                    "application/json": {
                        "example": {
                            "status": ErrorStatus.FAILED.value,
                            "code": ErrorCode.INVALID_PARAMETER.value,
                            "message": "query cannot be empty",
                            "timestamp": "2024-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories/search",
                        }
                    }
                },
            },
            500: {
                "description": "Internal server error",
                "content": {
                    "application/json": {
                        "example": {
                            "status": ErrorStatus.FAILED.value,
                            "code": ErrorCode.SYSTEM_ERROR.value,
                            "message": "Failed to retrieve memory, please try again later",
                            "timestamp": "2024-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories/search",
                        }
                    }
                },
            },
        },
    )
    async def search_memories(
        self,
        fastapi_request: FastAPIRequest,
        request_body: RetrieveMemRequest = None,  # For OpenAPI request body documentation
    ) -> SearchMemoriesResponse:
        """
        Search relevant memory data

        Retrieve relevant memory data based on query text using keyword, vector, hybrid, RRF, or agentic methods.
        Parameters are passed via request body (GET with body, similar to Elasticsearch style).

        Args:
            fastapi_request: FastAPI request object
            request_body: Request body parameters (used for OpenAPI documentation only)

        Returns:
            Dict[str, Any]: Memory search response

        Raises:
            HTTPException: When request processing fails
        """
        del request_body  # Used for OpenAPI documentation only
        try:
            # Get params from query params first
            # Handle array parameters (like group_ids) that may appear multiple times
            query_params = dict(fastapi_request.query_params)

            # Special handling for array parameters
            # If group_ids appears multiple times (?group_ids=a&group_ids=b), collect all values
            if "group_ids" in fastapi_request.query_params:
                all_group_ids = fastapi_request.query_params.getlist("group_ids")
                if all_group_ids:
                    query_params["group_ids"] = all_group_ids

            # Same handling for memory_types
            if "memory_types" in fastapi_request.query_params:
                all_memory_types = fastapi_request.query_params.getlist("memory_types")
                if all_memory_types:
                    query_params["memory_types"] = all_memory_types

            # Also try to get params from body (for GET + body requests like Elasticsearch)
            if body := await fastapi_request.body():
                with suppress(json.JSONDecodeError, TypeError):
                    if isinstance(body_data := json.loads(body), dict):
                        query_params.update(body_data)

            query_text = query_params.get("query")

            # Debug: Log group_ids parameter
            logger.info(
                "Received search request: user_id=%s, query=%s, retrieve_method=%s, group_ids=%s (type=%s)",
                query_params.get("user_id"),
                query_text,
                query_params.get("retrieve_method"),
                query_params.get("group_ids"),
                type(query_params.get("group_ids")).__name__,
            )

            # Directly use converter to transform
            retrieve_request = convert_dict_to_retrieve_mem_request(
                query_params, query=query_text
            )
            logger.info(
                "After conversion: retrieve_method=%s", retrieve_request.retrieve_method
            )

            # Use retrieve_mem method (supports keyword, vector, hybrid, rrf, agentic)
            response = await self.memory_manager.retrieve_mem(retrieve_request)

            # Return unified response format
            profile_count = len(response.profiles) if response.profiles else 0
            episodic_count = len(response.memories) if response.memories else 0
            logger.info(
                "Search request complete: user_id=%s, profiles=%d, episodic=%d",
                query_params.get("user_id"),
                profile_count,
                episodic_count,
            )
            return {
                "status": ErrorStatus.OK.value,
                "message": f"Memory search successful, retrieved {profile_count} profiles and {episodic_count} episodic memories",
                "result": response,
            }

        except ValueError as e:
            logger.error("Search request parameter error: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except HTTPException:
            # Re-raise HTTPException
            raise
        except Exception as e:
            logger.error("Search request processing failed: %s", e, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to retrieve memory, please try again later",
            ) from e

    @delete(
        "",
        response_model=DeleteMemoriesResponse,
        summary="Delete memories (soft delete)",
        description="""
        Soft delete memory records based on combined filter criteria
        
        ## Functionality:
        - Soft delete records matching combined filter conditions
        - If multiple conditions provided, ALL must be satisfied (AND logic)
        - At least one filter must be specified
        
        ## Filter Parameters (combined with AND):
        - **memory_id**: Filter by specific memory id
        - **user_id**: Filter by user ID
        - **group_id**: Filter by group ID
        
        ## Examples:
        - memory_id only: Delete specific memory
        - user_id only: Delete all user's memories
        - user_id + group_id: Delete user's memories in specific group
        - memory_id + user_id + group_id: Delete if all conditions match
        
        ## Soft Delete:
        - Records are marked as deleted, not physically removed
        - Deleted records can be restored if needed
        - Deleted records won't appear in regular queries
        
        ## Use cases:
        - User requests data deletion
        - Group chat cleanup
        - Privacy compliance (GDPR, etc.)
        - Conversation history management
        """,
        responses={
            400: {
                "description": "Request parameter error",
                "content": {
                    "application/json": {
                        "example": {
                            "status": ErrorStatus.FAILED.value,
                            "code": ErrorCode.INVALID_PARAMETER.value,
                            "message": "At least one of memory_id, user_id, or group_id must be provided",
                            "timestamp": "2025-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories",
                        }
                    }
                },
            },
            404: {
                "description": "Memory not found",
                "content": {
                    "application/json": {
                        "example": {
                            "status": ErrorStatus.FAILED.value,
                            "code": ErrorCode.RESOURCE_NOT_FOUND.value,
                            "message": "No memories found matching the criteria or already deleted",
                            "timestamp": "2025-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories",
                        }
                    }
                },
            },
            500: {
                "description": "Internal server error",
                "content": {
                    "application/json": {
                        "example": {
                            "status": ErrorStatus.FAILED.value,
                            "code": ErrorCode.SYSTEM_ERROR.value,
                            "message": "Failed to delete memories, please try again later",
                            "timestamp": "2025-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories",
                        }
                    }
                },
            },
        },
    )
    async def delete_memories(
        self,
        fastapi_request: FastAPIRequest,
        request_body: DeleteMemoriesRequestDTO = None,  # OpenAPI documentation (body params)
    ) -> DeleteMemoriesResponse:
        """
        Soft delete memory data based on combined filter criteria

        Filters are combined with AND logic. Omit any filter you do not want to apply.

        Args:
            fastapi_request: FastAPI request object
            request_body: Request body parameters (used for OpenAPI documentation only)

        Returns:
            Dict[str, Any]: Delete result response

        Raises:
            HTTPException: When request processing fails
        """
        del request_body  # Used for OpenAPI documentation only

        try:
            from core.oxm.constants import MAGIC_ALL

            # Get params from query params first (for compatibility)
            params = dict(fastapi_request.query_params)

            # Try to get params from body (preferred method)
            if body := await fastapi_request.body():
                with suppress(json.JSONDecodeError, TypeError):
                    if isinstance(body_data := json.loads(body), dict):
                        params.update(body_data)

            # Backward compatibility: support id and event_id as alias for memory_id
            id_value = (
                params.get("memory_id")
                or params.get("id")
                or params.get("event_id", MAGIC_ALL)
            )

            # Extract and validate parameters using DeleteMemoriesRequestDTO
            try:
                delete_request = DeleteMemoriesRequestDTO(
                    memory_id=id_value,
                    user_id=params.get("user_id", MAGIC_ALL),
                    group_id=params.get("group_id", MAGIC_ALL),
                )
            except ValueError as e:
                logger.error("Delete request validation failed: %s", e)
                raise HTTPException(status_code=400, detail=str(e)) from e

            logger.info(
                "Received delete request: memory_id=%s, user_id=%s, group_id=%s",
                delete_request.memory_id,
                delete_request.user_id,
                delete_request.group_id,
            )

            # Get delete service
            delete_service = get_bean_by_type(MemCellDeleteService)

            # Execute delete operation (combined filters)
            result = await delete_service.delete_by_combined_criteria(
                id=delete_request.memory_id,
                user_id=delete_request.user_id,
                group_id=delete_request.group_id,
            )

            # Check for validation errors only
            if "error" in result:
                logger.warning("Delete operation validation failed: %s", result)
                raise HTTPException(status_code=400, detail=result["error"])

            # Log deletion result
            logger.info(
                "Delete request completed: count=%d, cascade_count=%s",
                result["count"],
                result.get("cascade_count", {}),
            )

            # Return success response (even if count is 0, it's still a valid operation)
            cascade_count = result.get("cascade_count", {})
            total_deleted = result["count"] + sum(cascade_count.values())
            return {
                "status": ErrorStatus.OK.value,
                "message": f"Delete operation completed, {total_deleted} records affected",
                "result": {
                    "deleted_count": result["count"],
                    "cascade_deleted": cascade_count,
                },
            }

        except HTTPException:
            # Re-raise HTTPException
            raise
        except Exception as e:
            logger.error("Delete request processing failed: %s", e, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to delete memories, please try again later",
            ) from e

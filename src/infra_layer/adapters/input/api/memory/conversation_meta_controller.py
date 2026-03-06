# -*- coding: utf-8 -*-
"""
Conversation Meta Controller - Conversation metadata management controller

Provides RESTful API routes for:
- Conversation metadata (GET/POST/PATCH /conversation-meta): get with default fallback, upsert, and partial update
"""

import logging
from contextlib import suppress
import json

from fastapi import HTTPException, Request as FastAPIRequest

from core.di.decorators import controller
from core.interface.controller.base_controller import BaseController, get, post, patch
from core.constants.errors import ErrorCode, ErrorStatus
from core.constants.exceptions import ValidationException
from infra_layer.adapters.input.api.dto.memory_dto import (
    # Request DTOs
    ConversationMetaCreateRequest,
    ConversationMetaGetRequest,
    ConversationMetaPatchRequest,
    # Response DTOs
    GetConversationMetaResponse,
    SaveConversationMetaResponse,
    PatchConversationMetaResponse,
)
from service.conversation_meta_service import ConversationMetaService

logger = logging.getLogger(__name__)


@controller("conversation_meta_controller", primary=True)
class ConversationMetaController(BaseController):
    """
    Conversation Meta Controller

    Handles conversation metadata CRUD operations.
    """

    def __init__(self, conversation_meta_service: ConversationMetaService):
        """Initialize controller"""
        super().__init__(
            prefix="/api/v0/memories",
            tags=["Conversation Meta Controller"],
            default_auth="none",
        )
        self.conversation_meta_service = conversation_meta_service
        logger.info(
            "ConversationMetaController initialized with ConversationMetaService"
        )

    @get(
        "/conversation-meta",
        response_model=GetConversationMetaResponse,
        summary="Get conversation metadata",
        description="""
        Retrieve conversation metadata by group_id with fallback to default config
        
        ## Functionality:
        - Query by group_id to get conversation metadata
        - If group_id not found, fallback to default config
        - If group_id not provided, returns default config
        
        ## Fallback Logic:
        - Try exact group_id first, then use default config
        
        ## Use Cases:
        - Get specific group's metadata
        - Get default settings (group_id not provided or null)
        - Auto-fallback to defaults when group config not set
        """,
        responses={
            404: {
                "description": "Conversation metadata not found",
                "content": {
                    "application/json": {
                        "example": {
                            "status": "failed",
                            "message": "Conversation metadata not found for group_id: group_123",
                        }
                    }
                },
            }
        },
    )
    async def get_conversation_meta(
        self,
        fastapi_request: FastAPIRequest,
        request_body: ConversationMetaGetRequest = None,  # OpenAPI documentation only
    ) -> GetConversationMetaResponse:
        """
        Get conversation metadata by group_id with fallback support

        Args:
            fastapi_request: FastAPI request object
            request_body: Get request parameters (used for OpenAPI documentation only)

        Returns:
            Dict[str, Any]: Conversation metadata response

        Raises:
            HTTPException: When request processing fails
        """
        del request_body  # Used for OpenAPI documentation only
        try:
            # Get params from query params first
            params = dict(fastapi_request.query_params)

            # Also try to get params from body (for GET + body requests)
            if body := await fastapi_request.body():
                with suppress(json.JSONDecodeError, TypeError):
                    if isinstance(body_data := json.loads(body), dict):
                        params.update(body_data)

            group_id = params.get("group_id")

            logger.info("Received conversation-meta get request: group_id=%s", group_id)

            # Query via service (fallback to default is handled internally)
            result = await self.conversation_meta_service.get_by_group_id(group_id)

            if not result:
                raise HTTPException(
                    status_code=404,
                    detail=f"Conversation metadata not found for group_id: {group_id}",
                )

            message = (
                "Using default config"
                if result.is_default and group_id
                else "Conversation metadata retrieved successfully"
            )

            return {
                "status": ErrorStatus.OK.value,
                "message": message,
                "result": result.model_dump(),
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                "conversation-meta get request processing failed: %s", e, exc_info=True
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to retrieve conversation metadata, please try again later",
            ) from e

    @post(
        "/conversation-meta",
        response_model=SaveConversationMetaResponse,
        summary="Save conversation metadata",
        description="""
        Save conversation metadata information, including scene, participants, tags, etc.
        
        ## Functionality:
        - If group_id exists, update the entire record (upsert)
        - If group_id does not exist, create a new record
        - If group_id is omitted, save as default config for the scene
        - All fields must be provided with complete data
        
        ## Default Config:
        - Default config is used as fallback when specific group_id config not found
        
        ## Notes:
        - This is a full update interface that will replace the entire record
        - If you only need to update partial fields, use the PATCH /conversation-meta interface
        """,
        responses={
            400: {
                "description": "Request parameter error",
                "content": {
                    "application/json": {
                        "example": {
                            "status": ErrorStatus.FAILED.value,
                            "code": ErrorCode.INVALID_PARAMETER.value,
                            "message": "Field 'scene': invalid scene value",
                            "timestamp": "2025-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories/conversation-meta",
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
                            "message": "Failed to save conversation metadata, please try again later",
                            "timestamp": "2025-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories/conversation-meta",
                        }
                    }
                },
            },
        },
    )
    async def save_conversation_meta(
        self,
        fastapi_request: FastAPIRequest,
        request_body: ConversationMetaCreateRequest = None,  # OpenAPI documentation only
    ) -> SaveConversationMetaResponse:
        """
        Save conversation metadata

        Save conversation metadata to MongoDB via service

        Args:
            fastapi_request: FastAPI request object
            request_body: Conversation metadata request body (used for OpenAPI documentation only)

        Returns:
            Dict[str, Any]: Save response, containing saved metadata information

        Raises:
            HTTPException: When request processing fails
        """
        del request_body  # Used for OpenAPI documentation only
        try:
            # 1. Parse request body into DTO
            request_data = await fastapi_request.json()
            create_request = ConversationMetaCreateRequest(**request_data)

            logger.info(
                "Received conversation-meta save request: group_id=%s",
                create_request.group_id,
            )

            # 2. Save via service
            result = await self.conversation_meta_service.save(create_request)

            if not result:
                raise HTTPException(
                    status_code=500, detail="Failed to save conversation metadata"
                )

            # 3. Return success response
            return {
                "status": ErrorStatus.OK.value,
                "message": "Conversation metadata saved successfully",
                "result": result.model_dump(),
            }

        except ValidationException as e:
            logger.error(
                "conversation-meta validation failed: %s", e.message, exc_info=True
            )
            raise HTTPException(status_code=400, detail=e.message) from e
        except ValueError as e:
            logger.error("conversation-meta request parameter error: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        except HTTPException:
            raise
        except Exception as e:
            logger.error(
                "conversation-meta request processing failed: %s", e, exc_info=True
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to save conversation metadata, please try again later",
            ) from e

    @patch(
        "/conversation-meta",
        response_model=PatchConversationMetaResponse,
        summary="Partially update conversation metadata",
        description="""
        Partially update conversation metadata, only updating provided fields
        
        ## Functionality:
        - Locate the conversation metadata to update by group_id
        - When group_id is null or not provided, updates the default config
        - Only update fields provided in the request, keep unchanged fields as-is
        - Suitable for scenarios requiring modification of partial information
        
        ## Fields that can be updated:
        - **name**: Conversation name
        - **description**: Conversation description
        - **scene_desc**: Scene description
        - **tags**: Tag list
        - **user_details**: User details (will completely replace existing user_details)
        - **default_timezone**: Default timezone
        
        ## Notes:
        - group_id can be a specific value or omitted (for default config)
        - If user_details field is provided, it will completely replace existing user details
        - Not allowed to modify core fields such as version, scene, group_id, conversation_created_at
        """,
        responses={
            400: {
                "description": "Request parameter error",
                "content": {
                    "application/json": {
                        "example": {
                            "status": ErrorStatus.FAILED.value,
                            "code": ErrorCode.INVALID_PARAMETER.value,
                            "message": "Missing required field group_id",
                            "timestamp": "2025-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories/conversation-meta",
                        }
                    }
                },
            },
            404: {
                "description": "Conversation metadata not found",
                "content": {
                    "application/json": {
                        "example": {
                            "status": ErrorStatus.FAILED.value,
                            "code": ErrorCode.RESOURCE_NOT_FOUND.value,
                            "message": "Specified conversation metadata not found: group_123",
                            "timestamp": "2025-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories/conversation-meta",
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
                            "message": "Failed to update conversation metadata, please try again later",
                            "timestamp": "2025-01-15T10:30:00+00:00",
                            "path": "/api/v0/memories/conversation-meta",
                        }
                    }
                },
            },
        },
    )
    async def patch_conversation_meta(
        self,
        fastapi_request: FastAPIRequest,
        request_body: ConversationMetaPatchRequest = None,  # OpenAPI documentation only
    ) -> PatchConversationMetaResponse:
        """
        Partially update conversation metadata

        Locate record by group_id, only update fields provided in the request

        Args:
            fastapi_request: FastAPI request object
            request_body: Patch request body (used for OpenAPI documentation only)

        Returns:
            Dict[str, Any]: Update response, containing updated metadata information

        Raises:
            HTTPException: When request processing fails
        """
        del request_body  # Used for OpenAPI documentation only
        try:
            # 1. Parse request body into DTO
            request_data = await fastapi_request.json()
            patch_request = ConversationMetaPatchRequest(**request_data)

            logger.info(
                "Received conversation-meta partial update request: group_id=%s",
                patch_request.group_id,
            )

            # 2. Patch via service
            result, updated_fields = await self.conversation_meta_service.patch(
                patch_request
            )

            if result is None:
                detail_msg = (
                    f"Specified conversation metadata not found: group_id={patch_request.group_id}"
                    if patch_request.group_id
                    else "Default config not found"
                )
                raise HTTPException(status_code=404, detail=detail_msg)

            # 3. Return success response
            if not updated_fields:
                return {
                    "status": ErrorStatus.OK.value,
                    "message": "No fields need updating",
                    "result": {
                        "id": result.id,
                        "group_id": result.group_id,
                        "updated_fields": [],
                    },
                }

            return {
                "status": ErrorStatus.OK.value,
                "message": f"Conversation metadata updated successfully, updated {len(updated_fields)} fields",
                "result": {
                    "id": result.id,
                    "group_id": result.group_id,
                    "scene": result.scene,
                    "name": result.name,
                    "updated_fields": updated_fields,
                    "updated_at": result.updated_at,
                },
            }

        except ValidationException as e:
            logger.error(
                "conversation-meta partial update validation failed: %s",
                e.message,
                exc_info=True,
            )
            raise HTTPException(status_code=400, detail=e.message) from e
        except HTTPException:
            # Re-raise HTTPException
            raise
        except KeyError as e:
            logger.error(
                "conversation-meta partial update request missing required field: %s", e
            )
            raise HTTPException(
                status_code=400, detail=f"Missing required field: {str(e)}"
            ) from e
        except ValueError as e:
            logger.error(
                "conversation-meta partial update request parameter error: %s", e
            )
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(
                "conversation-meta partial update request processing failed: %s",
                e,
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail="Failed to update conversation metadata, please try again later",
            ) from e

# -*- coding: utf-8 -*-
"""
Conversation metadata service

Provides business logic for conversation metadata operations.
"""

import logging
from typing import Any, Dict, Optional

from core.di import service
from core.di.utils import get_bean_by_type
from infra_layer.adapters.out.persistence.repository.conversation_meta_raw_repository import (
    ConversationMetaRawRepository,
)
from infra_layer.adapters.out.persistence.document.memory.conversation_meta import (
    ConversationMeta,
    UserDetailModel,
    LlmCustomSettingModel,
)
from infra_layer.adapters.input.api.dto.memory_dto import (
    ConversationMetaCreateRequest,
    ConversationMetaPatchRequest,
    ConversationMetaResponse,
)
from service.conversation_meta_checker import ConversationMetaChecker, Operation

logger = logging.getLogger(__name__)


@service("conversation_meta_service")
class ConversationMetaService:
    """
    Conversation metadata service

    Provides:
    - Get conversation metadata with automatic fallback to default
    - Create/update conversation metadata
    - Partial update conversation metadata
    """

    def __init__(self):
        """Initialize service"""
        self._repository: Optional[ConversationMetaRawRepository] = None

    def _get_repository(self) -> ConversationMetaRawRepository:
        """Get repository (lazy loading)"""
        if self._repository is None:
            self._repository = get_bean_by_type(ConversationMetaRawRepository)
        return self._repository

    def _to_response(self, meta: ConversationMeta) -> ConversationMetaResponse:
        """
        Convert ConversationMeta document to response DTO

        Args:
            meta: ConversationMeta document

        Returns:
            ConversationMetaResponse DTO
        """
        # Convert llm_custom_setting model to dict for response
        llm_setting = getattr(meta, "llm_custom_setting", None)
        llm_setting_dict = llm_setting.to_dict() if llm_setting else None

        return ConversationMetaResponse(
            id=str(meta.id),
            group_id=meta.group_id,
            scene=meta.scene,
            scene_desc=meta.scene_desc,
            llm_custom_setting=llm_setting_dict,
            name=meta.name,
            description=meta.description,
            conversation_created_at=meta.conversation_created_at,
            default_timezone=meta.default_timezone,
            user_details={
                uid: {
                    "full_name": detail.full_name,
                    "role": detail.role,
                    "extra": detail.extra,
                }
                for uid, detail in (meta.user_details or {}).items()
            },
            tags=meta.tags or [],
            is_default=meta.group_id is None,
            created_at=meta.created_at.isoformat() if meta.created_at else None,
            updated_at=meta.updated_at.isoformat() if meta.updated_at else None,
        )

    async def get_by_group_id(
        self, group_id: Optional[str]
    ) -> Optional[ConversationMetaResponse]:
        """
        Get conversation metadata by group_id

        Automatically falls back to default config if group_id not found.

        Args:
            group_id: Group ID (None to get default config directly)

        Returns:
            ConversationMetaResponse or None if not found
        """
        repo = self._get_repository()
        meta = await repo.get_by_group_id_with_fallback(group_id)

        if not meta:
            logger.debug("Conversation metadata not found for group_id: %s", group_id)
            return None

        logger.info(
            "Retrieved conversation metadata: group_id=%s, is_default=%s",
            meta.group_id,
            meta.group_id is None,
        )
        return self._to_response(meta)

    async def save(
        self, request: ConversationMetaCreateRequest
    ) -> Optional[ConversationMetaResponse]:
        """
        Save (create or update) conversation metadata

        Args:
            request: ConversationMetaCreateRequest DTO

        Returns:
            ConversationMetaResponse or None if failed

        Raises:
            ValidationException: If validation fails based on config level
        """
        repo = self._get_repository()

        # Convert user_details to UserDetailModel
        user_details_model = None
        if request.user_details:
            user_details_model = {}
            for uid, detail in request.user_details.items():
                user_details_model[uid] = UserDetailModel(
                    full_name=detail.full_name, role=detail.role, extra=detail.extra
                )

        # Convert llm_custom_setting to LlmCustomSettingModel
        llm_setting_model = LlmCustomSettingModel.from_any(
            getattr(request, "llm_custom_setting", None)
        )

        # Validate request based on config level (global vs group) + model whitelist
        ConversationMetaChecker.validate_create_request(
            group_id=request.group_id,
            scene=request.scene,
            scene_desc=request.scene_desc,
            name=request.name,
            llm_custom_setting=llm_setting_model,
        )

        # Build conversation data - all fields go through build_save_data
        # This filters based on:
        # - Config level (GLOBAL_ONLY_FIELDS / GROUP_ONLY_FIELDS)
        # - Operation type (IMMUTABLE_ON_PATCH for PATCH)
        # - Fields not in any restriction set are allowed for both levels
        conversation_data = ConversationMetaChecker.build_save_data(
            group_id=request.group_id,
            operation=Operation.CREATE,
            fields={
                "scene": request.scene,
                "scene_desc": request.scene_desc,
                "llm_custom_setting": llm_setting_model,
                "name": request.name,
                "description": request.description,
                "conversation_created_at": request.created_at,
                "default_timezone": request.default_timezone,
                "user_details": user_details_model,
                "tags": request.tags,
            },
            exclude_none=True,
        )

        saved_meta = await repo.upsert_by_group_id(
            group_id=request.group_id, conversation_data=conversation_data
        )

        if not saved_meta:
            logger.error(
                "Failed to save conversation metadata: group_id=%s", request.group_id
            )
            return None

        logger.info(
            "Saved conversation metadata: group_id=%s, is_default=%s",
            saved_meta.group_id,
            saved_meta.group_id is None,
        )
        return self._to_response(saved_meta)

    async def patch(
        self, request: ConversationMetaPatchRequest
    ) -> tuple[Optional[ConversationMetaResponse], list[str]]:
        """
        Partially update conversation metadata

        Args:
            request: ConversationMetaPatchRequest DTO

        Returns:
            Tuple of (ConversationMetaResponse or None, list of updated field names)

        Raises:
            ValidationException: If validation fails based on config level
        """
        repo = self._get_repository()

        # Check if exists (no fallback - we need exact match for PATCH)
        existing_meta = await repo.get_by_group_id(request.group_id)
        if not existing_meta:
            logger.warning(
                "Conversation metadata not found for patch: group_id=%s",
                request.group_id,
            )
            return None, []

        # Convert user_details to UserDetailModel if provided
        user_details_model = None
        if request.user_details is not None:
            user_details_model = {}
            for uid, detail in request.user_details.items():
                user_details_model[uid] = UserDetailModel(
                    full_name=detail.full_name, role=detail.role, extra=detail.extra
                )

        # Convert llm_custom_setting to LlmCustomSettingModel if provided
        llm_setting_model = LlmCustomSettingModel.from_any(
            getattr(request, "llm_custom_setting", None)
        )

        # Validate patch request based on config level + model whitelist
        ConversationMetaChecker.validate_patch_request(
            group_id=request.group_id,
            update_fields={
                k: v
                for k, v in {
                    "name": request.name,
                    "scene_desc": request.scene_desc,
                    "llm_custom_setting": llm_setting_model,
                }.items()
                if v is not None
            },
            llm_custom_setting=llm_setting_model,
        )

        # Build update data - all fields go through build_save_data
        # This filters based on:
        # - Config level (GLOBAL_ONLY_FIELDS / GROUP_ONLY_FIELDS)
        # - Operation type (IMMUTABLE_ON_PATCH - e.g., scene cannot be modified)
        # - Fields not in any restriction set are allowed for both levels
        filtered_data = ConversationMetaChecker.build_save_data(
            group_id=request.group_id,
            operation=Operation.PATCH,
            fields={
                "name": request.name,
                "description": request.description,
                "scene_desc": request.scene_desc,
                "llm_custom_setting": llm_setting_model,
                "tags": request.tags,
                "default_timezone": request.default_timezone,
                "user_details": user_details_model,
            },
            exclude_none=True,
        )

        if not filtered_data:
            logger.debug("No fields to update for group_id=%s", request.group_id)
            return self._to_response(existing_meta), []

        updated_fields = list(filtered_data.keys())

        # Perform update
        updated_meta = await repo.update_by_group_id(
            group_id=request.group_id, update_data=filtered_data
        )

        if not updated_meta:
            logger.error(
                "Failed to update conversation metadata: group_id=%s", request.group_id
            )
            return None, []

        logger.info(
            "Updated conversation metadata: group_id=%s, updated_fields=%s",
            updated_meta.group_id,
            updated_fields,
        )
        return self._to_response(updated_meta), updated_fields

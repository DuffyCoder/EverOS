# -*- coding: utf-8 -*-
"""
Conversation metadata checker/validator

Provides validation logic for conversation metadata operations.
Distinguishes between global config (no group_id) and group-level config (with group_id).
"""

import logging
import os
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from core.constants.exceptions import ValidationException

logger = logging.getLogger(__name__)


class ConfigLevel(str, Enum):
    """Configuration level"""

    GLOBAL = "global"  # Global config (group_id is None)
    GROUP = "group"  # Group-level config (group_id is provided)

    @classmethod
    def from_group_id(cls, group_id: Optional[str]) -> "ConfigLevel":
        """Determine config level from group_id"""
        return cls.GLOBAL if group_id is None else cls.GROUP


class Operation(str, Enum):
    """Operation type"""

    CREATE = "create"  # Create new record
    PATCH = "patch"  # Partial update existing record


class ConversationMetaChecker:
    """
    Conversation metadata validation checker

    Validates request parameters based on configuration level:
    - GLOBAL (group_id=None): Can set scene, scene_desc, and all other fields
    - GROUP (group_id provided): Cannot set scene (inherited from global config)
    """

    # Fields that are only allowed for global config
    GLOBAL_ONLY_FIELDS: Set[str] = {"scene", "scene_desc", "llm_custom_setting"}

    # Fields that are only allowed for group config
    GROUP_ONLY_FIELDS: Set[str] = {"name"}

    # Fields that cannot be modified on PATCH (immutable after creation)
    IMMUTABLE_ON_PATCH: Set[str] = {"scene", "conversation_created_at"}

    # Fields that are allowed for both global and group config
    COMMON_FIELDS: Set[str] = {
        "description",
        "tags",
        "user_details",
        "default_timezone",
        "conversation_created_at",
    }

    # All allowed fields for global config
    GLOBAL_ALLOWED_FIELDS: Set[str] = GLOBAL_ONLY_FIELDS | COMMON_FIELDS

    # All allowed fields for group config
    GROUP_ALLOWED_FIELDS: Set[str] = GROUP_ONLY_FIELDS | COMMON_FIELDS

    @classmethod
    def get_config_level(cls, group_id: Optional[str]) -> ConfigLevel:
        """
        Get configuration level from group_id

        Args:
            group_id: Group ID (None for global config)

        Returns:
            ConfigLevel enum value
        """
        return ConfigLevel.from_group_id(group_id)

    @classmethod
    def validate_create_request(
        cls,
        group_id: Optional[str],
        scene: Optional[str] = None,
        scene_desc: Optional[Dict[str, Any]] = None,
        name: Optional[str] = None,
        llm_custom_setting: Any = None,
    ) -> None:
        """
        Validate create request parameters based on config level

        Args:
            group_id: Group ID (None for global config)
            scene: Scene identifier
            scene_desc: Scene description
            name: Display name (group name)
            llm_custom_setting: LLM custom setting (validated against provider whitelist)

        Raises:
            ValidationException: If validation fails
        """
        config_level = cls.get_config_level(group_id)

        if config_level == ConfigLevel.GROUP:
            # Group config: scene and scene_desc are not allowed, name is required
            if scene is not None:
                raise ValidationException(
                    message="Group-level config cannot set 'scene'. "
                    "Scene is inherited from global config. "
                    "Please remove 'scene' field or use group_id=null for global config.",
                    field="scene",
                    details={"error_code": "INVALID_FIELD_FOR_GROUP_CONFIG"},
                )
            if scene_desc is not None:
                raise ValidationException(
                    message="Group-level config cannot set 'scene_desc'. "
                    "Scene description is inherited from global config. "
                    "Please remove 'scene_desc' field or use group_id=null for global config.",
                    field="scene_desc",
                    details={"error_code": "INVALID_FIELD_FOR_GROUP_CONFIG"},
                )
            if name is None:
                raise ValidationException(
                    message="Group-level config requires 'name' field to be set.",
                    field="name",
                    details={"error_code": "MISSING_REQUIRED_FIELD"},
                )
            logger.debug(
                "Validated CREATE request for GROUP config: group_id=%s", group_id
            )
        else:
            # Global config: scene and scene_desc are required, name is not allowed
            if scene is None:
                raise ValidationException(
                    message="Global config requires 'scene' field to be set.",
                    field="scene",
                    details={"error_code": "MISSING_REQUIRED_FIELD"},
                )
            if scene_desc is None:
                raise ValidationException(
                    message="Global config requires 'scene_desc' field to be set.",
                    field="scene_desc",
                    details={"error_code": "MISSING_REQUIRED_FIELD"},
                )
            if name is not None:
                raise ValidationException(
                    message="Global config cannot set 'name'. "
                    "Name is only for group-level config. "
                    "Please remove 'name' field or provide a group_id.",
                    field="name",
                    details={"error_code": "INVALID_FIELD_FOR_GLOBAL_CONFIG"},
                )
            logger.debug("Validated CREATE request for GLOBAL config")

        # Validate llm_custom_setting provider/model against whitelist
        cls._validate_llm_custom_setting(llm_custom_setting)

    @classmethod
    def validate_patch_request(
        cls,
        group_id: Optional[str],
        update_fields: Dict[str, Any],
        llm_custom_setting: Any = None,
    ) -> None:
        """
        Validate patch request parameters based on config level

        Args:
            group_id: Group ID (None for global config)
            update_fields: Fields to update (field_name -> value)
            llm_custom_setting: LLM custom setting (validated against provider whitelist)

        Raises:
            ValidationException: If validation fails
        """
        config_level = cls.get_config_level(group_id)
        field_names = set(update_fields.keys())

        if config_level == ConfigLevel.GROUP:
            # Group config: check for disallowed fields (global-only fields)
            disallowed_fields = cls.GLOBAL_ONLY_FIELDS & field_names
            if disallowed_fields:
                raise ValidationException(
                    message=f"Group-level config cannot update fields: {sorted(disallowed_fields)}. "
                    "These fields can only be set in global config (group_id=null).",
                    details={"error_code": "INVALID_FIELD_FOR_GROUP_CONFIG"},
                )
            logger.debug(
                "Validated PATCH request for GROUP config: group_id=%s, fields=%s",
                group_id,
                list(field_names),
            )
        else:
            # Global config: check for disallowed fields (group-only fields)
            disallowed_fields = cls.GROUP_ONLY_FIELDS & field_names
            if disallowed_fields:
                raise ValidationException(
                    message=f"Global config cannot update fields: {sorted(disallowed_fields)}. "
                    "These fields can only be set in group-level config (with group_id).",
                    details={"error_code": "INVALID_FIELD_FOR_GLOBAL_CONFIG"},
                )
            logger.debug(
                "Validated PATCH request for GLOBAL config: fields=%s",
                list(field_names),
            )

        # Validate llm_custom_setting provider/model against whitelist
        cls._validate_llm_custom_setting(llm_custom_setting)

    @classmethod
    def get_allowed_fields(cls, group_id: Optional[str]) -> Set[str]:
        """
        Get allowed fields for the given config level

        Args:
            group_id: Group ID (None for global config)

        Returns:
            Set of allowed field names
        """
        config_level = cls.get_config_level(group_id)
        if config_level == ConfigLevel.GLOBAL:
            return cls.GLOBAL_ALLOWED_FIELDS.copy()
        return cls.GROUP_ALLOWED_FIELDS.copy()

    @classmethod
    def get_disallowed_fields(cls, group_id: Optional[str]) -> Set[str]:
        """
        Get disallowed fields for the given config level

        Args:
            group_id: Group ID (None for global config)

        Returns:
            Set of disallowed field names
        """
        config_level = cls.get_config_level(group_id)
        if config_level == ConfigLevel.GLOBAL:
            return cls.GROUP_ONLY_FIELDS.copy()  # Global cannot set group-only fields
        return cls.GLOBAL_ONLY_FIELDS.copy()  # Group cannot set global-only fields

    @classmethod
    def filter_allowed_fields(
        cls, group_id: Optional[str], fields: Dict[str, Any]
    ) -> tuple[Dict[str, Any], List[str]]:
        """
        Filter out disallowed fields based on config level

        Args:
            group_id: Group ID (None for global config)
            fields: Fields to filter

        Returns:
            Tuple of (filtered_fields, removed_field_names)
        """
        disallowed = cls.get_disallowed_fields(group_id)
        filtered = {}
        removed = []

        for key, value in fields.items():
            if key in disallowed:
                removed.append(key)
            else:
                filtered[key] = value

        if removed:
            logger.warning(
                "Filtered out disallowed fields for group_id=%s: %s", group_id, removed
            )

        return filtered, removed

    @classmethod
    def _validate_llm_custom_setting(cls, llm_setting: Any) -> None:
        """
        Validate LLM custom setting provider/model against whitelist.

        Reads {PROVIDER}_WHITE_LIST env var (comma-separated model names).
        If the env var is not set or empty, no restriction is applied.

        Args:
            llm_setting: Object with boundary/extraction attributes, each having provider/model

        Raises:
            ValidationException: If a model is not in the provider's whitelist
        """
        if not llm_setting:
            return

        from memory_layer.constants import EXTRACT_SCENES

        for task_name in EXTRACT_SCENES:
            config = getattr(llm_setting, task_name, None)
            if config is None:
                continue
            provider = getattr(config, "provider", None)
            model = getattr(config, "model", None)
            if not provider or not model:
                continue
            cls._validate_model_whitelist(provider, model, task_name)

    @staticmethod
    def _validate_model_whitelist(provider: str, model: str, task_name: str) -> None:
        """
        Validate model against the provider's whitelist from environment variable.

        Reads {PROVIDER}_WHITE_LIST env var (comma-separated model names).
        If the env var is not set or empty, no restriction is applied.

        Args:
            provider: Provider name (e.g., "openai", "openrouter")
            model: Model name
            task_name: Task name for error context (e.g., "boundary", "extraction")

        Raises:
            ValidationException: If model is not in the whitelist
        """
        env_key = f"{provider.upper()}_WHITE_LIST"
        raw = os.getenv(env_key, "").strip()
        if not raw:
            return
        allowed_models = {m.strip() for m in raw.split(",") if m.strip()}
        if not allowed_models:
            return
        if model not in allowed_models:
            raise ValidationException(
                message=f"Model '{model}' is not allowed for provider '{provider}' "
                f"(task: {task_name}). "
                f"Allowed models: {', '.join(sorted(allowed_models))}.",
                field=f"llm_custom_setting.{task_name}.model",
                details={"error_code": "MODEL_NOT_IN_WHITELIST"},
            )

    @classmethod
    def build_save_data(
        cls,
        group_id: Optional[str],
        operation: Operation,
        fields: Dict[str, Any],
        exclude_none: bool = True,
    ) -> Dict[str, Any]:
        """
        Build data for save operation based on config level and operation type.

        This method filters fields based on:
        1. Config level (GLOBAL vs GROUP):
           - Fields in GLOBAL_ONLY_FIELDS: only included for global config
           - Fields in GROUP_ONLY_FIELDS: only included for group config
           - Fields not in either set: included for BOTH levels (default allow)
        2. Operation type (CREATE vs PATCH):
           - Fields in IMMUTABLE_ON_PATCH: excluded for PATCH operation

        This design means new fields don't need to be added to any set
        unless they need explicit restriction.

        Args:
            group_id: Group ID (None for global config)
            operation: Operation type (CREATE or PATCH)
            fields: All fields to filter
            exclude_none: If True, exclude fields with None value.
                         If False, include all fields that exist (value can be None).

        Returns:
            Dict containing only the fields allowed for the given level and operation
        """
        config_level = cls.get_config_level(group_id)
        data: Dict[str, Any] = {}

        # Determine which fields are DISALLOWED based on config level
        if config_level == ConfigLevel.GLOBAL:
            level_disallowed = cls.GROUP_ONLY_FIELDS
        else:
            level_disallowed = cls.GLOBAL_ONLY_FIELDS

        # Determine which fields are DISALLOWED based on operation
        if operation == Operation.PATCH:
            operation_disallowed = cls.IMMUTABLE_ON_PATCH
        else:
            operation_disallowed = set()

        # Combine all disallowed fields
        all_disallowed = level_disallowed | operation_disallowed

        # Include fields that:
        # 1. Exist in input
        # 2. Are not disallowed (by level or operation)
        # 3. Pass the exclude_none check if enabled
        for field_name, value in fields.items():
            if field_name in all_disallowed:
                continue
            if exclude_none and value is None:
                continue
            data[field_name] = value

        return data

"""Conversation metadata resource DTOs.

This module contains DTOs related to conversation metadata CRUD operations:
- Create (POST /api/v0/memories/conversation-meta)
- Get (GET /api/v0/memories/conversation-meta)
- Patch (PATCH /api/v0/memories/conversation-meta)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator, SkipValidation

from api_specs.dtos.base import BaseApiResponse
from api_specs.memory_models import MessageSenderRole
from common_utils.datetime_utils import get_timezone


# =============================================================================
# Common Types
# =============================================================================


class UserDetail(BaseModel):
    """User details

    Structure for the value of ConversationMetaRequest.user_details
    """

    full_name: Optional[str] = Field(
        default=None, description="User full name", examples=["John Smith"]
    )
    role: Optional[str] = Field(
        default=None,
        description="""User type role, used to identify if this user is a human or AI.
Enum values from MessageSenderRole:
- user: Human user
- assistant: AI assistant/bot""",
        examples=["user", "assistant"],
    )
    custom_role: Optional[str] = Field(
        default=None,
        description="User's job/position role (e.g. developer, designer, manager)",
        examples=["developer"],
    )
    extra: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional information",
        examples=[{"department": "Engineering"}],
    )

    @model_validator(mode="after")
    def validate_role(self):
        """Validate that role is a valid MessageSenderRole value"""
        if self.role is not None and not MessageSenderRole.is_valid(self.role):
            raise ValueError(
                f"Invalid role '{self.role}'. Must be one of: {[r.value for r in MessageSenderRole]}"
            )
        return self


class LlmProviderConfig(BaseModel):
    """LLM provider configuration

    Defines the provider and model for a specific LLM task.
    """

    provider: str = Field(
        ...,
        description="LLM provider name",
        examples=["openai", "openrouter", "anthropic"],
    )
    model: str = Field(
        ...,
        description="Model name",
        examples=["gpt-4.1-mini", "qwen/qwen3-235b-a22b-2507"],
    )
    extra: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional provider-specific configuration",
        examples=[{"temperature": 0.7, "max_tokens": 1024}],
    )


class LlmCustomSetting(BaseModel):
    """LLM custom settings for algorithm control

    Allows configuring different LLM providers/models for different tasks.
    Only applicable to global config (group_id=null).

    Example:
        {
            "boundary": {"provider": "openai", "model": "gpt-4.1-mini"},
            "extraction": {"provider": "openrouter", "model": "qwen/qwen3-235b-a22b-2507"}
        }
    """

    boundary: Optional[LlmProviderConfig] = Field(
        default=None,
        description="LLM config for boundary detection (fast, cheap model recommended)",
        examples=[{"provider": "openai", "model": "gpt-4.1-mini"}],
    )
    extraction: Optional[LlmProviderConfig] = Field(
        default=None,
        description="LLM config for memory extraction (high quality model recommended)",
        examples=[{"provider": "openrouter", "model": "qwen/qwen3-235b-a22b-2507"}],
    )
    extra: Optional[Dict[str, Any]] = Field(
        default=None, description="Additional task-specific LLM configurations"
    )


# =============================================================================
# Internal Request (Business Layer)
# =============================================================================


class ConversationMetaRequest(BaseModel):
    """Conversation metadata request (internal use)"""

    scene: str  # Scene identifier
    scene_desc: Dict[
        str, Any
    ]  # Scene description, usually contains fields like description
    name: str  # Conversation name
    group_id: str  # Group ID
    created_at: str  # Creation time, ISO format string
    description: Optional[str] = None  # Conversation description
    default_timezone: Optional[str] = Field(
        default_factory=lambda: get_timezone().key
    )  # Default timezone
    user_details: Dict[str, UserDetail] = Field(
        default_factory=dict
    )  # User details, key is dynamic (e.g., user_001, robot_001), value structure is fixed
    tags: List[str] = Field(default_factory=list)  # List of tags


# =============================================================================
# Create DTOs (POST /api/v0/memories/conversation-meta)
# =============================================================================


class ConversationMetaCreateRequest(BaseModel):
    """
    Create conversation metadata request body

    Used for POST /api/v0/memories/conversation-meta endpoint

    ## Config Levels:
    - **Global Config** (group_id=null): Sets default settings for a scene.
      - Required fields: scene, scene_desc, created_at
      - Optional fields: llm_custom_setting, description, tags, user_details, default_timezone
      - Cannot set: name (name is only for group config)
    - **Group Config** (group_id provided): Sets settings for a specific group.
      - Required fields: name, created_at
      - Cannot set: scene, scene_desc, llm_custom_setting (inherited from global config)
    """

    scene: Optional[str] = Field(
        default=None,
        description="""Scene identifier. **Required for global config (group_id=null), 
not allowed for group config (group_id provided).**

Enum values from ScenarioType:
- group_chat: work/group chat scenario, suitable for group conversations such as multi-person collaboration and project discussions
- assistant: assistant scenario, suitable for one-on-one AI assistant conversations""",
        examples=["group_chat"],
    )
    scene_desc: Optional[Dict[str, Any]] = Field(
        default=None,
        description="""Scene description object. **Required for global config (group_id=null), 
not allowed for group config (group_id provided).**

Can include fields like description, type, etc.""",
        examples=[
            {
                "description": "Project discussion group chat",
                "type": "project_discussion",
            }
        ],
    )
    llm_custom_setting: Optional[LlmCustomSetting] = Field(
        default=None,
        description="""LLM custom settings for algorithm control. **Only for global config (group_id=null), 
not allowed for group config (group_id provided).**

Allows configuring different LLM providers/models for different tasks like boundary detection and memory extraction.""",
        examples=[
            {
                "boundary": {"provider": "openai", "model": "gpt-4.1-mini"},
                "extraction": {
                    "provider": "openrouter",
                    "model": "qwen/qwen3-235b-a22b-2507",
                },
            }
        ],
    )
    name: Optional[str] = Field(
        default=None,
        description="""Group/conversation name. **Required for group config (group_id provided), 
not allowed for global config (group_id=null).**""",
        examples=["Project Discussion Group"],
    )
    description: Optional[str] = Field(
        default=None,
        description="Conversation description",
        examples=["Technical discussion for new feature development"],
    )
    group_id: Optional[str] = Field(
        default=None,
        description="Group unique identifier. When null/not provided, represents default settings for this scene.",
        examples=["group_123", None],
    )
    created_at: str = Field(
        ...,
        description="Conversation creation time (ISO 8601 format)",
        examples=["2025-01-15T10:00:00+00:00"],
    )
    default_timezone: Optional[str] = Field(
        default=None, description="Default timezone", examples=["UTC"]
    )
    user_details: Optional[Dict[str, UserDetail]] = Field(
        default=None,
        description="Participant details, key is user ID, value is user detail object",
        examples=[
            {
                "user_001": {
                    "full_name": "John Smith",
                    "role": "user",
                    "custom_role": "developer",
                    "extra": {"department": "Engineering"},
                },
                "bot_001": {
                    "full_name": "AI Assistant",
                    "role": "assistant",
                    "custom_role": "assistant",
                    "extra": {"type": "ai"},
                },
            }
        ],
    )
    tags: Optional[List[str]] = Field(
        default=None, description="Tag list", examples=[["work", "technical"]]
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Group config - name REQUIRED, scene/scene_desc NOT allowed",
                    "value": {
                        "name": "Project Discussion Group",
                        "description": "Technical discussion for new feature development",
                        "group_id": "group_123",
                        "created_at": "2025-01-15T10:00:00+00:00",
                        "default_timezone": "UTC",
                        "user_details": {
                            "user_001": {
                                "full_name": "John Smith",
                                "role": "user",
                                "custom_role": "developer",
                                "extra": {"department": "Engineering"},
                            },
                            "bot_001": {
                                "full_name": "AI Assistant",
                                "role": "assistant",
                            },
                        },
                        "tags": ["work", "technical"],
                    },
                },
                {
                    "summary": "Global config - scene/scene_desc REQUIRED, name NOT allowed",
                    "value": {
                        "scene": "group_chat",
                        "scene_desc": {
                            "description": "Default conversation meta config"
                        },
                        "description": "Default settings for group_chat scene",
                        "group_id": None,
                        "created_at": "2025-01-15T10:00:00+00:00",
                        "default_timezone": "UTC",
                        "tags": ["default"],
                    },
                },
            ]
        }
    }


# =============================================================================
# Get DTOs (GET /api/v0/memories/conversation-meta)
# =============================================================================


class ConversationMetaGetRequest(BaseModel):
    """
    Get conversation metadata request parameters

    Used for GET /api/v0/memories/conversation-meta endpoint
    """

    group_id: Optional[str] = Field(
        default=None,
        description="Group ID to look up. If not found, will automatically fallback to default config (group_id=null). If not provided, returns default config directly.",
        examples=["group_123"],
    )

    model_config = {"json_schema_extra": {"example": {"group_id": "group_123"}}}


class ConversationMetaResponse(BaseModel):
    """
    Conversation metadata response DTO (result data)

    Used for GET /api/v0/memories/conversation-meta response
    """

    id: str = Field(..., description="Document ID")
    group_id: Optional[str] = Field(
        default=None, description="Group ID (null for global config)"
    )
    scene: Optional[str] = Field(
        default=None, description="Scene identifier (only for global config)"
    )
    scene_desc: Optional[Dict[str, Any]] = Field(
        default=None, description="Scene description (only for global config)"
    )
    llm_custom_setting: Optional[LlmCustomSetting] = Field(
        default=None, description="LLM custom settings (only for global config)"
    )
    name: Optional[str] = Field(
        default=None, description="Group/conversation name (only for group config)"
    )
    description: Optional[str] = Field(default=None, description="Description")
    conversation_created_at: Optional[str] = Field(
        default=None, description="Conversation creation time"
    )
    default_timezone: Optional[str] = Field(
        default=None, description="Default timezone"
    )
    user_details: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict, description="User details"
    )
    tags: List[str] = Field(default_factory=list, description="Tags")
    is_default: bool = Field(
        default=False, description="Whether this is the global (default) config"
    )
    created_at: Optional[str] = Field(default=None, description="Record creation time")
    updated_at: Optional[str] = Field(default=None, description="Record update time")

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "507f1f77bcf86cd799439011",
                "group_id": "group_123",
                "scene": "group_chat",
                "scene_desc": {"description": "Project discussion group chat"},
                "name": "Project Discussion",
                "description": "Technical discussion group",
                "conversation_created_at": "2025-01-15T10:00:00+00:00",
                "default_timezone": "UTC",
                "user_details": {
                    "user_001": {
                        "full_name": "John",
                        "role": "user",
                        "custom_role": "developer",
                    },
                    "bot_001": {"full_name": "AI Assistant", "role": "assistant"},
                },
                "tags": ["work", "tech"],
                "is_default": False,
                "created_at": "2025-01-15T10:00:00+00:00",
                "updated_at": "2025-01-15T10:00:00+00:00",
            }
        }
    }


class GetConversationMetaResponse(BaseApiResponse[ConversationMetaResponse]):
    """Get conversation metadata API response

    Response for GET /api/v0/memories/conversation-meta endpoint.
    """

    result: ConversationMetaResponse = Field(description="Conversation metadata")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Found by group_id",
                    "value": {
                        "status": "ok",
                        "message": "Conversation metadata retrieved successfully",
                        "result": {
                            "id": "507f1f77bcf86cd799439011",
                            "group_id": "group_123",
                            "scene": "group_chat",
                            "scene_desc": {
                                "description": "Project discussion group chat"
                            },
                            "name": "Project Discussion",
                            "conversation_created_at": "2025-01-15T10:00:00+00:00",
                            "is_default": False,
                        },
                    },
                },
                {
                    "summary": "Fallback to default config",
                    "value": {
                        "status": "ok",
                        "message": "Using default config",
                        "result": {
                            "id": "507f1f77bcf86cd799439012",
                            "group_id": None,
                            "scene": "group_chat",
                            "scene_desc": {
                                "description": "Default conversation meta config"
                            },
                            "name": "Default Settings",
                            "conversation_created_at": "2025-01-15T10:00:00+00:00",
                            "is_default": True,
                        },
                    },
                },
            ]
        }
    }


class SaveConversationMetaResponse(BaseApiResponse[ConversationMetaResponse]):
    """Save conversation metadata API response

    Response for POST /api/v0/memories/conversation-meta endpoint.
    """

    result: ConversationMetaResponse = Field(description="Saved conversation metadata")

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "ok",
                "message": "Conversation metadata saved successfully",
                "result": {
                    "id": "507f1f77bcf86cd799439011",
                    "group_id": "group_123",
                    "scene": "group_chat",
                    "scene_desc": {"description": "Project discussion group chat"},
                    "name": "Project Discussion",
                    "conversation_created_at": "2025-01-15T10:00:00+00:00",
                    "is_default": False,
                },
            }
        }
    }


# =============================================================================
# Patch DTOs (PATCH /api/v0/memories/conversation-meta)
# =============================================================================


class ConversationMetaPatchRequest(BaseModel):
    """
    Partial update conversation metadata request body

    Used for PATCH /api/v0/memories/conversation-meta endpoint

    ## Config Levels:
    - **Global Config** (group_id=null): Can update scene_desc, llm_custom_setting, description, tags, etc.
      Cannot modify 'name' (name is only for group config).
    - **Group Config** (group_id provided): Can update name, description, tags, etc.
      Cannot modify 'scene', 'scene_desc', or 'llm_custom_setting' (inherited from global config).
    """

    group_id: Optional[str] = Field(
        default=None,
        description="Group ID to update. When null, updates the global (default) config.",
        examples=["group_123", None],
    )
    name: Optional[str] = Field(
        default=None,
        description="""New group/conversation name. **Only allowed for group config (group_id provided).
Not allowed for global config.**""",
        examples=["New Group Name"],
    )
    description: Optional[str] = Field(
        default=None, description="New description", examples=["Updated description"]
    )
    scene_desc: Optional[Dict[str, Any]] = Field(
        default=None,
        description="""New scene description. **Only allowed for global config (group_id=null).
Not allowed for group config (inherited from global config).**""",
        examples=[{"description": "Updated scene description"}],
    )
    llm_custom_setting: Optional[LlmCustomSetting] = Field(
        default=None,
        description="""New LLM custom settings. **Only allowed for global config (group_id=null).
Not allowed for group config (inherited from global config).**""",
        examples=[
            {
                "boundary": {"provider": "openai", "model": "gpt-4.1-mini"},
                "extraction": {
                    "provider": "openrouter",
                    "model": "qwen/qwen3-235b-a22b-2507",
                },
            }
        ],
    )
    tags: Optional[List[str]] = Field(
        default=None, description="New tag list", examples=[["tag1", "tag2"]]
    )
    user_details: Optional[Dict[str, UserDetail]] = Field(
        default=None,
        description="New user details (will completely replace existing user_details)",
        examples=[
            {
                "user_001": {
                    "full_name": "John Smith",
                    "role": "user",
                    "custom_role": "lead",
                }
            }
        ],
    )
    default_timezone: Optional[str] = Field(
        default=None, description="New default timezone", examples=["Asia/Shanghai"]
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Update group config - can update name",
                    "value": {
                        "group_id": "group_123",
                        "name": "New Group Name",
                        "tags": ["updated", "tags"],
                    },
                },
                {
                    "summary": "Update global config - can update scene_desc, llm_custom_setting",
                    "value": {
                        "group_id": None,
                        "scene_desc": {"description": "Updated scene description"},
                        "llm_custom_setting": {"temperature": 0.8},
                    },
                },
            ]
        }
    }


class PatchConversationMetaResult(BaseModel):
    """Patch conversation metadata result data"""

    id: str = Field(..., description="Document ID")
    group_id: Optional[str] = Field(
        default=None, description="Group ID (null for default config)"
    )
    scene: Optional[str] = Field(default=None, description="Scene identifier")
    name: Optional[str] = Field(default=None, description="Conversation name")
    updated_fields: List[str] = Field(
        default_factory=list, description="List of updated field names"
    )
    updated_at: Optional[str] = Field(default=None, description="Record update time")

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "507f1f77bcf86cd799439011",
                "group_id": "group_123",
                "scene": "group_chat",
                "name": "New conversation name",
                "updated_fields": ["name", "tags"],
                "updated_at": "2025-01-15T10:30:00+00:00",
            }
        }
    }


class PatchConversationMetaResponse(BaseApiResponse[PatchConversationMetaResult]):
    """Patch conversation metadata API response

    Response for PATCH /api/v0/memories/conversation-meta endpoint.
    """

    result: PatchConversationMetaResult = Field(
        description="Patch result with updated fields"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "status": "ok",
                "message": "Conversation metadata updated successfully, updated 2 fields",
                "result": {
                    "id": "507f1f77bcf86cd799439011",
                    "group_id": "group_123",
                    "scene": "group_chat",
                    "name": "New conversation name",
                    "updated_fields": ["name", "tags"],
                    "updated_at": "2025-01-15T10:30:00+00:00",
                },
            }
        }
    }

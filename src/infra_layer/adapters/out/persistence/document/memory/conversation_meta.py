"""
ConversationMeta Beanie ODM model

A conversation metadata document model based on Beanie ODM, storing complete metadata of conversations.
"""

from datetime import datetime
from typing import List, Optional, Dict, Any
from core.oxm.mongo.document_base import DocumentBase
from pydantic import Field, ConfigDict, BaseModel
from pymongo import DESCENDING, IndexModel, ASCENDING
from core.oxm.mongo.audit_base import AuditBase
from common_utils.datetime_utils import get_timezone


class UserDetailModel(BaseModel):
    """User detail nested model

    Used to store user basic information and additional extended information
    """

    full_name: str = Field(..., description="User full name")
    role: Optional[str] = Field(
        default=None, description="User role, e.g.: user, assistant, admin, etc."
    )
    extra: Optional[Dict[str, Any]] = Field(
        default=None, description="Extension fields, supporting dynamic schema"
    )


class LlmProviderConfigModel(BaseModel):
    """LLM provider configuration model

    Defines the provider and model for a specific LLM task
    """

    provider: str = Field(
        ..., description="LLM provider name, e.g.: openai, openrouter."
    )
    model: str = Field(
        ...,
        description="Model name, e.g.: qwen/qwen3-235b-a22b-2507(openrouter), gpt-4.1-mini(openai), etc.",
    )
    extra: Optional[Dict[str, Any]] = Field(
        default=None, description="Additional provider-specific configuration"
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict representation"""
        result = {"provider": self.provider, "model": self.model}
        if self.extra:
            result["extra"] = self.extra
        return result

    @classmethod
    def from_any(cls, data: Any) -> Optional["LlmProviderConfigModel"]:
        """
        Create from dict or DTO object

        Args:
            data: dict, DTO object, or None

        Returns:
            LlmProviderConfigModel or None
        """
        if data is None:
            return None

        if isinstance(data, cls):
            return data

        if isinstance(data, dict):
            return cls(
                provider=data.get("provider", ""),
                model=data.get("model", ""),
                extra=data.get("extra"),
            )

        # Handle DTO object
        if hasattr(data, "provider") and hasattr(data, "model"):
            return cls(
                provider=data.provider,
                model=data.model,
                extra=getattr(data, "extra", None),
            )

        return None


class LlmCustomSettingModel(BaseModel):
    """LLM custom settings model for algorithm control

    Allows configuring different LLM providers/models for different tasks.
    Only applicable to global config (group_id=null).

    Example:
        {
            "boundary": {"provider": "openai", "model": "gpt-4.1-mini"},
            "extraction": {"provider": "openrouter", "model": "qwen/qwen3-235b-a22b-2507"}
        }
    """

    boundary: Optional[LlmProviderConfigModel] = Field(
        default=None,
        description="LLM config for boundary detection (fast, cheap model recommended)",
    )
    extraction: Optional[LlmProviderConfigModel] = Field(
        default=None,
        description="LLM config for memory extraction (high quality model recommended)",
    )
    extra: Optional[Dict[str, Any]] = Field(
        default=None, description="Additional task-specific LLM configurations"
    )

    def to_dict(self) -> Optional[Dict[str, Any]]:
        """
        Convert to dict representation for response

        Returns:
            Dict representation or None if empty
        """
        result: Dict[str, Any] = {}

        if self.boundary:
            result["boundary"] = self.boundary.to_dict()

        if self.extraction:
            result["extraction"] = self.extraction.to_dict()

        if self.extra:
            result["extra"] = self.extra

        return result if result else None

    @classmethod
    def from_any(cls, data: Any) -> Optional["LlmCustomSettingModel"]:
        """
        Create from dict or DTO object

        Args:
            data: dict, DTO object, or None

        Returns:
            LlmCustomSettingModel or None
        """
        if data is None:
            return None

        if isinstance(data, cls):
            return data

        # Handle dict input
        if isinstance(data, dict):
            boundary = LlmProviderConfigModel.from_any(data.get("boundary"))
            extraction = LlmProviderConfigModel.from_any(data.get("extraction"))
            extra = data.get("extra")

            if boundary is None and extraction is None and extra is None:
                return None

            return cls(boundary=boundary, extraction=extraction, extra=extra)

        # Handle DTO object
        if hasattr(data, "boundary") or hasattr(data, "extraction"):
            boundary = LlmProviderConfigModel.from_any(getattr(data, "boundary", None))
            extraction = LlmProviderConfigModel.from_any(
                getattr(data, "extraction", None)
            )
            extra = getattr(data, "extra", None)

            if boundary is None and extraction is None and extra is None:
                return None

            return cls(boundary=boundary, extraction=extraction, extra=extra)

        return None


class ConversationMeta(DocumentBase, AuditBase):
    """
    Conversation metadata document model

    Stores complete metadata of conversations, including scene, participants, tags, etc.
    Used for context management and memory retrieval in multi-turn conversations.
    """

    # Scene information (Global config only)
    scene: Optional[str] = Field(
        default=None,
        description="Scene identifier, used to distinguish different application scenarios. Only for global config.",
    )
    scene_desc: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Scene description information. Only for global config.",
    )
    llm_custom_setting: Optional[LlmCustomSettingModel] = Field(
        default=None,
        description="LLM custom settings for algorithm control. Only for global config.",
    )

    # Conversation basic information (Group config only)
    name: Optional[str] = Field(
        default=None, description="Conversation/group name. Only for group config."
    )
    description: Optional[str] = Field(
        default=None, description="Conversation description"
    )
    group_id: Optional[str] = Field(
        default=None,
        description="Group ID, used to associate a group of conversations. When None, represents default settings.",
    )

    # Time information
    conversation_created_at: str = Field(
        ..., description="Conversation creation time, ISO format string"
    )
    default_timezone: Optional[str] = Field(
        default_factory=lambda: get_timezone().key,
        description="Default timezone, e.g.: UTC",
    )

    # Participant information
    user_details: Dict[str, UserDetailModel] = Field(
        default_factory=dict,
        description="Dictionary of participant details, key is dynamic user ID (e.g., user_001, robot_001), value is user detail",
    )

    # Tags and categories
    tags: List[str] = Field(
        default_factory=list,
        description="List of tags, used for classification and retrieval",
    )

    model_config = ConfigDict(
        # Collection name
        collection="conversation_metas",
        # Validation configuration
        validate_assignment=True,
        # JSON serialization configuration
        json_encoders={datetime: lambda dt: dt.isoformat()},
        # Example data
        json_schema_extra={
            "example": {
                "scene": "scene_a",
                "scene_desc": {"description": "Scene description"},
                "name": "User health consultation conversation",
                "description": "Conversation records between user and AI assistant regarding Beijing travel, health management, sports rehabilitation, etc.",
                "group_id": "example_group_id",  # Can be None for default settings
                "conversation_created_at": "2025-08-26T00:00:00Z",
                "default_timezone": "UTC",
                "user_details": {
                    "user_001": {
                        "full_name": "User",
                        "role": "User",
                        "extra": {
                            "height": 170,
                            "weight": 86,
                            "bmi": 29.8,
                            "waist_circumference": 104,
                            "origin": "Sichuan",
                            "preferences": {
                                "food": "hotpot",
                                "activities": "group activities",
                            },
                        },
                    },
                    "robot_001": {
                        "full_name": "AI Assistant",
                        "role": "Assistant",
                        "extra": {"type": "assistant"},
                    },
                },
                "tags": [
                    "health consultation",
                    "travel planning",
                    "sports rehabilitation",
                    "diet advice",
                ],
            }
        },
        extra="allow",
    )

    class Settings:
        """Beanie settings"""

        name = "conversation_metas"
        indexes = [
            IndexModel(
                [("conversation_created_at", ASCENDING)],
                name="idx_conversation_created_at",
            ),
            # Creation time index
            IndexModel([("created_at", DESCENDING)], name="idx_created_at"),
            # Update time index
            IndexModel([("updated_at", DESCENDING)], name="idx_updated_at"),
            IndexModel(
                [("group_id", ASCENDING)], name="idx_group_id_unique", unique=True
            ),
            # Name index for search optimization (prefix match)
            IndexModel([("name", ASCENDING)], name="idx_name"),
        ]
        validate_on_save = True
        use_state_management = True

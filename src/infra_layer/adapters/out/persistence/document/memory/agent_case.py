"""
AgentCaseRecord - Beanie ODM model for agent cases.

Stores a compressed agent task-solving experience extracted from an agent conversation MemCell.
Each record has: task_intent, approach, quality_score.
"""

from datetime import datetime
from typing import List, Optional, Dict, Any
from core.oxm.mongo.document_base_with_soft_delete import DocumentBaseWithSoftDelete
from pydantic import Field, ConfigDict
from pymongo import IndexModel, ASCENDING, DESCENDING
from core.oxm.mongo.audit_base import AuditBase
from beanie import PydanticObjectId


class AgentCaseRecord(DocumentBaseWithSoftDelete, AuditBase):
    """
    Agent case document model.

    Stores the compressed representation of one agent task-solving interaction.
    One MemCell produces at most one AgentCaseRecord.
    """

    # Identity fields
    user_id: Optional[str] = Field(
        default=None, description="User ID who initiated the task"
    )
    group_id: Optional[str] = Field(
        default=None, description="Group/session ID"
    )
    group_name: Optional[str] = Field(
        default=None, description="Group name"
    )
    timestamp: datetime = Field(..., description="Task occurrence time")

    # Core experience fields (flat, one experience per record)
    task_intent: str = Field(
        default="", description="Rewritten task intent as retrieval key"
    )
    approach: str = Field(
        default="", description="Step-by-step approach with decisions and lessons"
    )
    quality_score: Optional[float] = Field(
        default=None, description="Task completion quality score (0.0-1.0)"
    )
    # Parent linkage (to MemCell)
    parent_type: Optional[str] = Field(
        default=None, description="Parent memory type (e.g., memcell)"
    )
    parent_id: Optional[str] = Field(
        default=None, description="Parent memory ID (MemCell event_id)"
    )

    # Vector embedding
    vector: Optional[List[float]] = Field(
        default=None, description="Embedding vector of task_intent"
    )
    vector_model: Optional[str] = Field(
        default=None, description="Vectorization model used"
    )

    # Extension
    extend: Optional[Dict[str, Any]] = Field(
        default=None, description="Reserved extension field"
    )

    model_config = ConfigDict(
        collection="agent_cases",
        validate_assignment=True,
        json_encoders={datetime: lambda dt: dt.isoformat()},
        json_schema_extra={
            "example": {
                "user_id": "user_12345",
                "group_id": "session_abc",
                "timestamp": "2026-02-14T10:30:00.000Z",
                "task_intent": "Search for open source Python web frameworks and compare their GitHub stars",
                "approach": "1. Searched GitHub for Python web frameworks with >5K stars using web_search\n2. Selected top 3: Django, Flask, FastAPI\n3. Compared GitHub stars and activity metrics\n   - Result: FastAPI has fastest growth rate",
                "quality_score": 0.85,
                "parent_type": "memcell",
                "parent_id": "67af1234abcd5678ef901234",
            }
        },
        extra="allow",
    )

    @property
    def event_id(self) -> Optional[PydanticObjectId]:
        return self.id

    class Settings:
        """Beanie settings"""

        name = "agent_cases"
        indexes = [
            # Soft delete support
            IndexModel(
                [("deleted_at", ASCENDING)],
                name="idx_deleted_at",
                sparse=True,
            ),
            # User identity
            IndexModel([("user_id", ASCENDING)], name="idx_user_id", sparse=True),
            # Parent linkage (MemCell)
            IndexModel([("parent_id", ASCENDING)], name="idx_parent_id", sparse=True),
            # User + timestamp for chronological queries
            IndexModel(
                [("user_id", ASCENDING), ("timestamp", DESCENDING)],
                name="idx_user_timestamp",
            ),
            # Group + timestamp for chronological queries
            IndexModel(
                [("group_id", ASCENDING), ("timestamp", DESCENDING)],
                name="idx_group_timestamp",
                sparse=True,
            ),
            # Audit fields
            IndexModel([("created_at", DESCENDING)], name="idx_created_at"),
            IndexModel([("updated_at", DESCENDING)], name="idx_updated_at"),
        ]
        validate_on_save = True
        use_state_management = True

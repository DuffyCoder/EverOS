"""
AgentSkillRecord - Beanie ODM model for agent skill.

Stores reusable skills extracted from clustered AgentCases
within a MemScene (cluster).
"""

from datetime import datetime
from typing import List, Optional, Dict, Any
from core.oxm.mongo.document_base_with_soft_delete import DocumentBaseWithSoftDelete
from pydantic import Field, ConfigDict
from pymongo import IndexModel, ASCENDING, DESCENDING
from core.oxm.mongo.audit_base import AuditBase
from beanie import PydanticObjectId


class AgentSkillRecord(DocumentBaseWithSoftDelete, AuditBase):
    """
    Agent skill document model.

    Stores a single reusable skill extracted from a MemScene
    (cluster of semantically similar AgentCase records).

    Skills are derived by analyzing patterns across multiple AgentCases
    in the same cluster, then merging/refining on each subsequent experience.
    """

    # Cluster linkage (MemScene)
    cluster_id: str = Field(
        ..., description="MemScene cluster ID this skill belongs to"
    )

    # Identity fields
    user_id: Optional[str] = Field(default=None, description="User ID (agent owner)")
    group_id: Optional[str] = Field(default=None, description="Group/session ID")

    # Core content
    name: Optional[str] = Field(default=None, description="Skill name")
    description: Optional[str] = Field(
        default=None,
        description="A clear description of what this skill does and when to use it",
    )
    content: str = Field(..., description="Full skill content")

    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Confidence score (0.0-1.0), increases with more supporting experiences",
    )

    # Vector embedding for semantic retrieval
    vector: Optional[List[float]] = Field(
        default=None, description="Embedding vector of name + description"
    )
    vector_model: Optional[str] = Field(
        default=None, description="Vectorization model used"
    )

    # Extension
    extend: Optional[Dict[str, Any]] = Field(
        default=None, description="Reserved extension field"
    )

    model_config = ConfigDict(
        collection="agent_skills",
        validate_assignment=True,
        json_encoders={datetime: lambda dt: dt.isoformat()},
        json_schema_extra={
            "example": {
                "cluster_id": "cluster_001",
                "name": "Technical comparison research",
                "description": "Compare open source technical solutions or frameworks by searching, extracting, and evaluating key metrics",
                "content": "1. search(tech + open source + github)\n2. Extract repo list from results\n3. Open README for each repo\n4. Compare by stars, activity, and features",
                "confidence": 0.85,
            }
        },
        extra="allow",
    )

    @property
    def skill_id(self) -> Optional[PydanticObjectId]:
        return self.id

    class Settings:
        """Beanie settings"""

        name = "agent_skills"
        indexes = [
            # Cluster ID — primary lookup key
            IndexModel([("cluster_id", ASCENDING)], name="idx_cluster_id"),
            # User ID
            IndexModel([("user_id", ASCENDING)], name="idx_user_id", sparse=True),
            # Group + cluster
            IndexModel(
                [("group_id", ASCENDING), ("cluster_id", ASCENDING)],
                name="idx_group_cluster",
                sparse=True,
            ),
            # Soft delete support
            IndexModel([("deleted_at", ASCENDING)], name="idx_deleted_at", sparse=True),
            # Audit fields
            IndexModel([("created_at", DESCENDING)], name="idx_created_at"),
            IndexModel([("updated_at", DESCENDING)], name="idx_updated_at"),
        ]
        validate_on_save = True
        use_state_management = True

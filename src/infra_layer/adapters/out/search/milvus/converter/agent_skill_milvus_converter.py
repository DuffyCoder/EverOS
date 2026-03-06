"""
AgentSkill Milvus Converter

Converts MongoDB AgentSkillRecord documents into Milvus Collection entities.
"""

import json
from typing import Dict, Any

from core.oxm.milvus.base_converter import BaseMilvusConverter
from core.observation.logger import get_logger
from infra_layer.adapters.out.search.milvus.memory.agent_skill_collection import (
    AgentSkillCollection,
)
from infra_layer.adapters.out.persistence.document.memory.agent_skill import (
    AgentSkillRecord,
)

logger = get_logger(__name__)


class AgentSkillMilvusConverter(BaseMilvusConverter[AgentSkillCollection]):
    """
    Converts MongoDB AgentSkillRecord documents into Milvus entities.

    Vector field: embedding of name + description.
    content field: name + description (maps to primary text field).
    metadata field: full skill details for downstream reconstruction.
    """

    @classmethod
    def from_mongo(cls, source_doc: AgentSkillRecord) -> Dict[str, Any]:
        """
        Convert from MongoDB AgentSkillRecord to Milvus entity dict.

        Args:
            source_doc: MongoDB AgentSkillRecord document instance

        Returns:
            Dict[str, Any]: Milvus entity dictionary ready for insertion

        Raises:
            ValueError: If source_doc is None
        """
        if source_doc is None:
            raise ValueError("MongoDB document cannot be empty")

        try:
            name = source_doc.name or ""
            description = source_doc.description or ""
            content = source_doc.content or ""

            # Primary text field: name + description combined
            content_field = "\n".join(s for s in [name, description] if s)

            # Search content: JSON list of name, description and content
            search_items = [s for s in [name, description, content] if s]

            created_at = (
                int(source_doc.created_at.timestamp()) if source_doc.created_at else 0
            )
            updated_at = (
                int(source_doc.updated_at.timestamp()) if source_doc.updated_at else 0
            )

            metadata = {
                "name": name,
                "description": source_doc.description or "",
                "content": content,
                "cluster_id": source_doc.cluster_id,
                "confidence": source_doc.confidence,
                "vector_model": source_doc.vector_model or "",
            }

            entity = {
                "id": str(source_doc.id),
                "vector": source_doc.vector if source_doc.vector else [],
                "user_id": source_doc.user_id or "",
                "group_id": source_doc.group_id or "",
                "cluster_id": source_doc.cluster_id or "",
                "content": content_field[:5000],
                "search_content": json.dumps(search_items, ensure_ascii=False)[:5000],
                "metadata": json.dumps(metadata, ensure_ascii=False),
                "created_at": created_at,
                "updated_at": updated_at,
            }

            return entity

        except Exception as e:
            logger.error("Failed to convert AgentSkillRecord to Milvus entity: %s", e)
            raise

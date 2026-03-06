"""
AgentCase Milvus Collection Definition

AgentCase-specific Collection class implemented based on TenantAwareMilvusCollectionWithSuffix.
Provides schema and index configuration for semantic search over agent task-solving experiences.
"""

from pymilvus import DataType, FieldSchema, CollectionSchema
from core.oxm.milvus.milvus_collection_base import IndexConfig
from core.tenants.tenantize.oxm.milvus.tenant_aware_collection_with_suffix import (
    TenantAwareMilvusCollectionWithSuffix,
)
from memory_layer.constants import VECTORIZE_DIMENSIONS


class AgentCaseCollection(TenantAwareMilvusCollectionWithSuffix):
    """
    AgentCase Milvus Collection

    Stores vector embeddings of agent task-solving experiences.
    The vector represents the task_intent of one experience per MemCell.
    """

    _COLLECTION_NAME = "agent_case"

    _SCHEMA = CollectionSchema(
        fields=[
            FieldSchema(
                name="id",
                dtype=DataType.VARCHAR,
                is_primary=True,
                auto_id=False,
                max_length=100,
                description="AgentCaseRecord unique identifier",
            ),
            FieldSchema(
                name="vector",
                dtype=DataType.FLOAT_VECTOR,
                dim=VECTORIZE_DIMENSIONS,
                description="Embedding of task_intent",
            ),
            FieldSchema(
                name="user_id",
                dtype=DataType.VARCHAR,
                max_length=100,
                description="User ID",
            ),
            FieldSchema(
                name="group_id",
                dtype=DataType.VARCHAR,
                max_length=100,
                description="Group/session ID",
            ),
            FieldSchema(
                name="timestamp",
                dtype=DataType.INT64,
                description="Task occurrence unix timestamp (seconds)",
            ),
            FieldSchema(
                name="task_intent",
                dtype=DataType.VARCHAR,
                max_length=5000,
                description="Task intent string for text access and search",
            ),
            FieldSchema(
                name="search_content",
                dtype=DataType.VARCHAR,
                max_length=5000,
                description="Task intent for keyword search",
            ),
            FieldSchema(
                name="parent_type",
                dtype=DataType.VARCHAR,
                max_length=100,
                description="Parent memory type (memcell)",
            ),
            FieldSchema(
                name="parent_id",
                dtype=DataType.VARCHAR,
                max_length=100,
                description="Parent MemCell event_id",
            ),
            FieldSchema(
                name="metadata",
                dtype=DataType.VARCHAR,
                max_length=50000,
                description="JSON metadata: {task_intent, approach, quality_score, vector_model}",
            ),
            FieldSchema(
                name="created_at",
                dtype=DataType.INT64,
                description="Creation unix timestamp",
            ),
            FieldSchema(
                name="updated_at",
                dtype=DataType.INT64,
                description="Update unix timestamp",
            ),
        ],
        description="Vector collection for agent case",
        enable_dynamic_field=True,
    )

    _INDEX_CONFIGS = [
        IndexConfig(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        ),
        IndexConfig(field_name="user_id", index_type="AUTOINDEX"),
        IndexConfig(field_name="group_id", index_type="AUTOINDEX"),
        IndexConfig(field_name="timestamp", index_type="AUTOINDEX"),
        IndexConfig(field_name="parent_id", index_type="AUTOINDEX"),
    ]

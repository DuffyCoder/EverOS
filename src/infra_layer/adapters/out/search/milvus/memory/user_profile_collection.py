"""
User Profile Milvus Collection Definition

A collection for storing individual profile items (explicit_info and implicit_traits)
from UserProfile for vector search.

Collection name matches MongoDB: user_profiles

Each item in explicit_info[] and implicit_traits[] is stored as a separate record
to enable fine-grained semantic retrieval.
"""

from pymilvus import DataType, FieldSchema, CollectionSchema
from core.oxm.milvus.milvus_collection_base import IndexConfig
from core.tenants.tenantize.oxm.milvus.tenant_aware_collection_with_suffix import (
    TenantAwareMilvusCollectionWithSuffix,
)
from memory_layer.constants import VECTORIZE_DIMENSIONS


class UserProfileCollection(TenantAwareMilvusCollectionWithSuffix):
    """
    User Profile Milvus Collection

    Stores individual profile items for vector search:
    - explicit_info[i] -> one record
    - implicit_traits[i] -> one record

    Collection name: user_profiles (matches MongoDB)

    Usage:
        collection.async_collection().insert([...])
        collection.async_collection().search([...])
    """

    # Base name for the Collection (matches MongoDB table name)
    _COLLECTION_NAME = "user_profiles"

    # Collection Schema definition
    _SCHEMA = CollectionSchema(
        fields=[
            # Primary key - auto generated UUID
            FieldSchema(
                name="id",
                dtype=DataType.VARCHAR,
                is_primary=True,
                auto_id=False,
                max_length=128,
                description="Unique identifier for profile item",
            ),
            # Vector field for semantic search
            FieldSchema(
                name="vector",
                dtype=DataType.FLOAT_VECTOR,
                dim=VECTORIZE_DIMENSIONS,
                description="Embedding vector from Qwen3-Embedding-4B",
            ),
            # User and group identification
            FieldSchema(
                name="user_id",
                dtype=DataType.VARCHAR,
                max_length=64,
                description="User ID",
            ),
            FieldSchema(
                name="group_id",
                dtype=DataType.VARCHAR,
                max_length=64,
                description="Group ID",
            ),
            # Item type and index for MongoDB lookup
            FieldSchema(
                name="item_type",
                dtype=DataType.VARCHAR,
                max_length=32,
                description="Item type: explicit_info or implicit_trait",
            ),
            FieldSchema(
                name="item_index",
                dtype=DataType.INT32,
                description="Index in the array (for MongoDB lookup)",
            ),
            # Embedding text (for debugging and reference)
            FieldSchema(
                name="embed_text",
                dtype=DataType.VARCHAR,
                max_length=4096,  # Increased from 1024 to handle longer profile descriptions
                description="Text used for generating embedding vector",
            ),
            # Timestamps
            FieldSchema(
                name="created_at",
                dtype=DataType.INT64,
                description="Creation timestamp",
            ),
            FieldSchema(
                name="updated_at",
                dtype=DataType.INT64,
                description="Update timestamp",
            ),
        ],
        description="Vector collection for user profile items (explicit_info and implicit_traits)",
        enable_dynamic_field=True,
    )

    # Index configuration
    _INDEX_CONFIGS = [
        # Vector field index (for similarity search)
        IndexConfig(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE",
            params={
                "M": 16,
                "efConstruction": 200,
            },
        ),
        # Scalar field indexes (for filtering)
        IndexConfig(
            field_name="user_id",
            index_type="AUTOINDEX",
        ),
        IndexConfig(
            field_name="group_id",
            index_type="AUTOINDEX",
        ),
        IndexConfig(
            field_name="item_type",
            index_type="AUTOINDEX",
        ),
    ]

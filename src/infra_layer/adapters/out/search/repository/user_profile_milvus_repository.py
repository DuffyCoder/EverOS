"""
User Profile Milvus Repository

Repository for storing and retrieving profile items (explicit_info and implicit_traits)
in Milvus for vector search.

Collection name matches MongoDB: user_profiles
"""

from datetime import datetime
from typing import List, Optional, Dict, Any
from bson import ObjectId

from core.oxm.milvus.base_repository import BaseMilvusRepository
from core.oxm.constants import MAGIC_ALL
from infra_layer.adapters.out.search.milvus.memory.user_profile_collection import (
    UserProfileCollection,
)
from core.observation.logger import get_logger
from common_utils.datetime_utils import get_now_with_timezone
from core.di.decorators import repository

logger = get_logger(__name__)


@repository("user_profile_milvus_repository", primary=False)
class UserProfileMilvusRepository(BaseMilvusRepository[UserProfileCollection]):
    """
    User Profile Milvus Repository

    Provides:
    - Insert profile items with vectors
    - Delete by user_id + group_id (for full rebuild)
    - Vector search with filtering
    """

    def __init__(self):
        """Initialize user profile repository"""
        super().__init__(UserProfileCollection)

    async def insert_profile_item(
        self,
        user_id: str,
        group_id: str,
        item_type: str,
        item_index: int,
        embed_text: str,
        vector: List[float],
        created_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Insert a single profile item

        Args:
            user_id: User ID
            group_id: Group ID
            item_type: "explicit_info" or "implicit_trait"
            item_index: Index in the source array
            embed_text: Text used for generating embedding
            vector: Embedding vector
            created_at: Creation time
            updated_at: Update time

        Returns:
            Inserted entity dict
        """
        try:
            now = get_now_with_timezone()
            if created_at is None:
                created_at = now
            if updated_at is None:
                updated_at = now

            # Generate unique ID (MongoDB ObjectId format, consistent with other memory types)
            item_id = str(ObjectId())

            entity = {
                "id": item_id,
                "vector": vector,
                "user_id": user_id or "",
                "group_id": group_id or "",
                "item_type": item_type,
                "item_index": item_index,
                "embed_text": embed_text or "",
                "created_at": int(created_at.timestamp()),
                "updated_at": int(updated_at.timestamp()),
            }

            await self.insert(entity)

            logger.debug(
                "✅ Profile item inserted: id=%s, user_id=%s, group_id=%s, type=%s, index=%d",
                item_id,
                user_id,
                group_id,
                item_type,
                item_index,
            )

            return entity

        except Exception as e:
            logger.error(
                "❌ Failed to insert profile item: user_id=%s, group_id=%s, type=%s, error=%s",
                user_id,
                group_id,
                item_type,
                e,
            )
            raise

    async def insert_batch(
        self,
        entities: List[Dict[str, Any]],
        flush: bool = True,
    ) -> int:
        """
        Batch insert profile items

        Args:
            entities: List of entity dicts with fields:
                - user_id, group_id, item_type, item_index, embed_text, vector
            flush: Whether to flush after insert

        Returns:
            Number of inserted entities
        """
        if not entities:
            return 0

        try:
            now = get_now_with_timezone()
            timestamp = int(now.timestamp())

            prepared_entities = []
            for entity in entities:
                # Generate unique ID (MongoDB ObjectId format, consistent with other memory types)
                item_id = str(ObjectId())

                prepared_entities.append({
                    "id": item_id,
                    "vector": entity["vector"],
                    "user_id": entity.get("user_id", ""),
                    "group_id": entity.get("group_id", ""),
                    "item_type": entity.get("item_type", ""),
                    "item_index": entity.get("item_index", 0),
                    "embed_text": entity.get("embed_text", ""),
                    "created_at": timestamp,
                    "updated_at": timestamp,
                })

            # Batch insert
            await self.collection.insert(prepared_entities)

            if flush:
                await self.collection.flush()

            logger.info(
                "✅ Batch inserted %d profile items",
                len(prepared_entities),
            )

            return len(prepared_entities)

        except Exception as e:
            logger.error("❌ Failed to batch insert profile items: %s", e)
            raise

    async def delete_by_user_group(
        self,
        user_id: str,
        group_id: str,
    ) -> int:
        """
        Delete all profile items for a user in a group

        Used before full rebuild of profile index.

        Args:
            user_id: User ID
            group_id: Group ID

        Returns:
            Number of deleted entities
        """
        try:
            # Build filter expression
            filter_expr = f'user_id == "{user_id}" and group_id == "{group_id}"'

            result = await self.collection.delete(filter_expr)

            count = result.delete_count if hasattr(result, 'delete_count') else 0
            
            # Flush to ensure deletion takes effect immediately
            # This is crucial for delete-then-insert scenarios
            if count > 0:
                await self.collection.flush()
                logger.debug(
                    "Flushed after deletion: user_id=%s, group_id=%s",
                    user_id,
                    group_id,
                )

            logger.info(
                "✅ Deleted profile items: user_id=%s, group_id=%s, count=%d",
                user_id,
                group_id,
                count,
            )

            return count

        except Exception as e:
            logger.error(
                "❌ Failed to delete profile items: user_id=%s, group_id=%s, error=%s",
                user_id,
                group_id,
                e,
            )
            return 0

    async def vector_search(
        self,
        query_vector: List[float],
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
        item_type: Optional[str] = None,
        limit: int = 10,
        score_threshold: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """
        Vector similarity search for profile items

        Args:
            query_vector: Query embedding vector
            user_id: Filter by user_id
            group_id: Filter by group_id
            item_type: Filter by item_type (explicit_info or implicit_trait)
            limit: Maximum number of results
            score_threshold: Minimum similarity score (COSINE: -1 to 1)

        Returns:
            List of matched items with scores
        """
        try:
            # Build filter expression
            conditions = []
            if user_id and user_id != MAGIC_ALL:
                conditions.append(f'user_id == "{user_id}"')
            if group_id:
                conditions.append(f'group_id == "{group_id}"')
            if item_type:
                conditions.append(f'item_type == "{item_type}"')

            filter_expr = " and ".join(conditions) if conditions else None

            # Search parameters with score threshold
            search_params = {
                "metric_type": "COSINE",
                "params": {"ef": 128},
            }
            
            # Add range search parameters to filter by minimum score threshold
            # For COSINE: higher is better, range is [-1, 1]
            # Returns results with similarity in [radius, range_filter]
            # radius: lower bound (minimum similarity threshold)
            # range_filter: upper bound (maximum similarity, must be > radius)
            if score_threshold > 0:
                search_params["params"]["radius"] = score_threshold  # Lower bound: minimum threshold

            results = await self.collection.search(
                data=[query_vector],
                anns_field="vector",
                param=search_params,
                limit=limit,
                expr=filter_expr,
                output_fields=["user_id", "group_id", "item_type", "item_index", "embed_text"],
            )

            # Process results
            items = []
            if results and len(results) > 0:
                for hit in results[0]:
                    entity = hit.entity
                    items.append({
                        "id": hit.id,
                        "score": hit.score,
                        "user_id": entity.get("user_id", ""),
                        "group_id": entity.get("group_id", ""),
                        "item_type": entity.get("item_type", ""),
                        "item_index": entity.get("item_index", 0),
                        "embed_text": entity.get("embed_text", ""),
                    })

            logger.debug(
                "🔍 Profile item search: found %d items (filter=%s, limit=%d)",
                len(items),
                filter_expr,
                limit,
            )

            return items

        except Exception as e:
            logger.error("❌ Failed to search profile items: %s", e)
            return []

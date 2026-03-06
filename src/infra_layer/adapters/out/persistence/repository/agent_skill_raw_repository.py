"""
Agent skill raw data repository.

Provides CRUD operations for agent skill records in MongoDB.
Skills are cluster-scoped: one repository manages all skill items per MemScene.
"""

from typing import List, Optional, Dict, Any
from pymongo.asynchronous.client_session import AsyncClientSession
from bson import ObjectId
from core.observation.logger import get_logger
from core.di.decorators import repository
from core.oxm.mongo.base_repository import BaseRepository
from core.oxm.constants import MAGIC_ALL
from infra_layer.adapters.out.persistence.document.memory.agent_skill import (
    AgentSkillRecord,
)

logger = get_logger(__name__)


@repository("agent_skill_raw_repository", primary=True)
class AgentSkillRawRepository(BaseRepository[AgentSkillRecord]):
    """
    Agent skill raw data repository.

    Manages skill items extracted from MemScene clusters (AgentCase clusters).
    Supports cluster-level upsert: replacing all skills for a cluster on each extraction.
    """

    def __init__(self):
        super().__init__(AgentSkillRecord)

    async def save_skill(
        self, record: AgentSkillRecord, session: Optional[AsyncClientSession] = None
    ) -> Optional[AgentSkillRecord]:
        """Insert a new agent skill record."""
        try:
            result = await record.insert(session=session)
            logger.debug(
                f"[AgentSkillRepo] Inserted skill: id={result.id}, "
                f"cluster={result.cluster_id}, name='{result.name}'"
            )
            return result
        except Exception as e:
            logger.error(f"[AgentSkillRepo] Failed to insert skill: {e}")
            return None

    async def get_by_cluster_id(
        self, cluster_id: str, session: Optional[AsyncClientSession] = None
    ) -> List[AgentSkillRecord]:
        """Retrieve all skill records for a cluster (MemScene)."""
        try:
            results = await self.model.find(
                {"cluster_id": cluster_id}, session=session
            ).to_list()
            return results
        except Exception as e:
            logger.error(f"[AgentSkillRepo] Failed to get by cluster_id: {e}")
            return []

    async def delete_by_cluster_id(
        self, cluster_id: str, session: Optional[AsyncClientSession] = None
    ) -> int:
        """Soft-delete all skill records for a cluster (before replacing them)."""
        try:
            result = await AgentSkillRecord.delete_many(
                {"cluster_id": cluster_id}, session=session
            )
            deleted_count = result.modified_count if result else 0
            logger.debug(
                f"[AgentSkillRepo] Soft-deleted {deleted_count} records for cluster={cluster_id}"
            )
            return deleted_count
        except Exception as e:
            logger.error(f"[AgentSkillRepo] Failed to delete by cluster_id: {e}")
            return 0

    async def replace_cluster_skills(
        self,
        cluster_id: str,
        new_records: List[AgentSkillRecord],
        old_record_ids: List[Any],
        session: Optional[AsyncClientSession] = None,
    ) -> List[AgentSkillRecord]:
        """Replace all skills for a cluster with new records.

        Uses insert-first-delete-later strategy to prevent data loss.

        Args:
            cluster_id: The MemScene cluster ID
            new_records: New skill records to save
            old_record_ids: IDs of existing records to soft-delete
            session: Optional MongoDB session (if provided, caller manages transaction)

        Returns:
            List of saved AgentSkillRecord
        """
        if session is not None:
            return await self._replace_cluster_skills_impl(
                cluster_id, new_records, old_record_ids, session
            )

        try:
            async with self.transaction() as txn_session:
                return await self._replace_cluster_skills_impl(
                    cluster_id, new_records, old_record_ids, txn_session
                )
        except Exception as e:
            logger.warning(
                "[AgentSkillRepo] Transaction not available for cluster=%s (%s), "
                "falling back to non-transactional mode",
                cluster_id,
                e,
            )
            try:
                return await self._replace_cluster_skills_impl(
                    cluster_id, new_records, old_record_ids, session=None
                )
            except Exception as fallback_e:
                logger.error(
                    "[AgentSkillRepo] Failed to replace skills for cluster=%s: %s",
                    cluster_id,
                    fallback_e,
                )
                return []

    async def _replace_cluster_skills_impl(
        self,
        cluster_id: str,
        new_records: List[AgentSkillRecord],
        old_record_ids: List[Any],
        session: Optional[AsyncClientSession] = None,
    ) -> List[AgentSkillRecord]:
        """Internal implementation for replace_cluster_skills.

        Uses insert-first-delete-later strategy to prevent data loss:
        1. Insert new records
        2. Soft-delete old records by their specific IDs
        """
        # Step 1: Insert new records first
        saved = []
        for record in new_records:
            result = await self.save_skill(record, session=session)
            if result:
                saved.append(result)
            else:
                logger.warning(
                    f"[AgentSkillRepo] Failed to insert skill record "
                    f"for cluster={cluster_id}, name='{record.name}'"
                )

        # Step 2: Soft-delete old records by specific IDs
        if old_record_ids:
            from common_utils.datetime_utils import get_now_with_timezone

            now = get_now_with_timezone()
            deleted_count = 0
            for old_id in old_record_ids:
                try:
                    await AgentSkillRecord.get_pymongo_collection().update_one(
                        {"_id": old_id, "deleted_at": None},
                        {
                            "$set": {
                                "deleted_at": now,
                                "deleted_id": abs(hash(str(old_id))),
                            }
                        },
                        session=session,
                    )
                    deleted_count += 1
                except Exception as e:
                    logger.warning(
                        f"[AgentSkillRepo] Failed to soft-delete old record "
                        f"id={old_id}: {e}"
                    )
            logger.debug(
                f"[AgentSkillRepo] Soft-deleted {deleted_count} old records "
                f"for cluster={cluster_id}"
            )

        if len(saved) < len(new_records):
            logger.warning(
                f"[AgentSkillRepo] Partial save for cluster={cluster_id}: "
                f"{len(saved)}/{len(new_records)} records saved"
            )
        else:
            logger.info(
                f"[AgentSkillRepo] Replaced skills for cluster={cluster_id}: "
                f"{len(saved)} records saved"
            )
        return saved

    def _build_filter_query(
        self,
        user_id: Optional[str] = MAGIC_ALL,
        group_ids: Optional[List[str]] = None,
        cluster_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a filter query dict from common parameters.

        Args:
            user_id: User ID filter
                - MAGIC_ALL ("__all__"): Don't filter by user_id
                - Other values: Exact match
        """
        query: Dict[str, Any] = {}
        if user_id is not None and user_id != MAGIC_ALL:
            query["user_id"] = user_id
        if group_ids is not None and len(group_ids) > 0:
            if len(group_ids) == 1:
                query["group_id"] = group_ids[0]
            else:
                query["group_id"] = {"$in": group_ids}
        if cluster_id is not None:
            query["cluster_id"] = cluster_id
        return query

    async def find_by_filters(
        self,
        user_id: Optional[str] = MAGIC_ALL,
        group_ids: Optional[List[str]] = None,
        cluster_id: Optional[str] = None,
        limit: int = 50,
        skip: int = 0,
        session: Optional[AsyncClientSession] = None,
    ) -> List[AgentSkillRecord]:
        """Find skill records with flexible filters."""
        try:
            query = self._build_filter_query(
                user_id=user_id, group_ids=group_ids, cluster_id=cluster_id
            )

            results = (
                await self.model.find(query, session=session)
                .skip(skip)
                .limit(limit)
                .to_list()
            )
            return results
        except Exception as e:
            logger.error(f"[AgentSkillRepo] Failed to find by filters: {e}")
            return []

    async def count_by_filters(
        self,
        user_id: Optional[str] = MAGIC_ALL,
        group_ids: Optional[List[str]] = None,
        cluster_id: Optional[str] = None,
        session: Optional[AsyncClientSession] = None,
    ) -> int:
        """Count skill records by filters (without pagination).

        Args:
            user_id: User ID filter
                - MAGIC_ALL ("__all__"): Don't filter by user_id
                - Other values: Exact match
            group_ids: Group IDs filter (list, supports $in for multiple)
            cluster_id: Cluster ID filter
            session: Optional MongoDB session

        Returns:
            Total count of matching records
        """
        try:
            query = self._build_filter_query(
                user_id=user_id, group_ids=group_ids, cluster_id=cluster_id
            )
            count = await self.model.find(query, session=session).count()
            logger.debug(
                "[AgentSkillRepo] count_by_filters: user_id=%s, group_ids=%s, cluster_id=%s, count=%d",
                user_id, group_ids, cluster_id, count,
            )
            return count
        except Exception as e:
            logger.error(f"[AgentSkillRepo] Failed to count by filters: {e}")
            return 0

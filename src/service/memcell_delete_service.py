"""
MemCell Delete Service - Handle soft delete logic for MemCell

Provides multiple deletion methods:
- Delete by single event_id
- Batch delete by user_id
- Batch delete by group_id

Cascade deletes related data from MongoDB, Milvus and Elasticsearch.
"""

from typing import Optional
from core.di.decorators import component
from core.observation.logger import get_logger
from infra_layer.adapters.out.persistence.repository.memcell_raw_repository import (
    MemCellRawRepository,
)
from infra_layer.adapters.out.persistence.repository.episodic_memory_raw_repository import (
    EpisodicMemoryRawRepository,
)
from infra_layer.adapters.out.persistence.repository.event_log_record_raw_repository import (
    EventLogRecordRawRepository,
)
from infra_layer.adapters.out.persistence.repository.foresight_record_repository import (
    ForesightRecordRawRepository,
)
from infra_layer.adapters.out.search.repository.episodic_memory_milvus_repository import (
    EpisodicMemoryMilvusRepository,
)
from infra_layer.adapters.out.search.repository.event_log_milvus_repository import (
    EventLogMilvusRepository,
)
from infra_layer.adapters.out.search.repository.foresight_milvus_repository import (
    ForesightMilvusRepository,
)
from infra_layer.adapters.out.search.repository.episodic_memory_es_repository import (
    EpisodicMemoryEsRepository,
)
from infra_layer.adapters.out.search.repository.event_log_es_repository import (
    EventLogEsRepository,
)
from infra_layer.adapters.out.search.repository.foresight_es_repository import (
    ForesightEsRepository,
)
from infra_layer.adapters.out.persistence.repository.memory_request_log_repository import (
    MemoryRequestLogRepository,
)

logger = get_logger(__name__)


@component("memcell_delete_service")
class MemCellDeleteService:
    """MemCell soft delete service"""

    def __init__(
        self,
        memcell_repository: MemCellRawRepository,
        episodic_memory_repository: EpisodicMemoryRawRepository,
        event_log_repository: EventLogRecordRawRepository,
        foresight_repository: ForesightRecordRawRepository,
        episodic_memory_milvus_repository: EpisodicMemoryMilvusRepository,
        event_log_milvus_repository: EventLogMilvusRepository,
        foresight_milvus_repository: ForesightMilvusRepository,
        episodic_memory_es_repository: EpisodicMemoryEsRepository,
        event_log_es_repository: EventLogEsRepository,
        foresight_es_repository: ForesightEsRepository,
        memory_request_log_repository: MemoryRequestLogRepository,
    ):
        """
        Initialize deletion service

        Args:
            memcell_repository: MemCell data repository
            episodic_memory_repository: EpisodicMemory data repository
            event_log_repository: EventLogRecord data repository
            foresight_repository: ForesightRecord data repository
            episodic_memory_milvus_repository: EpisodicMemory Milvus repository
            event_log_milvus_repository: EventLog Milvus repository
            foresight_milvus_repository: Foresight Milvus repository
            episodic_memory_es_repository: EpisodicMemory ES repository
            event_log_es_repository: EventLog ES repository
            foresight_es_repository: Foresight ES repository
            memory_request_log_repository: MemoryRequestLog repository
        """
        self.memcell_repository = memcell_repository
        self.episodic_memory_repository = episodic_memory_repository
        self.event_log_repository = event_log_repository
        self.foresight_repository = foresight_repository
        self.episodic_memory_milvus_repository = episodic_memory_milvus_repository
        self.event_log_milvus_repository = event_log_milvus_repository
        self.foresight_milvus_repository = foresight_milvus_repository
        self.episodic_memory_es_repository = episodic_memory_es_repository
        self.event_log_es_repository = event_log_es_repository
        self.foresight_es_repository = foresight_es_repository
        self.memory_request_log_repository = memory_request_log_repository
        logger.info("MemCellDeleteService initialized")

    async def delete_by_event_id(
        self, event_id: str, deleted_by: Optional[str] = None
    ) -> bool:
        """
        Soft delete a single MemCell by event_id

        Args:
            event_id: The event_id of MemCell
            deleted_by: Identifier of the deleter (optional)

        Returns:
            bool: Returns True if deletion succeeds, False if not found or already deleted

        Example:
            >>> service = MemCellDeleteService(repo)
            >>> success = await service.delete_by_event_id("507f1f77bcf86cd799439011", "admin")
        """
        logger.info(
            "Deleting MemCell by event_id: event_id=%s, deleted_by=%s",
            event_id,
            deleted_by,
        )

        try:
            result = await self.memcell_repository.delete_by_event_id(
                event_id=event_id, deleted_by=deleted_by
            )

            if result:
                logger.info(
                    "Successfully deleted MemCell: event_id=%s, deleted_by=%s",
                    event_id,
                    deleted_by,
                )
            else:
                logger.warning(
                    "MemCell not found or already deleted: event_id=%s", event_id
                )

            return result

        except Exception as e:
            logger.error(
                "Failed to delete MemCell by event_id: event_id=%s, error=%s",
                event_id,
                e,
                exc_info=True,
            )
            raise

    async def delete_by_user_id(
        self, user_id: str, deleted_by: Optional[str] = None
    ) -> int:
        """
        Batch soft delete all MemCells of a user by user_id

        Args:
            user_id: User ID
            deleted_by: Identifier of the deleter (optional)

        Returns:
            int: Number of deleted records

        Example:
            >>> service = MemCellDeleteService(repo)
            >>> count = await service.delete_by_user_id("user_123", "admin")
            >>> print(f"Deleted {count} records")
        """
        logger.info(
            "Deleting MemCells by user_id: user_id=%s, deleted_by=%s",
            user_id,
            deleted_by,
        )

        try:
            count = await self.memcell_repository.delete_by_user_id(
                user_id=user_id, deleted_by=deleted_by
            )

            logger.info(
                "Successfully deleted MemCells by user_id: user_id=%s, deleted_by=%s, count=%d",
                user_id,
                deleted_by,
                count,
            )

            return count

        except Exception as e:
            logger.error(
                "Failed to delete MemCells by user_id: user_id=%s, error=%s",
                user_id,
                e,
                exc_info=True,
            )
            raise

    async def delete_by_group_id(
        self, group_id: str, deleted_by: Optional[str] = None
    ) -> int:
        """
        Batch soft delete all MemCells of a group by group_id

        Args:
            group_id: Group ID
            deleted_by: Identifier of the deleter (optional)

        Returns:
            int: Number of deleted records

        Example:
            >>> service = MemCellDeleteService(repo)
            >>> count = await service.delete_by_group_id("group_456", "admin")
            >>> print(f"Deleted {count} records")
        """
        logger.info(
            "Deleting MemCells by group_id: group_id=%s, deleted_by=%s",
            group_id,
            deleted_by,
        )

        try:
            # Use repository's delete_by_group_id method
            count = await self.memcell_repository.delete_by_group_id(
                group_id=group_id, deleted_by=deleted_by
            )

            logger.info(
                "Successfully deleted MemCells by group_id: group_id=%s, deleted_by=%s, count=%d",
                group_id,
                deleted_by,
                count,
            )

            return count

        except Exception as e:
            logger.error(
                "Failed to delete MemCells by group_id: group_id=%s, error=%s",
                group_id,
                e,
                exc_info=True,
            )
            raise

    async def delete_by_combined_criteria(
        self,
        id: Optional[str] = None,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> dict:
        """
        Delete MemCell based on combined criteria (multiple conditions must all be satisfied)

        This method performs cascade soft delete:
        1. Delete MemCells matching the criteria
        2. Cascade delete related EpisodicMemory (parent_type=memcell) from MongoDB
        3. Cascade delete related EventLogRecord (parent_type=memcell or episode) from MongoDB
        4. Cascade delete related ForesightRecord (parent_type=memcell or episode) from MongoDB
        5. Cascade delete related data from Milvus (EpisodicMemory, EventLog, Foresight)

        Args:
            id: The id of MemCell (one of the combined conditions)
            user_id: User ID (one of the combined conditions)
            group_id: Group ID (one of the combined conditions)

        Returns:
            dict: Dictionary containing deletion results
                - filters: List of filter conditions used
                - count: Number of deleted MemCell records
                - cascade_count: Dict of cascade deleted counts
                - success: Whether the operation succeeded

        Example:
            >>> service = MemCellDeleteService(repo)
            >>> # Delete records of a specific user in a specific group
            >>> result = await service.delete_by_combined_criteria(
            ...     user_id="user_123",
            ...     group_id="group_456",
            ... )
            >>> print(result)
            {'filters': ['user_id', 'group_id'], 'count': 5,
             'success': True, 'cascade_count': {...}}
        """
        from core.oxm.constants import MAGIC_ALL

        # Build filter conditions
        filters_used = []

        if id and id != MAGIC_ALL:
            filters_used.append("id")

        if user_id and user_id != MAGIC_ALL:
            filters_used.append("user_id")

        if group_id and group_id != MAGIC_ALL:
            filters_used.append("group_id")

        # If no filter conditions are provided
        if not filters_used:
            logger.warning("No deletion criteria provided (all are MAGIC_ALL)")
            return {
                "filters": [],
                "count": 0,
                "success": False,
                "error": "No deletion criteria provided",
            }

        logger.info(
            "Deleting MemCells with combined criteria: filters=%s", filters_used
        )

        try:
            # Build cascade filter based on deletion criteria
            # Use the same user_id/group_id for cascade delete, or parent_id for id-based delete
            cascade_user_id = user_id if user_id and user_id != MAGIC_ALL else None
            cascade_group_id = group_id if group_id and group_id != MAGIC_ALL else None
            cascade_parent_id = id if id and id != MAGIC_ALL else None

            # Step 1: Cascade delete related records from MongoDB and Milvus (no memory load)
            cascade_count = await self._cascade_delete_by_filter(
                user_id=cascade_user_id,
                group_id=cascade_group_id,
                parent_id=cascade_parent_id,
            )

            # Step 2: Soft delete MemCells using repository
            count = await self.memcell_repository.delete_by_filters(
                memcell_id=id if id != MAGIC_ALL else None,
                user_id=user_id if user_id != MAGIC_ALL else None,
                group_id=group_id if group_id != MAGIC_ALL else None,
            )

            total_deleted = count + sum(cascade_count.values())
            logger.info(
                "Delete operation completed: memcell_count=%d, cascade_count=%s, total=%d",
                count,
                cascade_count,
                total_deleted,
            )

            return {"count": count, "cascade_count": cascade_count}

        except Exception as e:
            logger.error(
                "Failed to delete MemCells with combined criteria: filters=%s, error=%s",
                filters_used,
                e,
                exc_info=True,
            )
            raise

    async def _cascade_delete_by_filter(
        self,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
        parent_id: Optional[str] = None,
    ) -> dict:
        """
        Cascade soft delete related records by filter conditions (no memory load)

        Directly deletes records using filter conditions without loading data into memory.
        Deletes from MongoDB, Milvus and Elasticsearch in parallel.

        Args:
            user_id: User ID filter (for user_id/group_id based delete)
            group_id: Group ID filter (for user_id/group_id based delete)
            parent_id: Parent ID filter (for id-based delete, used to cascade by parent_id)

        Returns:
            dict: Dictionary containing cascade deleted counts
                - episodic_memory_mongo: Number of deleted EpisodicMemory records from MongoDB
                - event_log_mongo: Number of deleted EventLogRecord records from MongoDB
                - foresight_mongo: Number of deleted ForesightRecord records from MongoDB
                - episodic_memory_milvus: Number of deleted EpisodicMemory records from Milvus
                - event_log_milvus: Number of deleted EventLog records from Milvus
                - foresight_milvus: Number of deleted Foresight records from Milvus
                - episodic_memory_es: Number of deleted EpisodicMemory records from ES
                - event_log_es: Number of deleted EventLog records from ES
                - foresight_es: Number of deleted Foresight records from ES
        """
        import asyncio
        from core.oxm.constants import MAGIC_ALL

        cascade_count = {
            "episodic_memory_mongo": 0,
            "event_log_mongo": 0,
            "foresight_mongo": 0,
            "memory_request_log": 0,
            "episodic_memory_milvus": 0,
            "event_log_milvus": 0,
            "foresight_milvus": 0,
            "episodic_memory_es": 0,
            "event_log_es": 0,
            "foresight_es": 0,
        }

        if not user_id and not group_id and not parent_id:
            return cascade_count

        logger.info(
            "Cascade deleting related records in parallel: user_id=%s, group_id=%s, parent_id=%s",
            user_id,
            group_id,
            parent_id,
        )

        # Define delete tasks and their names
        tasks = []
        task_names = []

        # MongoDB tasks: support parent_id filter, always include
        tasks.append(
            self.episodic_memory_repository.delete_by_filters(
                user_id=user_id if user_id else MAGIC_ALL,
                group_id=group_id if group_id else MAGIC_ALL,
                parent_id=parent_id,
            )
        )
        task_names.append("episodic_memory_mongo")

        tasks.append(
            self.event_log_repository.delete_by_filters(
                user_id=user_id if user_id else MAGIC_ALL,
                group_id=group_id if group_id else MAGIC_ALL,
                parent_id=parent_id,
            )
        )
        task_names.append("event_log_mongo")

        tasks.append(
            self.foresight_repository.delete_by_filters(
                user_id=user_id if user_id else MAGIC_ALL,
                group_id=group_id if group_id else MAGIC_ALL,
                parent_id=parent_id,
            )
        )
        task_names.append("foresight_mongo")

        # memory_request_log: only has user_id/group_id, no parent_id
        if user_id or group_id:
            tasks.append(
                self.memory_request_log_repository.delete_by_filters(
                    user_id=user_id if user_id else MAGIC_ALL,
                    group_id=group_id if group_id else MAGIC_ALL,
                )
            )
            task_names.append("memory_request_log")

        # Milvus and ES tasks: require user_id or group_id, skip when only parent_id
        if user_id or group_id:
            # Milvus tasks
            tasks.append(
                self.episodic_memory_milvus_repository.delete_by_filters(
                    user_id=user_id, group_id=group_id
                )
            )
            task_names.append("episodic_memory_milvus")

            tasks.append(
                self.event_log_milvus_repository.delete_by_filters(
                    user_id=user_id, group_id=group_id
                )
            )
            task_names.append("event_log_milvus")

            tasks.append(
                self.foresight_milvus_repository.delete_by_filters(
                    user_id=user_id, group_id=group_id
                )
            )
            task_names.append("foresight_milvus")

            # Elasticsearch tasks
            tasks.append(
                self.episodic_memory_es_repository.delete_by_filters(
                    user_id=user_id, group_id=group_id
                )
            )
            task_names.append("episodic_memory_es")

            tasks.append(
                self.event_log_es_repository.delete_by_filters(
                    user_id=user_id, group_id=group_id
                )
            )
            task_names.append("event_log_es")

            tasks.append(
                self.foresight_es_repository.delete_by_filters(
                    user_id=user_id, group_id=group_id
                )
            )
            task_names.append("foresight_es")
        else:
            logger.info(
                "Skipping Milvus/ES cascade delete: no user_id or group_id filter (parent_id=%s)",
                parent_id,
            )

        # Execute all tasks in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        for name, result in zip(task_names, results):
            if isinstance(result, Exception):
                logger.error("Failed to cascade delete %s: %s", name, result)
            else:
                cascade_count[name] = result
                logger.info("Cascade deleted %s: count=%d", name, result)

        return cascade_count

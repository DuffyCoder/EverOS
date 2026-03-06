# -*- coding: utf-8 -*-
"""
MemoryRequestLog Repository

Memory request log data access layer, providing CRUD operations for memories request records.
Used as a replacement for the conversation_data functionality.
"""

import json
from datetime import datetime
from typing import List, Optional, Dict, Any
from pymongo.asynchronous.client_session import AsyncClientSession
from core.observation.logger import get_logger
from core.di.decorators import repository
from core.oxm.mongo.base_repository import BaseRepository
from core.oxm.constants import MAGIC_ALL
from infra_layer.adapters.out.persistence.document.request.memory_request_log import (
    MemoryRequestLog,
)

logger = get_logger(__name__)


@repository("memory_request_log_repository", primary=True)
class MemoryRequestLogRepository(BaseRepository[MemoryRequestLog]):
    """
    Memory Request Log Repository

    Provides CRUD operations and query functionality for memories API request records.
    Can be used as an alternative implementation for conversation_data.
    """

    def __init__(self):
        super().__init__(MemoryRequestLog)

    # ==================== Save Methods ====================

    async def save(
        self,
        memory_request_log: MemoryRequestLog,
        session: Optional[AsyncClientSession] = None,
    ) -> Optional[MemoryRequestLog]:
        """
        Save Memory request log

        Args:
            memory_request_log: MemoryRequestLog object
            session: Optional MongoDB session

        Returns:
            Saved MemoryRequestLog or None
        """
        try:
            await memory_request_log.insert(session=session)
            logger.debug(
                "Memory request log saved successfully: id=%s, group_id=%s, request_id=%s",
                memory_request_log.id,
                memory_request_log.group_id,
                memory_request_log.request_id,
            )
            return memory_request_log
        except Exception as e:
            logger.error("Failed to save Memory request log: %s", e)
            return None

    async def save_from_raw_data(
        self,
        raw_data_content: Dict[str, Any],
        data_id: Optional[str],
        group_id: str,
        group_name: Optional[str],
        request_id: str,
        version: Optional[str] = None,
        endpoint_name: Optional[str] = None,
        method: Optional[str] = None,
        url: Optional[str] = None,
        event_id: Optional[str] = None,
        raw_input_dict: Optional[Dict[str, Any]] = None,
        session: Optional[AsyncClientSession] = None,
    ) -> Optional[str]:
        """
        Parse raw data fields, create a MemoryRequestLog document, and save it.

        Extracts core message fields (sender, content, timestamps, etc.) from the
        raw data content dict, constructs a MemoryRequestLog, and persists it.

        Args:
            raw_data_content: The content dict from RawData (raw_data.content)
            data_id: Message ID (raw_data.data_id)
            group_id: Conversation group ID
            group_name: Group name
            request_id: Request ID
            version: API version
            endpoint_name: Endpoint name
            method: HTTP method
            url: Request URL
            event_id: Event ID
            raw_input_dict: Raw input dictionary (used to generate raw_input_str)
            session: Optional MongoDB session

        Returns:
            Optional[str]: Returns message_id if saved successfully, None otherwise
        """
        content_dict = raw_data_content or {}
        message_id = data_id

        # Extract core message fields
        sender = (
            content_dict.get("speaker_id")
            or content_dict.get("createBy")
            or content_dict.get("sender")
        )
        sender_name = (
            content_dict.get("speaker_name")
            or content_dict.get("sender_name")
            or sender
        )
        content = content_dict.get("content")
        role = content_dict.get("role")
        message_create_time = self._parse_create_time(
            content_dict.get("timestamp")
            or content_dict.get("createTime")
            or content_dict.get("create_time")
        )
        refer_list = content_dict.get("referList") or content_dict.get("refer_list")

        # Generate raw_input_str
        raw_input_str = None
        if raw_input_dict:
            try:
                raw_input_str = json.dumps(raw_input_dict, ensure_ascii=False)
            except (TypeError, ValueError):
                pass

        # Create MemoryRequestLog document
        memory_request_log = MemoryRequestLog(
            group_id=group_id,
            request_id=request_id,
            user_id=sender,
            message_id=message_id,
            message_create_time=message_create_time,
            sender=sender,
            sender_name=sender_name,
            role=role,
            content=content,
            group_name=group_name,
            refer_list=self._normalize_refer_list(refer_list),
            raw_input=raw_input_dict or content_dict,
            raw_input_str=raw_input_str,
            version=version,
            endpoint_name=endpoint_name,
            method=method,
            url=url,
            event_id=event_id,
        )

        await self.save(memory_request_log, session=session)

        logger.debug(
            "Saved request log from raw data: group_id=%s, message_id=%s, content_preview=%s",
            group_id,
            message_id,
            (content or "")[:50],
        )

        return message_id

    @staticmethod
    def _parse_create_time(create_time: Any) -> Optional[str]:
        """Parse creation time and return ISO format string"""
        if create_time is None:
            return None
        if isinstance(create_time, datetime):
            return create_time.isoformat()
        if isinstance(create_time, str):
            try:
                from common_utils.datetime_utils import from_iso_format

                parsed = from_iso_format(create_time)
                return parsed.isoformat() if parsed else create_time
            except Exception:
                return create_time
        return None

    @staticmethod
    def _normalize_refer_list(refer_list: Any) -> Optional[List[str]]:
        """
        Normalize refer_list to a list of strings

        Args:
            refer_list: Original refer_list, could be a list of strings or dictionaries

        Returns:
            Normalized list of strings
        """
        if not refer_list:
            return None

        if not isinstance(refer_list, list):
            return None

        result = []
        for item in refer_list:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                msg_id = item.get("message_id") or item.get("id")
                if msg_id:
                    result.append(str(msg_id))

        return result if result else None

    # ==================== Query Methods ====================

    async def get_by_request_id(
        self, request_id: str, session: Optional[AsyncClientSession] = None
    ) -> Optional[MemoryRequestLog]:
        """
        Get Memory request log by request ID

        Args:
            request_id: Request ID
            session: Optional MongoDB session

        Returns:
            MemoryRequestLog or None
        """
        try:
            result = await MemoryRequestLog.find_one(
                {"request_id": request_id}, session=session
            )
            return result
        except Exception as e:
            logger.error("Failed to get Memory request log by request ID: %s", e)
            return None

    async def find_one_by_group_user_message(
        self,
        group_id: str,
        user_id: str,
        message_id: str,
        session: Optional[AsyncClientSession] = None,
    ) -> Optional[MemoryRequestLog]:
        """
        Find a single Memory request log by group_id, user_id, and message_id

        Used for duplicate detection before saving new request logs.
        Uses composite index (group_id, user_id, message_id) for efficient lookup.

        Args:
            group_id: Conversation group ID
            user_id: User ID (sender)
            message_id: Message ID
            session: Optional MongoDB session

        Returns:
            MemoryRequestLog if found, None otherwise
        """
        try:
            result = await MemoryRequestLog.find_one(
                {"group_id": group_id, "user_id": user_id, "message_id": message_id},
                session=session,
            )
            if result:
                logger.debug(
                    "Found existing request log: group_id=%s, user_id=%s, message_id=%s",
                    group_id,
                    user_id,
                    message_id,
                )
            return result
        except Exception as e:
            logger.error(
                "Failed to find Memory request log by group_id/user_id/message_id: "
                "group_id=%s, user_id=%s, message_id=%s, error=%s",
                group_id,
                user_id,
                message_id,
                e,
            )
            return None

    async def find_by_group_id(
        self,
        group_id: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        sync_status: Optional[int] = 0,
        session: Optional[AsyncClientSession] = None,
    ) -> List[MemoryRequestLog]:
        """
        Query Memory request logs by group_id

        Args:
            group_id: Conversation group ID
            start_time: Start time
            end_time: End time
            limit: Maximum number of records to return
            sync_status: Sync status filter (default 0=in window accumulation, None=no filter)
                - -1: Just a log record
                -  0: In window accumulation
                -  1: Already fully used
                - None: No filter, return all statuses
            session: Optional MongoDB session

        Returns:
            List of MemoryRequestLog
        """
        try:
            query = {"group_id": group_id}

            # Filter by status
            if sync_status is not None:
                query["sync_status"] = sync_status

            if start_time:
                query["created_at"] = {"$gte": start_time}
            if end_time:
                if "created_at" in query:
                    query["created_at"]["$lte"] = end_time
                else:
                    query["created_at"] = {"$lte": end_time}

            results = (
                await MemoryRequestLog.find(query, session=session)
                .sort([("created_at", 1)])  # Ascending order by time, oldest first
                .limit(limit)
                .to_list()
            )
            logger.debug(
                "Query Memory request logs by group_id: group_id=%s, sync_status=%s, count=%d",
                group_id,
                sync_status,
                len(results),
            )
            return results
        except Exception as e:
            logger.error("Failed to query Memory request logs by group_id: %s", e)
            return []

    async def find_by_group_id_with_statuses(
        self,
        group_id: str,
        sync_status_list: List[int],
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        ascending: bool = True,
        exclude_message_ids: Optional[List[str]] = None,
        session: Optional[AsyncClientSession] = None,
    ) -> List[MemoryRequestLog]:
        """
        Query Memory request logs by group_id with multiple sync_status values

        This method is designed to handle edge cases by allowing queries for
        multiple sync_status values at once (e.g., both -1 and 0).

        Args:
            group_id: Conversation group ID
            sync_status_list: List of sync_status values to filter by
                - [-1]: Just log records
                - [0]: In window accumulation
                - [1]: Already fully used
                - [-1, 0]: Both pending and accumulating (for edge case handling)
            start_time: Start time (optional)
            end_time: End time (optional)
            limit: Maximum number of records to return
            ascending: If True (default), sort by created_at ascending (oldest first);
                       if False, sort descending (newest first)
            exclude_message_ids: Message IDs to exclude from results
            session: Optional MongoDB session

        Returns:
            List of MemoryRequestLog
        """
        try:
            query = {"group_id": group_id}

            # Filter by multiple statuses
            if sync_status_list:
                if len(sync_status_list) == 1:
                    query["sync_status"] = sync_status_list[0]
                else:
                    query["sync_status"] = {"$in": sync_status_list}

            if start_time:
                query["created_at"] = {"$gte": start_time}
            if end_time:
                if "created_at" in query:
                    query["created_at"]["$lte"] = end_time
                else:
                    query["created_at"] = {"$lte": end_time}

            # Exclude specific message_ids
            if exclude_message_ids:
                query["message_id"] = {"$nin": exclude_message_ids}

            # Determine sort order
            sort_order = 1 if ascending else -1

            results = (
                await MemoryRequestLog.find(query, session=session)
                .sort([("created_at", sort_order)])
                .limit(limit)
                .to_list()
            )
            logger.debug(
                "Query Memory request logs by group_id with statuses: group_id=%s, sync_status_list=%s, exclude=%d, count=%d",
                group_id,
                sync_status_list,
                len(exclude_message_ids) if exclude_message_ids else 0,
                len(results),
            )
            return results
        except Exception as e:
            logger.error(
                "Failed to query Memory request logs by group_id with statuses: %s", e
            )
            return []

    async def find_by_user_id(
        self,
        user_id: str,
        limit: int = 100,
        session: Optional[AsyncClientSession] = None,
    ) -> List[MemoryRequestLog]:
        """
        Query Memory request logs by user ID

        Args:
            user_id: User ID
            limit: Maximum number of records to return
            session: Optional MongoDB session

        Returns:
            List of MemoryRequestLog
        """
        try:
            results = (
                await MemoryRequestLog.find({"user_id": user_id}, session=session)
                .sort([("created_at", -1)])
                .limit(limit)
                .to_list()
            )
            logger.debug(
                "Query Memory request logs by user_id: user_id=%s, count=%d",
                user_id,
                len(results),
            )
            return results
        except Exception as e:
            logger.error("Failed to query Memory request logs by user_id: %s", e)
            return []

    async def delete_by_group_id(
        self, group_id: str, session: Optional[AsyncClientSession] = None
    ) -> int:
        """
        Delete Memory request logs by group_id

        Args:
            group_id: Conversation group ID
            session: Optional MongoDB session

        Returns:
            Number of deleted records
        """
        try:
            result = await MemoryRequestLog.find(
                {"group_id": group_id}, session=session
            ).delete()
            deleted_count = result.deleted_count if result else 0
            logger.info(
                "Deleted Memory request logs: group_id=%s, deleted=%d",
                group_id,
                deleted_count,
            )
            return deleted_count
        except Exception as e:
            logger.error(
                "Failed to delete Memory request logs: group_id=%s, error=%s",
                group_id,
                e,
            )
            return 0

    async def delete_by_filters(
        self,
        user_id: Optional[str] = MAGIC_ALL,
        group_id: Optional[str] = MAGIC_ALL,
        session: Optional[AsyncClientSession] = None,
    ) -> int:
        """
        Soft delete Memory request logs by filter conditions

        Args:
            user_id: User ID filter
                - MAGIC_ALL ("__all__"): Don't filter by user_id
                - Other values: Exact match
            group_id: Group ID filter
                - MAGIC_ALL ("__all__"): Don't filter by group_id
                - Other values: Exact match
            session: Optional MongoDB session

        Returns:
            Number of soft-deleted records
        """
        try:
            filter_dict = {}

            if user_id != MAGIC_ALL:
                if user_id == "" or user_id is None:
                    filter_dict["user_id"] = {"$in": [None, ""]}
                else:
                    filter_dict["user_id"] = user_id

            if group_id != MAGIC_ALL:
                if group_id == "" or group_id is None:
                    filter_dict["group_id"] = {"$in": [None, ""]}
                else:
                    filter_dict["group_id"] = group_id

            if not filter_dict:
                logger.warning(
                    "No filter conditions provided for delete_by_filters"
                )
                return 0

            result = await self.model.delete_many(filter_dict, session=session)
            count = result.modified_count if result else 0
            logger.info(
                "Soft deleted Memory request logs: filter=%s, deleted=%d",
                filter_dict,
                count,
            )
            return count
        except Exception as e:
            logger.error(
                "Failed to soft delete Memory request logs: filter=%s, error=%s",
                {"user_id": user_id, "group_id": group_id},
                e,
            )
            return 0

    # ==================== Sync Status Management ====================
    # sync_status state transitions:
    # -1 (log record) -> 0 (window accumulation) -> 1 (used)
    #
    # - save_conversation_data: -1 -> 0 (confirm enters window accumulation)
    # - delete_conversation_data: 0 -> 1 (mark as fully used)

    async def confirm_accumulation_by_group_id(
        self, group_id: str, session: Optional[AsyncClientSession] = None
    ) -> int:
        """
        Confirm log records for the specified group_id as window accumulation state

        Batch update sync_status: -1 -> 0, used for save_conversation_data.
        Uses (group_id, sync_status) composite index for efficient querying.

        Note: This method updates all sync_status=-1 records under this group.
        For precise control, use confirm_accumulation_by_message_ids.

        Args:
            group_id: Conversation group ID
            session: Optional MongoDB session

        Returns:
            Number of updated records
        """
        try:
            collection = MemoryRequestLog.get_pymongo_collection()
            result = await collection.update_many(
                {"group_id": group_id, "sync_status": -1},
                {"$set": {"sync_status": 0}},
                session=session,
            )
            modified_count = result.modified_count if result else 0
            logger.info(
                "Confirmed window accumulation: group_id=%s, modified=%d",
                group_id,
                modified_count,
            )
            return modified_count
        except Exception as e:
            logger.error(
                "Failed to confirm window accumulation: group_id=%s, error=%s",
                group_id,
                e,
            )
            return 0

    async def confirm_accumulation_by_message_ids(
        self,
        group_id: str,
        message_ids: List[str],
        session: Optional[AsyncClientSession] = None,
    ) -> int:
        """
        Confirm log records for the specified message_id list as window accumulation state

        Precise update: only update records with specified message_id to avoid
        accidentally updating data from other concurrent requests.
        sync_status: -1 -> 0

        Args:
            group_id: Conversation group ID (for additional validation)
            message_ids: List of message_ids to update
            session: Optional MongoDB session

        Returns:
            Number of updated records
        """
        if not message_ids:
            logger.debug("message_ids is empty, skipping update")
            return 0

        try:
            collection = MemoryRequestLog.get_pymongo_collection()
            result = await collection.update_many(
                {
                    "group_id": group_id,
                    "message_id": {"$in": message_ids},
                    "sync_status": -1,
                },
                {"$set": {"sync_status": 0}},
                session=session,
            )
            modified_count = result.modified_count if result else 0
            logger.info(
                "Confirmed window accumulation (precise): group_id=%s, message_ids=%d, modified=%d",
                group_id,
                len(message_ids),
                modified_count,
            )
            return modified_count
        except Exception as e:
            logger.error(
                "Failed to confirm window accumulation (precise): group_id=%s, error=%s",
                group_id,
                e,
            )
            return 0

    async def mark_as_used_by_group_id(
        self,
        group_id: str,
        exclude_message_ids: Optional[List[str]] = None,
        session: Optional[AsyncClientSession] = None,
    ) -> int:
        """
        Mark all pending and accumulating data for the specified group_id as used

        Batch update sync_status: -1 or 0 -> 1, used for delete_conversation_data
        (after boundary detection). Processes both pending (-1) and accumulating (0) records.

        Uses (group_id, sync_status) composite index for efficient querying.

        Args:
            group_id: Conversation group ID
            exclude_message_ids: Message IDs to exclude from update
            session: Optional MongoDB session

        Returns:
            Number of updated records
        """
        try:
            collection = MemoryRequestLog.get_pymongo_collection()
            query = {"group_id": group_id, "sync_status": {"$in": [-1, 0]}}

            # Exclude specific message_ids
            if exclude_message_ids:
                query["message_id"] = {"$nin": exclude_message_ids}

            result = await collection.update_many(
                query, {"$set": {"sync_status": 1}}, session=session
            )
            modified_count = result.modified_count if result else 0
            logger.info(
                "Marked as used: group_id=%s, exclude=%d, modified=%d",
                group_id,
                len(exclude_message_ids) if exclude_message_ids else 0,
                modified_count,
            )
            return modified_count
        except Exception as e:
            logger.error("Failed to mark as used: group_id=%s, error=%s", group_id, e)
            return 0

    # ==================== Flexible Query Methods ====================

    async def find_pending_by_filters(
        self,
        user_id: Optional[str] = MAGIC_ALL,
        group_ids: Optional[List[str]] = None,
        sync_status_list: Optional[List[int]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000,
        skip: int = 0,
        ascending: bool = True,
        session: Optional[AsyncClientSession] = None,
    ) -> List[MemoryRequestLog]:
        """
        Query pending Memory request logs by flexible filters

        Supports MAGIC_ALL logic similar to episodic_memory_raw_repository:
        - MAGIC_ALL ("__all__"): Don't filter by this field
        - None or "": Filter for null/empty values
        - Other values: Exact match

        Args:
            user_id: User ID filter
                - MAGIC_ALL: Don't filter by user_id
                - None or "": Filter for null/empty values
                - Other values: Exact match
            group_ids: List of Group IDs to filter (None to skip filtering, searches all groups)
            sync_status_list: List of sync_status values to filter by
                - Default: [-1, 0] (pending and accumulating, i.e., unconsumed)
                - [-1]: Just log records
                - [0]: In window accumulation
                - [1]: Already fully used
            start_time: Start time (optional)
            end_time: End time (optional)
            limit: Maximum number of records to return
            skip: Number of records to skip
            ascending: If True (default), sort by created_at ascending (oldest first);
                       if False, sort descending (newest first)
            session: Optional MongoDB session

        Returns:
            List of MemoryRequestLog
        """
        # Default to unconsumed statuses
        if sync_status_list is None:
            sync_status_list = [-1, 0]

        try:
            query = {}

            # Handle user_id filter with MAGIC_ALL logic
            if user_id != MAGIC_ALL:
                if user_id == "" or user_id is None:
                    # Explicitly filter for null or empty string
                    query["user_id"] = {"$in": [None, ""]}
                else:
                    query["user_id"] = user_id

            # Handle group_ids filter: None means no filter (search all groups)
            if group_ids is not None and len(group_ids) > 0:
                # Use $in for multiple group_ids
                query["group_id"] = {"$in": group_ids}

            # Filter by sync_status
            if sync_status_list:
                if len(sync_status_list) == 1:
                    query["sync_status"] = sync_status_list[0]
                else:
                    query["sync_status"] = {"$in": sync_status_list}

            # Handle time range filter
            if start_time is not None or end_time is not None:
                time_filter = {}
                if start_time is not None:
                    time_filter["$gte"] = start_time
                if end_time is not None:
                    time_filter["$lte"] = end_time
                query["created_at"] = time_filter

            # Determine sort order
            sort_order = 1 if ascending else -1

            results = (
                await MemoryRequestLog.find(query, session=session)
                .sort([("created_at", sort_order)])
                .skip(skip)
                .limit(limit)
                .to_list()
            )

            logger.debug(
                "Query pending Memory request logs: user_id=%s, group_ids=%s, "
                "sync_status_list=%s, skip=%d, limit=%d, count=%d",
                user_id,
                group_ids,
                sync_status_list,
                skip,
                limit,
                len(results),
            )
            return results
        except Exception as e:
            logger.error(
                "Failed to query pending Memory request logs: user_id=%s, group_ids=%s, error=%s",
                user_id,
                group_ids,
                e,
            )
            return []

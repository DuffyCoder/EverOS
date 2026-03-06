# -*- coding: utf-8 -*-
"""
Memory Request Logging Service

Directly extract data from MemorizeRequest and save to MemoryRequestLog,
replacing the original event listener approach to make timing more controllable.
"""

from typing import List, Optional, Dict, Any

from common_utils.datetime_utils import to_iso_format
from core.di import service
from core.di.utils import get_bean_by_type
from core.observation.logger import get_logger
from core.context.context import get_current_app_info
from core.oxm.constants import MAGIC_ALL
from api_specs.dtos import MemorizeRequest, RawData, PendingMessage
from infra_layer.adapters.out.persistence.document.request.memory_request_log import (
    MemoryRequestLog,
)
from infra_layer.adapters.out.persistence.repository.memory_request_log_repository import (
    MemoryRequestLogRepository,
)

logger = get_logger(__name__)


@service("memory_request_log_service")
class MemoryRequestLogService:
    """
    Memory Request Logging Service

    Extract each message from new_raw_data_list in MemorizeRequest and save to MemoryRequestLog.
    Return the list of saved message_ids for use in subsequent processes.
    """

    def __init__(self):
        self._repository: Optional[MemoryRequestLogRepository] = None

    def _get_repository(self) -> MemoryRequestLogRepository:
        """Get Repository (lazy loading)"""
        if self._repository is None:
            self._repository = get_bean_by_type(MemoryRequestLogRepository)
        return self._repository

    async def save_request_logs(
        self,
        request: MemorizeRequest,
        version: Optional[str] = None,
        endpoint_name: Optional[str] = None,
        method: Optional[str] = None,
        url: Optional[str] = None,
        raw_input_dict: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """
        Extract data from MemorizeRequest and save to MemoryRequestLog

        Iterate through each RawData in new_raw_data_list, extract core fields and save.
        Saved records have sync_status=-1 (pending confirmation).

        Args:
            request: MemorizeRequest object
            version: API version (optional)
            endpoint_name: Endpoint name (optional)
            method: HTTP method (optional)
            url: Request URL (optional)
            raw_input_dict: Raw input dictionary (optional, used to generate raw_input_str)

        Returns:
            List[str]: List of saved message_ids
        """
        if not request.new_raw_data_list:
            logger.debug("new_raw_data_list is empty, skipping save")
            return []

        # Get current request context information
        app_info = get_current_app_info()
        request_id = app_info.get("request_id", "unknown")

        saved_message_ids = []
        repo = self._get_repository()

        for raw_data in request.new_raw_data_list:
            try:
                message_id = await self._save_single_raw_data(
                    raw_data=raw_data,
                    group_id=request.group_id,
                    group_name=request.group_name,
                    request_id=request_id,
                    repo=repo,
                    version=version,
                    endpoint_name=endpoint_name,
                    method=method,
                    url=url,
                    event_id=request_id,  # Use request_id as event_id
                    raw_input_dict=raw_input_dict,
                )
                if message_id:
                    saved_message_ids.append(message_id)
            except Exception as e:
                logger.error(
                    "Failed to save RawData to MemoryRequestLog: data_id=%s, error=%s",
                    raw_data.data_id,
                    e,
                )

        logger.info(
            "Saved %d request logs: group_id=%s, message_ids=%s",
            len(saved_message_ids),
            request.group_id,
            saved_message_ids,
        )

        return saved_message_ids

    async def _save_single_raw_data(
        self,
        raw_data: RawData,
        group_id: Optional[str],
        group_name: Optional[str],
        request_id: str,
        repo: MemoryRequestLogRepository,
        version: Optional[str] = None,
        endpoint_name: Optional[str] = None,
        method: Optional[str] = None,
        url: Optional[str] = None,
        event_id: Optional[str] = None,
        raw_input_dict: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Save a single RawData to MemoryRequestLog

        Delegates to MemoryRequestLogRepository.save_from_raw_data for document
        creation and persistence.

        Args:
            raw_data: RawData object
            group_id: Group ID
            group_name: Group name
            request_id: Request ID
            repo: Repository instance
            version: API version
            endpoint_name: Endpoint name
            method: HTTP method
            url: Request URL
            event_id: Event ID
            raw_input_dict: Raw input dictionary (used to generate raw_input_str)

        Returns:
            Optional[str]: Returns message_id if saved successfully, None otherwise
        """
        if not group_id:
            logger.debug("group_id is empty, skipping save")
            return None

        return await repo.save_from_raw_data(
            raw_data_content=raw_data.content or {},
            data_id=raw_data.data_id,
            group_id=group_id,
            group_name=group_name,
            request_id=request_id,
            version=version,
            endpoint_name=endpoint_name,
            method=method,
            url=url,
            event_id=event_id,
            raw_input_dict=raw_input_dict,
        )

    # ==================== Query Methods ====================

    async def check_duplicate_message(
        self, group_id: str, user_id: str, message_id: str
    ) -> bool:
        """
        Check if a message with the given group_id, user_id, and message_id already exists

        Used for duplicate detection before processing new memorize requests.
        This helps prevent duplicate message processing when the same message
        is submitted multiple times.

        Args:
            group_id: Conversation group ID
            user_id: User ID (sender)
            message_id: Message ID

        Returns:
            bool: True if the message already exists, False otherwise
        """
        repo = self._get_repository()
        try:
            existing = await repo.find_one_by_group_user_message(
                group_id=group_id, user_id=user_id, message_id=message_id
            )
            if existing:
                logger.info(
                    "Duplicate message detected: group_id=%s, user_id=%s, message_id=%s",
                    group_id,
                    user_id,
                    message_id,
                )
                return True
            return False
        except Exception as e:
            logger.error(
                "Failed to check duplicate message: group_id=%s, user_id=%s, message_id=%s, error=%s",
                group_id,
                user_id,
                message_id,
                e,
            )
            # In case of error, return False to allow the request to proceed
            # This is a fail-open approach to avoid blocking legitimate requests
            return False

    async def get_pending_request_logs(
        self,
        user_id: Optional[str] = MAGIC_ALL,
        group_ids: Optional[List[str]] = None,
        sync_status_list: Optional[List[int]] = None,
        limit: int = 1000,
        skip: int = 0,
        ascending: bool = True,
    ) -> List[MemoryRequestLog]:
        """
        Get pending (unconsumed) Memory request logs

        Query request logs that have not been consumed yet (sync_status=-1 or 0).
        Supports flexible filtering with MAGIC_ALL logic:
        - MAGIC_ALL ("__all__"): Don't filter by this field
        - None or "": Filter for null/empty values
        - Other values: Exact match

        Args:
            user_id: User ID filter
                - MAGIC_ALL: Don't filter by user_id (default)
                - None or "": Filter for null/empty values
                - Other values: Exact match
            group_ids: List of Group IDs to filter (None to skip filtering, searches all groups)
            sync_status_list: List of sync_status values to filter by
                - Default: [-1, 0] (pending and accumulating, i.e., unconsumed)
                - [-1]: Just log records
                - [0]: In window accumulation
                - [1]: Already fully used
            limit: Maximum number of records to return (default 100)
            skip: Number of records to skip (default 0)
            ascending: If True (default), sort by created_at ascending (oldest first);
                       if False, sort descending (newest first)

        Returns:
            List[MemoryRequestLog]: List of pending request logs
        """
        # Default to unconsumed statuses
        if sync_status_list is None:
            sync_status_list = [-1, 0]

        repo = self._get_repository()

        try:
            results = await repo.find_pending_by_filters(
                user_id=user_id,
                group_ids=group_ids,
                sync_status_list=sync_status_list,
                limit=limit,
                skip=skip,
                ascending=ascending,
            )

            logger.debug(
                "Retrieved pending request logs: user_id=%s, group_ids=%s, "
                "sync_status_list=%s, count=%d",
                user_id,
                group_ids,
                sync_status_list,
                len(results),
            )
            return results
        except Exception as e:
            logger.error(
                "Failed to get pending request logs: user_id=%s, group_ids=%s, error=%s",
                user_id,
                group_ids,
                e,
            )
            return []

    async def get_pending_messages(
        self,
        user_id: Optional[str] = MAGIC_ALL,
        group_ids: Optional[List[str]] = None,
        limit: int = 1000,
    ) -> List[PendingMessage]:
        """
        Get pending (unconsumed) messages as list of PendingMessage objects.

        This is a convenience method that wraps get_pending_request_logs
        and converts the results to PendingMessage dataclass instances.

        Args:
            user_id: User ID filter (MAGIC_ALL to skip filtering)
            group_ids: List of Group IDs to filter (None to skip filtering, searches all groups)
            limit: Maximum number of records to return (default 1000)

        Returns:
            List[PendingMessage]: List of pending messages
        """
        logs = await self.get_pending_request_logs(
            user_id=user_id, group_ids=group_ids, limit=limit
        )

        # Convert to list of PendingMessage
        result = []
        for log in logs:
            pending_msg = PendingMessage(
                id=str(log.id),
                request_id=log.request_id,
                message_id=log.message_id,
                group_id=log.group_id,
                user_id=log.user_id,
                sender=log.sender,
                sender_name=log.sender_name,
                group_name=log.group_name,
                content=log.content,
                refer_list=log.refer_list,
                message_create_time=log.message_create_time,
                created_at=to_iso_format(log.created_at),
                updated_at=to_iso_format(log.updated_at),
            )
            result.append(pending_msg)

        logger.debug(
            "Converted %d pending request logs to PendingMessage: user_id=%s, group_ids=%s",
            len(result),
            user_id,
            group_ids,
        )
        return result

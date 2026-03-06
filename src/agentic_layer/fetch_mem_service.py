"""
Memory retrieval service

This module provides a service layer interface for accessing memory data, interfacing with repository classes that access the database.
Provides ID-based query functionality, supporting retrieval of various memory types.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import List, Optional, Union

from core.di import get_bean_by_type, get_bean, service
from core.oxm.constants import MAGIC_ALL
from common_utils.datetime_utils import from_iso_format
from infra_layer.adapters.out.persistence.document.memory.foresight_record import (
    ForesightRecord,
    ForesightRecordProjection,
)
from infra_layer.adapters.out.persistence.repository.episodic_memory_raw_repository import (
    EpisodicMemoryRawRepository,
)
from infra_layer.adapters.out.persistence.document.memory.episodic_memory import (
    EpisodicMemoryProjection,
)
from infra_layer.adapters.out.persistence.repository.core_memory_raw_repository import (
    CoreMemoryRawRepository,
)
from infra_layer.adapters.out.persistence.repository.behavior_history_raw_repository import (
    BehaviorHistoryRawRepository,
)
from infra_layer.adapters.out.persistence.repository.conversation_meta_raw_repository import (
    ConversationMetaRawRepository,
)
from infra_layer.adapters.out.persistence.repository.event_log_record_raw_repository import (
    EventLogRecordRawRepository,
)
from infra_layer.adapters.out.persistence.document.memory.event_log_record import (
    EventLogRecord,
    EventLogRecordProjection,
)
from infra_layer.adapters.out.persistence.repository.foresight_record_repository import (
    ForesightRecordRawRepository,
)
from infra_layer.adapters.out.persistence.repository.memcell_raw_repository import (
    MemCellRawRepository,
)
from infra_layer.adapters.out.persistence.document.memory.foresight_record import (
    ForesightRecordProjection,
)
from infra_layer.adapters.out.persistence.repository.user_profile_raw_repository import (
    UserProfileRawRepository,
)
from infra_layer.adapters.out.persistence.repository.global_user_profile_raw_repository import (
    GlobalUserProfileRawRepository,
)
from infra_layer.adapters.out.persistence.repository.agent_case_raw_repository import (
    AgentCaseRawRepository,
)
from infra_layer.adapters.out.persistence.repository.agent_skill_raw_repository import (
    AgentSkillRawRepository,
)
from api_specs.dtos import FetchMemResponse
from api_specs.memory_models import (
    MemoryType,
    BaseMemoryModel,
    ProfileModel,
    GlobalUserProfileModel,
    CombinedProfileModel,
    PreferenceModel,
    EpisodicMemoryModel,
    BehaviorHistoryModel,
    CoreMemoryModel,
    EventLogModel,
    ForesightModel,
    AgentCaseModel,
    AgentSkillModel,
    Metadata,
)

logger = logging.getLogger(__name__)


class FetchMemoryServiceInterface(ABC):
    """Memory retrieval service interface"""

    @abstractmethod
    async def find_memories(
        self,
        user_id: str,
        memory_type: MemoryType,
        group_ids: Optional[List[str]] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> FetchMemResponse:
        """
        Find memories by user ID and optional filters

        Args:
            user_id: User ID
            memory_type: Memory type
            group_ids: List of Group IDs for batch query (None to skip group filtering)
            start_time: Start time for time range filtering (optional)
            end_time: End time for time range filtering (optional)
            page: Page number, starts from 1
            page_size: Number of records per page

        Returns:
            Memory query response
        """
        pass


@service(name="fetch_memory_service", primary=True)
class FetchMemoryServiceImpl(FetchMemoryServiceInterface):
    """Real implementation of memory retrieval service

    Uses repository instances injected by DI framework for database access.
    """

    def __init__(self):
        """Initialize service"""
        self._episodic_repo = None
        self._core_repo = None
        self._behavior_repo = None
        self._conversation_meta_repo = None
        self._event_log_repo = None
        self._foresight_record_repo = None
        self._user_profile_repo = None
        self._global_user_profile_repo = None
        self._agent_case_repo = None
        self._agent_skill_repo = None
        logger.info("FetchMemoryServiceImpl initialized")

    def _get_repositories(self):
        """Get repository instances"""
        if self._episodic_repo is None:
            self._episodic_repo = get_bean_by_type(EpisodicMemoryRawRepository)
        if self._core_repo is None:
            self._core_repo = get_bean_by_type(CoreMemoryRawRepository)
        if self._behavior_repo is None:
            self._behavior_repo = get_bean_by_type(BehaviorHistoryRawRepository)
        if self._conversation_meta_repo is None:
            self._conversation_meta_repo = get_bean_by_type(
                ConversationMetaRawRepository
            )
        if self._event_log_repo is None:
            self._event_log_repo = get_bean_by_type(EventLogRecordRawRepository)
        if self._foresight_record_repo is None:
            self._foresight_record_repo = get_bean_by_type(ForesightRecordRawRepository)
        if self._user_profile_repo is None:
            self._user_profile_repo = get_bean_by_type(UserProfileRawRepository)
        if self._global_user_profile_repo is None:
            self._global_user_profile_repo = get_bean_by_type(
                GlobalUserProfileRawRepository
            )
        if self._agent_case_repo is None:
            self._agent_case_repo = get_bean_by_type(
                AgentCaseRawRepository
            )
        if self._agent_skill_repo is None:
            self._agent_skill_repo = get_bean_by_type(AgentSkillRawRepository)

    async def _get_user_details_cache(self, group_id: str) -> dict:
        """
        Get user details cache from conversation-meta for batch processing

        Args:
            group_id: Group ID

        Returns:
            Dictionary mapping user_id to user details (full_name, email, phone)
        """
        try:
            if not group_id or group_id == MAGIC_ALL:
                return {}

            # Ensure repository is initialized
            if self._conversation_meta_repo is None:
                self._get_repositories()

            # Query conversation metadata
            conversation_meta = (
                await self._conversation_meta_repo.get_by_group_id_with_fallback(
                    group_id, key="user_details"
                )
            )

            if not conversation_meta or not conversation_meta.user_details:
                return {}

            # Build user details cache
            user_cache = {}
            for uid, user_detail in conversation_meta.user_details.items():
                user_cache[uid] = {
                    'full_name': user_detail.full_name,
                    'email': (
                        user_detail.extra.get('email') if user_detail.extra else None
                    ),
                    'phone': (
                        user_detail.extra.get('phone') if user_detail.extra else None
                    ),
                }

            return user_cache

        except Exception as e:
            logger.warning(f"Failed to get user details cache: {e}")
            return {}

    async def _get_memcells_for_memories(self, memories: list) -> dict:
        """
        Batch fetch MemCells for memories that have parent_type='memcell'

        Args:
            memories: List of memory records (EpisodicMemory/EventLog/ForesightRecord)

        Returns:
            Dict[parent_id, MemCell]: Mapping dictionary
        """
        # Collect parent_ids (only parent_type=memcell)
        parent_ids = []
        for mem in memories:
            parent_type = getattr(mem, 'parent_type', None)
            parent_id = getattr(mem, 'parent_id', None)
            if parent_type == 'memcell' and parent_id:
                parent_ids.append(parent_id)

        if not parent_ids:
            return {}

        # Deduplicate
        unique_parent_ids = list(set(parent_ids))

        try:
            # Batch fetch MemCells
            memcell_repo = get_bean_by_type(MemCellRawRepository)
            memcells_cache = await memcell_repo.get_by_event_ids(unique_parent_ids)
            logger.debug(
                f"Fetched {len(memcells_cache)} MemCells for {len(unique_parent_ids)} parent_ids"
            )
            return memcells_cache
        except Exception as e:
            logger.warning(f"Failed to fetch MemCells: {e}")
            return {}

    def _convert_base_memory(self, core_memory) -> BaseMemoryModel:
        """Convert core memory to base memory model

        Args:
            core_memory: Core memory document

        Returns:
            BaseMemoryModel with basic user information
        """
        base_info = self._core_repo.get_base(core_memory)

        return BaseMemoryModel(
            id=str(core_memory.id),
            user_id=core_memory.user_id,
            content=f"User: {base_info.get('user_name', 'Unknown')} | Position: {base_info.get('position', 'Unknown')} | Department: {base_info.get('department', 'Unknown')}",
            created_at=core_memory.created_at,
            updated_at=core_memory.updated_at,
            metadata={
                "user_name": base_info.get('user_name', ''),
                "position": base_info.get('position', ''),
                "department": base_info.get('department', ''),
                "company": base_info.get('company', ''),
                "location": base_info.get('location', ''),
                "contact": base_info.get('contact', {}),
            },
        )

    def _convert_user_profile(self, user_profile) -> ProfileModel:
        """Convert user profile document to ProfileModel

        Args:
            user_profile: User profile document

        Returns:
            ProfileModel instance
        """
        return ProfileModel(
            id=str(user_profile.id),
            user_id=user_profile.user_id,
            group_id=user_profile.group_id,
            profile_data=user_profile.profile_data,
            scenario=user_profile.scenario,
            confidence=user_profile.confidence,
            version=user_profile.version,
            cluster_ids=user_profile.cluster_ids,
            memcell_count=user_profile.memcell_count,
            last_updated_cluster=user_profile.last_updated_cluster,
            created_at=user_profile.created_at,
            updated_at=user_profile.updated_at,
        )

    def _convert_global_user_profile(
        self, global_user_profile
    ) -> GlobalUserProfileModel:
        """Convert global user profile document to GlobalUserProfileModel

        Args:
            global_user_profile: Global user profile document

        Returns:
            GlobalUserProfileModel instance
        """
        return GlobalUserProfileModel(
            id=str(global_user_profile.id),
            user_id=global_user_profile.user_id,
            profile_data=global_user_profile.profile_data,
            custom_profile_data=global_user_profile.custom_profile_data,
            confidence=global_user_profile.confidence,
            memcell_count=global_user_profile.memcell_count,
            created_at=global_user_profile.created_at,
            updated_at=global_user_profile.updated_at,
        )

    def _convert_preferences_from_core_memory(
        self, core_memory
    ) -> list[PreferenceModel]:
        """Convert core memory to preference models

        Args:
            core_memory: Core memory document

        Returns:
            List of PreferenceModel instances
        """
        preference_info = self._core_repo.get_preference(core_memory)
        memories = []

        for key, value in preference_info.items():
            memories.append(
                PreferenceModel(
                    id=f"{core_memory.id}_{key}",
                    user_id=core_memory.user_id,
                    category="Personal preference",
                    preference_key=key,
                    preference_value=str(value),
                    confidence_score=1.0,
                    created_at=core_memory.created_at,
                    updated_at=core_memory.updated_at,
                    metadata={"source": "core_memory", "original_key": key},
                )
            )

        return memories

    def _convert_episodic_memory(
        self,
        episodic_memory,
        user_details_cache: dict = None,
        memcells_cache: dict = None,
    ) -> EpisodicMemoryModel:
        """Convert episodic memory document to model

        Args:
            episodic_memory: Episodic memory document
            user_details_cache: User details cache for batch metadata creation
            memcells_cache: MemCell cache for original_data retrieval
        """
        # Create metadata with user details from cache
        user_info = (
            user_details_cache.get(episodic_memory.user_id, {})
            if user_details_cache
            else {}
        )
        metadata = Metadata(
            source=MemoryType.EPISODIC_MEMORY.value,
            user_id=episodic_memory.user_id,
            group_ids=[episodic_memory.group_id] if episodic_memory.group_id else None,
            memory_types=[MemoryType.EPISODIC_MEMORY.value],
            full_name=user_info.get('full_name'),
            email=user_info.get('email'),
            phone=user_info.get('phone'),
        )

        # Get original_data from MemCell cache
        original_data = None
        parent_type = getattr(episodic_memory, 'parent_type', None)
        parent_id = getattr(episodic_memory, 'parent_id', None)
        if parent_type == 'memcell' and parent_id and memcells_cache:
            memcell = memcells_cache.get(parent_id)
            if memcell and hasattr(memcell, 'original_data') and memcell.original_data:
                original_data = memcell.original_data

        return EpisodicMemoryModel(
            id=str(episodic_memory.id),
            user_id=episodic_memory.user_id,
            episode_id=str(episodic_memory.event_id),
            episode=episodic_memory.episode,
            subject=episodic_memory.subject,
            summary=episodic_memory.summary,
            timestamp=episodic_memory.timestamp,
            participants=episodic_memory.participants or [],
            location=(
                episodic_memory.extend.get("location", "")
                if episodic_memory.extend
                else ""
            ),
            keywords=episodic_memory.keywords or [],
            group_id=episodic_memory.group_id,
            group_name=episodic_memory.group_name,
            created_at=episodic_memory.created_at,
            updated_at=episodic_memory.updated_at,
            metadata=metadata,
            parent_type=parent_type,
            parent_id=parent_id,
            original_data=original_data,
        )

    def _convert_event_log(
        self,
        event_log: Union[EventLogRecord, EventLogRecordProjection],
        user_details_cache: dict = None,
        memcells_cache: dict = None,
    ) -> EventLogModel:
        """Convert event log document to model

        Supports both EventLogRecord and EventLogRecordShort types.
        EventLogRecordShort does not contain the vector field.

        Args:
            event_log: Event log document
            user_details_cache: User details cache for batch metadata creation
            memcells_cache: MemCell cache for original_data retrieval
        """
        # Create metadata with user details from cache
        user_info = (
            user_details_cache.get(event_log.user_id, {}) if user_details_cache else {}
        )
        metadata = Metadata(
            source=MemoryType.EVENT_LOG.value,
            user_id=event_log.user_id,
            group_ids=[event_log.group_id] if event_log.group_id else None,
            memory_types=[MemoryType.EVENT_LOG.value],
            full_name=user_info.get('full_name'),
            email=user_info.get('email'),
            phone=user_info.get('phone'),
        )

        # Get original_data from MemCell cache
        original_data = None
        if (
            event_log.parent_type == 'memcell'
            and event_log.parent_id
            and memcells_cache
        ):
            memcell = memcells_cache.get(event_log.parent_id)
            if memcell and hasattr(memcell, 'original_data') and memcell.original_data:
                original_data = memcell.original_data

        return EventLogModel(
            id=str(event_log.id),
            user_id=event_log.user_id,
            atomic_fact=event_log.atomic_fact,
            parent_type=event_log.parent_type,
            parent_id=event_log.parent_id,
            timestamp=event_log.timestamp,
            user_name=event_log.user_name,
            group_id=event_log.group_id,
            group_name=event_log.group_name,
            participants=event_log.participants,
            vector=getattr(
                event_log, 'vector', None
            ),  # EventLogRecordShort does not have vector field
            vector_model=event_log.vector_model,
            event_type=event_log.event_type,
            extend=event_log.extend,
            created_at=event_log.created_at,
            updated_at=event_log.updated_at,
            metadata=metadata,
            original_data=original_data,
        )

    def _convert_foresight_record(
        self,
        foresight_record: Union[ForesightRecord, ForesightRecordProjection],
        user_details_cache: dict = None,
        memcells_cache: dict = None,
    ) -> ForesightModel:
        """Convert foresight record document to model

        Supports both ForesightRecord and ForesightRecordProjection types.
        ForesightRecordProjection does not contain the vector field.

        Args:
            foresight_record: Foresight record document
            user_details_cache: User details cache for batch metadata creation
            memcells_cache: MemCell cache for original_data retrieval
        """
        # Create metadata with user details from cache
        uid = foresight_record.user_id or ""
        user_info = user_details_cache.get(uid, {}) if user_details_cache else {}
        metadata = Metadata(
            source=MemoryType.FORESIGHT.value,
            user_id=uid,
            group_ids=(
                [foresight_record.group_id] if foresight_record.group_id else None
            ),
            memory_types=[MemoryType.FORESIGHT.value],
            full_name=user_info.get('full_name'),
            email=user_info.get('email'),
            phone=user_info.get('phone'),
        )

        # Get original_data from MemCell cache
        original_data = None
        if (
            foresight_record.parent_type == 'memcell'
            and foresight_record.parent_id
            and memcells_cache
        ):
            memcell = memcells_cache.get(foresight_record.parent_id)
            if memcell and hasattr(memcell, 'original_data') and memcell.original_data:
                original_data = memcell.original_data

        return ForesightModel(
            id=str(foresight_record.id),
            content=foresight_record.content,
            foresight=foresight_record.content,
            evidence=foresight_record.evidence,
            parent_type=foresight_record.parent_type,
            parent_id=foresight_record.parent_id,
            user_id=foresight_record.user_id,
            user_name=foresight_record.user_name,
            group_id=foresight_record.group_id,
            group_name=foresight_record.group_name,
            start_time=foresight_record.start_time,
            end_time=foresight_record.end_time,
            duration_days=foresight_record.duration_days,
            participants=foresight_record.participants,
            vector=getattr(
                foresight_record, 'vector', None
            ),  # ForesightRecordProjection does not have vector field
            vector_model=foresight_record.vector_model,
            extend=foresight_record.extend,
            created_at=foresight_record.created_at,
            updated_at=foresight_record.updated_at,
            metadata=metadata,
            original_data=original_data,
        )

    def _convert_agent_case(self, record) -> AgentCaseModel:
        """Convert AgentCaseRecord to AgentCaseModel."""
        metadata = Metadata(
            source=MemoryType.AGENT_CASE.value,
            user_id=record.user_id or "",
            group_ids=[record.group_id] if record.group_id else None,
            memory_types=[MemoryType.AGENT_CASE.value],
        )

        return AgentCaseModel(
            id=str(record.id),
            user_id=record.user_id,
            group_id=record.group_id,
            timestamp=record.timestamp,
            task_intent=record.task_intent or "",
            approach=record.approach or "",
            quality_score=record.quality_score,
            parent_type=record.parent_type,
            parent_id=record.parent_id,
            extend=record.extend,
            created_at=record.created_at,
            updated_at=record.updated_at,
            metadata=metadata,
        )

    def _convert_agent_skill(self, record) -> AgentSkillModel:
        """Convert AgentSkillRecord to AgentSkillModel."""
        metadata = Metadata(
            source=MemoryType.AGENT_SKILL.value,
            user_id=record.user_id or "",
            group_ids=[record.group_id] if record.group_id else None,
            memory_types=[MemoryType.AGENT_SKILL.value],
        )

        return AgentSkillModel(
            id=str(record.id),
            cluster_id=record.cluster_id,
            name=record.name,
            content=record.content,
            user_id=record.user_id,
            description=record.description,
            group_id=record.group_id,
            confidence=record.confidence,
            extend=record.extend,
            created_at=record.created_at,
            updated_at=record.updated_at,
            metadata=metadata,
        )

    async def find_memories(
        self,
        user_id: str,
        memory_type: MemoryType,
        group_ids: Optional[List[str]] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> FetchMemResponse:
        """
        Find memories by user ID and optional filters

        Args:
            user_id: User ID (MAGIC_ALL to skip user filtering)
            memory_type: Memory type
            group_ids: List of Group IDs for batch query (None to skip group filtering)
            start_time: Start time for time range filtering (ISO format string)
            end_time: End time for time range filtering (ISO format string)
            page: Page number, starts from 1
            page_size: Number of records per page

        Returns:
            Memory query response

        Time Field Mapping by Memory Type:
        ----------------------------------
        The start_time and end_time parameters map to different fields based on memory type:

        - EPISODIC_MEMORY: Filters by `timestamp` field (event occurrence time)
        - EVENT_LOG: Filters by `timestamp` field (log record time)
        - FORESIGHT: Filters by validity period overlap (`start_time`, `end_time` fields)
                    Uses overlap logic: foresight active if [foresight.start, foresight.end] overlaps [query.start, query.end]
        - PROFILE: No time filtering supported (only has `created_at`, `updated_at` audit fields)
        - BASE_MEMORY: No time filtering supported (core memory snapshot)
        - PREFERENCE: No time filtering supported (extracted from core memory)
        - ENTITY: No time filtering supported in current implementation
        - RELATION: No time filtering supported in current implementation
        - BEHAVIOR_HISTORY: No time filtering supported in current implementation
        """
        # Convert page/page_size to limit/offset for internal use
        limit = page_size
        offset = (page - 1) * page_size

        logger.debug(
            f"Fetching {memory_type} memories for user_id={user_id}, group_ids={group_ids}, "
            f"time_range=[{start_time}, {end_time}], page={page}, page_size={page_size}"
        )

        try:
            self._get_repositories()
            memories = []
            total_count = 0  # Total matching records for pagination

            # Parse time range if provided
            start_dt = from_iso_format(start_time) if start_time else None
            end_dt = from_iso_format(end_time) if end_time else None

            # Fetch user details cache once for batch metadata creation
            # This optimizes performance by querying conversation_meta only once
            # Use first group_id for user details cache (for backward compatibility)
            first_group_id = group_ids[0] if group_ids and len(group_ids) > 0 else None
            user_details_cache = await self._get_user_details_cache(first_group_id)
            logger.debug(
                f"Fetched user details cache with {len(user_details_cache)} users"
            )

            match memory_type:
                case MemoryType.FORESIGHT:
                    # Foresight: supports group_ids filtering and time range overlap queries
                    # Time filtering is based on foresight validity period (start_time, end_time fields)

                    # Query records and count in parallel
                    foresight_records, total_count = await asyncio.gather(
                        self._foresight_record_repo.find_by_filters(
                            user_id=user_id,
                            group_ids=group_ids,
                            start_time=start_dt,
                            end_time=end_dt,
                            limit=limit,
                            skip=offset,
                            model=ForesightRecordProjection,
                        ),
                        self._foresight_record_repo.count_by_filters(
                            user_id=user_id,
                            group_ids=group_ids,
                            start_time=start_dt,
                            end_time=end_dt,
                        ),
                    )

                    # Batch fetch MemCells for original_data
                    memcells_cache = await self._get_memcells_for_memories(
                        foresight_records
                    )

                    memories = [
                        self._convert_foresight_record(
                            record,
                            user_details_cache=user_details_cache,
                            memcells_cache=memcells_cache,
                        )
                        for record in foresight_records
                    ]

                case MemoryType.EPISODIC_MEMORY:
                    # Episodic memory: fully supports group_ids and timestamp filtering at DB level

                    # Query records and count in parallel
                    episodic_memories, total_count = await asyncio.gather(
                        self._episodic_repo.find_by_filters(
                            user_id=user_id,
                            group_ids=group_ids,
                            start_time=start_dt,
                            end_time=end_dt,
                            limit=limit,
                            skip=offset,
                            sort_desc=True,
                            model=EpisodicMemoryProjection,
                        ),
                        self._episodic_repo.count_by_filters(
                            user_id=user_id,
                            group_ids=group_ids,
                            start_time=start_dt,
                            end_time=end_dt,
                        ),
                    )

                    # Batch fetch MemCells for original_data
                    memcells_cache = await self._get_memcells_for_memories(
                        episodic_memories
                    )

                    memories = [
                        self._convert_episodic_memory(
                            mem,
                            user_details_cache=user_details_cache,
                            memcells_cache=memcells_cache,
                        )
                        for mem in episodic_memories
                    ]
                case MemoryType.EVENT_LOG:
                    # Event log: fully supports group_ids and timestamp filtering at DB level

                    # Query records and count in parallel
                    event_logs, total_count = await asyncio.gather(
                        self._event_log_repo.find_by_filters(
                            user_id=user_id,
                            group_ids=group_ids,
                            start_time=start_dt,
                            end_time=end_dt,
                            limit=limit,
                            skip=offset,
                            sort_desc=True,
                            model=EventLogRecordProjection,
                        ),
                        self._event_log_repo.count_by_filters(
                            user_id=user_id,
                            group_ids=group_ids,
                            start_time=start_dt,
                            end_time=end_dt,
                        ),
                    )

                    # Batch fetch MemCells for original_data
                    memcells_cache = await self._get_memcells_for_memories(event_logs)

                    memories = [
                        self._convert_event_log(
                            event_log,
                            user_details_cache=user_details_cache,
                            memcells_cache=memcells_cache,
                        )
                        for event_log in event_logs
                    ]

                case MemoryType.PROFILE:
                    # Profile: supports user_id and group_ids filtering, no time filtering
                    # Uses created_at/updated_at fields (not time range filterable)
                    # Also fetches global_user_profile and returns CombinedProfileModel

                    # Fetch user_profiles, count, and global_user_profile concurrently
                    tasks = [
                        self._user_profile_repo.find_by_filters(
                            user_id=user_id,
                            group_ids=group_ids,
                            limit=limit,
                            skip=offset,
                        ),
                        self._user_profile_repo.count_by_filters(
                            user_id=user_id, group_ids=group_ids
                        ),
                    ]

                    if user_id and user_id != MAGIC_ALL:
                        tasks.append(
                            self._global_user_profile_repo.get_by_user_id(
                                user_id=user_id
                            )
                        )
                        user_profiles, total_count, global_user_profile = (
                            await asyncio.gather(*tasks)
                        )
                    else:
                        user_profiles, total_count = await asyncio.gather(*tasks)
                        global_user_profile = None

                    profile_models = [
                        self._convert_user_profile(up) for up in user_profiles
                    ]

                    global_profile_model = None
                    if global_user_profile:
                        global_profile_model = self._convert_global_user_profile(
                            global_user_profile
                        )

                    # Return CombinedProfileModel containing both profiles
                    combined_profile = CombinedProfileModel(
                        user_id=user_id,
                        group_ids=group_ids,
                        profiles=profile_models,
                        global_profile=global_profile_model,
                    )
                    memories = [combined_profile]

                case MemoryType.AGENT_CASE:
                    agent_cases, total_count = await asyncio.gather(
                        self._agent_case_repo.find_by_filters(
                            user_id=user_id,
                            group_ids=group_ids,
                            start_time=start_dt,
                            end_time=end_dt,
                            limit=limit,
                            skip=offset,
                            sort_desc=True,
                        ),
                        self._agent_case_repo.count_by_filters(
                            user_id=user_id,
                            group_ids=group_ids,
                            start_time=start_dt,
                            end_time=end_dt,
                        ),
                    )

                    memories = [
                        self._convert_agent_case(record)
                        for record in agent_cases
                    ]

                case MemoryType.AGENT_SKILL:
                    skill_records, total_count = await asyncio.gather(
                        self._agent_skill_repo.find_by_filters(
                            user_id=user_id,
                            group_ids=group_ids,
                            limit=limit,
                            skip=offset,
                        ),
                        self._agent_skill_repo.count_by_filters(
                            user_id=user_id,
                            group_ids=group_ids,
                        ),
                    )

                    memories = [
                        self._convert_agent_skill(record) for record in skill_records
                    ]

            # Create response-level metadata (for the query itself)
            # This is query-level metadata, not user-specific
            response_metadata = Metadata(
                source=memory_type.value,
                user_id=user_id,
                group_ids=group_ids,
                memory_types=[memory_type.value],
            )

            return FetchMemResponse(
                memories=memories,
                total_count=total_count,  # Total matching records for pagination
                count=len(memories),  # Current page record count
                metadata=response_metadata,
            )

        except Exception as e:
            import traceback

            traceback.print_exc()
            logger.error(
                f"Error fetching memories for user_id={user_id}, group_ids={group_ids}: {e}",
                exc_info=True,
            )
            # Return error response with basic metadata
            error_metadata = Metadata(
                source=memory_type.value,
                user_id=user_id,
                group_ids=group_ids,
                memory_types=[memory_type.value],
            )

            return FetchMemResponse(
                memories=[], total_count=0, count=0, metadata=error_metadata
            )


def get_fetch_memory_service() -> FetchMemoryServiceInterface:
    """Get memory retrieval service instance

    Retrieve service instance via dependency injection framework, supporting singleton pattern.
    """
    return get_bean("fetch_memory_service")

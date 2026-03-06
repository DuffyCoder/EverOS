"""
Memory retrieval process configuration

Centralized management of all trigger conditions and thresholds for easy adjustment and maintenance.
"""

from dataclasses import dataclass
import os

from api_specs.memory_types import ParentType


@dataclass
class MemorizeConfig:
    """Memory retrieval process configuration"""

    # ===== Clustering configuration =====
    # Semantic similarity threshold; memcells exceeding this value will be clustered into the same cluster
    cluster_similarity_threshold: float = 0.3
    # Maximum time gap (days); memcells exceeding this gap will not be clustered together
    cluster_max_time_gap_days: int = 7

    # ===== Profile extraction configuration =====
    # Minimum number of memcells required to trigger Profile extraction
    profile_min_memcells: int = 1
    # Minimum confidence required for Profile extraction
    profile_min_confidence: float = 0.6
    # Whether to enable version control
    profile_enable_versioning: bool = True
    # Life Profile maximum items (ASSISTANT scene only)
    profile_life_max_items: int = 25

    # ===== Smart Mask configuration =====
    # Threshold for enabling smart_mask optimization (when history messages exceed this)
    smart_mask_history_threshold: int = 5

    # ===== Parent type configuration =====
    # Default parent type for Episode (memcell or episode)
    default_episode_parent_type: str = ParentType.MEMCELL.value
    # Default parent type for Foresight (memcell or episode)
    default_foresight_parent_type: str = ParentType.MEMCELL.value
    # Default parent type for EventLog (memcell or episode)
    default_eventlog_parent_type: str = ParentType.MEMCELL.value
    # ===== Agent Skill extraction configuration =====
    # Minimum number of AgentCases in a cluster to trigger skill extraction
    skill_min_experiences: int = 1


# Global default configuration (can be overridden via from_env())
# TODO Move nescessary configurations to ENV. Use default values for now.
DEFAULT_MEMORIZE_CONFIG = MemorizeConfig()

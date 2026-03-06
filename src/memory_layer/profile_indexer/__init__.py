"""
Profile Indexer Module

Provides indexing services for user profiles into vector databases (Milvus).
"""

from .profile_life_indexer import ProfileLifeIndexer, index_user_profile

__all__ = [
    "ProfileLifeIndexer",
    "index_user_profile",
]

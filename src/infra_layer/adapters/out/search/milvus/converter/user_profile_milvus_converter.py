"""
User Profile Milvus Converter

Responsible for converting UserProfile data into Milvus Collection entities.
Unlike other converters, this handles one-to-many conversion:
- One UserProfile document -> Multiple Milvus entities (one per profile item)

Main handles:
- Splitting profile_data into individual items (explicit_info and implicit_traits)
- Building embed_text for each item
- Field mapping and data format conversion
"""

from typing import Dict, Any, List
import time

from core.oxm.milvus.base_converter import BaseMilvusConverter
from core.observation.logger import get_logger
from infra_layer.adapters.out.search.milvus.memory.user_profile_collection import (
    UserProfileCollection,
)
from infra_layer.adapters.out.persistence.document.memory.user_profile import (
    UserProfile as MongoUserProfile,
)
from memory_layer.memory_extractor.profile_memory_life.types import (
    ProfileMemoryLife,
    ExplicitInfo,
    ImplicitTrait,
)
from bson import ObjectId

logger = get_logger(__name__)

# Maximum length for embed_text field (matches Milvus schema)
MAX_EMBED_TEXT_LENGTH = 4096


class UserProfileMilvusConverter(BaseMilvusConverter[UserProfileCollection]):
    """
    User Profile Milvus Converter

    Converts UserProfile data into Milvus Collection entities.
    Handles one-to-many conversion: one profile -> multiple items.
    
    Each explicit_info and implicit_trait becomes a separate Milvus entity
    for fine-grained semantic search.
    """

    @classmethod
    def from_mongo(cls, source_doc: MongoUserProfile) -> List[Dict[str, Any]]:
        """
        Convert from MongoDB UserProfile document to multiple Milvus entities
        
        Note: Unlike other converters, this returns a List of entities
        because one UserProfile contains multiple searchable items.

        Use cases:
        - During Milvus index rebuilding, convert MongoDB documents into Milvus entities
        - During profile indexing, convert profile data into Milvus entities
        
        Args:
            source_doc: MongoDB UserProfile document instance
            
        Returns:
            List[Dict[str, Any]]: List of Milvus entity dictionaries (one per profile item)
            
        Raises:
            ValueError: If document is invalid
            Exception: If conversion error occurs
        """
        # Basic validation
        if source_doc is None:
            raise ValueError("MongoDB document cannot be empty")
            
        if not source_doc.profile_data:
            logger.warning(
                "UserProfile has no profile_data: user_id=%s, group_id=%s",
                source_doc.user_id,
                source_doc.group_id,
            )
            return []
        
        try:
            # Parse profile_data as ProfileMemoryLife (from_dict handles nested object conversion)
            profile_life = ProfileMemoryLife.from_dict(
                source_doc.profile_data,
                user_id=source_doc.user_id,
                group_id=source_doc.group_id,
            )
            
            user_id = source_doc.user_id
            group_id = source_doc.group_id
            entities = []
            current_time = int(time.time())
            
            # Process explicit_info items
            for i, info in enumerate(profile_life.explicit_info or []):
                entity = cls._build_entity_from_explicit_info(
                    user_id=user_id,
                    group_id=group_id,
                    info=info,
                    item_index=i,
                    timestamp=current_time,
                )
                if entity:
                    entities.append(entity)
            
            # Process implicit_traits items
            for i, trait in enumerate(profile_life.implicit_traits or []):
                entity = cls._build_entity_from_implicit_trait(
                    user_id=user_id,
                    group_id=group_id,
                    trait=trait,
                    item_index=i,
                    timestamp=current_time,
                )
                if entity:
                    entities.append(entity)
            
            logger.debug(
                "Converted UserProfile to %d Milvus entities: user_id=%s, group_id=%s",
                len(entities),
                user_id,
                group_id,
            )
            
            return entities
            
        except Exception as e:
            logger.error(
                "Failed to convert MongoDB UserProfile to Milvus entities: %s",
                e,
                exc_info=True,
            )
            raise

    @classmethod
    def _build_entity_from_explicit_info(
        cls,
        user_id: str,
        group_id: str,
        info: ExplicitInfo,
        item_index: int,
        timestamp: int,
    ) -> Dict[str, Any]:
        """
        Build Milvus entity from ExplicitInfo
        
        Args:
            user_id: User ID
            group_id: Group ID
            info: ExplicitInfo object
            item_index: Index in the array
            timestamp: Current timestamp
            
        Returns:
            Dict[str, Any]: Milvus entity dictionary (without vector)
        """
        if not info.description:
            return None
        
        # Build embed_text: category + description
        embed_text = f"{info.category}: {info.description}"
        
        # Truncate if exceeds max length
        if len(embed_text) > MAX_EMBED_TEXT_LENGTH:
            logger.warning(
                "embed_text exceeds max length (%d > %d), truncating: user_id=%s, item_type=explicit_info, item_index=%d",
                len(embed_text),
                MAX_EMBED_TEXT_LENGTH,
                user_id,
                item_index,
            )
            embed_text = embed_text[:MAX_EMBED_TEXT_LENGTH]
        
        return {
            "id": str(ObjectId()),  # Generate MongoDB-like ObjectId
            "user_id": user_id,
            "group_id": group_id,
            "item_type": "explicit_info",
            "item_index": item_index,
            "embed_text": embed_text,
            "created_at": timestamp,
            "updated_at": timestamp,
            # Note: vector field needs to be set externally after generating embeddings
            "vector": [],
        }

    @classmethod
    def _build_entity_from_implicit_trait(
        cls,
        user_id: str,
        group_id: str,
        trait: ImplicitTrait,
        item_index: int,
        timestamp: int,
    ) -> Dict[str, Any]:
        """
        Build Milvus entity from ImplicitTrait
        
        Args:
            user_id: User ID
            group_id: Group ID
            trait: ImplicitTrait object
            item_index: Index in the array
            timestamp: Current timestamp
            
        Returns:
            Dict[str, Any]: Milvus entity dictionary (without vector)
        """
        if not trait.description:
            return None
        
        # Build embed_text: trait_name + description + basis
        embed_text = f"{trait.trait_name}: {trait.description}"
        if trait.basis:
            embed_text += f". {trait.basis}"
        
        # Truncate if exceeds max length
        if len(embed_text) > MAX_EMBED_TEXT_LENGTH:
            logger.warning(
                "embed_text exceeds max length (%d > %d), truncating: user_id=%s, item_type=implicit_trait, item_index=%d",
                len(embed_text),
                MAX_EMBED_TEXT_LENGTH,
                user_id,
                item_index,
            )
            embed_text = embed_text[:MAX_EMBED_TEXT_LENGTH]
        
        return {
            "id": str(ObjectId()),  # Generate MongoDB-like ObjectId
            "user_id": user_id,
            "group_id": group_id,
            "item_type": "implicit_trait",
            "item_index": item_index,
            "embed_text": embed_text,
            "created_at": timestamp,
            "updated_at": timestamp,
            # Note: vector field needs to be set externally after generating embeddings
            "vector": [],
        }

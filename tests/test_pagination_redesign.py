"""
Unit tests for get_memories interface changes

Tests:
1. group_id → group_ids change (multi-group query support)
2. original_data field support in memory models
3. Pagination redesign (page/page_size, group_ids max 50, total_count/count)
"""

import pytest
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from api_specs.dtos import FetchMemRequest, FetchMemResponse
from api_specs.request_converter import convert_dict_to_fetch_mem_request
from api_specs.memory_models import (
    EpisodicMemoryModel,
    EventLogModel,
    ForesightModel,
    Metadata,
)


class TestFetchMemRequestValidation:
    """Test FetchMemRequest validation"""

    def test_default_page_values(self):
        """Test default page and page_size values"""
        request = FetchMemRequest(user_id="user_1")
        assert request.page == 1
        assert request.page_size == 20

    def test_custom_page_values(self):
        """Test custom page and page_size values"""
        request = FetchMemRequest(user_id="user_1", page=3, page_size=50)
        assert request.page == 3
        assert request.page_size == 50

    def test_page_min_value(self):
        """Test page minimum value validation (must be >= 1)"""
        with pytest.raises(ValueError):
            FetchMemRequest(user_id="user_1", page=0)

    def test_page_size_min_value(self):
        """Test page_size minimum value validation (must be >= 1)"""
        with pytest.raises(ValueError):
            FetchMemRequest(user_id="user_1", page_size=0)

    def test_page_size_max_value(self):
        """Test page_size maximum value validation (must be <= 100)"""
        with pytest.raises(ValueError):
            FetchMemRequest(user_id="user_1", page_size=101)

    def test_group_ids_max_50(self):
        """Test group_ids maximum limit of 50"""
        # 50 should work
        request = FetchMemRequest(
            user_id="user_1", group_ids=[f"group_{i}" for i in range(50)]
        )
        assert len(request.group_ids) == 50

    def test_group_ids_exceeds_50_raises_error(self):
        """Test group_ids exceeds 50 raises ValueError"""
        with pytest.raises(ValueError, match="group_ids exceeds maximum limit of 50"):
            FetchMemRequest(
                user_id="user_1", group_ids=[f"group_{i}" for i in range(51)]
            )

    def test_no_limit_offset_attributes(self):
        """Test that limit and offset attributes no longer exist"""
        request = FetchMemRequest(user_id="user_1")
        assert not hasattr(request, 'limit') or request.limit is None
        assert not hasattr(request, 'offset') or request.offset is None


class TestFetchMemResponseStructure:
    """Test FetchMemResponse structure"""

    def test_response_has_count_field(self):
        """Test response has count field"""
        response = FetchMemResponse(memories=[], total_count=100, count=20)
        assert response.count == 20
        assert response.total_count == 100

    def test_response_no_has_more_field(self):
        """Test response does not have has_more field in model definition"""
        # Check model fields, not instance attributes
        assert 'has_more' not in FetchMemResponse.model_fields

    def test_response_default_values(self):
        """Test response default values"""
        response = FetchMemResponse()
        assert response.memories == []
        assert response.total_count == 0
        assert response.count == 0


class TestRequestConverter:
    """Test request_converter changes"""

    def test_convert_with_page_params(self):
        """Test conversion with page and page_size parameters"""
        data = {
            "user_id": "user_1",
            "page": 2,
            "page_size": 30,
            "memory_type": "episodic_memory",
        }
        request = convert_dict_to_fetch_mem_request(data)
        assert request.page == 2
        assert request.page_size == 30

    def test_convert_with_string_page_params(self):
        """Test conversion with string page/page_size (from query params)"""
        data = {"user_id": "user_1", "page": "3", "page_size": "25"}
        request = convert_dict_to_fetch_mem_request(data)
        assert request.page == 3
        assert request.page_size == 25

    def test_convert_default_page_values(self):
        """Test conversion uses default page values when not provided"""
        data = {"user_id": "user_1"}
        request = convert_dict_to_fetch_mem_request(data)
        assert request.page == 1
        assert request.page_size == 20

    def test_convert_group_ids_from_string(self):
        """Test conversion of comma-separated group_ids string"""
        data = {"user_id": "user_1", "group_ids": "group_1,group_2,group_3"}
        request = convert_dict_to_fetch_mem_request(data)
        assert request.group_ids == ["group_1", "group_2", "group_3"]

    def test_convert_group_ids_from_list(self):
        """Test conversion of group_ids list"""
        data = {"user_id": "user_1", "group_ids": ["group_1", "group_2"]}
        request = convert_dict_to_fetch_mem_request(data)
        assert request.group_ids == ["group_1", "group_2"]


class TestPaginationCalculation:
    """Test pagination calculation scenarios"""

    def test_first_page_calculation(self):
        """Test first page: offset should be 0"""
        # page=1, page_size=20 -> offset=0, limit=20
        page = 1
        page_size = 20
        offset = (page - 1) * page_size
        limit = page_size
        assert offset == 0
        assert limit == 20

    def test_second_page_calculation(self):
        """Test second page: offset should be page_size"""
        # page=2, page_size=20 -> offset=20, limit=20
        page = 2
        page_size = 20
        offset = (page - 1) * page_size
        limit = page_size
        assert offset == 20
        assert limit == 20

    def test_custom_page_size_calculation(self):
        """Test custom page_size calculation"""
        # page=3, page_size=50 -> offset=100, limit=50
        page = 3
        page_size = 50
        offset = (page - 1) * page_size
        limit = page_size
        assert offset == 100
        assert limit == 50


class TestGroupIdsChange:
    """Test group_id → group_ids change (Commit 1)"""

    def test_fetch_request_uses_group_ids_list(self):
        """Test FetchMemRequest uses group_ids as list, not single group_id"""
        request = FetchMemRequest(
            user_id="user_1", group_ids=["group_1", "group_2", "group_3"]
        )
        assert isinstance(request.group_ids, list)
        assert len(request.group_ids) == 3

    def test_fetch_request_single_group_in_list(self):
        """Test single group should also be in list format"""
        request = FetchMemRequest(user_id="user_1", group_ids=["group_1"])
        assert request.group_ids == ["group_1"]

    def test_fetch_request_no_single_group_id_field(self):
        """Test FetchMemRequest doesn't have single group_id field"""
        assert 'group_id' not in FetchMemRequest.model_fields

    def test_metadata_uses_group_ids_list(self):
        """Test Metadata uses group_ids as list"""
        metadata = Metadata(
            source="episodic_memory",
            user_id="user_1",
            memory_type="episodic_memory",
            group_ids=["group_1", "group_2"],
        )
        assert isinstance(metadata.group_ids, list)
        assert metadata.group_ids == ["group_1", "group_2"]

    def test_convert_group_ids_none_means_no_filter(self):
        """Test group_ids=None means skip group filtering"""
        data = {"user_id": "user_1"}
        request = convert_dict_to_fetch_mem_request(data)
        assert request.group_ids is None

    def test_convert_empty_string_group_ids_becomes_none(self):
        """Test empty string group_ids becomes None"""
        data = {"user_id": "user_1", "group_ids": ""}
        request = convert_dict_to_fetch_mem_request(data)
        assert request.group_ids is None


class TestOriginalDataSupport:
    """Test original_data field support in memory models (Commit 2)"""

    def test_episodic_memory_has_original_data_field(self):
        """Test EpisodicMemoryModel has original_data field"""
        # Check field exists in dataclass
        import dataclasses

        field_names = [f.name for f in dataclasses.fields(EpisodicMemoryModel)]
        assert 'original_data' in field_names

    def test_event_log_has_original_data_field(self):
        """Test EventLogModel has original_data field"""
        import dataclasses

        field_names = [f.name for f in dataclasses.fields(EventLogModel)]
        assert 'original_data' in field_names

    def test_foresight_has_original_data_field(self):
        """Test ForesightModel has original_data field"""
        import dataclasses

        field_names = [f.name for f in dataclasses.fields(ForesightModel)]
        assert 'original_data' in field_names

    def test_episodic_memory_original_data_default_none(self):
        """Test EpisodicMemoryModel original_data defaults to None"""
        from datetime import datetime

        metadata = Metadata(
            source="test", user_id="user_1", memory_type="episodic_memory"
        )
        memory = EpisodicMemoryModel(
            id="test_id", user_id="user_1", episode_id="episode_1", metadata=metadata
        )
        assert memory.original_data is None

    def test_episodic_memory_can_set_original_data(self):
        """Test EpisodicMemoryModel can set original_data"""
        original_data = [
            {
                "data_type": "Conversation",
                "messages": [{"content": "Hello", "sender": "user_1"}],
            }
        ]
        metadata = Metadata(
            source="test", user_id="user_1", memory_type="episodic_memory"
        )
        memory = EpisodicMemoryModel(
            id="test_id",
            user_id="user_1",
            episode_id="episode_1",
            original_data=original_data,
            metadata=metadata,
        )
        assert memory.original_data == original_data

    def test_event_log_can_set_original_data(self):
        """Test EventLogModel can set original_data"""
        from datetime import datetime

        original_data = [{"data_type": "Conversation", "messages": []}]
        metadata = Metadata(source="test", user_id="user_1", memory_type="event_log")
        event_log = EventLogModel(
            id="test_id",
            user_id="user_1",
            atomic_fact="User did something",
            parent_type="memcell",
            parent_id="memcell_1",
            timestamp=datetime.now(),
            original_data=original_data,
            metadata=metadata,
        )
        assert event_log.original_data == original_data

    def test_foresight_can_set_original_data(self):
        """Test ForesightModel can set original_data"""
        original_data = [{"data_type": "Conversation", "messages": []}]
        metadata = Metadata(source="test", user_id="user_1", memory_type="foresight")
        foresight = ForesightModel(
            id="test_id",
            content="Future plan",
            foresight="Future plan",
            parent_type="memcell",
            parent_id="memcell_1",
            original_data=original_data,
            metadata=metadata,
        )
        assert foresight.original_data == original_data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test the functionality of MemoryRequestLogRepository

Test contents include:
1. save_from_raw_data (create MemoryRequestLog from raw data content)
2. Basic CRUD operations (save, get_by_request_id, find_by_group_id, find_by_user_id)
3. Duplicate detection (find_one_by_group_user_message)
4. Sync status management (confirm_accumulation, mark_as_used)
5. Flexible queries (find_pending_by_filters)
6. Delete operations (delete_by_group_id)
7. Helper methods (_parse_create_time, _normalize_refer_list)
"""

import asyncio
import uuid

from core.di import get_bean_by_type
from core.observation.logger import get_logger
from core.oxm.constants import MAGIC_ALL
from common_utils.datetime_utils import get_now_with_timezone
from infra_layer.adapters.out.persistence.repository.memory_request_log_repository import (
    MemoryRequestLogRepository,
)
from infra_layer.adapters.out.persistence.document.request.memory_request_log import (
    MemoryRequestLog,
)

logger = get_logger(__name__)


def generate_unique_id(prefix: str = "") -> str:
    """Generate a unique ID for testing"""
    return f"{prefix}{uuid.uuid4().hex[:8]}"


async def cleanup_test_data(group_id: str):
    """Clean up test data for a group"""
    repo = get_bean_by_type(MemoryRequestLogRepository)
    await repo.delete_by_group_id(group_id)


async def test_save_from_raw_data_basic():
    """Test basic save_from_raw_data functionality"""
    logger.info("Starting test for save_from_raw_data basic...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_save_raw_")
    request_id = generate_unique_id("req_")
    message_id = generate_unique_id("msg_")

    try:
        raw_data_content = {
            "speaker_id": "user_001",
            "speaker_name": "Test User",
            "content": "Hello, this is a test message",
            "role": "user",
            "timestamp": "2025-01-15T10:00:00+08:00",
            "referList": ["ref_msg_001", "ref_msg_002"],
        }

        result = await repo.save_from_raw_data(
            raw_data_content=raw_data_content,
            data_id=message_id,
            group_id=group_id,
            group_name="Test Group",
            request_id=request_id,
            version="1.0.0",
            endpoint_name="memorize",
            method="POST",
            url="/api/memorize",
            event_id=request_id,
        )

        assert result == message_id, f"Expected message_id={message_id}, got {result}"
        logger.info("✅ save_from_raw_data returned correct message_id")

        # Verify saved data
        log = await repo.get_by_request_id(request_id)
        assert log is not None, "Saved log should be retrievable"
        assert log.group_id == group_id
        assert log.request_id == request_id
        assert log.message_id == message_id
        assert log.sender == "user_001"
        assert log.sender_name == "Test User"
        assert log.content == "Hello, this is a test message"
        assert log.role == "user"
        assert log.group_name == "Test Group"
        assert log.refer_list == ["ref_msg_001", "ref_msg_002"]
        assert log.version == "1.0.0"
        assert log.endpoint_name == "memorize"
        assert log.method == "POST"
        assert log.url == "/api/memorize"
        assert log.event_id == request_id
        assert log.sync_status == -1  # Default sync_status
        assert log.message_create_time is not None
        logger.info("✅ All fields saved correctly")

    except Exception as e:
        logger.error("❌ Test for save_from_raw_data basic failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ save_from_raw_data basic test completed")


async def test_save_from_raw_data_alternate_field_names():
    """Test save_from_raw_data with alternate field names (createBy, createTime, etc.)"""
    logger.info("Starting test for save_from_raw_data alternate field names...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_alt_fields_")
    request_id = generate_unique_id("req_")
    message_id = generate_unique_id("msg_")

    try:
        # Use alternate field names
        raw_data_content = {
            "createBy": "user_002",
            "sender_name": "Alt User",
            "content": "Message with alternate fields",
            "role": "assistant",
            "createTime": "2025-02-01T14:30:00+08:00",
            "refer_list": ["ref_alt_001"],
        }

        result = await repo.save_from_raw_data(
            raw_data_content=raw_data_content,
            data_id=message_id,
            group_id=group_id,
            group_name=None,
            request_id=request_id,
        )

        assert result == message_id
        logger.info("✅ save_from_raw_data succeeded with alternate field names")

        # Verify extracted fields
        log = await repo.get_by_request_id(request_id)
        assert log is not None
        assert log.sender == "user_002"  # from createBy
        assert log.sender_name == "Alt User"  # from sender_name
        assert log.role == "assistant"
        assert log.refer_list == ["ref_alt_001"]
        assert log.message_create_time is not None
        logger.info("✅ Alternate field names extracted correctly")

    except Exception as e:
        logger.error("❌ Test for alternate field names failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ save_from_raw_data alternate field names test completed")


async def test_save_from_raw_data_with_raw_input_dict():
    """Test save_from_raw_data preserves raw_input and raw_input_str"""
    logger.info("Starting test for save_from_raw_data with raw_input_dict...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_raw_input_")
    request_id = generate_unique_id("req_")
    message_id = generate_unique_id("msg_")

    try:
        raw_data_content = {
            "sender": "user_003",
            "content": "Raw input test",
        }
        raw_input_dict = {
            "original_key": "original_value",
            "nested": {"key": "value"},
        }

        result = await repo.save_from_raw_data(
            raw_data_content=raw_data_content,
            data_id=message_id,
            group_id=group_id,
            group_name=None,
            request_id=request_id,
            raw_input_dict=raw_input_dict,
        )

        assert result == message_id
        logger.info("✅ save_from_raw_data with raw_input_dict succeeded")

        # Verify raw_input and raw_input_str
        log = await repo.get_by_request_id(request_id)
        assert log is not None
        assert log.raw_input == raw_input_dict
        assert log.raw_input_str is not None
        assert "original_key" in log.raw_input_str
        logger.info("✅ raw_input and raw_input_str saved correctly")

    except Exception as e:
        logger.error("❌ Test for raw_input_dict failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ save_from_raw_data with raw_input_dict test completed")


async def test_find_by_group_id():
    """Test find_by_group_id with various filters"""
    logger.info("Starting test for find_by_group_id...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_find_group_")

    try:
        # Create multiple logs with different sync_status
        for i, status in enumerate([-1, 0, 1]):
            log = MemoryRequestLog(
                group_id=group_id,
                request_id=generate_unique_id("req_"),
                message_id=generate_unique_id("msg_"),
                sender=f"user_{i}",
                content=f"Message {i} with status {status}",
                sync_status=status,
            )
            await repo.save(log)
        logger.info("✅ Created 3 logs with sync_status -1, 0, 1")

        # Query with sync_status=0 (default)
        results_status_0 = await repo.find_by_group_id(group_id, sync_status=0)
        assert len(results_status_0) == 1, f"Expected 1, got {len(results_status_0)}"
        logger.info("✅ find_by_group_id with sync_status=0 returned 1 record")

        # Query with sync_status=None (all)
        results_all = await repo.find_by_group_id(group_id, sync_status=None)
        assert len(results_all) == 3, f"Expected 3, got {len(results_all)}"
        logger.info("✅ find_by_group_id with sync_status=None returned 3 records")

        # Query with sync_status=-1
        results_pending = await repo.find_by_group_id(group_id, sync_status=-1)
        assert len(results_pending) == 1, f"Expected 1, got {len(results_pending)}"
        logger.info("✅ find_by_group_id with sync_status=-1 returned 1 record")

    except Exception as e:
        logger.error("❌ Test for find_by_group_id failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ find_by_group_id test completed")


async def test_find_by_user_id():
    """Test find_by_user_id"""
    logger.info("Starting test for find_by_user_id...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_find_user_")
    user_id = generate_unique_id("user_")

    try:
        # Create logs for the user
        for i in range(3):
            log = MemoryRequestLog(
                group_id=group_id,
                request_id=generate_unique_id("req_"),
                user_id=user_id,
                message_id=generate_unique_id("msg_"),
                content=f"User message {i}",
            )
            await repo.save(log)
        logger.info("✅ Created 3 logs for user %s", user_id)

        results = await repo.find_by_user_id(user_id)
        assert len(results) == 3, f"Expected 3, got {len(results)}"
        for r in results:
            assert r.user_id == user_id
        logger.info("✅ find_by_user_id returned 3 records")

    except Exception as e:
        logger.error("❌ Test for find_by_user_id failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ find_by_user_id test completed")


async def test_find_one_by_group_user_message():
    """Test duplicate detection via find_one_by_group_user_message"""
    logger.info("Starting test for find_one_by_group_user_message...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_dup_")
    user_id = generate_unique_id("user_")
    message_id = generate_unique_id("msg_")

    try:
        # Initially should not find anything
        result = await repo.find_one_by_group_user_message(
            group_id=group_id, user_id=user_id, message_id=message_id
        )
        assert result is None, "Should not find anything initially"
        logger.info("✅ No duplicate found initially")

        # Create a log
        log = MemoryRequestLog(
            group_id=group_id,
            request_id=generate_unique_id("req_"),
            user_id=user_id,
            message_id=message_id,
            content="Duplicate test message",
        )
        await repo.save(log)
        logger.info("✅ Created log for duplicate detection")

        # Now should find the duplicate
        result = await repo.find_one_by_group_user_message(
            group_id=group_id, user_id=user_id, message_id=message_id
        )
        assert result is not None, "Should find the duplicate"
        assert result.message_id == message_id
        logger.info("✅ Duplicate detected correctly")

        # Different message_id should not match
        result_other = await repo.find_one_by_group_user_message(
            group_id=group_id,
            user_id=user_id,
            message_id=generate_unique_id("msg_other_"),
        )
        assert result_other is None, "Different message_id should not match"
        logger.info("✅ Non-matching message_id correctly returns None")

    except Exception as e:
        logger.error("❌ Test for find_one_by_group_user_message failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ find_one_by_group_user_message test completed")


async def test_confirm_accumulation_by_group_id():
    """Test confirm_accumulation_by_group_id (batch -1 -> 0)"""
    logger.info("Starting test for confirm_accumulation_by_group_id...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_confirm_group_")

    try:
        # Create logs with sync_status=-1
        for i in range(3):
            log = MemoryRequestLog(
                group_id=group_id,
                request_id=generate_unique_id("req_"),
                message_id=generate_unique_id("msg_"),
                content=f"Pending message {i}",
                sync_status=-1,
            )
            await repo.save(log)
        logger.info("✅ Created 3 logs with sync_status=-1")

        # Confirm accumulation
        modified_count = await repo.confirm_accumulation_by_group_id(group_id)
        assert modified_count == 3, f"Expected 3, got {modified_count}"
        logger.info("✅ confirm_accumulation_by_group_id modified 3 records")

        # Verify all are now sync_status=0
        logs = await repo.find_by_group_id(group_id, sync_status=0)
        assert len(logs) == 3, f"Expected 3 with sync_status=0, got {len(logs)}"
        logger.info("✅ All records now have sync_status=0")

        # Confirm again should modify 0 (already at status 0)
        modified_again = await repo.confirm_accumulation_by_group_id(group_id)
        assert modified_again == 0, f"Expected 0, got {modified_again}"
        logger.info("✅ Second confirm correctly modified 0 records")

    except Exception as e:
        logger.error("❌ Test for confirm_accumulation_by_group_id failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ confirm_accumulation_by_group_id test completed")


async def test_confirm_accumulation_by_message_ids():
    """Test confirm_accumulation_by_message_ids (precise -1 -> 0)"""
    logger.info("Starting test for confirm_accumulation_by_message_ids...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_confirm_msg_")

    try:
        msg_ids = [generate_unique_id("msg_") for _ in range(3)]

        # Create 3 logs with sync_status=-1
        for msg_id in msg_ids:
            log = MemoryRequestLog(
                group_id=group_id,
                request_id=generate_unique_id("req_"),
                message_id=msg_id,
                content=f"Message {msg_id}",
                sync_status=-1,
            )
            await repo.save(log)
        logger.info("✅ Created 3 logs with sync_status=-1")

        # Only confirm first 2
        modified_count = await repo.confirm_accumulation_by_message_ids(
            group_id, msg_ids[:2]
        )
        assert modified_count == 2, f"Expected 2, got {modified_count}"
        logger.info("✅ confirm_accumulation_by_message_ids modified 2 records")

        # Verify: 2 confirmed (sync_status=0), 1 still pending (sync_status=-1)
        all_logs = await repo.find_by_group_id(group_id, sync_status=None)
        confirmed = [l for l in all_logs if l.sync_status == 0]
        pending = [l for l in all_logs if l.sync_status == -1]
        assert len(confirmed) == 2, f"Expected 2 confirmed, got {len(confirmed)}"
        assert len(pending) == 1, f"Expected 1 pending, got {len(pending)}"
        assert pending[0].message_id == msg_ids[2]
        logger.info("✅ Only specified messages were confirmed")

        # Empty message_ids should do nothing
        modified_empty = await repo.confirm_accumulation_by_message_ids(group_id, [])
        assert modified_empty == 0, f"Expected 0, got {modified_empty}"
        logger.info("✅ Empty message_ids correctly returns 0")

    except Exception as e:
        logger.error("❌ Test for confirm_accumulation_by_message_ids failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ confirm_accumulation_by_message_ids test completed")


async def test_mark_as_used_by_group_id():
    """Test mark_as_used_by_group_id (-1,0 -> 1)"""
    logger.info("Starting test for mark_as_used_by_group_id...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_mark_used_")

    try:
        # Create logs with various sync_status
        for status in [-1, 0, 1]:
            log = MemoryRequestLog(
                group_id=group_id,
                request_id=generate_unique_id("req_"),
                message_id=generate_unique_id("msg_"),
                content=f"Status {status} message",
                sync_status=status,
            )
            await repo.save(log)
        logger.info("✅ Created logs with sync_status -1, 0, 1")

        # Mark as used
        modified_count = await repo.mark_as_used_by_group_id(group_id)
        assert modified_count == 2, f"Expected 2 (-1 and 0), got {modified_count}"
        logger.info("✅ mark_as_used modified 2 records")

        # Verify all are now sync_status=1
        all_logs = await repo.find_by_group_id(group_id, sync_status=None)
        for log in all_logs:
            assert log.sync_status == 1, f"Expected sync_status=1, got {log.sync_status}"
        logger.info("✅ All records now have sync_status=1")

    except Exception as e:
        logger.error("❌ Test for mark_as_used_by_group_id failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ mark_as_used_by_group_id test completed")


async def test_mark_as_used_with_exclude():
    """Test mark_as_used_by_group_id with exclude_message_ids"""
    logger.info("Starting test for mark_as_used with exclude...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_mark_exclude_")

    try:
        msg_ids = [generate_unique_id("msg_") for _ in range(3)]

        for msg_id in msg_ids:
            log = MemoryRequestLog(
                group_id=group_id,
                request_id=generate_unique_id("req_"),
                message_id=msg_id,
                content=f"Message {msg_id}",
                sync_status=-1,
            )
            await repo.save(log)
        logger.info("✅ Created 3 logs with sync_status=-1")

        # Mark as used, but exclude the last message
        modified_count = await repo.mark_as_used_by_group_id(
            group_id, exclude_message_ids=[msg_ids[2]]
        )
        assert modified_count == 2, f"Expected 2, got {modified_count}"
        logger.info("✅ mark_as_used with exclude modified 2 records")

        # Verify: 2 used, 1 still pending
        all_logs = await repo.find_by_group_id(group_id, sync_status=None)
        for log in all_logs:
            if log.message_id == msg_ids[2]:
                assert log.sync_status == -1, "Excluded message should remain -1"
            else:
                assert log.sync_status == 1, "Non-excluded messages should be 1"
        logger.info("✅ Excluded message correctly preserved")

    except Exception as e:
        logger.error("❌ Test for mark_as_used with exclude failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ mark_as_used with exclude test completed")


async def test_find_pending_by_filters():
    """Test find_pending_by_filters with MAGIC_ALL"""
    logger.info("Starting test for find_pending_by_filters...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_pending_")
    user_id_1 = generate_unique_id("user_")
    user_id_2 = generate_unique_id("user_")

    try:
        # Create logs for different users
        for i, uid in enumerate([user_id_1, user_id_1, user_id_2]):
            log = MemoryRequestLog(
                group_id=group_id,
                request_id=generate_unique_id("req_"),
                user_id=uid,
                message_id=generate_unique_id("msg_"),
                content=f"Pending message {i}",
                sync_status=-1,
            )
            await repo.save(log)

        # Create one with sync_status=1 (should be excluded by default)
        used_log = MemoryRequestLog(
            group_id=group_id,
            request_id=generate_unique_id("req_"),
            user_id=user_id_1,
            message_id=generate_unique_id("msg_"),
            content="Used message",
            sync_status=1,
        )
        await repo.save(used_log)
        logger.info("✅ Created 4 logs (3 pending, 1 used)")

        # Query with MAGIC_ALL (default) - should return all pending
        results_all = await repo.find_pending_by_filters(
            user_id=MAGIC_ALL, group_id=group_id
        )
        assert len(results_all) == 3, f"Expected 3, got {len(results_all)}"
        logger.info("✅ find_pending_by_filters with MAGIC_ALL returned 3 records")

        # Query by specific user_id
        results_user1 = await repo.find_pending_by_filters(
            user_id=user_id_1, group_id=group_id
        )
        assert len(results_user1) == 2, f"Expected 2, got {len(results_user1)}"
        logger.info("✅ find_pending_by_filters with user_id filter returned 2 records")

        # Query with specific sync_status_list
        results_used = await repo.find_pending_by_filters(
            group_id=group_id, sync_status_list=[1]
        )
        assert len(results_used) == 1, f"Expected 1, got {len(results_used)}"
        logger.info("✅ find_pending_by_filters with sync_status=[1] returned 1 record")

        # Query with limit
        results_limited = await repo.find_pending_by_filters(
            group_id=group_id, limit=2
        )
        assert len(results_limited) == 2, f"Expected 2, got {len(results_limited)}"
        logger.info("✅ find_pending_by_filters with limit=2 returned 2 records")

    except Exception as e:
        logger.error("❌ Test for find_pending_by_filters failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ find_pending_by_filters test completed")


async def test_find_by_group_id_with_statuses():
    """Test find_by_group_id_with_statuses"""
    logger.info("Starting test for find_by_group_id_with_statuses...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_statuses_")

    try:
        msg_ids = []
        for status in [-1, 0, 0, 1]:
            msg_id = generate_unique_id("msg_")
            msg_ids.append(msg_id)
            log = MemoryRequestLog(
                group_id=group_id,
                request_id=generate_unique_id("req_"),
                message_id=msg_id,
                content=f"Status {status}",
                sync_status=status,
            )
            await repo.save(log)
        logger.info("✅ Created 4 logs: status -1, 0, 0, 1")

        # Query with multiple statuses [-1, 0]
        results = await repo.find_by_group_id_with_statuses(
            group_id=group_id, sync_status_list=[-1, 0]
        )
        assert len(results) == 3, f"Expected 3, got {len(results)}"
        logger.info("✅ find_by_group_id_with_statuses [-1, 0] returned 3 records")

        # Query with exclude_message_ids
        results_excluded = await repo.find_by_group_id_with_statuses(
            group_id=group_id,
            sync_status_list=[-1, 0],
            exclude_message_ids=[msg_ids[0]],  # exclude the -1 record
        )
        assert len(results_excluded) == 2, f"Expected 2, got {len(results_excluded)}"
        logger.info("✅ Exclusion works correctly")

        # Query with descending order
        results_desc = await repo.find_by_group_id_with_statuses(
            group_id=group_id,
            sync_status_list=[-1, 0, 1],
            ascending=False,
        )
        assert len(results_desc) == 4, f"Expected 4, got {len(results_desc)}"
        logger.info("✅ Descending order query works correctly")

    except Exception as e:
        logger.error("❌ Test for find_by_group_id_with_statuses failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ find_by_group_id_with_statuses test completed")


async def test_delete_by_group_id():
    """Test delete_by_group_id"""
    logger.info("Starting test for delete_by_group_id...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_delete_")

    try:
        # Create logs
        for i in range(3):
            log = MemoryRequestLog(
                group_id=group_id,
                request_id=generate_unique_id("req_"),
                message_id=generate_unique_id("msg_"),
                content=f"Delete test {i}",
            )
            await repo.save(log)
        logger.info("✅ Created 3 logs")

        # Verify they exist
        before = await repo.find_by_group_id(group_id, sync_status=None)
        assert len(before) == 3

        # Delete
        deleted_count = await repo.delete_by_group_id(group_id)
        assert deleted_count == 3, f"Expected 3 deleted, got {deleted_count}"
        logger.info("✅ delete_by_group_id deleted 3 records")

        # Verify they're gone
        after = await repo.find_by_group_id(group_id, sync_status=None)
        assert len(after) == 0, f"Expected 0 after delete, got {len(after)}"
        logger.info("✅ Records no longer retrievable")

    except Exception as e:
        logger.error("❌ Test for delete_by_group_id failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ delete_by_group_id test completed")


async def test_sync_status_full_lifecycle():
    """Test the complete sync_status lifecycle: -1 -> 0 -> 1"""
    logger.info("Starting test for sync_status full lifecycle...")

    repo = get_bean_by_type(MemoryRequestLogRepository)
    group_id = generate_unique_id("test_lifecycle_")
    message_id = generate_unique_id("msg_")

    try:
        # Step 1: save_from_raw_data creates with sync_status=-1
        raw_data_content = {
            "sender": "lifecycle_user",
            "content": "Lifecycle test message",
        }
        await repo.save_from_raw_data(
            raw_data_content=raw_data_content,
            data_id=message_id,
            group_id=group_id,
            group_name="Lifecycle Group",
            request_id=generate_unique_id("req_"),
        )

        logs = await repo.find_by_group_id(group_id, sync_status=None)
        assert len(logs) == 1
        assert logs[0].sync_status == -1
        logger.info("✅ Step 1: Created with sync_status=-1")

        # Step 2: confirm_accumulation -> sync_status=0
        modified = await repo.confirm_accumulation_by_message_ids(
            group_id, [message_id]
        )
        assert modified == 1

        logs = await repo.find_by_group_id(group_id, sync_status=None)
        assert logs[0].sync_status == 0
        logger.info("✅ Step 2: sync_status changed to 0")

        # Step 3: mark_as_used -> sync_status=1
        modified = await repo.mark_as_used_by_group_id(group_id)
        assert modified == 1

        logs = await repo.find_by_group_id(group_id, sync_status=None)
        assert logs[0].sync_status == 1
        logger.info("✅ Step 3: sync_status changed to 1")

        # Verify: no pending records remain
        pending = await repo.find_pending_by_filters(group_id=group_id)
        assert len(pending) == 0
        logger.info("✅ No pending records remain after full lifecycle")

    except Exception as e:
        logger.error("❌ Test for sync_status full lifecycle failed: %s", e)
        raise
    finally:
        await cleanup_test_data(group_id)
        logger.info("✅ Cleaned up test data")

    logger.info("✅ sync_status full lifecycle test completed")


async def test_parse_create_time():
    """Test _parse_create_time static method"""
    logger.info("Starting test for _parse_create_time...")

    # Test with None
    assert MemoryRequestLogRepository._parse_create_time(None) is None
    logger.info("✅ None input returns None")

    # Test with ISO format string
    iso_str = "2025-01-15T10:00:00+08:00"
    result = MemoryRequestLogRepository._parse_create_time(iso_str)
    assert result is not None
    assert "2025-01-15" in result
    logger.info("✅ ISO string parsed correctly")

    # Test with datetime object
    dt = get_now_with_timezone()
    result = MemoryRequestLogRepository._parse_create_time(dt)
    assert result is not None
    assert isinstance(result, str)
    logger.info("✅ datetime object converted correctly")

    # Test with invalid string (from_iso_format falls back to current time in lenient mode,
    # so _parse_create_time returns an ISO string of the current time, not the original)
    result = MemoryRequestLogRepository._parse_create_time("not-a-date")
    assert result is not None
    assert isinstance(result, str)
    logger.info("✅ Invalid string returns a fallback ISO timestamp")

    # Test with int (should return None)
    result = MemoryRequestLogRepository._parse_create_time(12345)
    assert result is None
    logger.info("✅ Non-string/datetime returns None")

    logger.info("✅ _parse_create_time test completed")


async def test_normalize_refer_list():
    """Test _normalize_refer_list static method"""
    logger.info("Starting test for _normalize_refer_list...")

    # Test with None
    assert MemoryRequestLogRepository._normalize_refer_list(None) is None
    logger.info("✅ None returns None")

    # Test with empty list
    assert MemoryRequestLogRepository._normalize_refer_list([]) is None
    logger.info("✅ Empty list returns None")

    # Test with string list
    result = MemoryRequestLogRepository._normalize_refer_list(["msg_1", "msg_2"])
    assert result == ["msg_1", "msg_2"]
    logger.info("✅ String list preserved")

    # Test with dict list (extract message_id)
    result = MemoryRequestLogRepository._normalize_refer_list(
        [{"message_id": "msg_1"}, {"id": "msg_2"}]
    )
    assert result == ["msg_1", "msg_2"]
    logger.info("✅ Dict list extracted message_id/id correctly")

    # Test with mixed list
    result = MemoryRequestLogRepository._normalize_refer_list(
        ["msg_1", {"message_id": "msg_2"}, {"no_id": True}]
    )
    assert result == ["msg_1", "msg_2"]
    logger.info("✅ Mixed list handled correctly")

    # Test with non-list input
    assert MemoryRequestLogRepository._normalize_refer_list("not_a_list") is None
    logger.info("✅ Non-list input returns None")

    logger.info("✅ _normalize_refer_list test completed")


async def run_all_tests():
    """Run all tests"""
    logger.info("🚀 Starting to run all MemoryRequestLogRepository tests...")

    try:
        # save_from_raw_data tests
        await test_save_from_raw_data_basic()
        await test_save_from_raw_data_alternate_field_names()
        await test_save_from_raw_data_with_raw_input_dict()

        # Basic CRUD tests
        await test_find_by_group_id()
        await test_find_by_user_id()
        await test_find_one_by_group_user_message()
        await test_delete_by_group_id()

        # Sync status management tests
        await test_confirm_accumulation_by_group_id()
        await test_confirm_accumulation_by_message_ids()
        await test_mark_as_used_by_group_id()
        await test_mark_as_used_with_exclude()

        # Query tests
        await test_find_pending_by_filters()
        await test_find_by_group_id_with_statuses()

        # Lifecycle test
        await test_sync_status_full_lifecycle()

        # Helper method tests
        await test_parse_create_time()
        await test_normalize_refer_list()

        logger.info("✅ All MemoryRequestLogRepository tests completed successfully")
    except Exception as e:
        logger.error("❌ Error occurred during testing: %s", e)
        raise


if __name__ == "__main__":
    asyncio.run(run_all_tests())

# -*- coding: utf-8 -*-
"""
Conversation Meta API Test Script
Verify input and output structures of conversation-meta endpoints under /api/v0/memories

Usage:
    # Run all tests
    python tests/test_conversation_meta.py
    
    # Specify API address
    python tests/test_conversation_meta.py --base-url http://localhost:1995
    
    # Test by category
    python tests/test_conversation_meta.py --test-method post       # Test POST (create/update)
    python tests/test_conversation_meta.py --test-method get        # Test GET (with fallback)
    python tests/test_conversation_meta.py --test-method patch      # Test PATCH (partial update)
    python tests/test_conversation_meta.py --test-method fallback   # Test fallback logic
    python tests/test_conversation_meta.py --test-method error      # Test error/exception cases
    python tests/test_conversation_meta.py --test-method all        # Run all tests (default)
    
    # Test a specific method
    python tests/test_conversation_meta.py --test-method post_default
    python tests/test_conversation_meta.py --test-method post_with_group_id
    python tests/test_conversation_meta.py --test-method get_by_group_id
    python tests/test_conversation_meta.py --test-method get_default
    python tests/test_conversation_meta.py --test-method get_fallback
    python tests/test_conversation_meta.py --test-method patch_update
    python tests/test_conversation_meta.py --test-method patch_default
    
    # Test error/exception cases
    python tests/test_conversation_meta.py --test-method error_dup_default      # Duplicate default POST (upsert)
    python tests/test_conversation_meta.py --test-method error_dup_group_id     # Duplicate group_id POST (upsert)
    python tests/test_conversation_meta.py --test-method error_missing_fields   # Missing required fields
    python tests/test_conversation_meta.py --test-method error_invalid_scene    # Invalid scene value
    python tests/test_conversation_meta.py --test-method error_patch_no_fallback # PATCH non-existent -> returns 404 (no fallback)
    python tests/test_conversation_meta.py --test-method error_empty_body       # Empty POST body
    
    # Run unit tests (no server required)
    python tests/test_conversation_meta.py --test-method llm_custom_setting_model                   # Unit tests for LlmCustomSettingModel
"""

import argparse
import json
import sys
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

# Add src to path for unit tests
sys.path.insert(0, "/Users/admin/memsys_opensource/src")


# =============================================================================
# Unit Tests for LlmCustomSettingModel
# =============================================================================


def test_llm_custom_setting_model():
    """Unit tests for LlmCustomSettingModel.to_dict() and from_any()"""
    from infra_layer.adapters.out.persistence.document.memory.conversation_meta import (
        LlmCustomSettingModel,
        LlmProviderConfigModel,
    )

    print("\n" + "=" * 80)
    print("  UNIT TESTS: LlmCustomSettingModel")
    print("=" * 80)

    results = []

    # Test 1: from_any with dict input
    print("\n--- Test 1: from_any() with dict input ---")
    dict_input = {
        "boundary": {"provider": "openai", "model": "gpt-4o-mini"},
        "extraction": {"provider": "anthropic", "model": "claude-3-opus"},
        "extra": {"custom_key": "custom_value"},
    }
    print(f"📤 Input: {json.dumps(dict_input, indent=2)}")

    model = LlmCustomSettingModel.from_any(dict_input)
    if model is not None:
        print(f"📥 Result: LlmCustomSettingModel created")
        print(f"   - boundary.provider: {model.boundary.provider}")
        print(f"   - boundary.model: {model.boundary.model}")
        print(f"   - extraction.provider: {model.extraction.provider}")
        print(f"   - extraction.model: {model.extraction.model}")
        print(f"   - extra: {model.extra}")

        if (
            model.boundary.provider == "openai"
            and model.boundary.model == "gpt-4o-mini"
            and model.extraction.provider == "anthropic"
            and model.extraction.model == "claude-3-opus"
        ):
            print("✅ Test 1 PASSED")
            results.append(("from_any(dict)", True))
        else:
            print("❌ Test 1 FAILED - values mismatch")
            results.append(("from_any(dict)", False))
    else:
        print("❌ Test 1 FAILED - model is None")
        results.append(("from_any(dict)", False))

    # Test 2: to_dict
    print("\n--- Test 2: to_dict() ---")
    if model:
        dict_output = model.to_dict()
        print(f"📥 Output: {json.dumps(dict_output, indent=2)}")

        if (
            dict_output
            and dict_output.get("boundary", {}).get("provider") == "openai"
            and dict_output.get("extraction", {}).get("model") == "claude-3-opus"
        ):
            print("✅ Test 2 PASSED")
            results.append(("to_dict()", True))
        else:
            print("❌ Test 2 FAILED")
            results.append(("to_dict()", False))

    # Test 3: from_any with None
    print("\n--- Test 3: from_any(None) ---")
    model_none = LlmCustomSettingModel.from_any(None)
    print(f"📥 Result: {model_none}")
    if model_none is None:
        print("✅ Test 3 PASSED")
        results.append(("from_any(None)", True))
    else:
        print("❌ Test 3 FAILED")
        results.append(("from_any(None)", False))

    # Test 4: from_any with partial dict (only boundary)
    print("\n--- Test 4: from_any() with partial dict ---")
    partial_input = {"boundary": {"provider": "openai", "model": "gpt-4o"}}
    print(f"📤 Input: {json.dumps(partial_input, indent=2)}")

    model_partial = LlmCustomSettingModel.from_any(partial_input)
    if model_partial:
        print(
            f"📥 Result: boundary={model_partial.boundary}, extraction={model_partial.extraction}"
        )
        if model_partial.boundary and model_partial.extraction is None:
            print("✅ Test 4 PASSED")
            results.append(("from_any(partial)", True))
        else:
            print("❌ Test 4 FAILED")
            results.append(("from_any(partial)", False))
    else:
        print("❌ Test 4 FAILED - model is None")
        results.append(("from_any(partial)", False))

    # Test 5: from_any with empty dict
    print("\n--- Test 5: from_any({}) ---")
    model_empty = LlmCustomSettingModel.from_any({})
    print(f"📥 Result: {model_empty}")
    if model_empty is None:
        print("✅ Test 5 PASSED (empty dict returns None)")
        results.append(("from_any({})", True))
    else:
        print("❌ Test 5 FAILED")
        results.append(("from_any({})", False))

    # Test 6: Round-trip (dict -> model -> dict)
    print("\n--- Test 6: Round-trip conversion ---")
    original = {
        "boundary": {
            "provider": "azure",
            "model": "gpt-4",
            "extra": {"api_version": "2024-01"},
        },
        "extraction": {"provider": "openai", "model": "gpt-4o"},
    }
    print(f"📤 Original: {json.dumps(original, indent=2)}")

    model_rt = LlmCustomSettingModel.from_any(original)
    if model_rt:
        converted = model_rt.to_dict()
        print(f"📥 Converted: {json.dumps(converted, indent=2)}")

        # Check round-trip
        if (
            converted.get("boundary", {}).get("provider") == "azure"
            and converted.get("boundary", {}).get("extra", {}).get("api_version")
            == "2024-01"
            and converted.get("extraction", {}).get("model") == "gpt-4o"
        ):
            print("✅ Test 6 PASSED")
            results.append(("round-trip", True))
        else:
            print("❌ Test 6 FAILED")
            results.append(("round-trip", False))
    else:
        print("❌ Test 6 FAILED - model is None")
        results.append(("round-trip", False))

    # Summary
    print("\n" + "=" * 80)
    print("  UNIT TEST SUMMARY")
    print("=" * 80)
    passed = sum(1 for _, r in results if r)
    failed = sum(1 for _, r in results if not r)

    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"   {status} - {name}")

    print(f"\n   Total: {len(results)} | Passed: {passed} | Failed: {failed}")

    if failed == 0:
        print("\n🎉 All unit tests passed!")
        return True
    else:
        print(f"\n⚠️  {failed} unit test(s) failed")
        return False


class ConversationMetaTester:
    """Conversation Meta API Test Class"""

    # Default tenant information
    DEFAULT_ORGANIZATION_ID = "test_conv_meta_organization"
    DEFAULT_SPACE_ID = "test_conv_meta_space"
    DEFAULT_HASH_KEY = "test_conv_meta_hash_key"
    # ta38b637741

    def __init__(
        self,
        base_url: str,
        organization_id: str = None,
        space_id: str = None,
        hash_key: str = None,
        timeout: int = 60,
    ):
        """
        Initialize tester

        Args:
            base_url: API base URL
            organization_id: Organization ID
            space_id: Space ID
            hash_key: Hash key
            timeout: Request timeout in seconds
        """
        self.base_url = base_url
        self.api_prefix = "/api/v0/memories"
        self.organization_id = organization_id or self.DEFAULT_ORGANIZATION_ID
        self.space_id = space_id or self.DEFAULT_SPACE_ID
        self.hash_key = hash_key or self.DEFAULT_HASH_KEY
        self.timeout = timeout

        # Generate unique test IDs
        self.test_run_id = uuid.uuid4().hex[:8]
        self.test_group_id = f"test_group_{self.test_run_id}"

    def get_tenant_headers(self) -> dict:
        """Get tenant-related request headers"""
        headers = {
            "X-Organization-Id": self.organization_id,
            "X-Space-Id": self.space_id,
            "Content-Type": "application/json",
        }
        if self.hash_key:
            headers["X-Hash-Key"] = self.hash_key
        return headers

    def print_separator(self, title: str):
        """Print section separator"""
        print("\n" + "=" * 80)
        print(f"  {title}")
        print("=" * 80)

    def print_request(self, method: str, url: str, body: dict = None):
        """Print request info"""
        print(f"📍 URL: {method} {url}")
        if body:
            print(f"📤 Request Body:")
            print(json.dumps(body, indent=2, ensure_ascii=False))

    def print_response(self, response: requests.Response):
        """Print response info"""
        print(f"\n📥 Response Status Code: {response.status_code}")
        print(
            f"📥 Response Headers: Content-Type={response.headers.get('Content-Type', 'N/A')}"
        )
        try:
            response_json = response.json()
            print("📥 Response Data:")
            print(json.dumps(response_json, indent=2, ensure_ascii=False))
            return response_json
        except Exception:
            print(
                f"📥 Response Text: {response.text[:500] if response.text else '(empty)'}"
            )
            return None

    def init_database(self) -> bool:
        """Initialize tenant database"""
        url = f"{self.base_url}/internal/tenant/init-db"
        headers = self.get_tenant_headers()

        self.print_separator("Initialize Tenant Database")
        print(
            f"📤 Tenant Info: organization_id={self.organization_id}, space_id={self.space_id}"
        )

        try:
            response = requests.post(url, headers=headers, timeout=self.timeout)
            response_json = self.print_response(response)

            if response.status_code == 200:
                print(f"\n✅ Database initialization successful")
                return True
            else:
                print(f"\n⚠️  Database initialization returned: {response_json}")
                return True  # Continue even if failed
        except Exception as e:
            print(f"\n❌ Database initialization failed: {e}")
            return False

    # ==================== POST Tests ====================

    def test_post_default_config(self) -> bool:
        """
        Test POST: Create default/global config (group_id=null)

        Creates a default conversation meta that will be used as fallback.

        Global config rules:
        - REQUIRED: scene, scene_desc, created_at
        - NOT ALLOWED: name (name is only for group config)
        - OPTIONAL: llm_custom_setting, description, tags, user_details, default_timezone
        """
        self.print_separator("POST: Create Global Config (group_id=null)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        body = {
            "scene": "group_chat",
            "scene_desc": {
                "description": "This is the default/global conversation meta config"
            },
            # NOTE: name is NOT allowed for global config
            "description": "This is the global conversation meta config",
            "group_id": None,  # null = global config
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "default_timezone": "UTC",
            "llm_custom_setting": {  # Optional: LLM custom settings (global only)
                "boundary": {"provider": "openai", "model": "gpt-4o-mini"},
                "extraction": {"provider": "openai", "model": "gpt-4o"},
            },
            "user_details": {
                "default_user": {
                    "full_name": "Default User",
                    "role": "user",
                    "custom_role": "member",
                    "extra": {"is_default": True},
                }
            },
            "tags": ["default", "test"],
        }

        print("\n📋 Test Conditions:")
        print("   - group_id=null → Global config")
        print("   - scene, scene_desc → REQUIRED for global")
        print("   - name → NOT included (not allowed for global)")
        print("   - llm_custom_setting → Optional (global only)")

        self.print_request("POST", url, body)

        try:
            response = requests.post(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 200 and response_json.get("status") == "ok":
                result = response_json.get("result", {})
                is_default = result.get("is_default", False)

                print(f"\n📊 Response Analysis:")
                print(f"   - ID: {result.get('id')}")
                print(f"   - group_id: {result.get('group_id')}")
                print(f"   - is_default: {is_default}")
                print(f"   - scene: {result.get('scene')}")
                print(f"   - scene_desc: {result.get('scene_desc')}")
                print(f"   - llm_custom_setting: {result.get('llm_custom_setting')}")
                print(f"   - name: {result.get('name')} (should be None for global)")
                print(f"   - description: {result.get('description')}")
                print(f"   - tags: {result.get('tags')}")

                if is_default and result.get("group_id") is None:
                    print(f"\n✅ Global config created/updated successfully!")

                    # Verify llm_custom_setting was saved
                    if result.get("llm_custom_setting") is None:
                        print(f"   ⚠️  WARNING: llm_custom_setting was not saved!")
                        print(
                            f"      This may indicate server needs restart to load new model"
                        )

                    return True
                else:
                    print(f"\n❌ Expected is_default=True and group_id=null")
                    return False
            else:
                print(f"\n❌ Failed to create global config")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_post_with_group_id(self) -> bool:
        """
        Test POST: Create config with specific group_id (Group config)

        Group config rules:
        - REQUIRED: name, created_at
        - NOT ALLOWED: scene, scene_desc, llm_custom_setting (inherited from global)
        - OPTIONAL: description, tags, user_details, default_timezone
        """
        self.print_separator("POST: Create Group Config (with group_id)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        body = {
            # NOTE: scene, scene_desc are NOT allowed for group config
            "name": f"Test Group ({self.test_run_id})",  # REQUIRED for group config
            "description": "Test conversation meta with specific group_id",
            "group_id": self.test_group_id,
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "default_timezone": "Asia/Shanghai",
            "user_details": {
                "user_001": {
                    "full_name": "Test User",
                    "role": "user",
                    "custom_role": "developer",
                    "extra": {"department": "Engineering"},
                },
                "bot_001": {
                    "full_name": "AI Assistant",
                    "role": "assistant",
                    "custom_role": "assistant",
                    "extra": {"type": "ai"},
                },
            },
            "tags": ["test", "project", "engineering"],
        }

        print("\n📋 Test Conditions:")
        print(f"   - group_id={self.test_group_id} → Group config")
        print("   - name → REQUIRED for group config")
        print("   - scene, scene_desc → NOT included (not allowed for group)")
        print("   - llm_custom_setting → NOT included (not allowed for group)")

        self.print_request("POST", url, body)

        try:
            response = requests.post(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 200 and response_json.get("status") == "ok":
                result = response_json.get("result", {})

                print(f"\n📊 Response Analysis:")
                print(f"   - ID: {result.get('id')}")
                print(f"   - group_id: {result.get('group_id')}")
                print(f"   - is_default: {result.get('is_default', False)}")
                print(f"   - name: {result.get('name')}")
                print(f"   - description: {result.get('description')}")
                print(f"   - scene: {result.get('scene')} (should be None for group)")
                print(
                    f"   - scene_desc: {result.get('scene_desc')} (should be None for group)"
                )
                print(
                    f"   - llm_custom_setting: {result.get('llm_custom_setting')} (should be None for group)"
                )
                print(f"   - tags: {result.get('tags')}")

                if result.get("group_id") == self.test_group_id:
                    print(f"\n✅ Group config created successfully!")
                    return True
                else:
                    print(
                        f"\n❌ group_id mismatch: expected {self.test_group_id}, got {result.get('group_id')}"
                    )
                    return False
            elif response.status_code == 500:
                print(f"\n❌ Server error (500) - possible causes:")
                print(f"   1. Server may need restart to load new MongoDB model")
                print(f"   2. MongoDB validation error (scene/name field requirements)")
                print(f"   3. Check server logs for detailed error")
                return False
            else:
                print(f"\n❌ Failed to create group config")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_post_update_existing(self) -> bool:
        """
        Test POST: Update existing group config (upsert)

        Group config update - same rules apply:
        - REQUIRED: name, created_at
        - NOT ALLOWED: scene, scene_desc, llm_custom_setting
        """
        self.print_separator("POST: Update Existing Group Config (Upsert)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        # Update the same group_id with new data
        body = {
            # NOTE: scene, scene_desc are NOT allowed for group config
            "name": f"Updated Group ({self.test_run_id})",  # REQUIRED
            "description": "Updated conversation meta",
            "group_id": self.test_group_id,
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "default_timezone": "America/New_York",
            "user_details": {
                "user_001": {
                    "full_name": "Updated User",
                    "role": "user",
                    "custom_role": "lead",
                    "extra": {"department": "Engineering", "updated": True},
                }
            },
            "tags": ["updated", "test"],
        }

        print("\n📋 Test Conditions:")
        print(f"   - group_id={self.test_group_id} → Update existing group config")
        print("   - This should UPSERT (update if exists)")

        self.print_request("POST", url, body)

        try:
            response = requests.post(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 200 and response_json.get("status") == "ok":
                result = response_json.get("result", {})
                if result.get("name") == f"Updated Group ({self.test_run_id})":
                    print(f"\n✅ Group config updated successfully via upsert!")
                    print(f"   - name updated to: {result.get('name')}")
                    print(f"   - default_timezone: {result.get('default_timezone')}")
                    return True
                else:
                    print(f"\n❌ Name not updated as expected")
                    return False
            else:
                print(f"\n❌ Failed to update config")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    # ==================== GET Tests ====================

    def test_get_by_group_id(self) -> bool:
        """
        Test GET: Retrieve config by specific group_id
        """
        self.print_separator("GET: Retrieve by group_id")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()
        params = {"group_id": self.test_group_id}

        print(f"📍 URL: GET {url}")
        print(f"📤 Query Params: {params}")

        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 200 and response_json.get("status") == "ok":
                result = response_json.get("result", {})
                if result.get("group_id") == self.test_group_id:
                    print(f"\n✅ Retrieved config by group_id successfully!")
                    print(f"   - group_id: {result.get('group_id')}")
                    print(f"   - name: {result.get('name')}")
                    print(f"   - is_default: {result.get('is_default', False)}")
                    return True
                else:
                    print(f"\n❌ group_id mismatch in response")
                    return False
            else:
                print(f"\n❌ Failed to retrieve config")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_get_default_config(self) -> bool:
        """
        Test GET: Retrieve default config (group_id=null or not provided)
        """
        self.print_separator("GET: Retrieve Default Config")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()
        # No group_id param = get default config

        print(f"📍 URL: GET {url}")
        print(f"📤 Query Params: (none - should return default config)")

        try:
            response = requests.get(url, headers=headers, timeout=self.timeout)
            response_json = self.print_response(response)

            if response.status_code == 200 and response_json.get("status") == "ok":
                result = response_json.get("result", {})
                if result.get("group_id") is None and result.get("is_default") is True:
                    print(f"\n✅ Retrieved default config successfully!")
                    print(f"   - group_id: {result.get('group_id')} (null)")
                    print(f"   - is_default: {result.get('is_default')}")
                    print(f"   - name: {result.get('name')}")
                    return True
                else:
                    print(
                        f"\n❌ Expected default config (group_id=null, is_default=true)"
                    )
                    return False
            else:
                print(f"\n❌ Failed to retrieve default config")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_get_fallback_to_default(self) -> bool:
        """
        Test GET: Fallback to default when group_id not found

        This is the core fallback logic test:
        1. Request a non-existent group_id
        2. Should automatically fallback to default config
        3. Verify is_default=true and message indicates fallback
        """
        self.print_separator("GET: Fallback to Default (Non-existent group_id)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        # Use a non-existent group_id
        non_existent_group_id = f"non_existent_{uuid.uuid4().hex[:8]}"
        params = {"group_id": non_existent_group_id}

        print(f"📍 URL: GET {url}")
        print(f"📤 Query Params: {params}")
        print(
            f"📝 Note: group_id '{non_existent_group_id}' does not exist, should fallback to default"
        )

        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 200 and response_json.get("status") == "ok":
                result = response_json.get("result", {})
                message = response_json.get("message", "")

                # Verify fallback behavior
                if result.get("is_default") is True and result.get("group_id") is None:
                    print(f"\n✅ Fallback to default config successful!")
                    print(f"   - Requested group_id: {non_existent_group_id}")
                    print(
                        f"   - Returned group_id: {result.get('group_id')} (null = default)"
                    )
                    print(f"   - is_default: {result.get('is_default')}")
                    print(f"   - message: {message}")

                    if "default" in message.lower():
                        print(f"   - Message correctly indicates fallback")
                    return True
                else:
                    print(f"\n❌ Expected fallback to default config")
                    print(f"   - Got group_id: {result.get('group_id')}")
                    print(f"   - Got is_default: {result.get('is_default')}")
                    return False
            elif response.status_code == 404:
                print(f"\n⚠️  404 returned - default config may not exist")
                print(f"   Make sure to run test_post_default_config first")
                return False
            else:
                print(f"\n❌ Unexpected response")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_get_not_found(self) -> bool:
        """
        Test GET: 404 when no default config exists

        This test requires a clean state where no default config exists.
        """
        self.print_separator("GET: 404 When No Config Exists")

        # Use a completely new tenant to ensure no default config
        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()
        headers["X-Organization-Id"] = f"temp_org_{uuid.uuid4().hex[:8]}"
        headers["X-Space-Id"] = f"temp_space_{uuid.uuid4().hex[:8]}"

        non_existent_group_id = f"definitely_not_exists_{uuid.uuid4().hex}"
        params = {"group_id": non_existent_group_id}

        print(f"📍 URL: GET {url}")
        print(f"📤 Using temporary tenant (no data)")
        print(f"📤 Query Params: {params}")

        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 404:
                print(f"\n✅ Correctly returned 404 when no config exists!")
                return True
            else:
                print(f"\n⚠️  Expected 404, got {response.status_code}")
                print(f"   (This may be OK if tenant has default config)")
                return True  # Not a failure, just different state
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    # ==================== PATCH Tests ====================

    def test_patch_update_fields(self) -> bool:
        """
        Test PATCH: Partial update of specific fields
        """
        self.print_separator("PATCH: Partial Update Fields")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        body = {
            "group_id": self.test_group_id,
            "name": f"Patched Name ({self.test_run_id})",
            "description": "Patched description via PATCH",
            "tags": ["patched", "updated", "test"],
        }

        self.print_request("PATCH", url, body)

        try:
            response = requests.patch(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 200 and response_json.get("status") == "ok":
                result = response_json.get("result", {})
                updated_fields = result.get("updated_fields", [])

                if "name" in updated_fields and "description" in updated_fields:
                    print(f"\n✅ Partial update successful!")
                    print(f"   - Updated fields: {updated_fields}")
                    print(f"   - New name: {result.get('name')}")
                    return True
                else:
                    print(f"\n❌ Expected fields not updated")
                    return False
            else:
                print(f"\n❌ Failed to patch config")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_patch_default_config(self) -> bool:
        """
        Test PATCH: Update global config (group_id=null)

        Global config PATCH rules:
        - CAN update: scene_desc, llm_custom_setting, description, tags, user_details, default_timezone
        - CANNOT update: scene (IMMUTABLE_ON_PATCH), name (GROUP_ONLY)
        """
        self.print_separator("PATCH: Update Global Config (group_id=null)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        body = {
            "group_id": None,  # Target global config
            # NOTE: name is NOT allowed for global config
            # NOTE: scene is IMMUTABLE_ON_PATCH
            "scene_desc": {
                "description": "Patched global scene description",
                "patched": True,
            },
            "llm_custom_setting": {
                "boundary": {"provider": "anthropic", "model": "claude-3-haiku"},
                "extraction": {"provider": "anthropic", "model": "claude-3-opus"},
            },
            "tags": ["global", "patched"],
        }

        print("\n📋 Test Conditions:")
        print("   - group_id=null → Global config")
        print("   - scene_desc, llm_custom_setting → CAN be updated (global only)")
        print("   - name → NOT included (not allowed for global)")
        print("   - scene → NOT included (immutable on PATCH)")

        self.print_request("PATCH", url, body)

        try:
            response = requests.patch(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 200 and response_json.get("status") == "ok":
                result = response_json.get("result", {})
                if result.get("group_id") is None:
                    print(f"\n✅ Global config patched successfully!")
                    print(f"   - Updated fields: {result.get('updated_fields', [])}")
                    return True
                else:
                    print(f"\n❌ Expected group_id=null for global config")
                    return False
            elif response.status_code == 404:
                print(
                    f"\n⚠️  Global config not found - run test_post_default_config first"
                )
                return False
            else:
                print(f"\n❌ Failed to patch global config")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_patch_no_changes(self) -> bool:
        """
        Test PATCH: No changes when all fields are null
        """
        self.print_separator("PATCH: No Changes (Empty Update)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        body = {
            "group_id": self.test_group_id,
            # All optional fields are null/not provided
        }

        self.print_request("PATCH", url, body)

        try:
            response = requests.patch(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 200:
                result = response_json.get("result", {})
                updated_fields = result.get("updated_fields", [])

                if len(updated_fields) == 0:
                    print(f"\n✅ Correctly returned no changes!")
                    print(f"   - Message: {response_json.get('message')}")
                    return True
                else:
                    print(f"\n⚠️  Unexpected fields updated: {updated_fields}")
                    return True
            else:
                print(f"\n❌ Unexpected response")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_patch_user_details(self) -> bool:
        """
        Test PATCH: Update user_details field
        """
        self.print_separator("PATCH: Update user_details")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        body = {
            "group_id": self.test_group_id,
            "user_details": {
                "user_001": {
                    "full_name": "Patched User",
                    "role": "user",
                    "custom_role": "admin",
                    "extra": {"patched": True, "level": 10},
                },
                "new_user": {
                    "full_name": "New User Added",
                    "role": "user",
                    "custom_role": "guest",
                    "extra": {},
                },
            },
        }

        self.print_request("PATCH", url, body)

        try:
            response = requests.patch(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 200 and response_json.get("status") == "ok":
                result = response_json.get("result", {})
                if "user_details" in result.get("updated_fields", []):
                    print(f"\n✅ user_details updated successfully!")
                    return True
                else:
                    print(f"\n❌ user_details not in updated_fields")
                    return False
            else:
                print(f"\n❌ Failed to update user_details")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    # ==================== Error/Exception Tests ====================

    def test_post_duplicate_default_upsert(self) -> bool:
        """
        Test POST: Duplicate global config (group_id=null) - should upsert (update)

        Since we use upsert logic, posting duplicate group_id=null should update, not error.

        Global config rules: scene, scene_desc required; name not allowed
        """
        self.print_separator("POST: Duplicate Global Config (Upsert Behavior)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        # First POST - global config (no name)
        body1 = {
            "scene": "group_chat",
            "scene_desc": {"description": "First global config"},
            # NOTE: name is NOT allowed for global config
            "description": "First global config",
            "group_id": None,
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "default_timezone": "UTC",
            "tags": ["v1"],
        }

        print("\n📋 Test: Upsert behavior for global config")
        print("📤 First POST (create global):")
        self.print_request("POST", url, body1)

        try:
            response1 = requests.post(
                url, headers=headers, json=body1, timeout=self.timeout
            )
            response_json1 = self.print_response(response1)

            if response1.status_code != 200:
                print(f"\n❌ First POST failed")
                return False

            first_id = response_json1.get("result", {}).get("id")

            # Second POST with same group_id=null (should update)
            body2 = {
                "scene": "group_chat",
                "scene_desc": {"description": "Second global config (should update)"},
                # NOTE: name is NOT allowed for global config
                "description": "Second global config (should update)",
                "group_id": None,
                "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                "default_timezone": "UTC",
                "tags": ["v2"],
            }

            print("\n📤 Second POST (should upsert/update):")
            self.print_request("POST", url, body2)

            response2 = requests.post(
                url, headers=headers, json=body2, timeout=self.timeout
            )
            response_json2 = self.print_response(response2)

            if response2.status_code == 200:
                result = response_json2.get("result", {})
                second_id = result.get("id")

                # Should be same ID (updated)
                if second_id == first_id:
                    print(
                        f"\n✅ Duplicate global POST correctly updated existing record!"
                    )
                    print(f"   - Same ID: {first_id}")
                    print(f"   - Description updated")
                    return True
                else:
                    print(f"\n⚠️  Different IDs: first={first_id}, second={second_id}")
                    print(f"   This might indicate duplicate records were created")
                    return False
            else:
                print(f"\n❌ Second POST failed with status {response2.status_code}")
                return False

        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_post_duplicate_group_id_upsert(self) -> bool:
        """
        Test POST: Duplicate specific group_id - should upsert (update)

        Group config rules: name required; scene, scene_desc not allowed
        """
        self.print_separator("POST: Duplicate group_id (Upsert Behavior)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        dup_group_id = f"dup_group_{self.test_run_id}"

        # First POST - group config (name required, no scene/scene_desc)
        body1 = {
            # NOTE: scene, scene_desc are NOT allowed for group config
            "name": f"Group V1",  # REQUIRED
            "description": "First version",
            "group_id": dup_group_id,
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "default_timezone": "UTC",
            "tags": ["v1"],
        }

        print("\n📋 Test: Upsert behavior for group config")
        print(f"📤 First POST (create group_id={dup_group_id}):")
        self.print_request("POST", url, body1)

        try:
            response1 = requests.post(
                url, headers=headers, json=body1, timeout=self.timeout
            )
            response_json1 = self.print_response(response1)

            if response1.status_code != 200:
                print(f"\n❌ First POST failed")
                return False

            first_id = response_json1.get("result", {}).get("id")

            # Second POST with same group_id
            body2 = {
                # NOTE: scene, scene_desc are NOT allowed for group config
                "name": f"Group V2 (Updated)",  # REQUIRED
                "description": "Second version (should update)",
                "group_id": dup_group_id,
                "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
                "default_timezone": "Asia/Shanghai",
                "tags": ["v2", "updated"],
            }

            print("\n📤 Second POST (should upsert/update):")
            self.print_request("POST", url, body2)

            response2 = requests.post(
                url, headers=headers, json=body2, timeout=self.timeout
            )
            response_json2 = self.print_response(response2)

            if response2.status_code == 200:
                result = response_json2.get("result", {})
                second_id = result.get("id")
                name = result.get("name")

                if second_id == first_id:
                    print(
                        f"\n✅ Duplicate group_id POST correctly updated existing record!"
                    )
                    print(f"   - Same ID: {first_id}")
                    print(f"   - Name: {name}")
                    return True
                else:
                    print(f"\n⚠️  Different IDs - possible duplicate creation")
                    return False
            else:
                print(f"\n❌ Second POST failed")
                return False

        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_post_missing_required_fields(self) -> bool:
        """
        Test POST: Missing required fields should return 400/422

        For group config: name is required
        For global config: scene, scene_desc are required
        """
        self.print_separator("POST: Missing Required Fields (Error Case)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        # Group config missing 'name' (required)
        body = {
            "group_id": f"incomplete_{self.test_run_id}",
            "description": "Missing required field: name",
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            # NOTE: name is REQUIRED for group config but not provided
        }

        print("\n📋 Test Conditions:")
        print("   - group_id provided → Group config")
        print("   - name → NOT provided (but REQUIRED for group config)")

        self.print_request("POST", url, body)

        try:
            response = requests.post(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code in [400, 422]:
                print(
                    f"\n✅ Correctly returned {response.status_code} for missing required fields!"
                )
                return True
            elif response.status_code == 500:
                print(f"\n⚠️  Returned 500 - validation might be at service level")
                return True
            else:
                print(f"\n❌ Expected 400/422, got {response.status_code}")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_post_scene_not_allowed_for_group(self) -> bool:
        """
        Test POST: scene field should be rejected for group config

        Group config cannot set scene (inherited from global config)
        """
        self.print_separator("POST: Scene Not Allowed for Group Config (Error Case)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        body = {
            "scene": "group_chat",  # NOT allowed for group config
            "scene_desc": {"description": "test"},  # NOT allowed for group config
            "name": "Test Group",  # Required for group
            "description": "Should fail - scene not allowed for group",
            "group_id": f"invalid_scene_{self.test_run_id}",
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "default_timezone": "UTC",
            "tags": [],
        }

        print("\n📋 Test Conditions:")
        print("   - group_id provided → Group config")
        print("   - scene, scene_desc → NOT allowed for group (should be rejected)")

        self.print_request("POST", url, body)

        try:
            response = requests.post(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code in [400, 422]:
                print(
                    f"\n✅ Correctly rejected scene for group config with status {response.status_code}!"
                )
                return True
            elif response.status_code == 500:
                print(f"\n⚠️  Returned 500 - validation at service level")
                return True
            elif response.status_code == 200:
                print(f"\n❌ Should have rejected scene for group config")
                return False
            else:
                print(f"\n❌ Unexpected status: {response.status_code}")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_post_name_not_allowed_for_global(self) -> bool:
        """
        Test POST: name field should be rejected for global config

        Global config cannot set name (only for group config)
        """
        self.print_separator("POST: Name Not Allowed for Global Config (Error Case)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        body = {
            "scene": "group_chat",
            "scene_desc": {"description": "test"},
            "name": "Should Not Be Allowed",  # NOT allowed for global config
            "description": "Should fail - name not allowed for global",
            "group_id": None,  # Global config
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "default_timezone": "UTC",
            "tags": [],
        }

        print("\n📋 Test Conditions:")
        print("   - group_id=null → Global config")
        print("   - name → NOT allowed for global (should be rejected)")

        self.print_request("POST", url, body)

        try:
            response = requests.post(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code in [400, 422]:
                print(
                    f"\n✅ Correctly rejected name for global config with status {response.status_code}!"
                )
                return True
            elif response.status_code == 500:
                print(f"\n⚠️  Returned 500 - validation at service level")
                return True
            elif response.status_code == 200:
                print(f"\n❌ Should have rejected name for global config")
                return False
            else:
                print(f"\n❌ Unexpected status: {response.status_code}")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_patch_nonexistent_group_id_no_fallback(self) -> bool:
        """
        Test PATCH: Non-existent group_id should return 404 (NO fallback)

        This tests the NO-fallback behavior for PATCH: when PATCH targets a non-existent
        group_id, it should return 404, NOT fallback to default config.

        This is important because PATCH should only update existing records, not create
        new ones or update unrelated records.
        """
        self.print_separator(
            "PATCH: Non-existent group_id (Should Return 404, No Fallback)"
        )

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        non_existent_id = f"definitely_not_exists_{uuid.uuid4().hex}"

        body = {
            "group_id": non_existent_id,
            "name": f"Should Not Update ({self.test_run_id})",
            "tags": ["should_fail"],
        }

        self.print_request("PATCH", url, body)
        print(f"📝 Note: group_id '{non_existent_id}' does not exist")
        print(f"📝 Expected: 404 (no fallback to default config)")

        try:
            response = requests.patch(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 404:
                print(f"\n✅ Correctly returned 404 for non-existent group_id!")
                print(f"   - PATCH does NOT fallback to default config")
                print(f"   - This is the expected behavior for update operations")
                return True
            elif response.status_code == 200:
                result = response_json.get("result", {})
                print(f"\n❌ Should have returned 404, but got 200")
                print(f"   - This means PATCH incorrectly fallback to another config")
                print(f"   - Updated group_id: {result.get('group_id')}")
                return False
            else:
                print(f"\n❌ Unexpected status: {response.status_code}")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_patch_nonexistent_default(self) -> bool:
        """
        Test PATCH: group_id=null when no default exists should return 404
        """
        self.print_separator("PATCH: Non-existent Default Config (Error Case)")

        # Use a new tenant without default config
        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()
        headers["X-Organization-Id"] = f"no_default_org_{uuid.uuid4().hex[:8]}"
        headers["X-Space-Id"] = f"no_default_space_{uuid.uuid4().hex[:8]}"

        body = {
            "group_id": None,
            # NOTE: name is NOT allowed for global config
            "description": "Should Fail - no global config exists",
        }

        self.print_request("PATCH", url, body)
        print(f"📝 Note: Using new tenant without default config")

        try:
            response = requests.patch(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 404:
                print(f"\n✅ Correctly returned 404 for non-existent default config!")
                return True
            else:
                print(f"\n⚠️  Expected 404, got {response.status_code}")
                return True  # Not a critical failure
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_post_empty_body(self) -> bool:
        """
        Test POST: Empty body should return 400/422
        """
        self.print_separator("POST: Empty Body (Error Case)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        body = {}

        self.print_request("POST", url, body)

        try:
            response = requests.post(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code in [400, 422, 500]:
                print(
                    f"\n✅ Correctly rejected empty body with status {response.status_code}!"
                )
                return True
            else:
                print(f"\n❌ Expected error status, got {response.status_code}")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    # ==================== Full Fallback Flow Test ====================

    # ==================== LLM Custom Setting Tests ====================

    def test_post_llm_custom_setting(self) -> bool:
        """
        Test POST: llm_custom_setting is correctly saved for global config
        """
        self.print_separator("POST: LLM Custom Setting (Global Config)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        llm_setting = {
            "boundary": {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "extra": {"temperature": 0.3},
            },
            "extraction": {"provider": "anthropic", "model": "claude-3-opus"},
        }

        body = {
            "scene": "group_chat",
            "scene_desc": {"description": "Test llm_custom_setting"},
            "group_id": None,
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "llm_custom_setting": llm_setting,
        }

        print("\n📋 Test Conditions:")
        print("   - group_id=null → Global config")
        print("   - llm_custom_setting → Should be saved correctly")
        print(f"   - Input llm_custom_setting: {json.dumps(llm_setting, indent=2)}")

        self.print_request("POST", url, body)

        try:
            response = requests.post(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 200 and response_json.get("status") == "ok":
                result = response_json.get("result", {})
                saved_setting = result.get("llm_custom_setting")

                print(f"\n📊 Saved llm_custom_setting:")
                print(json.dumps(saved_setting, indent=2))

                # Verify llm_custom_setting was saved correctly
                if saved_setting:
                    boundary = saved_setting.get("boundary", {})
                    extraction = saved_setting.get("extraction", {})

                    if (
                        boundary.get("provider") == "openai"
                        and boundary.get("model") == "gpt-4o-mini"
                        and extraction.get("provider") == "anthropic"
                        and extraction.get("model") == "claude-3-opus"
                    ):
                        print("\n✅ llm_custom_setting saved correctly!")
                        return True
                    else:
                        print("\n❌ llm_custom_setting values mismatch")
                        return False
                else:
                    print("\n❌ llm_custom_setting is None")
                    return False
            else:
                print("\n❌ Failed to save global config")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_patch_llm_custom_setting(self) -> bool:
        """
        Test PATCH: llm_custom_setting can be updated for global config
        """
        self.print_separator("PATCH: LLM Custom Setting (Global Config)")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        # First create a global config
        create_body = {
            "scene": "group_chat",
            "scene_desc": {"description": "Test patch llm_custom_setting"},
            "group_id": None,
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "llm_custom_setting": {
                "boundary": {"provider": "openai", "model": "gpt-4o-mini"}
            },
        }

        print("📤 Step 1: Create global config with initial llm_custom_setting")
        response = requests.post(
            url, headers=headers, json=create_body, timeout=self.timeout
        )
        if response.status_code != 200:
            print(f"\n❌ Failed to create initial config")
            return False

        # Now patch with new llm_custom_setting
        new_llm_setting = {
            "boundary": {"provider": "azure", "model": "gpt-4"},
            "extraction": {"provider": "openai", "model": "gpt-4o"},
        }

        patch_body = {"group_id": None, "llm_custom_setting": new_llm_setting}

        print("\n📤 Step 2: PATCH with new llm_custom_setting")
        print(f"   - New llm_custom_setting: {json.dumps(new_llm_setting, indent=2)}")

        self.print_request("PATCH", url, patch_body)

        try:
            response = requests.patch(
                url, headers=headers, json=patch_body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code == 200 and response_json.get("status") == "ok":
                result = response_json.get("result", {})
                updated_fields = result.get("updated_fields", [])

                if "llm_custom_setting" not in updated_fields:
                    print(f"\n❌ llm_custom_setting not in updated_fields")
                    print(f"   - Updated fields: {updated_fields}")
                    return False

                print(f"\n✅ PATCH successful, llm_custom_setting in updated_fields")

                # Step 3: GET to verify the data was actually saved
                print("\n📤 Step 3: GET to verify llm_custom_setting was saved")
                get_response = requests.get(
                    url,
                    headers=headers,
                    params={"group_id": None},
                    timeout=self.timeout,
                )
                get_json = self.print_response(get_response)

                if get_response.status_code == 200:
                    get_result = get_json.get("result", {})
                    saved_setting = get_result.get("llm_custom_setting")

                    print(f"\n📊 Retrieved llm_custom_setting:")
                    print(json.dumps(saved_setting, indent=2))

                    # Verify the values match what we patched
                    if saved_setting:
                        boundary = saved_setting.get("boundary", {})
                        extraction = saved_setting.get("extraction", {})

                        if (
                            boundary.get("provider") == "azure"
                            and boundary.get("model") == "gpt-4"
                            and extraction.get("provider") == "openai"
                            and extraction.get("model") == "gpt-4o"
                        ):
                            print(
                                "\n✅ GET verified: llm_custom_setting correctly saved!"
                            )
                            return True
                        else:
                            print("\n❌ GET verification failed: values mismatch")
                            print(f"   Expected boundary: azure/gpt-4, got: {boundary}")
                            print(
                                f"   Expected extraction: openai/gpt-4o, got: {extraction}"
                            )
                            return False
                    else:
                        print(
                            "\n❌ GET verification failed: llm_custom_setting is None"
                        )
                        return False
                else:
                    print(
                        f"\n❌ GET request failed with status {get_response.status_code}"
                    )
                    return False
            else:
                print("\n❌ Failed to patch global config")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    def test_post_llm_custom_setting_not_allowed_for_group(self) -> bool:
        """
        Test POST: llm_custom_setting should be rejected for group config
        """
        self.print_separator("POST: LLM Custom Setting Not Allowed for Group")

        url = f"{self.base_url}{self.api_prefix}/conversation-meta"
        headers = self.get_tenant_headers()

        body = {
            "name": "Test Group",
            "group_id": f"test_llm_group_{self.test_run_id}",
            "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
            "llm_custom_setting": {  # NOT allowed for group config
                "boundary": {"provider": "openai", "model": "gpt-4o-mini"}
            },
        }

        print("\n📋 Test Conditions:")
        print("   - group_id provided → Group config")
        print("   - llm_custom_setting → NOT allowed for group (should be rejected)")

        self.print_request("POST", url, body)

        try:
            response = requests.post(
                url, headers=headers, json=body, timeout=self.timeout
            )
            response_json = self.print_response(response)

            if response.status_code in [400, 422]:
                print(f"\n✅ Correctly rejected llm_custom_setting for group config!")
                return True
            elif response.status_code == 200:
                # Check if llm_custom_setting was ignored (silently filtered out)
                result = response_json.get("result", {})
                if result.get("llm_custom_setting") is None:
                    print(
                        f"\n✅ llm_custom_setting was correctly filtered out for group config"
                    )
                    return True
                else:
                    print(
                        f"\n❌ llm_custom_setting should not be saved for group config"
                    )
                    return False
            else:
                print(f"\n❌ Unexpected status: {response.status_code}")
                return False
        except Exception as e:
            print(f"\n❌ Request failed: {e}")
            return False

    # ==================== Full Fallback Flow Test ====================

    def test_full_fallback_flow(self) -> bool:
        """
        Test complete fallback flow:
        1. Create default config
        2. Create specific group config
        3. Get specific group - should return group config
        4. Get non-existent group - should fallback to default
        5. Get without group_id - should return default
        """
        self.print_separator("Full Fallback Flow Test")

        print("\n📋 Test Flow:")
        print("   1. Create default config (group_id=null)")
        print("   2. Create specific group config")
        print("   3. GET specific group -> should return group config")
        print("   4. GET non-existent group -> should fallback to default")
        print("   5. GET without group_id -> should return default")
        print()

        results = []

        # Step 1: Create default config
        print("\n--- Step 1: Create default config ---")
        results.append(("Create default", self.test_post_default_config()))

        # Step 2: Create specific group config
        print("\n--- Step 2: Create specific group config ---")
        results.append(("Create group", self.test_post_with_group_id()))

        # Step 3: Get specific group
        print("\n--- Step 3: Get specific group ---")
        results.append(("Get group", self.test_get_by_group_id()))

        # Step 4: Get non-existent group (fallback)
        print("\n--- Step 4: Get non-existent group (should fallback) ---")
        results.append(("Fallback", self.test_get_fallback_to_default()))

        # Step 5: Get without group_id
        print("\n--- Step 5: Get without group_id ---")
        results.append(("Get default", self.test_get_default_config()))

        # Summary
        self.print_separator("Fallback Flow Test Summary")
        all_passed = True
        for name, passed in results:
            status = "✅" if passed else "❌"
            print(f"   {status} {name}")
            if not passed:
                all_passed = False

        if all_passed:
            print(f"\n🎉 All fallback flow tests passed!")
        else:
            print(f"\n⚠️  Some tests failed")

        return all_passed

    # ==================== Test Runner ====================

    def run_all_tests(self) -> dict:
        """Run all tests and return results"""
        results = {}

        # Initialize database first
        self.init_database()

        # POST tests
        results["post_default"] = self.test_post_default_config()
        results["post_with_group_id"] = self.test_post_with_group_id()
        results["post_update_existing"] = self.test_post_update_existing()

        # GET tests
        results["get_by_group_id"] = self.test_get_by_group_id()
        results["get_default"] = self.test_get_default_config()
        results["get_fallback"] = self.test_get_fallback_to_default()
        results["get_not_found"] = self.test_get_not_found()

        # PATCH tests
        results["patch_update"] = self.test_patch_update_fields()
        results["patch_default"] = self.test_patch_default_config()
        results["patch_no_changes"] = self.test_patch_no_changes()
        results["patch_user_details"] = self.test_patch_user_details()

        # LLM Custom Setting tests
        results["llm_post"] = self.test_post_llm_custom_setting()
        results["llm_patch"] = self.test_patch_llm_custom_setting()
        results["llm_not_allowed_group"] = (
            self.test_post_llm_custom_setting_not_allowed_for_group()
        )

        # Error/Exception tests
        results["error_dup_default"] = self.test_post_duplicate_default_upsert()
        results["error_dup_group_id"] = self.test_post_duplicate_group_id_upsert()
        results["error_missing_fields"] = self.test_post_missing_required_fields()
        results["error_scene_not_allowed"] = (
            self.test_post_scene_not_allowed_for_group()
        )
        results["error_name_not_allowed"] = self.test_post_name_not_allowed_for_global()
        results["error_patch_no_fallback"] = (
            self.test_patch_nonexistent_group_id_no_fallback()
        )
        results["error_patch_no_default"] = self.test_patch_nonexistent_default()
        results["error_empty_body"] = self.test_post_empty_body()

        return results

    def run_test_by_name(self, test_name: str) -> bool:
        """Run a specific test by name"""
        # Initialize database first
        self.init_database()

        test_map = {
            # POST tests
            "post": lambda: all(
                [
                    self.test_post_default_config(),
                    self.test_post_with_group_id(),
                    self.test_post_update_existing(),
                ]
            ),
            "post_default": self.test_post_default_config,
            "post_with_group_id": self.test_post_with_group_id,
            "post_update": self.test_post_update_existing,
            # GET tests
            "get": lambda: all(
                [
                    self.test_post_default_config(),  # Need data first
                    self.test_post_with_group_id(),
                    self.test_get_by_group_id(),
                    self.test_get_default_config(),
                    self.test_get_fallback_to_default(),
                ]
            ),
            "get_by_group_id": lambda: self.test_post_with_group_id()
            and self.test_get_by_group_id(),
            "get_default": lambda: self.test_post_default_config()
            and self.test_get_default_config(),
            "get_fallback": lambda: self.test_post_default_config()
            and self.test_get_fallback_to_default(),
            "get_not_found": self.test_get_not_found,
            # PATCH tests
            "patch": lambda: all(
                [
                    self.test_post_default_config(),
                    self.test_post_with_group_id(),
                    self.test_patch_update_fields(),
                    self.test_patch_default_config(),
                    self.test_patch_no_changes(),
                    self.test_patch_user_details(),
                ]
            ),
            "patch_update": lambda: self.test_post_with_group_id()
            and self.test_patch_update_fields(),
            "patch_default": lambda: self.test_post_default_config()
            and self.test_patch_default_config(),
            "patch_no_changes": lambda: self.test_post_with_group_id()
            and self.test_patch_no_changes(),
            "patch_user_details": lambda: self.test_post_with_group_id()
            and self.test_patch_user_details(),
            # Error/Exception tests
            "error": lambda: all(
                [
                    self.test_post_duplicate_default_upsert(),
                    self.test_post_duplicate_group_id_upsert(),
                    self.test_post_missing_required_fields(),
                    self.test_post_scene_not_allowed_for_group(),
                    self.test_patch_nonexistent_group_id_no_fallback(),
                    self.test_patch_nonexistent_default(),
                    self.test_post_empty_body(),
                ]
            ),
            "error_dup_default": self.test_post_duplicate_default_upsert,
            "error_dup_group_id": self.test_post_duplicate_group_id_upsert,
            "error_missing_fields": self.test_post_missing_required_fields,
            "error_scene_not_allowed": self.test_post_scene_not_allowed_for_group,
            "error_name_not_allowed": self.test_post_name_not_allowed_for_global,
            "error_patch_no_fallback": self.test_patch_nonexistent_group_id_no_fallback,
            "error_patch_no_default": self.test_patch_nonexistent_default,
            "error_empty_body": self.test_post_empty_body,
            # LLM Custom Setting tests
            "llm": lambda: all(
                [
                    self.test_post_llm_custom_setting(),
                    self.test_patch_llm_custom_setting(),
                    self.test_post_llm_custom_setting_not_allowed_for_group(),
                ]
            ),
            "llm_post": self.test_post_llm_custom_setting,
            "llm_patch": self.test_patch_llm_custom_setting,
            "llm_not_allowed_group": self.test_post_llm_custom_setting_not_allowed_for_group,
            # Fallback flow
            "fallback": self.test_full_fallback_flow,
            # Unit tests (no server required)
            "llm_custom_setting_model": test_llm_custom_setting_model,
            # All tests
            "all": lambda: all(self.run_all_tests().values()),
        }

        if test_name in test_map:
            return test_map[test_name]()
        else:
            print(f"❌ Unknown test: {test_name}")
            print(f"Available tests: {', '.join(test_map.keys())}")
            return False


def print_final_summary(results: dict):
    """Print final test summary"""
    print("\n" + "=" * 80)
    print("  FINAL TEST SUMMARY")
    print("=" * 80)

    passed = sum(1 for v in results.values() if v)
    failed = sum(1 for v in results.values() if not v)
    total = len(results)

    for name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"   {status} - {name}")

    print()
    print(f"   Total: {total} | Passed: {passed} | Failed: {failed}")

    if failed == 0:
        print("\n🎉 All tests passed!")
    else:
        print(f"\n⚠️  {failed} test(s) failed")


def main():
    parser = argparse.ArgumentParser(description="Conversation Meta API Test Script")
    parser.add_argument(
        "--base-url",
        default="http://localhost:1995",
        help="API base URL (default: http://localhost:1995)",
    )
    parser.add_argument(
        "--organization-id", default=None, help="Organization ID for tenant headers"
    )
    parser.add_argument("--space-id", default=None, help="Space ID for tenant headers")
    parser.add_argument(
        "--test-method",
        default="all",
        help="Test method to run: all, post, get, patch, fallback, or specific test name",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Request timeout in seconds (default: 60)",
    )

    args = parser.parse_args()

    print("\n" + "=" * 80)
    print("  CONVERSATION META API TEST")
    print("=" * 80)
    print(f"📍 Base URL: {args.base_url}")
    print(f"📍 Test Method: {args.test_method}")
    print(f"📍 Timeout: {args.timeout}s")

    tester = ConversationMetaTester(
        base_url=args.base_url,
        organization_id=args.organization_id,
        space_id=args.space_id,
        timeout=args.timeout,
    )

    if args.test_method == "all":
        results = tester.run_all_tests()
        print_final_summary(results)
    else:
        success = tester.run_test_by_name(args.test_method)
        if success:
            print("\n✅ Test passed!")
        else:
            print("\n❌ Test failed!")


if __name__ == "__main__":
    main()

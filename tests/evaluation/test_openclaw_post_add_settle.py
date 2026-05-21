"""Verify OpenClawAdapter.wait_post_add_settle posts to OV /system/wait.

Task 2 of 2026-05-21 ov-global-settle-active-poll plan.
"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from evaluation.src.adapters.openclaw.adapter import OpenClawAdapter


def _make_adapter(post_add_settle_cfg):
    """Construct an adapter with minimal config to exercise the hook only.

    The full adapter __init__ does heavy work (repo introspection, manifest
    load). For this unit test we bypass __init__ via __new__ and only set
    the attribute the hook reads (self.config).
    """
    adapter = OpenClawAdapter.__new__(OpenClawAdapter)
    adapter.config = {
        "ov_ingest": {
            "base_url": "http://oviking.test:1933",
            "api_key_env": "TEST_OV_KEY",
            "account_id": "default",
            "user_id_template": "{conv_id}",
            "post_add_settle": post_add_settle_cfg,
        }
    }
    return adapter


class _FakeResp:
    def __init__(self, status=200, body='{"status":"ok","result":{"embedding":{"processed":42,"error_count":0,"errors":[]}}}'):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._body


@pytest.mark.asyncio
async def test_wait_post_add_settle_disabled_returns_none():
    adapter = _make_adapter({"enabled": False})
    os.environ["TEST_OV_KEY"] = "k"
    result = await adapter.wait_post_add_settle()
    assert result is None


@pytest.mark.asyncio
async def test_wait_post_add_settle_no_config_returns_none():
    """When post_add_settle key is missing entirely, hook is a no-op."""
    adapter = OpenClawAdapter.__new__(OpenClawAdapter)
    adapter.config = {"ov_ingest": {"base_url": "http://x", "api_key_env": "K"}}
    result = await adapter.wait_post_add_settle()
    assert result is None


@pytest.mark.asyncio
async def test_wait_post_add_settle_posts_to_system_wait():
    adapter = _make_adapter({"enabled": True, "timeout_sec": 1800, "http_buffer_sec": 60})
    os.environ["TEST_OV_KEY"] = "secret-k"

    fake_session = MagicMock()
    fake_session.post = MagicMock(return_value=_FakeResp())
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=fake_session):
        result = await adapter.wait_post_add_settle()

    fake_session.post.assert_called_once()
    call_args = fake_session.post.call_args
    assert call_args.args[0].endswith("/api/v1/system/wait")
    assert call_args.kwargs["json"] == {"timeout": 1800.0}
    headers = call_args.kwargs["headers"]
    assert headers["X-API-Key"] == "secret-k"
    assert headers["X-OpenViking-Account"] == "default"

    assert result is not None
    assert "embedding" in result

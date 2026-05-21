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
    the attributes the hook reads. Production layout: self.config is the
    full top-level yaml dict and self._openclaw_cfg = config['openclaw'].
    The hook reads ov_ingest via self._openclaw_cfg.
    """
    adapter = OpenClawAdapter.__new__(OpenClawAdapter)
    adapter._openclaw_cfg = {
        "ov_ingest": {
            "base_url": "http://oviking.test:1933",
            "api_key_env": "TEST_OV_KEY",
            "account_id": "default",
            "user_id_template": "{conv_id}",
            "post_add_settle": post_add_settle_cfg,
        }
    }
    adapter.config = {"openclaw": adapter._openclaw_cfg}
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
    adapter._openclaw_cfg = {"ov_ingest": {"base_url": "http://x", "api_key_env": "K"}}
    adapter.config = {"openclaw": adapter._openclaw_cfg}
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


@pytest.mark.asyncio
async def test_wait_post_add_settle_resolves_via_openclaw_cfg_not_top_level():
    """Regression: production adapter has self.config = full top-level yaml
    dict; ov_ingest is nested under config['openclaw']. Hook must read via
    self._openclaw_cfg (mirroring _ingest_via_session_bundle), NOT
    self.config.get('ov_ingest'). Earlier impl looked at top-level and
    silently no-op'd."""
    adapter = OpenClawAdapter.__new__(OpenClawAdapter)
    adapter._openclaw_cfg = {
        "ov_ingest": {
            "base_url": "http://oviking.test:1933",
            "api_key_env": "TEST_OV_KEY",
            "post_add_settle": {"enabled": True, "timeout_sec": 1800},
        }
    }
    # Top-level config has NO direct ov_ingest — only nested under openclaw.
    # This mirrors what BaseAdapter(__init__) does when given parsed yaml.
    adapter.config = {
        "openclaw": adapter._openclaw_cfg,
        "llm": {},
        "search": {},
    }
    os.environ["TEST_OV_KEY"] = "k"

    fake_session = MagicMock()
    fake_session.post = MagicMock(return_value=_FakeResp())
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession", return_value=fake_session):
        result = await adapter.wait_post_add_settle()

    # Must have made the POST — proves hook resolved ov_ingest via
    # self._openclaw_cfg even though self.config has it nested
    fake_session.post.assert_called_once()
    assert result is not None

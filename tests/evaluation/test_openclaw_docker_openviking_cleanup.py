"""Regression tests for OpenViking clean_groups support in docker adapter."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from evaluation.src.adapters.openclaw.adapter import OpenClawAdapter
from evaluation.src.adapters.openclaw_docker_adapter import DockerizedOpenclawAdapter
from evaluation.src.core.data_models import Conversation


def _make_adapter(
    *,
    clean_groups: bool = True,
    openclaw_cfg: dict[str, Any] | None = None,
) -> DockerizedOpenclawAdapter:
    adapter = DockerizedOpenclawAdapter.__new__(DockerizedOpenclawAdapter)
    adapter.config = {"clean_groups": clean_groups}
    adapter._openclaw_cfg = openclaw_cfg or {
        "context_engine_mode": "openviking",
        "ov_ingest": {
            "base_url": "http://127.0.0.1:1933",
            "api_key_env": "OPENVIKING_TEST_KEY",
            "account_id": "acct",
            "user_id_template": "user-{conv_id}",
            "agent_id_template": "agent-{conv_id}",
        },
    }
    return adapter


def _conv(conv_id: str) -> Conversation:
    return Conversation(conversation_id=conv_id, messages=[])


@pytest.mark.asyncio
async def test_add_cleans_openviking_groups_before_ingest(monkeypatch):
    adapter = _make_adapter()
    conversations = [_conv("locomo_3"), _conv("locomo_4")]
    calls: list[tuple[str, list[str]]] = []

    async def fake_clean(self, convs):
        calls.append(("clean", [c.conversation_id for c in convs]))

    async def fake_super_add(self, convs, **kwargs):
        calls.append(("add", [c.conversation_id for c in convs]))
        return {"status": "ok", "kwargs": kwargs}

    monkeypatch.setattr(
        DockerizedOpenclawAdapter,
        "_clean_openviking_groups",
        fake_clean,
        raising=False,
    )
    monkeypatch.setattr(OpenClawAdapter, "add", fake_super_add)

    result = await adapter.add(conversations, output_dir="/tmp/out")

    assert result["status"] == "ok"
    assert calls == [
        ("clean", ["locomo_3", "locomo_4"]),
        ("add", ["locomo_3", "locomo_4"]),
    ]


@pytest.mark.asyncio
async def test_add_skips_openviking_cleanup_on_resume(monkeypatch):
    adapter = _make_adapter()
    conversations = [_conv("locomo_3")]
    calls: list[str] = []

    async def fail_clean(self, convs):
        raise AssertionError("cleanup must not run on resume")

    async def fake_super_add(self, convs, **kwargs):
        calls.append("add")
        return {"status": "ok", "kwargs": kwargs}

    monkeypatch.setattr(
        DockerizedOpenclawAdapter,
        "_clean_openviking_groups",
        fail_clean,
        raising=False,
    )
    monkeypatch.setattr(OpenClawAdapter, "add", fake_super_add)

    result = await adapter.add(conversations, output_dir="/tmp/out", resume=True)

    assert result["status"] == "ok"
    assert result["kwargs"]["resume"] is True
    assert calls == ["add"]


def test_openviking_cleanup_plan_uses_ov_ingest_tenant_templates(monkeypatch):
    monkeypatch.setenv("OPENVIKING_TEST_KEY", "sk-test")
    adapter = _make_adapter()

    plan = adapter._openviking_cleanup_plan_for_conv("locomo_8")

    assert plan["base_url"] == "http://127.0.0.1:1933"
    assert [target["uri"] for target in plan["targets"]] == [
        "viking://user/user-locomo_8/memories",
        "viking://agent/agent-locomo_8/memories",
    ]
    headers = plan["targets"][0]["headers"]
    assert headers["X-API-Key"] == "sk-test"
    assert headers["X-OpenViking-Account"] == "acct"
    assert headers["X-OpenViking-User"] == "user-locomo_8"
    assert headers["X-OpenViking-Agent"] == "agent-locomo_8"


class _FakeResponse:
    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self) -> str:
        return self._text


class _FakeHTTP:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.delete = MagicMock(return_value=response)


class _FakeHTTPSequence:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.delete = MagicMock(side_effect=responses)


@pytest.mark.asyncio
async def test_openviking_cleanup_delete_treats_404_as_already_clean():
    adapter = _make_adapter()
    http = _FakeHTTP(_FakeResponse(404, '{"status":"error"}'))

    result = await adapter._delete_openviking_uri(
        http,
        "http://ov",
        "viking://user/missing/memories",
        {"X-OpenViking-User": "missing"},
    )

    assert result["status"] == "missing"
    http.delete.assert_called_once_with(
        "http://ov/api/v1/fs",
        headers={"X-OpenViking-User": "missing"},
        params={"uri": "viking://user/missing/memories", "recursive": "true"},
    )


@pytest.mark.asyncio
async def test_openviking_cleanup_delete_fails_on_server_error():
    adapter = _make_adapter()
    http = _FakeHTTP(_FakeResponse(500, "boom"))

    with pytest.raises(RuntimeError, match="OpenViking cleanup failed"):
        await adapter._delete_openviking_uri(
            http,
            "http://ov",
            "viking://user/locomo_0/memories",
            {},
        )


@pytest.mark.asyncio
async def test_openviking_cleanup_delete_retries_retryable_path_busy(monkeypatch):
    adapter = _make_adapter(openclaw_cfg={
        "context_engine_mode": "openviking",
        "ov_ingest": {
            "cleanup_max_retries": 2,
            "cleanup_retry_delay_sec": 0.5,
        },
    })
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(
        "evaluation.src.adapters.openclaw_docker_adapter.asyncio.sleep",
        fake_sleep,
    )
    http = _FakeHTTPSequence([
        _FakeResponse(
            409,
            (
                '{"status":"error","error":{"code":"CONFLICT",'
                '"message":"Resource is being processed",'
                '"details":{"conflict_type":"path_busy","retryable":true}}}'
            ),
        ),
        _FakeResponse(
            409,
            (
                '{"status":"error","error":{"code":"CONFLICT",'
                '"message":"Resource is still being processed",'
                '"details":{"conflict_type":"path_busy","retryable":true}}}'
            ),
        ),
        _FakeResponse(200, '{"status":"ok","result":{"deleted":true}}'),
    ])

    result = await adapter._delete_openviking_uri(
        http,
        "http://ov",
        "viking://user/locomo_4/memories",
        {"X-OpenViking-User": "locomo_4"},
    )

    assert result["status"] == "deleted"
    assert http.delete.call_count == 3
    assert sleep_calls == [0.5, 0.5]

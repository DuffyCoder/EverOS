"""
Regression coverage for the Apr 22 flush-prompt bug.

OpenAIProvider previously had a single-role ``messages`` shape hard-wired
to ``[{"role": "user", "content": prompt}]``. Callers that needed a
system instruction (openclaw flush most visibly) concatenated
``f"{system}\\n\\n{user}"`` and relied on the LLM inferring roles from
position. Some Azure deployments stopped respecting that, collapsing
the whole string into a conversational user turn and returning
"Would you like a summary?" instead of following the flush directives.

These tests pin the fix:
* ``system_prompt=None`` — messages has exactly one ``user`` entry
  (backward compatibility for every other caller).
* ``system_prompt="..."`` — messages has ``[system, user]`` in that
  order, with the prompt kwarg going to the user entry.
* ``system_prompt=""`` — treated as absent (no empty system entry
  sent to gateways that reject blank content).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest


class _Resp:
    """Minimal ``aiohttp`` response stub. status=200 + canned body."""

    status = 200

    def __init__(self, body: dict):
        self._body_bytes = json.dumps(body).encode("utf-8")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    @property
    def content(self):
        body_bytes = self._body_bytes

        class _Iter:
            async def iter_any(self):
                yield body_bytes

        return _Iter()


class _Session:
    """Captures the JSON posted to chat/completions for inspection."""

    last_payload: dict | None = None

    def __init__(self, *_, **__):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def post(self, _url, *, json, headers):  # noqa: A002 — matches aiohttp kw
        _Session.last_payload = json
        return _Resp(
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


@pytest.fixture
def patched_session(monkeypatch):
    from memory_layer.llm import openai_provider as mod

    _Session.last_payload = None
    monkeypatch.setattr(mod.aiohttp, "ClientSession", _Session)
    yield _Session


def _make_provider():
    from memory_layer.llm.openai_provider import OpenAIProvider

    return OpenAIProvider(
        model="gpt-4o-mini",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        temperature=0.0,
    )


def test_generate_without_system_prompt_sends_single_user_message(patched_session):
    """Backward compat: no system_prompt → single user entry."""
    provider = _make_provider()
    asyncio.run(provider.generate(prompt="hello"))
    payload = patched_session.last_payload
    assert payload is not None
    assert payload["messages"] == [{"role": "user", "content": "hello"}]


def test_generate_with_system_prompt_sends_two_messages(patched_session):
    """Fix: system_prompt kwarg produces [system, user] in order so the
    LLM can distinguish instructions from content. Collapsing them into
    one string broke openclaw flush on some deployments."""
    provider = _make_provider()
    asyncio.run(
        provider.generate(
            prompt="## Transcript\n- Alice: hi",
            system_prompt="Distill the transcript into bullets.",
        )
    )
    payload = patched_session.last_payload
    assert payload["messages"] == [
        {"role": "system", "content": "Distill the transcript into bullets."},
        {"role": "user", "content": "## Transcript\n- Alice: hi"},
    ]


def test_generate_with_empty_system_prompt_is_treated_as_absent(patched_session):
    """Empty string shouldn't emit a blank ``system`` message — some
    OpenAI-compatible gateways reject empty content."""
    provider = _make_provider()
    asyncio.run(provider.generate(prompt="x", system_prompt=""))
    payload = patched_session.last_payload
    assert payload["messages"] == [{"role": "user", "content": "x"}]


def test_llm_provider_wrapper_forwards_system_prompt(patched_session):
    """LLMProvider wrapper must pass system_prompt through to the
    concrete provider instead of silently dropping it."""
    from memory_layer.llm.llm_provider import LLMProvider

    wrapper = LLMProvider(
        provider_type="openai",
        model="gpt-4o-mini",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        temperature=0.0,
    )
    asyncio.run(
        wrapper.generate(
            prompt="body",
            system_prompt="sys",
        )
    )
    payload = patched_session.last_payload
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    assert payload["messages"][1] == {"role": "user", "content": "body"}

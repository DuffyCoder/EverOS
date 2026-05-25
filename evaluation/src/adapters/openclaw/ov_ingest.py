"""Direct OV SDK ingest for the OpenClaw benchmark adapter.

Mirrors the canonical OV bench ingest in
``/Data3/shutong.shan/memory/refs/OpenViking/benchmark/locomo/openclaw/import_to_ov.py``:

  for each LoCoMo session:
      ov_session_id = POST /api/v1/sessions
      for each msg:
          POST /api/v1/sessions/<sid>/messages   (role=user, parts=[{type:text,text:"[<speaker>]: <body>"}])
          ``<body>`` includes url / blip / query lines when present (see
          ``format_locomo_message_content_for_ingest`` in ``loaders.py``).
      commit_resp = POST /api/v1/sessions/<sid>/commit  (telemetry=True)
      poll GET /api/v1/tasks/<task_id> until status="completed"

Why not piggyback on the OpenClaw OV plugin's ``afterTurn`` hook (the path
session_bundle ingest used originally):

  * afterTurn only commits when ``pendingTokens >= cfg.commitTokenThreshold``
    (default 20000 tokens, configurable). A LoCoMo conversation accumulates
    enough tokens at full scale, but at smoke scale (~30 messages, ~1500
    tokens) the threshold is never crossed and OV ends up with zero archives
    even though messages were added. Reference benchmark sidesteps this by
    explicitly calling commit per session.

  * afterTurn's commit uses ``wait: false`` and the server's phase-2 memory
    extraction is asynchronous. QA can start before the extraction task is
    done, leading to empty retrieval. The reference benchmark polls
    ``get_task`` until ``completed`` before moving on.

This module is a host-side aiohttp wrapper because the adapter runs on the
host (not in a container) when ingesting; the OV server is reached via the
host loopback. The OV plugin inside the docker container is still the
retrieval path at QA time — its ``assemble`` hook reads from the same OV
server we just populated."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Iterable, Optional

import aiohttp


logger = logging.getLogger(__name__)


DEFAULT_TASK_POLL_TIMEOUT_SEC = 600.0
DEFAULT_TASK_POLL_INTERVAL_SEC = 1.0


class OVIngestError(RuntimeError):
    """Raised when OV REST returns a non-success response or a task fails."""


class OVIngestClient:
    """Minimal aiohttp wrapper for the OV REST endpoints used by ingest.

    Each method opens a request against a shared ``aiohttp.ClientSession``
    passed in by the caller, so request pooling stays intact across the
    create/add/commit cycle for one LoCoMo session.

    Tenant routing is via headers (``X-OpenViking-Account``,
    ``X-OpenViking-User``, ``X-OpenViking-Agent``). The OV server multiplexes
    on these; passing ``account_id`` + ``user_id`` from yaml ensures the
    SDK ingest writes to the same tenant the OpenClaw OV plugin reads from
    at QA time.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        account_id: str = "default",
        user_id: str = "default",
        agent_id: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.account_id = account_id
        self.user_id = user_id
        self.agent_id = agent_id  # may be None → uses OV server default

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["X-API-Key"] = self.api_key
        if self.agent_id:
            h["X-OpenViking-Agent"] = self.agent_id
        if self.account_id:
            h["X-OpenViking-Account"] = self.account_id
        if self.user_id:
            h["X-OpenViking-User"] = self.user_id
        return h

    async def create_session(
        self, http: aiohttp.ClientSession, session_id: Optional[str] = None
    ) -> str:
        body: dict[str, Any] = {}
        if session_id:
            body["session_id"] = session_id
        async with http.post(
            f"{self.base_url}/api/v1/sessions",
            headers=self._headers(),
            json=body,
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise OVIngestError(
                    f"create_session failed: HTTP {resp.status} {text[:300]}"
                )
            data = _parse_response_envelope(text)
            sid = data.get("session_id") if isinstance(data, dict) else None
            if not sid:
                raise OVIngestError(
                    f"create_session returned no session_id: {text[:300]}"
                )
            return str(sid)

    async def add_message(
        self,
        http: aiohttp.ClientSession,
        session_id: str,
        *,
        role: str,
        text: str,
        created_at: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": role,
            "parts": [{"type": "text", "text": text}],
        }
        if created_at:
            payload["created_at"] = created_at
        async with http.post(
            f"{self.base_url}/api/v1/sessions/{session_id}/messages",
            headers=self._headers(),
            json=payload,
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise OVIngestError(
                    f"add_message {session_id}: HTTP {resp.status} {body[:300]}"
                )
            return _parse_response_envelope(body)

    async def commit_session(
        self,
        http: aiohttp.ClientSession,
        session_id: str,
        *,
        telemetry: bool = True,
    ) -> dict[str, Any]:
        async with http.post(
            f"{self.base_url}/api/v1/sessions/{session_id}/commit",
            headers=self._headers(),
            json={"telemetry": telemetry},
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                raise OVIngestError(
                    f"commit_session {session_id}: HTTP {resp.status} {body[:300]}"
                )
            return _parse_response_envelope(body)

    async def get_task(
        self, http: aiohttp.ClientSession, task_id: str
    ) -> Optional[dict[str, Any]]:
        async with http.get(
            f"{self.base_url}/api/v1/tasks/{task_id}",
            headers=self._headers(),
        ) as resp:
            body = await resp.text()
            if resp.status == 404:
                return None
            if resp.status >= 400:
                raise OVIngestError(
                    f"get_task {task_id}: HTTP {resp.status} {body[:300]}"
                )
            return _parse_response_envelope(body)

    async def wait_for_task(
        self,
        http: aiohttp.ClientSession,
        task_id: str,
        *,
        timeout_sec: float = DEFAULT_TASK_POLL_TIMEOUT_SEC,
        interval_sec: float = DEFAULT_TASK_POLL_INTERVAL_SEC,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_sec
        last_status = "unknown"
        while time.monotonic() < deadline:
            task = await self.get_task(http, task_id)
            if task is None:
                last_status = "not_found"
            else:
                last_status = (task.get("status") or "unknown").lower()
                if last_status == "completed":
                    return task
                if last_status in ("failed", "cancelled"):
                    raise OVIngestError(
                        f"task {task_id} {last_status}: {str(task)[:300]}"
                    )
            await asyncio.sleep(interval_sec)
        raise OVIngestError(
            f"task {task_id} did not complete within {timeout_sec}s "
            f"(last status: {last_status})"
        )


def _parse_response_envelope(text: str) -> dict[str, Any]:
    """Decode an OV envelope.

    OV responses follow ``{"status": "ok", "result": <obj>, "error": null,
    "telemetry": <obj>|null}``. Some endpoints return the raw object inline.
    Return ``result`` when present, the parsed body otherwise.
    """
    import json
    try:
        body = json.loads(text)
    except json.JSONDecodeError as err:
        raise OVIngestError(f"non-JSON response: {text[:300]}") from err
    if not isinstance(body, dict):
        return {"raw": body}
    if body.get("status") == "error":
        err = body.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise OVIngestError(f"OV error: {msg or text[:300]}")
    if "result" in body and isinstance(body["result"], dict):
        result = dict(body["result"])
        if "telemetry" in body:
            result.setdefault("_telemetry", body["telemetry"])
        return result
    return body


async def ingest_session_to_ov(
    client: OVIngestClient,
    http: aiohttp.ClientSession,
    *,
    session_key: str,
    messages: Iterable[Any],
    speaker_format: str = "[{speaker}]: {text}",
    ov_session_id: Optional[str] = None,
    commit_after: bool = True,
    wait_task: bool = True,
) -> dict[str, Any]:
    """Push one LoCoMo session into OV: create + add × N + commit (+ wait task).

    ``messages`` is an iterable of ``Message``-like objects (must expose
    ``speaker_name``, ``content``, ``timestamp``). The output dict carries
    ``ov_session_id``, ``task_id``, terminal task status, and counts so the
    caller can emit a single ``ov_session_ingested`` event line.

    ``ov_session_id`` lets the caller reuse one OV session across multiple
    LoCoMo sub-sessions (mirrors official openclaw-eval's same-user pattern
    so pendingTokens accumulates and assemble can resolve archive segments
    at QA time). When ``None``, a fresh OV session is created.

    ``commit_after`` controls whether commit_session is invoked at the end.
    When False, the caller is expected to commit later (or rely on
    plugin-side afterTurn auto-commit once pendingTokens crosses threshold).

    ``wait_task``: when ``True`` (default), block on the phase2
    fact-extraction task before returning so memories_extracted /
    token_usage are populated. When ``False``, fire-and-forget — return
    immediately with the task_id and ``status="queued_phase2"``; the
    caller is responsible for waiting (e.g. batched wait at end-of-conv)
    so the next pipeline stage doesn't start before fact-extract
    finishes. The fire-and-forget path lets multiple LoCoMo sub-sessions
    queue phase2 work onto the OV server in parallel rather than
    serializing one LLM call at a time.
    """
    started = time.perf_counter()
    msg_list = [m for m in messages if (m.content or "").strip()]
    if not msg_list:
        return {
            "session_key": session_key,
            "ov_session_id": ov_session_id,
            "task_id": None,
            "status": "skipped_no_messages",
            "num_messages": 0,
            "duration_sec": 0.0,
        }

    if ov_session_id is None:
        ov_session_id = await client.create_session(http)

    for msg in msg_list:
        speaker = (msg.speaker_name or "unknown").strip()
        text = msg.content.strip()
        line = speaker_format.format(speaker=speaker, text=text)
        created_at = (
            msg.timestamp.isoformat()
            if getattr(msg, "timestamp", None) is not None
            else None
        )
        await client.add_message(
            http, ov_session_id,
            role="user",
            text=line,
            created_at=created_at,
        )

    if not commit_after:
        return {
            "session_key": session_key,
            "ov_session_id": ov_session_id,
            "task_id": None,
            "status": "queued",
            "num_messages": len(msg_list),
            "duration_sec": round(time.perf_counter() - started, 3),
        }

    commit_resp = await client.commit_session(http, ov_session_id, telemetry=True)
    task_id = commit_resp.get("task_id")
    commit_status = commit_resp.get("status")

    final: dict[str, Any] = {
        "session_key": session_key,
        "ov_session_id": ov_session_id,
        "task_id": task_id,
        "commit_status": commit_status,
        "num_messages": len(msg_list),
    }

    if task_id and not wait_task:
        # Fire-and-forget: caller will batch-wait this task_id later
        # (e.g. at end-of-conv ingest). Keeps the per-session call cheap
        # so multiple sessions can be added in parallel without
        # serializing on each phase2 LLM call.
        final["status"] = "queued_phase2"
        final["duration_sec"] = round(time.perf_counter() - started, 3)
        return final

    if task_id:
        try:
            task = await client.wait_for_task(http, task_id)
            result = task.get("result") if isinstance(task, dict) else {}
            tu = (result or {}).get("token_usage") or {}
            final["status"] = task.get("status") if isinstance(task, dict) else None
            final["memories_extracted"] = (result or {}).get(
                "memories_extracted", {}
            )
            final["embedding_tokens"] = (
                (tu.get("embedding") or {}).get("total")
                if isinstance(tu.get("embedding"), dict)
                else tu.get("embedding")
            )
            final["llm_total_tokens"] = (
                (tu.get("llm") or {}).get("total")
                if isinstance(tu.get("llm"), dict)
                else tu.get("llm")
            )
        except OVIngestError as err:
            final["status"] = "task_failed"
            final["error"] = str(err)
    else:
        final["status"] = commit_status or "no_task_id"

    final["duration_sec"] = round(time.perf_counter() - started, 3)
    return final

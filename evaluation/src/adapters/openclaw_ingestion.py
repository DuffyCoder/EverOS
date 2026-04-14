"""
Session-bucketed markdown rendering + LLM-driven flush for OpenClaw.

This is the "faithful" ingest path - closer to what OpenClaw actually does
at runtime than the raw-transcript dump in bench/openclaw_adapter.py.

Two modes controlled by config["openclaw"]["flush_mode"]:

* ``disabled``: dump the raw session transcript as markdown bullets. Matches
  v0.1 / v0.2 behaviour exactly so numbers stay comparable.

* ``shared_llm``: send each session transcript through the **framework-
  side** LLM (same provider used for the answer prompt) with a prompt
  modelled on OpenClaw's ``buildMemoryFlushPlan`` (extensions/memory-core/
  src/flush-plan.ts). This is an APPROXIMATION of OpenClaw's production
  selective-retention behaviour, not a faithful reproduction of it:
    - OpenClaw's real flush runs mid-turn inside the agent runner with
      the agent's own LLM config, triggered by a token-budget heuristic.
    - We do it once per session at ingest time with the benchmark's LLM
      provider, and OpenClaw's own ``compaction.memoryFlush.enabled`` is
      kept OFF so search never triggers a second flush.
  Called ``shared_llm`` to make the divergence visible in config.

Files are written as ``memory/session-<SX>-<YYYY-MM-DD>.md`` so OpenClaw's
FTS scan picks them up, and so ``source_sessions`` projection downstream
can read the session id straight out of the path.
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from pathlib import Path
from typing import Awaitable, Callable, Optional

from evaluation.src.adapters.openclaw_manifest import project_message_id_to_session_id
from evaluation.src.core.data_models import Conversation, Message


logger = logging.getLogger(__name__)

# Fallback prompts used only when a native OpenClaw flush plan is not
# available (stub mode / bridge unreachable). In the happy path the plan
# is fetched via ``build_flush_plan`` from OpenClaw's own memory-core and
# passed in as ``flush_plan`` - see openclaw_adapter._ingest_conversation.
_FALLBACK_FLUSH_SYSTEM_PROMPT = (
    "You are OpenClaw's memory compaction agent. Distill the SESSION TRANSCRIPT "
    "into retention-worthy memories before the conversation context is compacted.\n"
    "\n"
    "Keep concrete facts, decisions, preferences, dates, numbers, names, places, "
    "and any contradictions. Drop greetings, filler, and repeated content.\n"
    "\n"
    "Output: a single markdown body, short bullet points, no preface or epilogue."
)

_FALLBACK_FLUSH_USER_PROMPT = (
    "Produce the distilled memories for this session now; output markdown only."
)

_DEFAULT_SILENT_TOKEN = "NO_REPLY"

_SESSION_CONTEXT_TEMPLATE = (
    "## Session metadata\n"
    "- session_id: {session_id}\n"
    "- date: {session_date}\n"
    "- speakers: {speakers}\n"
    "- message_count: {message_count}\n"
    "\n"
    "## Session transcript\n"
    "{transcript}\n"
)


def bucket_conversation_by_session(
    conversation: Conversation,
) -> "OrderedDict[str, list[Message]]":
    """Group messages into sessions preserving first-seen order.

    Messages without ``metadata['dia_id']`` are silently skipped: they cannot
    be projected to a session id and would leak into a bucket without a
    meaningful label.
    """
    buckets: "OrderedDict[str, list[Message]]" = OrderedDict()
    for msg in conversation.messages:
        dia_id = msg.metadata.get("dia_id")
        if not dia_id:
            continue
        try:
            sid = project_message_id_to_session_id(dia_id)
        except ValueError:
            continue
        buckets.setdefault(sid, []).append(msg)
    return buckets


def session_date(messages: list[Message], fallback: str = "1970-01-01") -> str:
    for msg in messages:
        if msg.timestamp is not None:
            return msg.timestamp.strftime("%Y-%m-%d")
    return fallback


def render_session_transcript(messages: list[Message]) -> str:
    """Render a session's messages as a bullet list for the flush prompt."""
    lines = []
    for msg in messages:
        ts = msg.timestamp.strftime("%H:%M") if msg.timestamp is not None else ""
        prefix = f"[{ts}] " if ts else ""
        lines.append(f"- {prefix}**{msg.speaker_name}**: {msg.content}")
    return "\n".join(lines)


def session_markdown_filename(session_id: str, date_str: str) -> str:
    return f"session-{session_id}-{date_str}.md"


def render_raw_session_markdown(session_id: str, messages: list[Message]) -> str:
    """disabled-mode renderer. Matches v0.1 layout but with session header."""
    body = render_session_transcript(messages)
    header = f"# {session_id}\n\n"
    return header + body + "\n"


async def render_flushed_session_markdown(
    session_id: str,
    messages: list[Message],
    llm_generate: Callable[[str, str], Awaitable[str]],
    flush_plan: Optional[dict] = None,
) -> tuple[str, bool]:
    """shared_llm-mode renderer. Uses the framework LLM to distil bullets.

    When ``flush_plan`` is provided (fetched from OpenClaw via the
    ``build_flush_plan`` bridge command), the system + user prompt come
    from OpenClaw's own ``buildMemoryFlushPlan`` output so the distil
    behaviour matches upstream. The framework LLM still executes the
    prompt (Option A scope: prompt is native, executor is shared).

    Returns (markdown_body, silent). When ``silent`` is True the flush
    agent replied with its NO_REPLY sentinel and there is nothing to
    retain for this session; callers should still write an empty file so
    downstream session-level projections stay consistent.
    """
    plan = flush_plan or {}
    system_prompt = plan.get("system_prompt") or _FALLBACK_FLUSH_SYSTEM_PROMPT
    plan_user_prompt = plan.get("prompt") or _FALLBACK_FLUSH_USER_PROMPT
    silent_token = plan.get("silent_token") or _DEFAULT_SILENT_TOKEN

    speakers = sorted({m.speaker_name for m in messages})
    context_block = _SESSION_CONTEXT_TEMPLATE.format(
        session_id=session_id,
        session_date=session_date(messages),
        speakers=", ".join(speakers) or "(unknown)",
        message_count=len(messages),
        transcript=render_session_transcript(messages),
    )
    # OpenClaw's plan prompt trails instructions; we prepend the session
    # context so the LLM has something concrete to distil from.
    user_prompt = context_block + "\n" + plan_user_prompt

    distilled = (await llm_generate(system_prompt, user_prompt)).strip()
    if distilled.startswith(silent_token):
        # Honour upstream semantics: nothing worth retaining. We still
        # leave a stub markdown so the session-bucketed filename stays
        # present for source_sessions projection.
        body = (
            f"# {session_id}\n\n"
            f"<!-- openclaw flush returned {silent_token}: "
            f"no durable memory for this session -->\n"
        )
        return body, True

    if not distilled:
        distilled = render_session_transcript(messages)
    return f"# {session_id}\n\n" + distilled + "\n", False


async def write_session_files(
    conversation: Conversation,
    memory_dir: Path,
    flush_mode: str,
    llm_generate: Optional[Callable[[str, str], Awaitable[str]]] = None,
    flush_plan: Optional[dict] = None,
) -> list[dict]:
    """Write one markdown file per session and return metadata rows.

    ``flush_plan`` (when provided) is the OpenClaw native plan returned by
    ``build_flush_plan`` bridge command - system_prompt / prompt /
    silent_token. It is only consulted for ``shared_llm`` mode.

    Returned rows contain session_id / path_rel / message_count / flush_mode
    plus ``silent`` (True when OpenClaw's flush agent replied with its
    NO_REPLY sentinel for this session).
    """
    buckets = bucket_conversation_by_session(conversation)
    if not buckets:
        return []

    memory_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for sid, messages in buckets.items():
        date_str = session_date(messages)
        filename = session_markdown_filename(sid, date_str)
        rel = f"memory/{filename}"
        abs_path = memory_dir / filename
        silent = False

        if flush_mode == "shared_llm":
            if llm_generate is None:
                raise ValueError("shared_llm flush requires llm_generate callable")
            body, silent = await render_flushed_session_markdown(
                sid, messages, llm_generate, flush_plan=flush_plan,
            )
        elif flush_mode == "disabled":
            body = render_raw_session_markdown(sid, messages)
        else:
            raise ValueError(f"unsupported flush_mode: {flush_mode!r}")

        abs_path.write_text(body, encoding="utf-8")
        rows.append(
            {
                "session_id": sid,
                "path_rel": rel,
                "date": date_str,
                "message_count": len(messages),
                "flush_mode": flush_mode,
                "silent": silent,
            }
        )
        logger.debug(
            "ingested session %s -> %s (flush_mode=%s, bytes=%d, silent=%s)",
            sid,
            rel,
            flush_mode,
            len(body.encode("utf-8")),
            silent,
        )
    return rows


def session_id_from_path(path_rel: str) -> Optional[str]:
    """Recover the session id from a file path returned by OpenClaw search.

    Matches session-<SX>-<date>.md; anything else returns None so callers
    can fall back to other projections.
    """
    if not path_rel:
        return None
    name = Path(path_rel).name
    if not name.startswith("session-"):
        return None
    # session-S3-2023-06-09.md -> parts = ["session", "S3", "2023", "06", "09.md"]
    parts = name.split("-")
    if len(parts) < 3:
        return None
    candidate = parts[1]
    if candidate.startswith("S") and candidate[1:].isdigit():
        return candidate
    return None

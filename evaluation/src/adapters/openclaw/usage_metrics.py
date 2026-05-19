"""Token metrics for OpenClaw ``agent_local`` answer runs.

Measurement goal (aligned with openclaw-openviking-eval ``usage.input_tokens``):
billable **prompt-side** tokens per QA — the context charged on each LLM hop,
not completion/output tokens.

Resolution order
----------------
**Tier A — provider usage** (accurate when sophnet returns stream usage):

1. ``last_call_usage`` / ``meta.agentMeta.lastCallUsage`` from the bridge
2. Last assistant ``usage`` in ``state/.../sessions/<conv>__<qid>.jsonl``
3. Sum of assistant ``usage`` rows in the same session jsonl

Tier A requires OpenClaw to request ``stream_options.include_usage`` (yaml
``compat.supportsUsageInStreaming: true`` plus docker
``_patch_streaming_usage_compat`` on ``openclaw.docker.json``).

**Tier B — session transcript estimate** (when provider reports zeros or omits
usage): tiktoken over system/tool overhead plus all ``user`` / ``assistant`` /
``toolResult`` text before each assistant hop.

**Tier C — coarse prompt estimate** (only if session jsonl is missing): system +
tool schema + harness question string (does not include injected memories).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from evaluation.src.core.stages.answer_stage import estimate_tokens

# Provenance labels stored in answer metadata and events.jsonl.
SourceKind = Literal[
    "last_call_usage",
    "session_jsonl_last",
    "session_jsonl_sum",
    "session_transcript_estimate",
    "session_transcript_sum",
    "prompt_estimate",
    "none",
]


@dataclass(frozen=True)
class TokenMetrics:
    final_context_tokens: int
    final_context_tokens_source: SourceKind
    agent_run_total_input_tokens: int
    agent_run_total_input_tokens_source: SourceKind

    def as_metadata(self) -> dict[str, Any]:
        return {
            "final_context_tokens": self.final_context_tokens,
            "final_context_tokens_source": self.final_context_tokens_source,
            "agent_run_total_input_tokens": self.agent_run_total_input_tokens,
            "agent_run_total_input_tokens_source": self.agent_run_total_input_tokens_source,
        }


def usage_input_tokens(usage: Any) -> Optional[int]:
    """Normalize OpenClaw / OpenAI usage dicts to prompt-side token count."""
    if not isinstance(usage, dict):
        return None

    prompt_tokens = usage.get("prompt_tokens")
    if isinstance(prompt_tokens, (int, float)) and prompt_tokens > 0:
        return int(prompt_tokens)

    input_val = 0
    for key in ("input", "input_tokens"):
        val = usage.get(key)
        if isinstance(val, (int, float)) and val > 0:
            input_val = int(val)
            break

    cache_read = 0
    for key in ("cacheRead", "cache_read", "cache_read_input_tokens"):
        val = usage.get(key)
        if isinstance(val, (int, float)) and val > 0:
            cache_read = int(val)

    cache_write = 0
    for key in ("cacheWrite", "cache_write", "cache_creation_input_tokens"):
        val = usage.get(key)
        if isinstance(val, (int, float)) and val > 0:
            cache_write = int(val)

    total = input_val + cache_read + cache_write
    return total if total > 0 else None


def session_jsonl_path(state_dir: Path, session_id: str) -> Path:
    return state_dir / "agents" / "main" / "sessions" / f"{session_id}.jsonl"


def resolve_state_dir_host(
    sandbox: dict,
    container_state_dir: Optional[str] = None,
) -> Path:
    """Map bridge ``state_dir`` to a host path under the conversation workspace."""
    workspace = Path(sandbox["workspace_dir"])
    if container_state_dir:
        rel = container_state_dir.strip()
        if rel.startswith("/workspace/"):
            rel = rel[len("/workspace/") :]
        elif rel == "/workspace":
            rel = ""
        elif rel.startswith("/"):
            rel = rel.lstrip("/")
        return workspace / rel if rel else workspace / "state"
    native = sandbox.get("native_store_dir")
    if native and Path(native).exists():
        return Path(native)
    state_under_ws = workspace / "state"
    if state_under_ws.exists():
        return state_under_ws
    return Path(native) if native else state_under_ws


def _bridge_last_call_usage(resp: dict) -> Any:
    last_usage = resp.get("last_call_usage")
    if last_usage is not None:
        return last_usage
    raw_meta = (resp.get("raw") or {}).get("meta") or {}
    agent_meta = raw_meta.get("agentMeta") or {}
    return agent_meta.get("lastCallUsage")


def _iter_assistant_usage_counts(session_path: Path) -> list[int]:
    if not session_path.is_file():
        return []
    counts: list[int] = []
    for line in session_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "message":
            continue
        msg = entry.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        count = usage_input_tokens(msg.get("usage"))
        if count is not None:
            counts.append(count)
    return counts


def _resolve_provider_tier(
    resp: dict,
    session_path: Path,
) -> tuple[Optional[int], SourceKind, Optional[int], SourceKind]:
    """Tier A: bridge last hop, then session jsonl per-hop usage."""
    final: Optional[int] = None
    final_source: SourceKind = "none"
    total: Optional[int] = None
    total_source: SourceKind = "none"

    from_bridge = usage_input_tokens(_bridge_last_call_usage(resp))
    if from_bridge is not None:
        final = from_bridge
        final_source = "last_call_usage"

    hop_counts = _iter_assistant_usage_counts(session_path)
    if hop_counts:
        if final is None:
            final = hop_counts[-1]
            final_source = "session_jsonl_last"
        total = sum(hop_counts)
        total_source = "session_jsonl_sum"

    return final, final_source, total, total_source


def _system_prompt_report(resp: dict) -> dict[str, Any]:
    raw = resp.get("raw") or {}
    meta = raw.get("meta") or {}
    report = meta.get("systemPromptReport") or {}
    return report if isinstance(report, dict) else {}


def estimate_prompt_overhead_tokens(resp: dict) -> int:
    """Fixed per-hop overhead: system prompt + tool JSON schemas (tiktoken proxy)."""
    system_chars = int(resp.get("system_prompt_chars") or 0)
    report = _system_prompt_report(resp)
    system_prompt = report.get("systemPrompt") or {}
    if not system_chars and isinstance(system_prompt.get("chars"), (int, float)):
        system_chars = int(system_prompt["chars"])
    tools = report.get("tools") or {}
    schema_chars = int(tools.get("schemaChars") or 0)
    return estimate_tokens((" " * system_chars) + (" " * schema_chars))


def estimate_coarse_prompt_tokens(resp: dict, query: str) -> int:
    """Tier C: overhead + harness question only (no transcript)."""
    return estimate_prompt_overhead_tokens(resp) + estimate_tokens(query or "")


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif block_type == "toolCall":
            name = block.get("name") or "tool"
            args = block.get("arguments")
            if isinstance(args, str):
                parts.append(f"[toolCall {name}] {args}")
            else:
                parts.append(
                    f"[toolCall {name}] {json.dumps(args, ensure_ascii=False)}"
                )
    return "\n".join(parts)


def _load_session_messages(session_path: Path) -> list[dict[str, Any]]:
    if not session_path.is_file():
        return []
    messages: list[dict[str, Any]] = []
    for line in session_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "message":
            continue
        msg = entry.get("message")
        if isinstance(msg, dict) and msg.get("role"):
            messages.append(msg)
    return messages


def estimate_session_transcript_input_tokens(
    session_path: Path,
    resp: dict,
) -> tuple[Optional[int], Optional[int]]:
    """Tier B: per-hop tiktoken before each assistant message.

    Models each LLM call as ``overhead + tokens(transcript_so_far)`` where
    transcript includes prior user, assistant, and toolResult content.
    Returns ``(final_hop, sum_of_hops)``.
    """
    messages = _load_session_messages(session_path)
    if not messages:
        return None, None

    overhead = estimate_prompt_overhead_tokens(resp)
    prefix_parts: list[str] = []
    hop_tokens: list[int] = []

    for message in messages:
        role = message.get("role")
        if role == "assistant":
            hop_tokens.append(
                overhead + estimate_tokens("\n".join(prefix_parts)),
            )
            text = _message_text(message)
            if text:
                prefix_parts.append(text)
        elif role in ("user", "toolResult"):
            text = _message_text(message)
            if text:
                prefix_parts.append(text)

    if not hop_tokens:
        return None, None
    return hop_tokens[-1], sum(hop_tokens)


def _resolve_transcript_tier(
    session_path: Path,
    resp: dict,
) -> tuple[Optional[int], SourceKind, Optional[int], SourceKind]:
    final, total = estimate_session_transcript_input_tokens(session_path, resp)
    final_source: SourceKind = (
        "session_transcript_estimate" if final is not None else "none"
    )
    total_source: SourceKind = (
        "session_transcript_sum" if total is not None else "none"
    )
    return final, final_source, total, total_source


def compute_agent_run_token_metrics(
    resp: dict,
    session_path: Path,
    query: str,
) -> TokenMetrics:
    """Resolve final and total prompt-side tokens using tier A → B → C."""
    final, final_src, total, total_src = _resolve_provider_tier(resp, session_path)

    if final is None or total is None:
        t_final, t_final_src, t_total, t_total_src = _resolve_transcript_tier(
            session_path, resp,
        )
        if final is None and t_final is not None:
            final, final_src = t_final, t_final_src
        if total is None and t_total is not None:
            total, total_src = t_total, t_total_src

    if final is None:
        final = estimate_coarse_prompt_tokens(resp, query)
        final_src = "prompt_estimate"

    if total is None:
        total = final
        total_src = final_src if total_src == "none" else total_src

    return TokenMetrics(
        final_context_tokens=final,
        final_context_tokens_source=final_src,
        agent_run_total_input_tokens=total,
        agent_run_total_input_tokens_source=total_src,
    )


def extract_agent_run_token_metrics(
    resp: dict,
    state_dir_host: Path,
    session_id: str,
    query: str,
) -> dict[str, Any]:
    """Build per-QA token fields for answer metadata and events.jsonl."""
    session_path = session_jsonl_path(state_dir_host, session_id)
    return compute_agent_run_token_metrics(resp, session_path, query).as_metadata()


# Backward-compatible helpers for tests and callers.
def sum_session_usage_input(state_dir: Path, session_id: str) -> Optional[int]:
    counts = _iter_assistant_usage_counts(session_jsonl_path(state_dir, session_id))
    return sum(counts) if counts else None


def last_session_usage_input(state_dir: Path, session_id: str) -> Optional[int]:
    counts = _iter_assistant_usage_counts(session_jsonl_path(state_dir, session_id))
    return counts[-1] if counts else None


def estimate_prompt_input_tokens(resp: dict, query: str) -> int:
    return estimate_coarse_prompt_tokens(resp, query)

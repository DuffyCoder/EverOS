"""
OpenClaw adapter for the EverMemOS evaluation pipeline.

Wraps OpenClaw memory lifecycle (ingest / flush / index / search / get) via a
Node bridge, exposes the BaseAdapter surface, and emits session-level
retrieval traces + lifecycle diagnostics alongside the shared answer prompt.
Per-conversation sandboxes are isolated on disk and the Node bridge is
invoked with a per-sandbox env so concurrent conversations do not
collide.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, List, Optional

from evaluation.src.adapters.base import BaseAdapter
from evaluation.src.adapters.openclaw.ingestion import (
    bucket_conversation_by_session,
    session_id_from_path,
    write_session_files,
)
from evaluation.src.adapters.openclaw.manifest import (
    build_session_manifest,
    project_message_id_to_session_id,
)
from evaluation.src.adapters.openclaw.resolved_config import (
    build_openclaw_resolved_config,
)
from evaluation.src.adapters.openclaw.runtime import (
    arun_bridge,
    build_sandbox_paths,
)
from evaluation.src.adapters.openclaw.usage_metrics import (
    extract_agent_run_token_metrics,
    resolve_state_dir_host,
)
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult


logger = logging.getLogger(__name__)


_RUN_ID_LATEST_FILE = "LATEST"
_ARTIFACT_ROOT = "artifacts/openclaw"

# this file lives at evaluation/src/adapters/openclaw/adapter.py
_EVAL_DIR = Path(__file__).resolve().parents[3]
_BRIDGE_SCRIPT_PATH = _EVAL_DIR / "scripts" / "openclaw_eval_bridge.mjs"
_PROMPTS_YAML_PATH = _EVAL_DIR / "config" / "prompts.yaml"


_DEFAULT_ANSWER_PROMPT = (
    "You are a helpful assistant answering a question about a conversation.\n"
    "Use the memory snippets in CONTEXT to answer concisely (<=6 words when possible).\n"
    "If the context does not contain the answer, respond with \"No relevant information.\".\n\n"
    "# CONTEXT\n{context}\n\n# QUESTION\n{question}\n\n# ANSWER"
)


def _read_latest_session_final_text(sandbox: dict) -> Optional[str]:
    """Return the last assistant text from the sandbox's most recently
    archived agent session jsonl, or None if no usable jsonl is found.

    The openclaw agent runner archives one ``<uuid>.jsonl.<unix_ts>`` file
    per QA turn under ``sessions_dir``. The most recent file (by the
    fixed-width unix-ts suffix, equivalent to a name sort) corresponds
    to the agent_run that just finished. Using the name suffix instead
    of mtime survives later ``tar``/``cp -p`` rewrites of mtime; in
    production the two agree.

    Each line is a turn event; assistant text content lives in
    ``event.message.content[*].text`` for entries whose ``type=="message"``
    and ``message.role=="assistant"``. Tool-only turns produce no text and
    are skipped. The final assistant text is the agent's actual answer.
    """
    sessions_dir_str = sandbox.get("sessions_dir")
    if not sessions_dir_str:
        return None
    sessions_dir = Path(sessions_dir_str)
    if not sessions_dir.is_dir():
        return None
    candidates = list(sessions_dir.glob("*.jsonl.*"))
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.name)
    last_text = ""
    try:
        with latest.open() as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "message":
                    continue
                msg = ev.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                buf = ""
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            buf += c.get("text", "")
                elif isinstance(content, str):
                    buf = content
                if buf:
                    last_text = buf
    except OSError:
        return None
    return last_text.strip() if last_text else None


@register_adapter("openclaw")
class OpenClawAdapter(BaseAdapter):
    def __init__(self, config: dict, output_dir: Any = None):
        super().__init__(config)
        self.output_dir = output_dir
        self._prepared: bool = False
        self._run_id: Optional[str] = None
        self._openclaw_cfg: dict = dict(config.get("openclaw") or {})
        search_cfg = config.get("search", {}) or {}
        self.max_inflight_queries_per_conversation: int = int(
            search_cfg.get("max_inflight_queries_per_conversation", 1)
        )
        self._conversation_semaphores: dict[str, asyncio.Semaphore] = {}
        self._llm_provider = None  # lazy
        self._shared_prompt_template: Optional[str] = None
        # repo_path resolution: yaml > env (env is still honored as a
        # fallback so smoke tests that only set OPENCLAW_REPO_PATH keep
        # working). P0-2 makes the bridge honor the payload value
        # unconditionally so the yaml setting is no longer cosmetic.
        self._openclaw_repo_path: str = (
            (self._openclaw_cfg.get("repo_path") or "").strip()
        )
        # v0.7: per-conversation sandbox cache. Populated by both add()
        # and build_lazy_index() so answer_mode=agent_local can find
        # the bridge payload via _sandbox_for(conversation_id).
        self._sandbox_by_conversation_id: dict[str, dict] = {}
        # Per-QA metrics populated by agent_local runs; consumed by answer_stage.
        self._answer_metrics_by_qid: dict[str, dict] = {}

    def pop_answer_metrics(self, question_id: str) -> dict:
        """Return and clear token metrics recorded for the last agent_run."""
        return self._answer_metrics_by_qid.pop(question_id, {})

    def _record_agent_run_token_metrics(
        self,
        sandbox: dict,
        qid: str,
        resp: dict,
        query: str,
        *,
        session_id: Optional[str] = None,
        container_state_dir: Optional[str] = None,
    ) -> dict:
        conv_id = sandbox.get("conversation_id", "")
        sid = session_id or f"{conv_id}__{qid}"
        state_host = resolve_state_dir_host(sandbox, container_state_dir)
        metrics = extract_agent_run_token_metrics(
            resp, state_host, sid, query,
        )
        self._answer_metrics_by_qid[qid] = metrics
        return metrics

    def _emit_agent_run_complete(
        self,
        sandbox: dict,
        conv_id: str,
        qid: str,
        resp: dict,
        query: str,
        *,
        session_id: Optional[str] = None,
        container_state_dir: Optional[str] = None,
    ) -> str:
        token_metrics = self._record_agent_run_token_metrics(
            sandbox,
            qid,
            resp,
            query,
            session_id=session_id,
            container_state_dir=container_state_dir,
        )
        self._append_events(sandbox, [{
            "event": "agent_run_complete",
            "conversation_id": conv_id,
            "question_id": qid,
            "duration_ms": resp.get("duration_ms"),
            "stop_reason": resp.get("stop_reason"),
            "aborted": resp.get("aborted"),
            "tool_names": resp.get("tool_names"),
            "system_prompt_chars": resp.get("system_prompt_chars"),
            "reply_len": len(resp.get("reply") or ""),
            "forced_terminate": bool(resp.get("forced_terminate", False)),
            **token_metrics,
        }])

        # openclaw bridge returns the FIRST assistant text from the agent
        # loop. When the agent does preamble → tool_call → tool_result →
        # final_answer, that first text is the preamble ("Let me search
        # for ...") and the real answer lives in a later assistant turn.
        # Pull the LAST assistant text from the just-archived session jsonl
        # and prefer it over the bridge's reply.
        reply = (resp.get("reply") or "").strip()
        final = _read_latest_session_final_text(sandbox)
        if final and final != reply:
            self._append_events(sandbox, [{
                "event": "agent_reply_overridden_to_final",
                "conversation_id": conv_id, "question_id": qid,
                "first_len": len(reply),
                "final_len": len(final),
            }])
            return final
        return reply

    # ----------------------------------------------------------------- prepare
    async def prepare(
        self,
        conversations: List[Conversation],
        output_dir: Any = None,
        checkpoint_manager: Any = None,
        **kwargs,
    ) -> None:
        """Idempotent initialization.

        Pipeline currently doesn't call prepare() explicitly, so add() calls it
        internally. When a future pipeline wires prepare() in, this flag keeps
        initialization from running twice.
        """
        if self._prepared:
            return
        self._prepared = True
        self._prepared_conversation_ids = [c.conversation_id for c in conversations]
        logger.debug(
            "openclaw adapter prepared for %d conversations",
            len(self._prepared_conversation_ids),
        )

    # --------------------------------------------------------------------- add
    async def add(
        self,
        conversations: List[Conversation],
        output_dir: Any = None,
        checkpoint_manager: Any = None,
        **kwargs,
    ) -> dict:
        if not self._prepared:
            await self.prepare(
                conversations=conversations,
                output_dir=output_dir,
                checkpoint_manager=checkpoint_manager,
                **kwargs,
            )

        root_dir = self._resolve_run_root(output_dir or self.output_dir)
        run_id = root_dir.name
        conversations_map: dict[str, dict] = {}

        answer_mode = self._openclaw_cfg.get("answer_mode", "shared_llm")
        memory_mode = self._openclaw_cfg.get("memory_mode", "memory-core")
        # Conv-parallel ingest cap. Each conv consumes a few hundred MB
        # of host RAM (transcript buffers + sandbox state) and queues
        # phase2 tasks onto the OV server; 4 parallel conversations
        # comfortably fits the 16 GB cloud budget alongside the OV
        # server (~10 GB). Tunable via yaml openclaw.add_max_concurrent_convs.
        add_max_concurrent_convs = int(
            self._openclaw_cfg.get("add_max_concurrent_convs", 4)
        )
        sem = asyncio.Semaphore(add_max_concurrent_convs)

        async def handle_one_conv(conv):
            async with sem:
                sandbox = self._prepare_conversation_sandbox(root_dir, conv)
                t0 = time.perf_counter()
                try:
                    # _ingest_conversation is the single source of truth for
                    # how content arrives in the memory system. For
                    # session_bundle ingest it runs prebootstrap + per-session
                    # agent_run + transcript archive + index inline; for
                    # legacy disabled / shared_llm it writes markdown directly
                    # then runs index. Either way it leaves visibility_state
                    # at "ingested" (or "settled" if memory_mode=noop).
                    await self._ingest_conversation(sandbox, conv)

                    # noop mode disables memorySearch entirely, so the status
                    # check would always report settled=false with 0 files/
                    # chunks. Skip the flush/settle in that case.
                    if memory_mode == "noop":
                        sandbox["visibility_state"] = "settled"
                        self._append_events(sandbox, [{
                            "event": "flush_skipped",
                            "reason": "memory_mode=noop",
                        }])
                    else:
                        await self._flush_and_settle_if_needed(sandbox)
                        self._assert_visibility_contract(sandbox)

                    # Legacy LLM-backbone paths (disabled / shared_llm) need a
                    # post-ingest workspace bootstrap so answer-stage agents
                    # have AGENTS.md/SOUL.md/TOOLS.md when answer_mode=
                    # agent_local. The session_bundle path already did this
                    # inline (must run BEFORE per-session agent_run), so skip
                    # the second call.
                    flush_mode_cfg = sandbox.get("flush_mode", "shared_llm")
                    if (
                        answer_mode == "agent_local"
                        and flush_mode_cfg != "session_bundle"
                    ):
                        await self._prebootstrap_workspace(sandbox)
                except Exception as err:
                    sandbox["run_status"] = "failed"
                    self._write_handle(sandbox, add_summary={"error": str(err)})
                    logger.exception(
                        "openclaw ingest failed for %s", conv.conversation_id
                    )
                    raise
                add_latency_ms = (time.perf_counter() - t0) * 1000.0
                sandbox["run_status"] = "ready"
                self._write_handle(
                    sandbox,
                    add_summary={
                        "conversation_id": conv.conversation_id,
                        "add_latency_ms": add_latency_ms,
                        "visibility_state": sandbox.get("visibility_state"),
                        "visibility_mode": sandbox.get("visibility_mode"),
                    },
                )
                return conv.conversation_id, sandbox

        # asyncio.gather: each conversation ingests in its own task,
        # bounded by ``sem``. Mutations to conversations_map /
        # _sandbox_by_conversation_id below happen single-threaded after
        # all tasks complete, so dict mutations don't need extra locking.
        results = await asyncio.gather(*[handle_one_conv(c) for c in conversations])
        for cid, sb in results:
            conversations_map[cid] = sb
            # v0.7: persist sandbox so answer() can locate it without
            # the `index` argument (answer_stage doesn't pass index).
            self._sandbox_by_conversation_id[cid] = sb

        return {
            "type": "openclaw_sandboxes",
            "run_id": run_id,
            "root_dir": str(root_dir),
            "conversations": conversations_map,
        }

    # --------------------------------------------------------- build_lazy_index
    def build_lazy_index(
        self, conversations: List[Conversation], output_dir: Any
    ) -> dict:
        """Rebuild handle dict from disk (resume / skip-add path).

        v0.7: also populates ``self._sandbox_by_conversation_id`` so
        answer_mode=agent_local can find sandbox state via
        ``_sandbox_for(conv_id)`` even when add() was skipped.
        """
        root_dir = self._locate_existing_run_root(Path(output_dir))
        handles: dict[str, dict] = {}
        for conv in conversations:
            handle_path = root_dir / "conversations" / conv.conversation_id / "handle.json"
            if not handle_path.exists():
                continue
            handle = json.loads(handle_path.read_text())
            if handle.get("run_status") != "ready":
                continue
            # visibility_mode decides what visibility_state is acceptable:
            #   settled mode   -> only 'settled' (strict)
            #   eventual mode  -> 'indexed' or 'settled' (search re-syncs)
            mode = handle.get("visibility_mode")
            state = handle.get("visibility_state")
            if mode == "settled":
                if state != "settled":
                    continue
            else:
                if state not in ("indexed", "settled"):
                    continue
            handles[conv.conversation_id] = handle
            # v0.7: ALSO populate the in-memory sandbox map so
            # answer() / agent_local path can find it without going back
            # to disk on every call.
            self._sandbox_by_conversation_id[conv.conversation_id] = handle
        return {
            "type": "openclaw_sandboxes",
            "run_id": root_dir.name,
            "root_dir": str(root_dir),
            "conversations": handles,
        }

    # ---------------------------------------------------------------- search
    async def search(
        self, query: str, conversation_id: str, index: Any, **kwargs
    ) -> SearchResult:
        # v0.7: in agent_local mode the agent owns retrieval inside its
        # own loop; pipeline-level search would double-cost and possibly
        # mutate openclaw memory state via memorySearch.sync.onSearch.
        # Return a placeholder marked skipped=True so retrieval_metrics
        # / content_overlap suppress these samples instead of scoring 0.
        if self._openclaw_cfg.get("answer_mode") == "agent_local":
            return SearchResult(
                query=query,
                conversation_id=conversation_id,
                results=[],
                retrieval_metadata={
                    "system": "openclaw",
                    "skipped": True,
                    "reason": "agent_local_owns_retrieval",
                    "question_id": kwargs.get("question_id"),
                },
            )

        sandbox = index["conversations"][conversation_id]
        backend_mode = sandbox.get("backend_mode", self._openclaw_cfg.get("backend_mode", "hybrid"))
        retrieval_route = sandbox.get(
            "retrieval_route", self._openclaw_cfg.get("retrieval_route", "search_then_get")
        )
        top_k = int(self.config.get("search", {}).get("top_k", 30))

        semaphore = self._conversation_semaphores.setdefault(
            conversation_id,
            asyncio.Semaphore(self.max_inflight_queries_per_conversation),
        )

        scheduler_start = time.perf_counter()
        async with semaphore:
            scheduler_wait_ms = (time.perf_counter() - scheduler_start) * 1000.0
            retrieval_start = time.perf_counter()

            search_payload = {
                **self._bridge_base_payload(sandbox),
                "command": "search",
                "query": query,
                "top_k": top_k,
            }
            bridge_response = await arun_bridge(self._bridge_script_path(), search_payload)
            hits = list(bridge_response.get("hits") or [])

            if retrieval_route == "search_then_get":
                hits = await self._enrich_with_get(sandbox, hits)

            results = []
            context_parts = []
            for rank, hit in enumerate(hits, start=1):
                hit_meta = dict(hit.get("metadata") or {})
                source_sessions = hit_meta.get("source_sessions") or self._derive_source_sessions(hit)
                hit_meta["source_sessions"] = source_sessions
                snippet = hit.get("snippet", "")
                results.append(
                    {
                        "content": snippet,
                        "score": float(hit.get("score", 0.0)),
                        "metadata": {
                            **hit_meta,
                            "artifact_locator": hit.get("artifact_locator"),
                        },
                    }
                )
                if snippet:
                    context_parts.append(f"{rank}. {snippet}")

            retrieval_latency_ms = (time.perf_counter() - retrieval_start) * 1000.0
            retrieval_metadata = {
                "system": "openclaw",
                "top_k": top_k,
                "backend_mode": backend_mode,
                "retrieval_route": retrieval_route,
                "retrieval_latency_ms": retrieval_latency_ms,
                "scheduler_wait_ms": scheduler_wait_ms,
                "formatted_context": "\n\n".join(context_parts),
                "conversation_id": conversation_id,
            }
            return SearchResult(
                query=query,
                conversation_id=conversation_id,
                results=results,
                retrieval_metadata=retrieval_metadata,
            )

    async def _enrich_with_get(self, sandbox: dict, hits: list[dict]) -> list[dict]:
        """For search_then_get routes, fetch narrower content per artifact.

        We avoid mutating the hit in-place so upstream bridge tracing stays
        intact.
        """
        enriched = []
        for hit in hits:
            locator = hit.get("artifact_locator")
            if not locator:
                enriched.append(hit)
                continue
            get_payload = {
                **self._bridge_base_payload(sandbox),
                "command": "get",
                "artifact_locator": locator,
            }
            try:
                resp = await arun_bridge(self._bridge_script_path(), get_payload)
            except Exception as err:  # noqa: BLE001
                logger.warning("get failed for artifact %s: %s", locator, err)
                enriched.append(hit)
                continue
            snippet = resp.get("snippet") or hit.get("snippet", "")
            new_hit = dict(hit)
            new_hit["snippet"] = snippet
            enriched.append(new_hit)
        return enriched

    @staticmethod
    def _derive_source_sessions(hit: dict) -> list[str]:
        """Best-effort source_sessions derivation when the bridge didn't set it.

        Preference order:
          1. metadata.source_message_ids (projected one by one)
          2. artifact_locator.path_rel (matches session-SX-*.md layout)
        Falls back to empty list rather than raising - retrieval metrics are
        the only consumer and they treat missing sessions as a zero-recall hit.
        """
        raw_message_ids = hit.get("metadata", {}).get("source_message_ids") or []
        out: list[str] = []
        for mid in raw_message_ids:
            try:
                out.append(project_message_id_to_session_id(mid))
            except ValueError:
                continue
        if out:
            return sorted(set(out))

        locator = hit.get("artifact_locator") or {}
        sid = session_id_from_path(locator.get("path_rel") or "")
        return [sid] if sid else []

    # ---------------------------------------------------------------- answer
    async def answer(self, query: str, context: str, **kwargs) -> str:
        """Generate an answer for ``query``.

        v0.7 dispatch by ``openclaw.answer_mode``:
          - ``shared_llm`` (default, Path A compat): build a context+question
            prompt and call evermemos's own LLM provider. Backwards
            compatible with v0.6 behavior.
          - ``agent_local`` (Path B): drive a real ``openclaw agent --local``
            run via the bridge. Per-QA session-id keeps chat history
            isolated; memory state is shared across QAs of the same conv
            because /workspace/memory is per-conv.
        """
        answer_mode = self._openclaw_cfg.get("answer_mode", "shared_llm")
        conv_id = kwargs.get("conversation_id")
        qid = kwargs.get("question_id")

        if answer_mode == "agent_local":
            if not conv_id or not qid:
                logger.warning(
                    "answer_mode=agent_local requires conversation_id+question_id; "
                    "got conv_id=%r qid=%r. Falling back to shared_llm.",
                    conv_id, qid,
                )
            else:
                return await self._generate_answer_via_agent(query, conv_id, qid)

        prompt = self._shared_answer_prompt().format(context=context, question=query)
        return await self._generate_answer(prompt)

    # v0.7: Path B answer path - drives real openclaw agent loop.
    async def _generate_answer_via_agent(
        self, query: str, conv_id: str, qid: str
    ) -> str:
        sandbox = self._sandbox_for(conv_id)
        agent_timeout = int(self._openclaw_cfg.get("agent_timeout_seconds", 180))
        payload = {
            **self._bridge_base_payload(sandbox),
            "command": "agent_run",
            "session_id": f"{conv_id}__{qid}",   # v0.6: per-QA isolation
            "message": query,
            "timeout_seconds": agent_timeout,
        }
        try:
            resp = await arun_bridge(
                self._bridge_script_path(),
                payload,
                timeout=float(agent_timeout) + 30.0,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning("agent_run bridge call failed for %s/%s: %s",
                           conv_id, qid, err)
            self._append_events(sandbox, [{
                "event": "agent_run_failed",
                "conversation_id": conv_id, "question_id": qid,
                "error": str(err),
            }])
            return ""

        if not resp.get("ok"):
            err = resp.get("error", "")
            logger.warning("agent_run failed for %s/%s: %s", conv_id, qid, err)
            self._append_events(sandbox, [{
                "event": "agent_run_failed",
                "conversation_id": conv_id, "question_id": qid,
                "error": err,
            }])
            return ""

        # v0.7 D4 fix: openclaw subprocess exit=0 doesn't mean the agent
        # loop succeeded. When the LLM call inside the agent fails (e.g.
        # provider rate limit), openclaw still returns a "graceful" reply
        # like "API rate limit reached" and sets stop_reason="error".
        # Treat this as adapter failure so it does not pollute accuracy
        # metrics with fake wrong answers.
        if resp.get("stop_reason") == "error":
            reply_excerpt = (resp.get("reply") or "")[:200]
            logger.warning(
                "agent_run completed but stop_reason=error for %s/%s; "
                "reply: %s", conv_id, qid, reply_excerpt,
            )
            self._append_events(sandbox, [{
                "event": "agent_run_internal_error",
                "conversation_id": conv_id, "question_id": qid,
                "reply_excerpt": reply_excerpt,
                "duration_ms": resp.get("duration_ms"),
            }])
            return ""

        return self._emit_agent_run_complete(
            sandbox, conv_id, qid, resp, query,
        )

    # v0.7: per-conv sandbox lookup, populated by add() and build_lazy_index()
    def _sandbox_for(self, conversation_id: str) -> dict:
        sandbox = self._sandbox_by_conversation_id.get(conversation_id)
        if sandbox is None:
            raise RuntimeError(
                f"No sandbox found for conversation_id={conversation_id!r}. "
                f"add() or build_lazy_index() must run before answer()."
            )
        return sandbox

    # v0.7: BaseAdapter.get_answer_timeout() override
    def get_answer_timeout(self) -> float:
        """Negotiate longer timeout for agent_local; default for shared_llm.

        agent_local can legitimately run 60-120s on complex prompts, so we
        pass agent_timeout_seconds + 30s margin. shared_llm (single LLM
        call) is fine with the historical 120s default.
        """
        if self._openclaw_cfg.get("answer_mode") == "agent_local":
            return float(self._openclaw_cfg.get("agent_timeout_seconds", 180)) + 30.0
        return 120.0

    # v0.7: pre-bootstrap workspace files via dummy agent run (retry + raise)
    async def _prebootstrap_workspace(self, sandbox: dict) -> None:
        """Run a dummy ``agent --local`` to write AGENTS.md/SOUL.md/etc.

        Without this, the answer-stage 50-worker semaphore can have
        multiple concurrent first-runs racing on the same workspace dir.
        Hard guard (raise on failure) — better to fail this conv at add()
        than silently corrupt state in answer phase.
        """
        last_error: Optional[str] = None
        for attempt in range(3):
            payload = {
                **self._bridge_base_payload(sandbox),
                "command": "agent_run",
                "session_id": f"{sandbox.get('conversation_id', 'unknown')}__bootstrap",
                "message": "Reply with: BOOTSTRAP_OK",
                "timeout_seconds": 60,
            }
            try:
                resp = await arun_bridge(
                    self._bridge_script_path(), payload, timeout=90.0
                )
                if resp.get("ok"):
                    last_error = None
                    break
                last_error = resp.get("error", "")
            except Exception as err:  # noqa: BLE001
                last_error = str(err)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s

        if last_error:
            raise RuntimeError(
                f"prebootstrap agent_run failed for "
                f"{sandbox.get('conversation_id')!r} after 3 attempts: {last_error}"
            )

        # Hard verify expected workspace files were written. If openclaw
        # changes its bootstrap layout upstream, fail loudly here rather
        # than continue and corrupt answer phase.
        ws = Path(sandbox["workspace_dir"])
        expected = ["AGENTS.md", "SOUL.md", "TOOLS.md"]
        missing = [n for n in expected if not (ws / n).exists()]
        if missing:
            raise RuntimeError(
                f"workspace bootstrap files missing for "
                f"{sandbox.get('conversation_id')!r}: {missing}. "
                f"openclaw agent --local did not write expected files."
            )

        self._append_events(sandbox, [{
            "event": "prebootstrap_complete",
            "conversation_id": sandbox.get("conversation_id"),
            "bootstrap_files": expected,
        }])

    async def _generate_answer(self, prompt: str) -> str:
        provider = self._get_llm_provider()
        result = await provider.generate(prompt=prompt, temperature=0)
        if "FINAL ANSWER:" in result:
            parts = result.split("FINAL ANSWER:")
            result = parts[1].strip() if len(parts) > 1 else result.strip()
        return result.strip()

    def _get_llm_provider(self):
        if self._llm_provider is not None:
            return self._llm_provider
        from memory_layer.llm.llm_provider import LLMProvider

        llm_cfg = self.config.get("llm", {}) or {}
        self._llm_provider = LLMProvider(
            provider_type=llm_cfg.get("provider", "openai"),
            model=llm_cfg.get("model", "gpt-4o-mini"),
            api_key=llm_cfg.get("api_key", ""),
            base_url=llm_cfg.get("base_url", "https://api.openai.com/v1"),
            temperature=llm_cfg.get("temperature", 0.0),
            max_tokens=llm_cfg.get("max_tokens", 1024),
        )
        return self._llm_provider

    def _shared_answer_prompt(self) -> str:
        """Return the answer prompt shared with other adapters.

        Uses the mem0-compatible prompt from prompts.yaml when available so
        openclaw answers are judged by the same yardstick as mem0/memos, and
        falls back to a concise in-process template if prompts.yaml is absent
        (e.g. in minimal test environments).
        """
        if self._shared_prompt_template is not None:
            return self._shared_prompt_template
        try:
            from evaluation.src.utils.config import load_yaml

            prompts = load_yaml(str(_PROMPTS_YAML_PATH))
            self._shared_prompt_template = prompts["online_api"]["default"]["answer_prompt_mem0"]
        except Exception:
            self._shared_prompt_template = _DEFAULT_ANSWER_PROMPT
        return self._shared_prompt_template

    # ----------------------------------------------------------- system info
    def get_system_info(self) -> dict:
        return {"name": "OpenClaw", "config": self.config}

    def _bridge_script_path(self) -> Path:
        return _BRIDGE_SCRIPT_PATH

    async def _invoke_bridge(
        self, sandbox: dict, payload: dict, timeout: float
    ) -> dict:
        """Run a bridge command via the host node + script.

        Subclasses (e.g. ``OpenClawDockerAdapter``) override this to route
        through ``docker exec`` so the bridge runs INSIDE the container
        where plugin sidecars (mem0/evermemos/zep) live. Form B plugins
        disable memory-core's CLI, so the host's ``openclaw memory index``
        path won't work — only the in-container sidecar HTTP path does.
        """
        return await arun_bridge(
            self._bridge_script_path(),
            payload,
            timeout=timeout,
        )

    def _bridge_base_payload(self, sandbox: dict) -> dict:
        """Fields every BridgeCommand needs: where OpenClaw lives, where the
        sandbox lives, and which config to read. repo_path comes from the
        yaml (preferred) so the config surface is authoritative; bridge
        still falls back to the env var for developer convenience.

        v0.7: agent_llm_env_vars is the explicit whitelist of env var
        names that ``envForSandbox`` will pass through to the openclaw
        subprocess. The resolved config carries ``${VAR}`` template
        strings for secrets (apiKey); without env passthrough OpenClaw
        throws MissingEnvVarError on those templates. List comes from
        yaml ``openclaw.agent_llm.env_vars`` (defaults to []).
        """
        agent_llm = self._openclaw_cfg.get("agent_llm") or {}
        env_vars = agent_llm.get("env_vars") or []
        return {
            "repo_path": self._openclaw_repo_path,
            "config_path": sandbox.get("resolved_config_path", ""),
            "workspace_dir": sandbox.get("workspace_dir", ""),
            "state_dir": sandbox.get("native_store_dir", ""),
            "home_dir": sandbox.get("home_dir", ""),
            "cwd_dir": sandbox.get("cwd_dir", ""),
            "agent_llm_env_vars": list(env_vars) if isinstance(env_vars, list) else [],
        }

    # ===================================================== internal helpers
    def _resolve_run_root(self, output_dir: Any) -> Path:
        if output_dir is None:
            raise ValueError("output_dir is required to resolve openclaw sandbox root")
        if self._run_id is None:
            self._run_id = time.strftime("run-%Y%m%dT%H%M%S")
        root = Path(output_dir) / _ARTIFACT_ROOT / self._run_id
        root.mkdir(parents=True, exist_ok=True)
        (Path(output_dir) / _ARTIFACT_ROOT / _RUN_ID_LATEST_FILE).write_text(self._run_id)
        return root

    def _locate_existing_run_root(self, output_dir: Path) -> Path:
        latest_file = output_dir / _ARTIFACT_ROOT / _RUN_ID_LATEST_FILE
        if latest_file.exists():
            run_id = latest_file.read_text().strip()
            root = output_dir / _ARTIFACT_ROOT / run_id
            if root.exists():
                return root
        # Fallback: newest run directory by mtime
        parent = output_dir / _ARTIFACT_ROOT
        if not parent.exists():
            raise FileNotFoundError(f"no openclaw artifacts under {parent}")
        runs = [p for p in parent.iterdir() if p.is_dir()]
        if not runs:
            raise FileNotFoundError(f"no openclaw runs under {parent}")
        runs.sort(key=lambda p: p.stat().st_mtime)
        return runs[-1]

    def _prepare_conversation_sandbox(
        self, root_dir: Path, conv: Conversation
    ) -> dict:
        run_id = root_dir.name
        output_dir = root_dir.parent.parent.parent  # strip artifacts/openclaw/<run>
        paths = build_sandbox_paths(output_dir, run_id, conv.conversation_id)

        # Create the full sandbox skeleton up-front so ingest + bridge can
        # just write files without mkdir guards.
        base = Path(paths["base_dir"])
        base.mkdir(parents=True, exist_ok=True)
        Path(paths["memory_dir"]).mkdir(parents=True, exist_ok=True)
        Path(paths["native_store_dir"]).mkdir(parents=True, exist_ok=True)
        (Path(paths["native_store_dir"]) / "memory").mkdir(parents=True, exist_ok=True)
        Path(paths["home_dir"]).mkdir(parents=True, exist_ok=True)
        Path(paths["cwd_dir"]).mkdir(parents=True, exist_ok=True)
        Path(paths["metrics_dir"]).mkdir(parents=True, exist_ok=True)
        Path(paths["events_path"]).touch(exist_ok=True)

        manifest = build_session_manifest(
            conv, dataset_name=self.config.get("dataset_name", "")
        )
        manifest_path = base / "session_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

        # Write the OpenClaw-schema config the CLI will read via
        # OPENCLAW_CONFIG_PATH.
        backend_mode = self._openclaw_cfg.get("backend_mode", "hybrid")
        flush_mode = self._openclaw_cfg.get("flush_mode", "shared_llm")
        # Pass memory_mode + agent_llm so resolved_config emits
        # plugins.allow/slots/entries and models.providers.<id> with secret
        # ${VAR} templates rebuilt from *_env markers.
        memory_mode = self._openclaw_cfg.get("memory_mode", "memory-core")
        agent_llm = self._openclaw_cfg.get("agent_llm")
        context_engine_mode = self._openclaw_cfg.get("context_engine_mode")
        resolved = build_openclaw_resolved_config(
            workspace_dir=paths["workspace_dir"],
            native_store_dir=paths["native_store_dir"],
            backend_mode=backend_mode,
            memory_mode=memory_mode,
            context_engine_mode=context_engine_mode,
            agent_llm=agent_llm,
            embedding=self._openclaw_cfg.get("embedding"),
        )
        resolved_config_path = Path(paths["config_path"])
        resolved_config_path.write_text(
            json.dumps(resolved, ensure_ascii=False, indent=2)
        )

        handle: dict = {
            "conversation_id": conv.conversation_id,
            "workspace_dir": paths["workspace_dir"],
            "memory_dir": paths["memory_dir"],
            "native_store_dir": paths["native_store_dir"],
            "home_dir": paths["home_dir"],
            "cwd_dir": paths["cwd_dir"],
            "resolved_config_path": str(resolved_config_path),
            "session_manifest_path": str(manifest_path),
            "prov_units_path": str(base / "prov_units.jsonl"),
            "artifact_bindings_path": str(base / "artifact_bindings.jsonl"),
            "events_path": paths["events_path"],
            "metrics_dir": paths["metrics_dir"],
            "backend_mode": backend_mode,
            "retrieval_route": self._openclaw_cfg.get(
                "retrieval_route", "search_then_get"
            ),
            "visibility_mode": self._openclaw_cfg.get("visibility_mode", "settled"),
            "flush_mode": flush_mode,
            "visibility_state": "prepared",
            "run_status": "pending",
            "last_flush_epoch": 0,
            "last_index_epoch": 0,
            "retrieval_eval_supported": True,
        }
        return handle

    def _write_handle(self, handle: dict, add_summary: Optional[dict] = None) -> None:
        base = Path(handle["workspace_dir"])
        handle_path = base / "handle.json"
        handle_path.write_text(json.dumps(handle, ensure_ascii=False, indent=2))

        if add_summary is not None:
            (Path(handle["metrics_dir"]) / "add_summary.json").write_text(
                json.dumps(add_summary, ensure_ascii=False, indent=2)
            )

    async def _ingest_conversation(self, sandbox: dict, conv: Conversation) -> None:
        """Build OpenClaw memory state for ``conv`` and drive an index pass.

        Three ingest modes selected by ``openclaw.flush_mode``:

          * ``session_bundle`` (agent-backbone, faithful):
                One agent_run per LoCoMo session, message bundle framed as
                ``[group chat conversation]\\n\\n<speaker>: <text>...\\n\\n
                <tail>``. Tail directive (yaml ``ingest_session_tail``)
                cues the agent to emit memory via its in-turn file_write
                tool calls. Between sessions the transcript jsonl is
                archived so the next session starts with empty short-term
                context but inherits prior memory/*.md files. Mirrors the
                reference openclaw-eval/eval.py ingest pattern.

          * ``shared_llm`` (legacy LLM-backbone):
                Framework LLM writes memory/session-*.md via
                ``buildMemoryFlushPlan``-style prompts. Approximation of
                OpenClaw's runtime flush; kept for backwards compat with
                llm-backbone evals.

          * ``disabled`` (legacy raw dump):
                Each session's transcript bullets dumped to
                memory/session-*.md without any LLM. v0.1/v0.2 baseline.

        The ``index`` bridge call runs after content is in place. A bridge
        failure raises and propagates so the surrounding add() marks
        run_status=failed rather than silently producing an empty sandbox.
        """
        flush_mode = sandbox.get("flush_mode", "shared_llm")

        if flush_mode == "session_bundle":
            await self._ingest_via_session_bundle(sandbox, conv)
            return

        llm_generate = self._make_flush_generate() if flush_mode == "shared_llm" else None

        flush_plan: Optional[dict] = None
        if flush_mode == "shared_llm":
            flush_plan = await self._fetch_native_flush_plan(sandbox)
            sandbox["flush_plan_native"] = bool(flush_plan and flush_plan.get("native"))
            self._append_events(
                sandbox,
                [
                    {
                        "event": "flush_plan_resolved",
                        "native": bool(flush_plan and flush_plan.get("native")),
                        "relative_path": (flush_plan or {}).get("relative_path"),
                        "soft_threshold_tokens": (flush_plan or {}).get(
                            "soft_threshold_tokens"
                        ),
                    }
                ],
            )

        rows = await write_session_files(
            conversation=conv,
            memory_dir=Path(sandbox["memory_dir"]),
            flush_mode=flush_mode,
            llm_generate=llm_generate,
            flush_plan=flush_plan,
            honor_silent_token=bool(self._openclaw_cfg.get("honor_silent_token", False)),
        )
        self._append_events(sandbox, [{"event": "session_ingested", **r} for r in rows])

        # Drive the real OpenClaw index build so search has something to hit.
        # Routed via _invoke_bridge so subclasses (e.g. docker) can run the
        # bridge inside the container where the sidecar / plugin lives.
        index_resp = await self._invoke_bridge(
            sandbox,
            {
                **self._bridge_base_payload(sandbox),
                "command": "index",
            },
            timeout=self._index_timeout(),
        )
        sandbox["last_index_epoch"] = int(index_resp.get("index_epoch") or 0)
        sandbox["visibility_state"] = "ingested"
        self._append_events(
            sandbox,
            [{"event": "index_complete", "index_epoch": sandbox["last_index_epoch"]}],
        )

    async def _ingest_via_session_bundle(
        self, sandbox: dict, conv: Conversation
    ) -> None:
        """Per-session bundle ingest, dispatching to the right channel(s)
        for the configured ``memory_mode`` × ``context_engine_mode`` combo.

        Channel matrix:

          memory_mode | context_engine | OV SDK | agent_run | index
          ------------|----------------|--------|-----------|------
          memory-core | (none)         |  no    |   yes     |  yes
          memory-core | openviking     |  yes   |   yes     |  yes
          noop        | openviking     |  yes   |   no      |  no

        The OV SDK channel mirrors the canonical OV bench
        ``import_to_ov.py``: per-session create_session → add_message × N →
        commit_session(telemetry=true) → poll task until completed. It is
        a host-side direct HTTP path, NOT routed through openclaw agent_run,
        because the OV plugin's ``afterTurn`` only commits when accumulated
        ``pendingTokens >= commitTokenThreshold`` (default 20k) — a smoke
        run never crosses that, and even when it does, the commit is
        ``wait: false`` so QA may start before the async memory extraction
        completes server-side.

        The agent_run channel is for memory-core: each LoCoMo session is
        rendered as a ``[group chat conversation]`` bundle, the agent's
        in-turn ``file_write`` writes ``memory/<DATE>.md``, then the
        session jsonl is archived so the next bundle starts with empty
        short-term context but inherits prior memory files. After all
        sessions complete, ``openclaw memory index --force`` indexes them
        for BM25 + vector retrieval.

        ``answer_mode == agent_local`` is required throughout — the bridge
        agent_run calls go through ``openclaw agent --local``.
        """
        conv_id = conv.conversation_id
        sessions = bucket_conversation_by_session(conv)
        memory_mode = self._openclaw_cfg.get("memory_mode", "memory-core")
        context_engine_mode = (
            self._openclaw_cfg.get("context_engine_mode") or ""
        ).strip()
        run_ov_sdk = context_engine_mode == "openviking"
        run_agent_loop = memory_mode != "noop"
        run_index = memory_mode != "noop"
        tail = self._select_ingest_tail() if run_agent_loop else ""

        self._append_events(sandbox, [{
            "event": "ingest_mode_session_bundle",
            "num_sessions": len(sessions),
            "tail_present": bool(tail),
            "ov_sdk_ingest": run_ov_sdk,
            "agent_loop_ingest": run_agent_loop,
            "context_engine_mode": context_engine_mode or None,
            "memory_mode": memory_mode,
        }])

        if not sessions:
            logger.warning(
                "session_bundle ingest: no session buckets for %s; "
                "skipping per-session ingest + index",
                conv_id,
            )
            sandbox["visibility_state"] = "ingested"
            return

        await self._prebootstrap_workspace(sandbox)

        if run_ov_sdk:
            await self._ingest_sessions_via_ov_sdk(sandbox, conv, sessions)

        if run_agent_loop:
            for session_key, session_msgs in sessions.items():
                if not session_msgs:
                    continue
                await self._ingest_one_session(
                    sandbox, session_key, session_msgs, tail
                )
                await self._archive_session_transcript(sandbox)

        if run_index:
            index_resp = await self._invoke_bridge(
                sandbox,
                {
                    **self._bridge_base_payload(sandbox),
                    "command": "index",
                },
                timeout=self._index_timeout(),
            )
            sandbox["last_index_epoch"] = int(index_resp.get("index_epoch") or 0)
            sandbox["visibility_state"] = "ingested"
            self._append_events(
                sandbox,
                [{
                    "event": "index_complete_session_bundle",
                    "index_epoch": sandbox["last_index_epoch"],
                    "ok": index_resp.get("ok"),
                    "error": index_resp.get("error"),
                    "input_artifacts": index_resp.get("input_artifacts"),
                    "output_artifacts": index_resp.get("output_artifacts"),
                }],
            )
        else:
            # OV-only: no memcore index to build. Mark as ingested; the
            # noop-memory short-circuit in add() will subsequently set
            # visibility_state to settled.
            sandbox["visibility_state"] = "ingested"
            self._append_events(sandbox, [{
                "event": "index_skipped_ov_only",
                "reason": "memory_mode=noop, no memcore index",
            }])

    async def _ingest_sessions_via_ov_sdk(
        self,
        sandbox: dict,
        conv: Conversation,
        sessions: dict,
    ) -> None:
        """Ingest every LoCoMo session into the OV server via direct HTTP.

        Mirrors reference ``import_to_ov.py`` per-session pattern. Yaml
        ``openclaw.ov_ingest`` controls connection details:

            ov_ingest:
              base_url: "http://127.0.0.1:1933"   # host-side OV server
              api_key_env: "OPENVIKING_API_KEY"
              account_id: "default"
              user_id_template: "{conv_id}"        # per-conv tenant
              user_id: ""                           # explicit override (skips template)
              agent_id: ""                          # optional fixed agent_id header
              task_timeout_sec: 600                 # per-session task wait

        The ``user_id_template`` defaults to ``"{conv_id}"`` so each
        LoCoMo conversation lives under its own OV user namespace
        (``viking://user/locomo_0/...``). Without per-conv isolation,
        memories from convs that share a name (e.g. multiple John's
        across conv-41/43/47) collide in a single namespace and the OV
        plugin's hierarchical retrieval surfaces the wrong John's facts
        at QA time. Reference openclaw-eval/eval.py uses equivalent
        per-sample isolation (``user = f"eval-{sample_idx}"``).

        The docker adapter substitutes ``{conv_id}`` into the
        container's ``OPENVIKING_USER_ID`` env so the plugin's QA-time
        queries hit the same tenant we wrote here.

        Failures here log + raise — without OV data, the OV-only / OV+memcore
        QA stages would silently produce 0% accuracy, so a hard fail at
        add() is preferable.
        """
        from evaluation.src.adapters.openclaw.ov_ingest import (
            OVIngestClient,
            OVIngestError,
            ingest_session_to_ov,
        )
        import aiohttp

        cfg = dict(self._openclaw_cfg.get("ov_ingest") or {})
        base_url = (cfg.get("base_url") or "http://127.0.0.1:1933").rstrip("/")
        api_key_env = cfg.get("api_key_env") or "OPENVIKING_API_KEY"
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        account_id = cfg.get("account_id") or "default"
        conv_id = conv.conversation_id
        # user_id is per-conv by default to keep OV memories from
        # different LoCoMo conversations in separate tenants. Without
        # this, recurring names (e.g. multiple "John"s in conv-41 /
        # conv-43 / conv-47) collide in a single namespace and QA
        # retrieval surfaces facts from the wrong conversation.
        user_id = self._resolve_ov_tenant_field(
            cfg, "user_id", "user_id_template", "{conv_id}", conv_id,
        )
        # Fix #4 完整版: agent_id 也支持 per-conv template, matching
        # official import_to_ov.py which sets both user_id=sample_id AND
        # agent_id=sample_id. OV server mirrors fact-extract into both
        # user/<uid> and agent/<aid> namespaces. plugin's agent_prefix is
        # not env-readable so the QA-side find on viking://agent/memories
        # still hits agent/default (empty under this scheme) — equivalent
        # to disabling that recall channel, which matches official behavior.
        agent_id = self._resolve_ov_tenant_field(
            cfg, "agent_id", "agent_id_template", "", conv_id,
        ) or None
        task_timeout = float(cfg.get("task_timeout_sec") or 600)

        client = OVIngestClient(
            base_url,
            api_key=api_key,
            account_id=account_id,
            user_id=user_id,
            agent_id=agent_id,
        )
        completed = 0
        failed = 0
        timeout = aiohttp.ClientTimeout(total=task_timeout + 60.0)

        # Single OV session per conversation — mirrors official openclaw-eval
        # eval.py's same-user pattern. All LoCoMo sub-sessions accumulate
        # pendingTokens on the same OV session_id; QA-side plugin uses the
        # same session_id (set by docker adapter via --session-id), so
        # Fix #3: per-LoCoMo-session commit on the same shared ov_session_id.
        # Each LoCoMo sub-session is followed by an explicit commit_session
        # call (telemetry=true, wait for task to complete). This mirrors the
        # official import_to_ov.py granularity (one fact-extraction LLM call
        # per LoCoMo session) while preserving openclaw-eval's shared sessionId
        # pattern (single UUID across ingest+QA, used by plugin assemble).
        # OV server archives the active segment on each commit but keeps the
        # session alive for further add_message → next session continues on
        # the same sid. End-of-conv state: N archives accumulated, active
        # segment empty, fact-extract has run N times producing finer-grained
        # entities / preferences / events in user/<conv_id>/memories.
        # Why this matters: a single end-of-conv commit (the prior behavior)
        # gave OV's fact-extract LLM N×~2k tokens of mixed conversation in
        # one call; output was coarse (e.g. conv-0 only 48 events, 2 entities,
        # 5 preferences). Per-session commit feeds smaller windows, yielding
        # finer extraction (target: 100+ events, 50+ entities for conv-0).
        async with aiohttp.ClientSession(timeout=timeout) as http:
            ov_session_id = await client.create_session(http)
            sandbox["ov_session_id"] = ov_session_id
            self._append_events(sandbox, [{
                "event": "ov_session_opened",
                "conversation_id": conv_id,
                "ov_session_id": ov_session_id,
            }])

            # Fire-and-forget phase2: each LoCoMo session's commit
            # returns immediately with a task_id; we batch-wait at
            # end-of-conv. This lets the per-session loop push messages
            # to OV faster than the phase2 LLM can process them, so the
            # OV server's task queue absorbs the bottleneck instead of
            # the client serializing on each commit. Critical for
            # conv-parallel ingest where 10× this loop runs at once.
            pending_task_ids: list[str] = []
            for session_key, msgs in sessions.items():
                try:
                    result = await ingest_session_to_ov(
                        client, http,
                        session_key=session_key,
                        messages=msgs,
                        ov_session_id=ov_session_id,
                        commit_after=True,
                        wait_task=False,
                    )
                    self._append_events(sandbox, [{
                        "event": "ov_session_messages_added",
                        "conversation_id": conv_id,
                        **{k: v for k, v in result.items() if k != "_telemetry"},
                    }])
                    status = result.get("status")
                    if status in ("completed", "queued", "queued_phase2", "skipped_no_messages"):
                        completed += 1
                    else:
                        failed += 1
                    tid = result.get("task_id")
                    if tid:
                        pending_task_ids.append(tid)
                except OVIngestError as err:
                    failed += 1
                    logger.error(
                        "OV SDK ingest_session failed for %s/%s: %s",
                        conv_id, session_key, err,
                    )
                    self._append_events(sandbox, [{
                        "event": "ov_session_add_failed",
                        "conversation_id": conv_id,
                        "session_key": session_key,
                        "error": str(err),
                    }])
                    raise

            # Batch wait: phase2 tasks must complete before the answer
            # stage queries plugin auto-recall against user/<conv_id>/
            # memories — otherwise QA can race ingest fact-extraction.
            # Tasks are independent, so wait on them concurrently;
            # otherwise this re-serializes the fire-and-forget design.
            async def _wait_one(tid: str) -> tuple[str, Any]:
                try:
                    return tid, await client.wait_for_task(http, tid)
                except OVIngestError as err:
                    return tid, err

            wait_results = await asyncio.gather(
                *[_wait_one(tid) for tid in pending_task_ids]
            )
            for tid, outcome in wait_results:
                if isinstance(outcome, OVIngestError):
                    logger.warning(
                        "OV phase2 task %s failed for %s: %s",
                        tid, conv_id, outcome,
                    )
                    self._append_events(sandbox, [{
                        "event": "ov_session_task_failed",
                        "conversation_id": conv_id,
                        "task_id": tid,
                        "error": str(outcome),
                    }])
                    continue
                task = outcome
                result_dict = task.get("result") if isinstance(task, dict) else {}
                self._append_events(sandbox, [{
                    "event": "ov_session_task_completed",
                    "conversation_id": conv_id,
                    "task_id": tid,
                    "status": task.get("status") if isinstance(task, dict) else None,
                    "memories_extracted": (
                        (result_dict or {}).get("memories_extracted")
                        if isinstance(result_dict, dict) else None
                    ),
                }])

        self._append_events(sandbox, [{
            "event": "ov_sdk_ingest_complete",
            "conversation_id": conv_id,
            "completed": completed,
            "failed": failed,
            "total_sessions": len(sessions),
            "ov_session_id": ov_session_id,
        }])

    async def _ingest_one_session(
        self,
        sandbox: dict,
        session_key: str,
        session_msgs: list,
        tail: str,
    ) -> None:
        """Bundle a single session into one agent_run call.

        Timeout scales with message count: 60s base + 4s per message,
        capped at 300s. A LoCoMo session of ~30 messages lands at 180s
        which is enough for one LLM round trip + 1-2 file_write calls.
        """
        bundle = self._build_session_bundle(session_msgs, tail)
        timeout = min(60.0 + len(session_msgs) * 4.0, 300.0)
        conv_id = sandbox["conversation_id"]
        try:
            resp = await self._invoke_bridge(
                sandbox,
                {
                    **self._bridge_base_payload(sandbox),
                    "command": "agent_run",
                    "session_id": conv_id,
                    "message": bundle,
                    "timeout_seconds": int(timeout),
                },
                timeout=timeout + 60.0,
            )
            ok = bool(resp.get("ok"))
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "session_bundle ingest failed for %s/%s: %s",
                conv_id, session_key, err,
            )
            ok = False
            resp = {"error": str(err)}
        self._append_events(sandbox, [{
            "event": "session_bundle_ingested",
            "conversation_id": conv_id,
            "session_key": session_key,
            "num_messages": len(session_msgs),
            "bundle_bytes": len(bundle.encode("utf-8")),
            "ok": ok,
            "stop_reason": resp.get("stop_reason"),
            "duration_ms": resp.get("duration_ms"),
            "tool_names": resp.get("tool_names"),
            "reply_excerpt": (resp.get("reply") or "")[:200],
            "error": resp.get("error"),
        }])

    async def _archive_session_transcript(self, sandbox: dict) -> None:
        """Rename the session jsonl so the next agent_run starts fresh.

        Memory under <state_dir>/agents/main/sessions/<sid>.jsonl is
        renamed to <sid>.jsonl.<ts>; memory/*.md files remain so memory
        persists across the archive.

        Failures are logged but do not abort ingest — a missed archive
        means the next session sees the prior session's transcript in
        addition to its own bundle, which produces messy context but
        not data loss.
        """
        conv_id = sandbox["conversation_id"]
        try:
            resp = await self._invoke_bridge(
                sandbox,
                {
                    **self._bridge_base_payload(sandbox),
                    "command": "archive_session",
                    "session_id": conv_id,
                },
                timeout=30.0,
            )
            ok = bool(resp.get("ok"))
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "archive_session failed for %s: %s", conv_id, err,
            )
            ok = False
            resp = {"error": str(err)}
        self._append_events(sandbox, [{
            "event": "session_transcript_archived",
            "conversation_id": conv_id,
            "ok": ok,
            "archived_to": resp.get("archived_to"),
            "reason": resp.get("reason"),
            "error": resp.get("error"),
        }])

    @staticmethod
    def _resolve_ov_tenant_field(
        cfg: dict,
        explicit_key: str,
        template_key: str,
        default_template: str,
        conv_id: str,
    ) -> str:
        """Resolve an OV tenant field (user_id / agent_id) per conversation.

        Priority:
          1. ``cfg[explicit_key]`` non-empty → use as-is (no substitution).
             For runs that explicitly want a fixed shared tenant.
          2. ``cfg[template_key]`` non-empty → format with ``{conv_id}``.
          3. ``default_template`` (constructor argument) → format with
             ``{conv_id}``.

        Empty default + no explicit + no template → empty string. The
        SDK ingest path treats an empty agent_id as "no X-OpenViking-Agent
        header" (server uses default agent), matching reference
        ``import_to_ov.py --no-user-agent-id`` semantics.
        """
        explicit = (cfg.get(explicit_key) or "").strip()
        if explicit:
            return explicit
        template = cfg.get(template_key)
        if template is None:
            template = default_template
        template = str(template).strip()
        if not template:
            return ""
        return template.format(conv_id=conv_id)

    def _select_ingest_tail(self) -> str:
        """Resolve the tail directive appended to each session bundle.

        Yaml ``openclaw.ingest_session_tail`` (string, optional) overrides
        any default. When unset:
          * memory_mode == "noop": empty tail. Memory writes are owned by
            the context-engine plugin (e.g. OV) via afterTurn hooks; the
            agent doesn't need a memory cue.
          * else: an explicit file_write directive. The minimal
            ``[try to remember what's said in the group]`` cue (used by
            reference openclaw-eval gateway runs) was empirically observed
            to elicit only narrative reply text under ``agent --local``,
            so we steer the agent toward the file_write tool by name.
            The ``[try to remember…]`` wording is preserved in the
            ingest_session_tail yaml override for any caller that wants
            to reproduce the reference behavior verbatim.
        """
        configured = self._openclaw_cfg.get("ingest_session_tail")
        if configured is not None:
            return str(configured)
        memory_mode = self._openclaw_cfg.get("memory_mode", "memory-core")
        if memory_mode == "noop":
            return ""
        return (
            "[End of session. Use the file_write tool to append durable "
            "memories from this conversation to memory/<YYYY-MM-DD>.md "
            "(use the date from the session header). Bullet list covering "
            "names, decisions, preferences, dates, places, and events. "
            "After writing the file, reply with NO_REPLY.]"
        )

    @staticmethod
    def _build_session_bundle(session_msgs: list, tail: str) -> str:
        """Render a session into a single multi-line user message.

        Output mirrors reference openclaw-eval bundles:

            [group chat conversation: <date_time>]

            <speaker>: <text>

            <speaker>: <text>

            ...

            <tail>

        ``date_time`` is the timestamp of the first message with one;
        omitted entirely if no message carries a timestamp.
        """
        first_dt = next(
            (m.timestamp for m in session_msgs if m.timestamp is not None),
            None,
        )
        if first_dt is not None:
            header = f"[group chat conversation: {first_dt.strftime('%Y-%m-%d %H:%M')}]"
        else:
            header = "[group chat conversation]"

        parts: list[str] = [header, ""]
        for msg in session_msgs:
            content = (msg.content or "").strip()
            if not content:
                continue
            parts.append(f"{msg.speaker_name}: {content}")
            parts.append("")
        if tail:
            parts.append(tail)
        return "\n".join(parts).rstrip() + "\n"

    async def _flush_and_settle_if_needed(self, sandbox: dict) -> None:
        """Transition visibility_state to its final value per visibility_mode.

        Post-conditions:
          * visibility_mode == 'settled': visibility_state becomes 'settled'
            **only** when OpenClaw's ``memory status`` returns settled=true.
            Otherwise this method raises so add() fails fast rather than
            persisting a handle that lies about being queryable.
          * visibility_mode == 'eventual': we do not wait; visibility_state
            stays at 'indexed' and the caller accepts that search() may
            trigger a background re-sync via memorySearch.sync.onSearch.

        Stage 3 Phase 5 short-circuit: when context_engine_mode is set AND
        the memory slot is bundled (memory-core/noop), the context-engine
        plugin (e.g. hypercompositor) intercepts ingest before the memory
        backend is touched. memory status reports settled=false forever
        because there's nothing to flush, breaking the eval pipeline at
        add(). Transition straight to 'settled' since the context-engine
        owns its own storage settlement contract.
        """
        if sandbox.get("visibility_mode") != "settled":
            sandbox["visibility_state"] = "indexed"
            return

        context_engine_mode = (self._openclaw_cfg.get("context_engine_mode") or "").strip()
        memory_mode = self._openclaw_cfg.get("memory_mode", "memory-core")
        if context_engine_mode and memory_mode in ("memory-core", "noop"):
            self._append_events(
                sandbox,
                [
                    {
                        "event": "settle_skipped_context_engine",
                        "context_engine_mode": context_engine_mode,
                        "memory_mode": memory_mode,
                    }
                ],
            )
            sandbox["visibility_state"] = "settled"
            return

        # Status check with bounded retry. OpenClaw's ``settled`` flag can
        # remain false for several seconds after a successful index pass
        # while the in-process file watcher reconciles. Smoke (1-2 sessions)
        # nearly always reports settled=true on the first call; full
        # LoCoMo (~30 sessions × 10 conv concurrent) sometimes needs the
        # watcher a beat longer. Retry 4×, exponential backoff (1/2/4/8s
        # = 15s total).
        #
        # Fallback acceptance: if after the retry budget files>0 AND
        # chunks>0 AND active_artifacts is empty, treat as effectively
        # settled. The queryability invariant our caller cares about is
        # "memorySearch finds something", and that requires non-zero
        # indexed chunks with no in-flight writes — exactly the fallback
        # condition. The strict ``settled`` boolean adds a watcher-state
        # check which can lag without affecting search correctness.
        status_resp: dict = {}
        settled = False
        attempts = 0
        for attempt in range(4):
            attempts = attempt + 1
            status_resp = await self._invoke_bridge(
                sandbox,
                {**self._bridge_base_payload(sandbox), "command": "status"},
                timeout=self._status_timeout(),
            )
            settled = status_resp.get("settled") is True
            if settled:
                break
            if attempt < 3:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s, 8s

        sandbox["last_flush_epoch"] = int(status_resp.get("flush_epoch") or 0)

        files = int(status_resp.get("files") or 0)
        chunks = int(status_resp.get("chunks") or 0)
        active = status_resp.get("active_artifacts") or []
        accepted_via_fallback = (
            not settled
            and files > 0
            and chunks > 0
            and not active
        )
        # Vacuously settled: the agent wrote no memory files at all, so
        # there is nothing to index. This happens under weak/missing
        # memory cues (e.g. reference openclaw-eval's default tail
        # ``[remember what's said, keep existing memory]``) where the
        # agent sometimes treats the bundled session as chat to
        # acknowledge rather than memory to record. The queryability
        # invariant becomes "memorySearch finds nothing" — which is the
        # correct measurement for an empty index, not a settle failure.
        # The downstream QA stage will see no memory and answer from
        # background knowledge, scoring whatever it scores for that conv.
        vacuously_settled = (
            not settled
            and files == 0
            and chunks == 0
            and not active
        )

        self._append_events(
            sandbox,
            [
                {
                    "event": "status_checked",
                    "settled": settled,
                    "accepted_via_fallback": accepted_via_fallback,
                    "vacuously_settled": vacuously_settled,
                    "files": files,
                    "chunks": chunks,
                    "active_artifacts": list(active),
                    "flush_epoch": sandbox["last_flush_epoch"],
                    "attempts": attempts,
                }
            ],
        )
        if not settled and not accepted_via_fallback and not vacuously_settled:
            raise RuntimeError(
                "openclaw status reported not settled for "
                f"{sandbox.get('conversation_id')!r} after {attempts} "
                f"attempts: {status_resp!r}"
            )
        sandbox["visibility_state"] = "settled"

    def _assert_visibility_contract(self, sandbox: dict) -> None:
        """Guard the plan's settled-mode guarantee at the add() boundary."""
        if sandbox.get("visibility_mode") == "settled":
            vs = sandbox.get("visibility_state")
            if vs != "settled":
                raise RuntimeError(
                    f"settled mode requires visibility_state=='settled' but got {vs!r}"
                )

    # -- helpers -----------------------------------------------------------

    def _make_flush_generate(self):
        """Return a coroutine callable that hits our LLM provider with
        (system_prompt, user_prompt) and returns plain text.

        Behavior on errors:
          * Up to ``flush_max_retries`` attempts with exponential backoff.
            Default 6 attempts at 3s base => 3/6/12/24/48/96s, ~3min total.
          * On final exhaustion we DO NOT raise; instead we return "" so
            the caller's fallback (render_session_transcript) writes the
            raw session into the memory file. A single LLM outage during a
            multi-hour benchmark run should not lose all completed work.
            The session row in events.jsonl marks ``flush_fallback=true``
            so post-hoc analysis can filter affected sessions.
        """
        max_retries = int(self._openclaw_cfg.get("flush_max_retries", 6))
        base_delay = float(self._openclaw_cfg.get("flush_retry_base_seconds", 3.0))

        async def _call(system_prompt: str, user_prompt: str) -> str:
            # Pass system_prompt as a separate ``system`` role message
            # instead of concatenating. Concatenation relied on the LLM
            # inferring roles from position; on some deployments (observed
            # on sophnet-proxied Azure gpt-4o-mini) the whole string gets
            # treated as one user turn and the model responds conversa-
            # tionally ("Would you like a summary?") instead of following
            # the flush instructions, producing useless memory content.
            provider = self._get_llm_provider()
            last_err: Optional[Exception] = None
            for attempt in range(max_retries):
                try:
                    result = await provider.generate(
                        prompt=user_prompt,
                        system_prompt=system_prompt,
                        temperature=0,
                    )
                    return result.strip() if isinstance(result, str) else ""
                except Exception as err:  # noqa: BLE001
                    last_err = err
                    if attempt == max_retries - 1:
                        break
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "flush LLM call failed (attempt %d/%d): %s; retrying in %.1fs",
                        attempt + 1, max_retries, err, delay,
                    )
                    await asyncio.sleep(delay)
            logger.error(
                "flush LLM exhausted %d retries (%s); session falls back to raw transcript",
                max_retries, last_err,
            )
            self._flush_fallback_count = (
                getattr(self, "_flush_fallback_count", 0) + 1
            )
            return ""

        return _call

    def _append_events(self, sandbox: dict, events: list[dict]) -> None:
        path = Path(sandbox.get("events_path", ""))
        if not path:
            return
        try:
            with path.open("a", encoding="utf-8") as fp:
                for event in events:
                    fp.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as err:  # noqa: BLE001
            logger.warning("failed to append events to %s: %s", path, err)

    def _index_timeout(self) -> float:
        return float(self._openclaw_cfg.get("index_timeout_seconds", 600.0))

    def _status_timeout(self) -> float:
        return float(self._openclaw_cfg.get("status_timeout_seconds", 60.0))

    async def _fetch_native_flush_plan(self, sandbox: dict) -> Optional[dict]:
        """Call the bridge's build_flush_plan to get OpenClaw's own flush
        plan (system_prompt / prompt / silent_token).

        NOTE: the bridge payload deliberately OMITS config_path for this
        command. Our runtime openclaw.json sets memoryFlush.enabled=false
        (so OpenClaw doesn't re-flush during search), but OpenClaw's
        ``buildMemoryFlushPlan`` returns null when that flag is off. We
        want the prompt text, not the runtime behaviour, so we ask the
        bridge to build the plan from OpenClaw defaults instead.

        Non-fatal: if the bridge cannot produce a plan (stub mode, missing
        dist, upstream error) we return None and the caller falls back to
        the in-process template. The event log records which branch we
        took.
        """
        base = self._bridge_base_payload(sandbox)
        plan_payload = {
            **base,
            "config_path": "",  # force OpenClaw defaults, not our runtime cfg
            "command": "build_flush_plan",
        }
        try:
            resp = await arun_bridge(
                self._bridge_script_path(),
                plan_payload,
                timeout=self._status_timeout(),
            )
        except Exception as err:  # noqa: BLE001
            logger.warning("build_flush_plan failed, falling back: %s", err)
            return None
        if not resp.get("ok") or resp.get("disabled") or not resp.get("system_prompt"):
            return None
        return {
            "native": bool(resp.get("native")),
            "system_prompt": resp["system_prompt"],
            "prompt": resp["prompt"],
            "silent_token": resp.get("silent_token") or "NO_REPLY",
            "relative_path": resp.get("relative_path"),
            "soft_threshold_tokens": resp.get("soft_threshold_tokens"),
        }

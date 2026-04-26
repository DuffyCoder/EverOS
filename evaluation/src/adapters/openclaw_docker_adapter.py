"""
DockerizedOpenclawAdapter — Stage 1 Path B via per-conversation containers.

Inherits from OpenClawAdapter to reuse:
  - Per-conv sandbox preparation (write_session_files, manifest, resolved
    config with secret hygiene)
  - answer_mode dispatch (shared_llm vs agent_local)
  - per-QA session-id pattern <conv>__<qid>
  - Sandbox persistence in add() and build_lazy_index()
  - get_answer_timeout() override
  - retrieval skipped suppress
  - stop_reason=error guard

What this subclass changes:
  - On prepare(): spawn one openclaw-eval docker container per conversation
  - The bridge invocation is routed through `docker exec <container> node
    /eval/openclaw_eval_bridge.mjs` instead of host node, so it actually
    runs inside the configured docker image (with memory plugin baked in)
  - Workspace is mounted volume; resolved config is written to mounted path
  - cleanup() stops + removes containers

Key design decisions (v0.7 §4.4 sandbox lookup applies same way; sandboxes
just gain a docker_container_id / volume_path field).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, List, Optional

from evaluation.src.adapters.openclaw_adapter import OpenClawAdapter
from evaluation.src.adapters.openclaw_runtime import (
    BridgeError,
    BridgeTimeout,
)
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation


logger = logging.getLogger(__name__)


@register_adapter("openclaw-docker")
class DockerizedOpenclawAdapter(OpenClawAdapter):
    """Path B adapter that runs each conversation in its own openclaw container."""

    def __init__(self, config: dict, output_dir: Any = None):
        super().__init__(config, output_dir)

        cfg = config.get("openclaw_docker") or {}
        if not cfg.get("image"):
            raise ValueError(
                "openclaw_docker.image is required (e.g. "
                "openclaw-eval:7da23c3-memory-core-0000000-slim)"
            )
        self._image: str = cfg["image"]
        self._max_concurrent: int = int(cfg.get("max_concurrent_containers", 4))
        self._mem_limit: str = cfg.get("mem_limit", "2g")
        self._docker_network: str = cfg.get("network", "bridge")
        self._exec_timeout: int = int(
            cfg.get("per_rpc_timeout_seconds",
                    self._openclaw_cfg.get("agent_timeout_seconds", 180) + 30)
        )

        # Per-conversation container handles; populated in add()/build_lazy_index().
        # value: {"container_id": str, "volume_dir": str, "image": str}
        self._docker_handles: dict[str, dict] = {}

        # Limit concurrent docker run invocations during prepare/add.
        self._spawn_sem: Optional[asyncio.Semaphore] = None

    # ---------------------------------------------------- container lifecycle

    async def _ensure_spawn_sem(self) -> asyncio.Semaphore:
        if self._spawn_sem is None:
            self._spawn_sem = asyncio.Semaphore(self._max_concurrent)
        return self._spawn_sem

    async def _docker_run_container(
        self, conv_id: str, sandbox: dict
    ) -> str:
        """Spawn a detached openclaw-eval container; return container id."""
        volume_dir = Path(sandbox["workspace_dir"]).resolve()
        volume_dir.mkdir(parents=True, exist_ok=True)

        env_pairs = self._docker_env_for_container()

        # --user matches host UID so the mounted /workspace volume is
        # writable to the container. Without this, files created by host
        # (workspace dirs, openclaw.json after entrypoint render) cannot
        # be read/written by the container's `node` user (uid 1000).
        cmd = [
            "docker", "run",
            "-d",
            "--rm",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--network", self._docker_network,
            "--memory", self._mem_limit,
            "--label", f"eval.run_id={self._run_id or 'unknown'}",
            "--label", f"eval.conv_id={conv_id}",
            "-v", f"{volume_dir}:/workspace:rw",
        ]
        for name, value in env_pairs:
            if value is not None:
                cmd.extend(["-e", f"{name}={value}"])
        cmd.append(self._image)

        logger.info("docker run for %s: image=%s volume=%s",
                    conv_id, self._image, volume_dir)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise BridgeError(
                f"docker run failed for {conv_id}: "
                f"exit={proc.returncode} stderr={stderr.decode()[:500]}"
            )
        cid = stdout.decode().strip()
        logger.info("container %s started for %s", cid[:12], conv_id)
        return cid

    def _docker_env_for_container(self) -> list[tuple[str, Optional[str]]]:
        """Compute -e flags for `docker run`. Mirrors bridge envForSandbox
        whitelist semantics: only forwards yaml-declared env_vars."""
        agent_llm = self._openclaw_cfg.get("agent_llm") or {}
        embedding = self._openclaw_cfg.get("embedding") or {}
        env_vars: list[str] = list(agent_llm.get("env_vars") or [])
        # Always forward MEMORY_PLUGIN_ID + MEMORY_MODE (entrypoint reads them).
        memory_plugin_id = self._openclaw_cfg.get("memory_mode", "memory-core")
        memory_mode = self._openclaw_cfg.get("memory_mode", "memory-core")
        # MEMORY_PLUGIN_ID encodes which plugin yaml asked for; defaults to
        # memory-core if memory_mode is itself memory-core or noop.
        if memory_mode in ("memory-core", "noop"):
            memory_plugin_id = "memory-core"

        # Compose pairs: explicit ones first, then secret env passthrough.
        pairs: list[tuple[str, Optional[str]]] = [
            ("MEMORY_PLUGIN_ID", memory_plugin_id),
            ("MEMORY_MODE", memory_mode),
            ("LLM_MODEL", agent_llm.get("model", {}).get("id")
                          or os.environ.get("LLM_MODEL")),
        ]
        # Pass-through secret + endpoint env vars from process env, only if
        # the yaml whitelist contains them (defense-in-depth: container only
        # ever receives env vars its config explicitly opted into).
        for name in env_vars:
            value = os.environ.get(name)
            if value is not None:
                pairs.append((name, value))
        return pairs

    async def _docker_stop_container(self, cid: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "stop", "-t", "10", cid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            # --rm in run flag deletes after stop; no rm needed
        except Exception as err:  # noqa: BLE001
            logger.warning("docker stop failed for %s: %s", cid, err)

    # --------------------------------------------------- subprocess routing

    def _bridge_script_path(self) -> Path:
        # In docker mode, bridge.mjs runs inside the container at /eval/.
        # Return host path for compatibility with super(); actual exec
        # routing is overridden in _arun_bridge_via_docker.
        return Path(__file__).parent.parent.parent / "scripts" / "openclaw_eval_bridge.mjs"

    async def _arun_bridge_via_docker(
        self, conv_id: str, payload: dict, timeout: float = 600.0
    ) -> dict:
        """Run a single bridge command inside the conv's docker container.

        Mirrors arun_bridge protocol: serialize payload to stdin JSON,
        receive single JSON object on stdout.
        """
        handle = self._docker_handles.get(conv_id)
        if handle is None:
            raise BridgeError(
                f"no docker container for conversation_id={conv_id!r}; "
                f"prepare() / add() must have spawned one first"
            )

        # Inside the container, repo_path is /app (where openclaw.mjs lives).
        # Override payload's repo_path to the in-container path.
        payload = {**payload, "repo_path": "/app"}

        cmd = [
            "docker", "exec", "-i",
            handle["container_id"],
            "node", "/eval/openclaw_eval_bridge.mjs",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=json.dumps(payload).encode()),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise BridgeTimeout(
                f"docker exec bridge call timed out after {timeout}s "
                f"for conv {conv_id}"
            )

        if proc.returncode != 0:
            raise BridgeError(
                f"docker exec bridge exited {proc.returncode}: "
                f"stderr={stderr.decode()[:500]}"
            )

        text = stdout.decode().strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            raise BridgeError(
                f"docker exec bridge returned non-JSON: {text[:500]}"
            ) from err

    # ----------------------------------------------------------- prepare

    async def prepare(
        self,
        conversations: List[Conversation],
        output_dir: Any = None,
        checkpoint_manager: Any = None,
        **kwargs,
    ) -> None:
        """Pre-spawn containers concurrently before add()/answer().

        We do this in prepare() so add()'s ingest step can issue bridge
        commands against an already-warm container.
        """
        await super().prepare(
            conversations=conversations,
            output_dir=output_dir,
            checkpoint_manager=checkpoint_manager,
            **kwargs,
        )
        # Resolve run root before spawning so workspace dirs land where
        # build_lazy_index expects them.
        if self._run_id is None:
            run_root = self._resolve_run_root(output_dir or self.output_dir)
            self._run_id = run_root.name

        sem = await self._ensure_spawn_sem()

        async def _spawn_one(conv: Conversation) -> None:
            async with sem:
                # Pre-create sandbox so volume dir exists.
                root_dir = self._resolve_run_root(output_dir or self.output_dir)
                sandbox = self._prepare_conversation_sandbox(root_dir, conv)
                cid = await self._docker_run_container(
                    conv.conversation_id, sandbox
                )
                self._docker_handles[conv.conversation_id] = {
                    "container_id": cid,
                    "volume_dir": sandbox["workspace_dir"],
                    "image": self._image,
                }
                # Stash sandbox into in-memory map; add() will overwrite
                # with full handle later.
                self._sandbox_by_conversation_id[conv.conversation_id] = sandbox

        await asyncio.gather(*[_spawn_one(c) for c in conversations])

    # ------------------------------------------------------------- cleanup

    async def cleanup(self) -> None:
        await asyncio.gather(*[
            self._docker_stop_container(h["container_id"])
            for h in self._docker_handles.values()
        ], return_exceptions=True)
        self._docker_handles.clear()

    # ---------------------------------------- override answer to use docker

    async def _generate_answer_via_agent(
        self, query: str, conv_id: str, qid: str
    ) -> str:
        """Override base impl to route bridge call through docker exec."""
        sandbox = self._sandbox_for(conv_id)
        agent_timeout = int(self._openclaw_cfg.get("agent_timeout_seconds", 180))
        payload = {
            **self._bridge_base_payload(sandbox),
            "command": "agent_run",
            "session_id": f"{conv_id}__{qid}",
            "message": query,
            "timeout_seconds": agent_timeout,
        }
        try:
            resp = await self._arun_bridge_via_docker(
                conv_id, payload,
                timeout=float(self._exec_timeout),
            )
        except (BridgeError, BridgeTimeout) as err:
            logger.warning("docker bridge failed for %s/%s: %s",
                           conv_id, qid, err)
            self._append_events(sandbox, [{
                "event": "agent_run_failed",
                "conversation_id": conv_id, "question_id": qid,
                "error": str(err),
            }])
            return ""

        if not resp.get("ok"):
            err = resp.get("error", "")
            logger.warning("docker agent_run failed for %s/%s: %s",
                           conv_id, qid, err)
            self._append_events(sandbox, [{
                "event": "agent_run_failed",
                "conversation_id": conv_id, "question_id": qid,
                "error": err,
            }])
            return ""

        # Inherit v0.7 D5 stop_reason=error guard from base behavior.
        if resp.get("stop_reason") == "error":
            reply_excerpt = (resp.get("reply") or "")[:200]
            logger.warning(
                "docker agent_run completed but stop_reason=error for "
                "%s/%s; reply: %s", conv_id, qid, reply_excerpt,
            )
            self._append_events(sandbox, [{
                "event": "agent_run_internal_error",
                "conversation_id": conv_id, "question_id": qid,
                "reply_excerpt": reply_excerpt,
                "duration_ms": resp.get("duration_ms"),
            }])
            return ""

        self._append_events(sandbox, [{
            "event": "agent_run_complete",
            "conversation_id": conv_id, "question_id": qid,
            "duration_ms": resp.get("duration_ms"),
            "stop_reason": resp.get("stop_reason"),
            "aborted": resp.get("aborted"),
            "tool_names": resp.get("tool_names"),
            "system_prompt_chars": resp.get("system_prompt_chars"),
            "reply_len": len(resp.get("reply", "")),
        }])
        return (resp.get("reply") or "").strip()

    # NOTE: search() and add()'s memory index step also need to route
    # through docker exec for the "shared_llm" answer mode. Stage 1
    # Week 2 task: route those too. For now, agent_local mode is the
    # primary use case (Path B), and search() returns skipped in that
    # mode (inherited from base), so this works for Path B end-to-end.
    # When we add Path A docker support, we'll route the memory-search
    # bridge command through docker exec as well.

    def get_system_info(self) -> dict:
        info = super().get_system_info()
        info["docker_image"] = self._image
        info["max_concurrent_containers"] = self._max_concurrent
        return info

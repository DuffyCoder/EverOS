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
        self._docker_cfg: dict = cfg
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

        env_pairs = self._docker_env_for_container(conv_id=conv_id)

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
        # Plugins that talk to a host-side service (e.g. evermemos plugin
        # fetching the EverMemOS HTTP API at host's :1995) need
        # host.docker.internal to resolve to the host machine. Docker
        # 20.10+ on Linux supports this via the host-gateway alias —
        # without --add-host the container can't reach the host's
        # localhost. yaml ``openclaw_docker.add_host_gateway: true`` opts
        # in; defaults true for memory_mode=evermemos.
        memory_mode = self._openclaw_cfg.get("memory_mode", "memory-core")
        add_host_gateway = bool(
            self._docker_cfg.get("add_host_gateway", memory_mode == "evermemos")
        )
        if add_host_gateway:
            cmd.extend(["--add-host", "host.docker.internal:host-gateway"])
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

    def _docker_env_for_container(
        self, conv_id: Optional[str] = None
    ) -> list[tuple[str, Optional[str]]]:
        """Compute -e flags for `docker run`. Mirrors bridge envForSandbox
        whitelist semantics: only forwards yaml-declared env_vars.

        ``conv_id`` (when provided) is propagated as ``EVERMEMOS_GROUP_ID``
        so the evermemos plugin scopes its memory_search to the active
        LoCoMo conversation. Other plugins ignore the var.
        """
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
        # Per-conv group_id for evermemos plugin (one container per conv).
        if memory_mode == "evermemos" and conv_id:
            pairs.append(("EVERMEMOS_GROUP_ID", conv_id))
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

        # Inside the container, paths are different from host (sandbox
        # dict has host paths like /Data3/.../artifacts/.../workspace).
        # Rewrite to in-container paths so bridge.mjs running inside the
        # container can find files. This mirrors what the entrypoint sets
        # via env vars (WORKSPACE_DIR=/workspace, etc).
        #
        # config_path uses a docker-specific filename to avoid colliding
        # with the host-side config the harness wrote at openclaw.json.
        # The entrypoint renders openclaw.docker.json with container paths.
        # Also forward yaml-declared agent_llm_env_vars so the in-container
        # bridge's envForSandbox passes secrets to the openclaw subprocess.
        agent_llm = self._openclaw_cfg.get("agent_llm") or {}
        env_vars = list(agent_llm.get("env_vars") or [])
        payload = {
            **payload,
            "repo_path": "/app",
            "config_path": "/workspace/openclaw.docker.json",
            "workspace_dir": "/workspace",
            "state_dir": "/workspace/state",
            "home_dir": "/workspace/home",
            "cwd_dir": "/workspace",
            "agent_llm_env_vars": payload.get("agent_llm_env_vars") or env_vars,
        }

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

    async def _invoke_bridge(
        self, sandbox: dict, payload: dict, timeout: float
    ) -> dict:
        """Override base impl so index/status/build_flush_plan all run
        inside the container. Form B plugins (mem0/evermemos/zep) ship a
        sidecar HTTP server bound to 127.0.0.1 inside the container —
        host-side bridge would not see it, and ``openclaw memory index``
        fails on host because memory-core's plugin entry is disabled in
        Form B mode.

        For ``memory_mode == evermemos`` the bridge ``index`` and
        ``status`` commands are short-circuited to no-ops because the
        adapter ingests directly to the host EverMemOS API (see
        ``_ingest_via_evermemos_api``); the in-container plugin is
        retrieval-only.
        """
        memory_mode = self._openclaw_cfg.get("memory_mode", "memory-core")
        if memory_mode == "evermemos":
            cmd = payload.get("command")
            if cmd == "index":
                return {
                    "ok": True,
                    "command": "index",
                    "flush_epoch": int(time.time()),
                    "index_epoch": int(time.time()),
                    "input_artifacts": [],
                    "output_artifacts": [],
                    "note": "evermemos: ingest done by adapter; bridge index is no-op",
                }
            if cmd == "status":
                return {
                    "ok": True,
                    "command": "status",
                    "settled": True,
                    "files": 0,
                    "chunks": 0,
                    "backend": "evermemos",
                    "active_artifacts": [],
                }
        conv_id = sandbox["conversation_id"]
        return await self._arun_bridge_via_docker(
            conv_id, payload, timeout=timeout,
        )

    async def _ingest_conversation(self, sandbox: dict, conv: Conversation) -> None:
        """Override for evermemos memory_mode: ingest directly via host
        EverMemOS HTTP API (one POST per message). Boundary detection
        on the server fires naturally with hundreds of LoCoMo messages.
        For other memory_modes (memory-core, mem0), defer to parent.
        """
        memory_mode = self._openclaw_cfg.get("memory_mode", "memory-core")
        if memory_mode != "evermemos":
            return await super()._ingest_conversation(sandbox, conv)
        await self._ingest_via_evermemos_api(sandbox, conv)

    async def _ingest_via_evermemos_api(
        self, sandbox: dict, conv: Conversation
    ) -> None:
        """POST each message in the conversation to the host EverMemOS
        ``/api/v1/memories`` endpoint. The last message uses
        ``?sync_mode=true`` so the server waits for boundary detection
        before returning, ensuring the index is queryable when add()
        finishes.
        """
        import aiohttp
        from common_utils.datetime_utils import to_iso_format

        api_url = (
            os.environ.get("EVERMEMOS_API_URL")
            or "http://localhost:1995"
        ).rstrip("/")
        api_key = os.environ.get("EVERMEMOS_API_KEY") or ""
        memories_url = f"{api_url}/api/v1/memories"

        conv_id = conv.conversation_id
        speaker_a = conv.metadata.get("speaker_a") or "speaker_a"
        speaker_b = conv.metadata.get("speaker_b") or "speaker_b"

        payloads: list[dict] = []
        for idx, msg in enumerate(conv.messages):
            sender_id = (
                msg.speaker_id
                or f"{msg.speaker_name.lower().replace(' ', '_')}_{conv_id}"
            )
            ts = to_iso_format(msg.timestamp) if msg.timestamp else None
            payloads.append({
                "group_id": conv_id,
                "group_name": conv_id,
                "message_id": msg.metadata.get("message_id")
                              or msg.metadata.get("dia_id")
                              or f"{conv_id}_{idx}",
                "create_time": ts or "",
                "sender": sender_id,
                "sender_name": msg.speaker_name,
                "role": "user",
                "content": msg.content,
                "refer_list": msg.metadata.get("refer_list") or [],
            })

        if not payloads:
            sandbox["last_index_epoch"] = int(time.time())
            sandbox["visibility_state"] = "ingested"
            return

        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"

        timeout = aiohttp.ClientTimeout(total=600)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for i, body in enumerate(payloads):
                # last one with sync_mode=true so we know flush completed
                params = {"sync_mode": "true"} if i == len(payloads) - 1 else None
                try:
                    async with session.post(memories_url, json=body, params=params) as resp:
                        await resp.text()
                        if resp.status >= 400:
                            logger.warning(
                                "evermemos ingest non-200 for %s/msg-%s: %s",
                                conv_id, i, resp.status,
                            )
                except Exception as err:
                    logger.warning(
                        "evermemos ingest failed for %s/msg-%s: %s",
                        conv_id, i, err,
                    )

        self._append_events(
            sandbox,
            [{"event": "evermemos_ingest_complete",
              "conversation_id": conv_id,
              "messages_posted": len(payloads),
              "api_url": memories_url}],
        )
        sandbox["last_index_epoch"] = int(time.time())
        sandbox["visibility_state"] = "ingested"

    # NOTE: search() bridge command (Path A direct memory-search) is not
    # yet routed through docker exec. agent_local mode (Path B) is the
    # primary use case; search() returns skipped in that mode (inherited
    # from base). Path A docker support is a follow-up.

    async def _prebootstrap_workspace(self, sandbox: dict) -> None:
        """Override base impl to route prebootstrap through the container.

        Base impl uses host node + bridge.mjs against host openclaw and
        writes AGENTS.md/SOUL.md/TOOLS.md to host's home_dir (which is a
        sibling of workspace_dir, NOT inside the volume). That bypasses
        the container entirely and leaves the container's /workspace
        without bootstrap files. Container's first answer call would then
        race on writing them.

        Docker route: do a dummy agent_run via _arun_bridge_via_docker
        which runs `openclaw agent --local` inside the container,
        producing AGENTS.md etc. under /workspace/home which IS inside
        the volume + therefore visible to subsequent docker exec calls.
        """
        conv_id = sandbox["conversation_id"]
        last_error: Optional[str] = None
        for attempt in range(3):
            payload = {
                "command": "agent_run",
                "session_id": f"{conv_id}__bootstrap",
                "message": "Reply with: BOOTSTRAP_OK",
                "timeout_seconds": 60,
            }
            try:
                resp = await self._arun_bridge_via_docker(
                    conv_id, payload, timeout=90.0,
                )
                if resp.get("ok"):
                    last_error = None
                    break
                last_error = resp.get("error", "")
            except (BridgeError, BridgeTimeout) as err:
                last_error = str(err)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)

        if last_error:
            raise RuntimeError(
                f"docker prebootstrap agent_run failed for "
                f"{conv_id!r} after 3 attempts: {last_error}"
            )

        # Verify bootstrap files. openclaw writes AGENTS.md/SOUL.md/etc.
        # to `agents.defaults.workspace` (= /workspace inside container),
        # NOT to HOME. Host's view of /workspace is sandbox["workspace_dir"].
        ws = Path(sandbox["workspace_dir"])
        expected = ["AGENTS.md", "SOUL.md", "TOOLS.md"]
        missing = [n for n in expected if not (ws / n).exists()]
        if missing:
            raise RuntimeError(
                f"workspace bootstrap files missing for {conv_id!r}: "
                f"{missing}. docker openclaw agent --local did not write "
                f"expected files."
            )

        self._append_events(sandbox, [{
            "event": "prebootstrap_complete",
            "conversation_id": conv_id,
            "bootstrap_files": expected,
            "via": "docker",
        }])

    def get_system_info(self) -> dict:
        info = super().get_system_info()
        info["docker_image"] = self._image
        info["max_concurrent_containers"] = self._max_concurrent
        return info

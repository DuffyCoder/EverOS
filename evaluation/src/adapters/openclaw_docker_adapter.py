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
  - cleanup() stops containers (``--rm`` by default removes them after stop).
    Optional: dump ``docker logs`` into each conversation workspace, and/or
    keep the container without ``--rm`` for post-mortem ``docker logs``/exec.

Key design decisions (v0.7 §4.4 sandbox lookup applies same way; sandboxes
just gain a docker_container_id / volume_path field).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlparse, urlunparse

from evaluation.src.adapters.openclaw.adapter import OpenClawAdapter
from evaluation.src.adapters.openclaw.runtime import (
    BridgeError,
    BridgeTimeout,
)
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation
from evaluation.src.utils.llm_keys import is_alt_llm_key_var, pick_key_for_conv


logger = logging.getLogger(__name__)

# Written under each conversation workspace before ``docker stop`` so logs
# survive ``docker run --rm`` (which deletes the container after stop).
EVAL_DOCKER_CONTAINER_LOG = "eval_docker_container.log"


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
        raw_mem_limit = cfg.get("mem_limit", "2g")
        self._mem_limit: str = str(raw_mem_limit).strip() if raw_mem_limit is not None else ""
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

        # After the run: capture ``docker logs`` to the mounted workspace so
        # operators can inspect stdout/stderr without attaching during the run.
        self._capture_container_logs: bool = bool(
            cfg.get("capture_container_logs", True)
        )
        # When False, omit ``docker run --rm`` so the container remains after
        # ``docker stop`` until ``docker rm`` (useful with ``docker logs``).
        self._remove_container_on_stop: bool = bool(
            cfg.get("remove_container_on_stop", True)
        )


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

        # OpenClaw resolves a fallback temp dir under /tmp (e.g. /tmp/openclaw-<uid>).
        # Some slim images ship a root-owned or non-writable /tmp while we run with
        # ``--user host_uid``; bind-mount a host-writable dir (under the eval workspace)
        # onto /tmp so bootstrap and logger init can mkdir there.
        tmp_cfg = self._docker_cfg.get("container_tmp_host_path")
        if isinstance(tmp_cfg, str) and tmp_cfg.strip():
            tmp_host = Path(tmp_cfg.strip()).expanduser().resolve()
        else:
            tmp_host = volume_dir / ".openclaw-container-tmp"
        tmp_host.mkdir(parents=True, exist_ok=True)

        env_pairs = self._docker_env_for_container(conv_id=conv_id)

        # --user matches host UID so the mounted /workspace volume is
        # writable to the container. Without this, files created by host
        # (workspace dirs, openclaw.json after entrypoint render) cannot
        # be read/written by the container's `node` user (uid 1000).
        cmd: list[str] = ["docker", "run", "-d"]
        if self._remove_container_on_stop:
            cmd.append("--rm")
        cmd.extend(
            [
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--network", self._docker_network,
                "--label", f"eval.run_id={self._run_id or 'unknown'}",
                "--label", f"eval.conv_id={conv_id}",
                "-v", f"{volume_dir}:/workspace:rw",
                "-v", f"{tmp_host}:/tmp:rw",
            ]
        )
        if self._mem_limit:
            cmd.extend(["--memory", self._mem_limit])
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

        logger.info(
            "docker run for %s: image=%s volume=%s tmp_bind=%s rm_on_stop=%s",
            conv_id,
            self._image,
            volume_dir,
            tmp_host,
            self._remove_container_on_stop,
        )
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
        await self._verify_container_alive(cid, conv_id)
        return cid

    async def _verify_container_alive(self, cid: str, conv_id: str) -> None:
        """Confirm the container is still running shortly after ``docker run``.

        ``docker run -d --rm`` returns as soon as the entrypoint starts,
        not when it has been alive long enough to be useful. If the
        entrypoint crashes (config error, OOM at startup, missing env
        var, plugin install failure) the container exits and ``--rm``
        garbage-collects it; the caller is left with a cid that points
        at nothing. The first ``docker exec`` then fails with
        ``Error response from daemon: No such container: <cid>``, and
        all retries inherit the same dead id.

        Settle the race by waiting 2.5s, then asking dockerd whether
        the container is still ``Running``. Failed startups raise
        BridgeError so ``_spawn_one`` can surface a real error instead
        of caching a corpse.
        """
        await asyncio.sleep(2.5)
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f",
            "{{.State.Status}}|{{.State.ExitCode}}|{{.State.Error}}",
            cid,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise BridgeError(
                f"container {cid[:12]} for {conv_id!r} vanished "
                f"within 2.5s of docker run: {stderr.decode()[:200]}"
            )
        status_line = stdout.decode().strip()
        status, exit_code, err = (status_line.split("|", 2) + ["", ""])[:3]
        if status != "running":
            raise BridgeError(
                f"container {cid[:12]} for {conv_id!r} not running 2.5s "
                f"after start: status={status} exit_code={exit_code} "
                f"error={err}"
            )

        # Patch the rendered openclaw.json to enable memoryFlush autoCapture.
        # The shipped docker image's /eval/entrypoint.sh hardcodes
        # ``compaction.memoryFlush.enabled = false`` in its jq render
        # (we fixed openclaw-eval/container/entrypoint.sh on disk to
        # honor MEMORY_FLUSH_ENABLED env var, but the docker image is
        # pre-built from an older version). Do the substitution in the
        # running container so a rebuild is not required to reproduce
        # OV team's autoCapture-driven memcore writes. No-op when the
        # config already has enabled=true.
        await self._patch_memory_flush_enabled(cid, conv_id)
        await self._patch_streaming_usage_compat(cid, conv_id)

    async def _patch_memory_flush_enabled(self, cid: str, conv_id: str) -> None:
        """Edit both /workspace/openclaw.json and /workspace/openclaw.docker.json
        in place to set compaction.memoryFlush.enabled=true.

        Why both: ``entrypoint.sh`` renders ``openclaw.docker.json`` (the
        file the in-container agent_run actually reads, per
        ``_arun_bridge_via_docker.config_path``), while ``openclaw.json``
        is the parent template harness writes earlier. We patch both so
        whichever path is picked up at QA time sees autoCapture=true.

        Uses ``jq`` (present in the openclaw-eval image).
        """
        # Patch both candidate config paths. ``|| true`` per file so a
        # missing file doesn't fail the whole patch.
        cmd = (
            "for f in /workspace/openclaw.docker.json /workspace/openclaw.json; do "
            "  if [ -f \"$f\" ]; then "
            "    jq '.agents.defaults.compaction.memoryFlush.enabled = true' \"$f\" "
            "      > \"$f.tmp\" && mv \"$f.tmp\" \"$f\"; "
            "  fi; "
            "done"
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", cid, "sh", "-c", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "memoryFlush patch failed for %s (cid=%s): %s",
                conv_id, cid[:12], stderr.decode()[:200],
            )

    async def _patch_streaming_usage_compat(self, cid: str, conv_id: str) -> None:
        """Enable Tier-A provider usage (``stream_options.include_usage``).

        Mirrors yaml ``agent_llm.model.compat.supportsUsageInStreaming`` on
        the config file the bridge actually reads. See
        ``openclaw.config_patches.STREAMING_USAGE_COMPAT_JQ``.
        """
        from evaluation.src.adapters.openclaw.config_patches import (
            STREAMING_USAGE_COMPAT_JQ,
            shell_patch_openclaw_configs,
        )

        cmd = shell_patch_openclaw_configs(STREAMING_USAGE_COMPAT_JQ)
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", cid, "sh", "-c", cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "streaming usage compat patch failed for %s (cid=%s): %s",
                conv_id, cid[:12], stderr.decode()[:200],
            )

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
        # MEMORY_FLUSH_ENABLED=true matches openclaw's own default (see
        # /Data3/shutong.shan/openclaw/repo/extensions/memory-core/src/
        # flush-plan.ts:104 — only ``enabled === false`` disables; absent
        # or undefined means enabled). Our container entrypoint.sh
        # defaults to false to preserve legacy session-bundle behavior
        # where the tail directive drove writes; we now switch to
        # autoCapture to align with OV team's reference setup and
        # reproduce the official 35.65% / 52.08% / 51.23% ordering.
        pairs: list[tuple[str, Optional[str]]] = [
            ("MEMORY_PLUGIN_ID", memory_plugin_id),
            ("MEMORY_MODE", memory_mode),
            ("MEMORY_FLUSH_ENABLED", "true"),
            ("LLM_MODEL", agent_llm.get("model", {}).get("id")
                          or os.environ.get("LLM_MODEL")),
        ]
        # Per-conv group_id for evermemos plugin (one container per conv).
        if memory_mode == "evermemos" and conv_id:
            pairs.append(("EVERMEMOS_GROUP_ID", conv_id))
        # Stage 3 Phase 2: forward context-engine plugin id when set so
        # entrypoint.sh's Phase 1 jq render injects slots.contextEngine.
        # Empty string treated as unset (defensive — yaml ${VAR:default}
        # expansion yields "" when neither var nor default is set).
        ce_mode = self._openclaw_cfg.get("context_engine_mode")
        if isinstance(ce_mode, str) and ce_mode.strip():
            pairs.append(("CONTEXT_ENGINE_PLUGIN_ID", ce_mode.strip()))

        # backend_mode -> vector toggle. fts_only disables vector store
        # so the broken sophnet embedding endpoint isn't called during
        # `openclaw memory index --force`. Default (no override) leaves
        # vector enabled to preserve legacy behavior.
        backend_mode = self._openclaw_cfg.get("backend_mode")
        if backend_mode == "fts_only":
            pairs.append(("MEMORY_VECTOR_ENABLED", "false"))

        # Pass-through secret + endpoint env vars from process env, only if
        # the yaml whitelist contains them (defense-in-depth: container only
        # ever receives env vars its config explicitly opted into).
        #
        # Per-conv OV tenant override: when ``ov_ingest`` is configured AND
        # we have a conv_id, derive ``OPENVIKING_USER_ID`` /
        # ``OPENVIKING_AGENT_ID`` from the per-conv template so the OV
        # plugin inside the container queries the same tenant the host-
        # side SDK ingest wrote to. Without this, OV memories from all 10
        # convs collide in a single ``viking://user/eval-1/...`` namespace
        # and questions about names that recur across convs (e.g. "John"
        # in conv-41/43/47) hit the wrong conv's facts.
        ov_ingest_cfg = dict(self._openclaw_cfg.get("ov_ingest") or {})
        ov_overrides: dict[str, str] = {}
        if ov_ingest_cfg and conv_id:
            from evaluation.src.adapters.openclaw.adapter import OpenClawAdapter
            user_id = OpenClawAdapter._resolve_ov_tenant_field(
                ov_ingest_cfg, "user_id", "user_id_template",
                "{conv_id}", conv_id,
            )
            if user_id:
                ov_overrides["OPENVIKING_USER_ID"] = user_id

        for name in env_vars:
            if is_alt_llm_key_var(name):
                continue
            value = os.environ.get(name)
            if name == "LLM_API_KEY" and conv_id:
                per_conv = pick_key_for_conv(conv_id)
                if per_conv:
                    value = per_conv
            if name in ov_overrides:
                # Per-conv override wins over host shell env so each
                # container's OV plugin queries its own tenant.
                value = ov_overrides[name]
            if value is not None:
                pairs.append((name, value))
        # If yaml omitted ``OPENVIKING_USER_ID`` from the env_vars
        # whitelist but we computed a per-conv override, still inject it
        # — the override is required for OV tenant alignment even when
        # the operator forgot to whitelist it explicitly.
        existing_names = {n for n, _ in pairs}
        for name, value in ov_overrides.items():
            if name not in existing_names:
                pairs.append((name, value))
        self._normalize_openviking_env_for_container(pairs)
        return pairs

    def _openviking_container_base_url(self, current: str) -> str:
        """Host-side ``127.0.0.1:port`` is wrong inside Docker; use host gateway."""
        default = "http://host.docker.internal:1933"
        if not (current or "").strip():
            return default
        try:
            parsed = urlparse(current.strip())
            host = (parsed.hostname or "").lower()
            if host in ("127.0.0.1", "localhost"):
                port = parsed.port or 1933
                netloc = f"host.docker.internal:{port}"
                return urlunparse(
                    (
                        parsed.scheme or "http",
                        netloc,
                        parsed.path or "",
                        parsed.params,
                        parsed.query,
                        parsed.fragment,
                    ),
                )
        except Exception:  # noqa: BLE001
            pass
        return current.strip()

    def _normalize_openviking_env_for_container(
        self, pairs: list[tuple[str, Optional[str]]],
    ) -> None:
        """Make OV HTTP reachable from the eval container (bridge → host).

        Common ``.env`` uses ``http://127.0.0.1:1933`` for host-side ingest
        tools; the OpenClaw plugin inside the container must call
        ``host.docker.internal`` when ``--add-host=host.docker.internal:host-gateway``
        is in effect. Also inject a default base URL when unset.
        """
        ce_mode = self._openclaw_cfg.get("context_engine_mode")
        if not (isinstance(ce_mode, str) and ce_mode.strip() == "openviking"):
            return

        memory_mode = self._openclaw_cfg.get("memory_mode", "memory-core")
        add_host = bool(
            self._docker_cfg.get("add_host_gateway", memory_mode == "evermemos")
        )
        if not add_host:
            return

        merged: dict[str, Optional[str]] = dict(pairs)
        normalized = self._openviking_container_base_url(
            (merged.get("OPENVIKING_BASE_URL") or "").strip(),
        )
        kept = [(n, v) for n, v in pairs if n != "OPENVIKING_BASE_URL"]
        pairs.clear()
        pairs.extend(kept)
        pairs.append(("OPENVIKING_BASE_URL", normalized))
        if merged.get("OPENVIKING_BASE_URL") != normalized:
            logger.info(
                "openviking docker: OPENVIKING_BASE_URL for container set to %r "
                "(was %r)",
                normalized,
                merged.get("OPENVIKING_BASE_URL"),
            )

        api_key = (merged.get("OPENVIKING_API_KEY") or "").strip()
        if not api_key:
            logger.warning(
                "openviking docker: OPENVIKING_API_KEY is missing or empty in the "
                "eval process environment; set it to match server.root_api_key "
                "in ov.conf (see EverOS/env.template)."
            )

    async def _docker_stop_container(self, cid: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "stop", "-t", "10", cid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            # With ``docker run --rm``, the container filesystem is deleted
            # after stop; capture logs before stop when ``capture_container_logs``.
            # Without ``--rm``, the exited container remains until ``docker rm``.
        except Exception as err:  # noqa: BLE001
            logger.warning("docker stop failed for %s: %s", cid, err)

    async def _dump_container_logs_to_workspace(
        self, cid: str, workspace_dir: str, conv_id: str
    ) -> None:
        """Write merged stdout/stderr of the eval container to the host workspace."""
        dest = Path(workspace_dir).resolve() / EVAL_DOCKER_CONTAINER_LOG
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "logs", "--timestamps", cid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            dest.parent.mkdir(parents=True, exist_ok=True)
            if stdout:
                dest.write_bytes(stdout)
            if proc.returncode != 0:
                logger.warning(
                    "docker logs exit=%s for conv=%s cid=%s; wrote %d bytes to %s",
                    proc.returncode,
                    conv_id,
                    cid[:12],
                    len(stdout or b""),
                    dest,
                )
                return
            logger.info(
                "captured eval container logs for %s → %s (%d bytes)",
                conv_id,
                dest,
                len(stdout or b""),
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "failed to capture docker logs for conv=%s cid=%s: %s",
                conv_id,
                cid[:12],
                err,
            )

    async def _stop_orphan_containers(self) -> None:
        """Stop containers labeled ``eval.run_id`` from prior eval runs.

        Defense-in-depth for the case where a previous run was SIGKILLed
        before its finally-block ran cleanup(). Without this sweep, those
        containers stay alive consuming RAM + workspace volume disk space
        until docker daemon timeout. Same-process leftovers (this PID's
        own containers) are skipped via ``self._docker_handles``.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "ps", "-q",
                "--filter", "label=eval.run_id",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return
            cids = [c for c in stdout.decode().splitlines() if c.strip()]
            ours = {h["container_id"][:12] for h in self._docker_handles.values()}
            orphans = [c for c in cids if c[:12] not in ours]
            if not orphans:
                return
            logger.info(
                "stopping %d orphan eval containers from prior runs",
                len(orphans),
            )
            await asyncio.gather(*[
                self._docker_stop_container(cid) for cid in orphans
            ], return_exceptions=True)
        except Exception as err:  # noqa: BLE001
            logger.warning("orphan container sweep failed: %s", err)

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

        # Rewrite host paths from ``_bridge_base_payload`` to the in-container
        # paths set up by the entrypoint. All QAs/ingest share /workspace/state.
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
        # Idempotent: if containers are already spawned (e.g. add() ran this
        # process, or prepare() was called twice on the replay path), don't
        # double-spawn. The pipeline now calls prepare() explicitly when Add is
        # skipped, so this guard keeps that path safe.
        if self._docker_handles:
            return
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

        # Sweep any orphan eval containers from a previous run that was
        # killed before its cleanup() finally-block fired. Idempotent and
        # cheap (one ``docker ps -q --filter`` call); safe to run on every
        # prepare(). Same-process containers spawned later this prepare()
        # are tracked in ``self._docker_handles`` and excluded from the
        # sweep.
        await self._stop_orphan_containers()

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
        async def _stop_one(conv_id: str, handle: dict) -> None:
            cid = handle["container_id"]
            vol = handle.get("volume_dir")
            if self._capture_container_logs and isinstance(vol, str) and vol:
                await self._dump_container_logs_to_workspace(cid, vol, conv_id)
            await self._docker_stop_container(cid)
            if not self._remove_container_on_stop:
                logger.info(
                    "eval container kept after stop (remove_container_on_stop=false): "
                    "cid=%s conv=%s — inspect: docker logs %s",
                    cid[:12],
                    conv_id,
                    cid,
                )

        await asyncio.gather(*[
            _stop_one(conv_id, h)
            for conv_id, h in list(self._docker_handles.items())
        ], return_exceptions=True)
        self._docker_handles.clear()

    # ---------------------------------------- override answer to use docker

    async def _generate_answer_via_agent(
        self, query: str, conv_id: str, qid: str
    ) -> str:
        """Override base impl to route bridge call through docker exec.

        All QAs share ``/workspace/state`` directly — no per-QA snapshot
        isolation. When ingest opened a per-conv OV session (UUID), the
        QA reuses it so plugin-side OVSessionId matches SDK-side and
        before_prompt_build/assemble see the accumulated session state.
        Post-QA we rename the in-container session jsonl so the next
        question starts with empty short-term conversation context —
        mirrors official openclaw-eval/eval.py reset_session.
        """
        sandbox = self._sandbox_for(conv_id)

        agent_timeout = int(self._openclaw_cfg.get("agent_timeout_seconds", 180))
        ov_sid = sandbox.get("ov_session_id")
        session_id_for_run = ov_sid or f"{conv_id}__{qid}"
        payload = {
            **self._bridge_base_payload(sandbox),
            "command": "agent_run",
            "session_id": session_id_for_run,
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

        # Per-QA reset_session, mirroring official openclaw-eval/eval.py:
        # rename the current .jsonl so the next QA starts with a fresh
        # short-term conversation buffer. OV plugin's pendingTokens
        # accumulator lives on the OV server keyed by ov_session_id (UUID)
        # and is unaffected by file-system renames here.
        try:
            await self._arun_bridge_via_docker(
                conv_id,
                {
                    **self._bridge_base_payload(sandbox),
                    "command": "archive_session",
                    "session_id": session_id_for_run,
                },
                timeout=10.0,
            )
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "archive_session non-fatal failure for %s/%s: %s",
                conv_id, qid, err,
            )

        return self._emit_agent_run_complete(
            sandbox,
            conv_id,
            qid,
            resp,
            query,
            session_id=session_id_for_run,
            container_state_dir="/workspace/state",
        )

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
        # Stage 3 Phase 5: context-engine plugins (e.g. hypercompositor)
        # do heavy first-run init (vector store, indexer warmup) on their
        # debut agent_run. With concurrent container starts the cumulative
        # warmup pushes a single bootstrap past the 60s ceiling. Bump to
        # 120s/180s when context_engine_mode is set.
        ce_mode = (self._openclaw_cfg.get("context_engine_mode") or "").strip()
        bootstrap_inner = 120 if ce_mode else 60
        bootstrap_outer = 180.0 if ce_mode else 90.0
        for attempt in range(3):
            payload = {
                "command": "agent_run",
                "session_id": f"{conv_id}__bootstrap",
                "message": "Reply with: BOOTSTRAP_OK",
                "timeout_seconds": bootstrap_inner,
            }
            try:
                resp = await self._arun_bridge_via_docker(
                    conv_id, payload, timeout=bootstrap_outer,
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

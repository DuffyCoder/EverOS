"""Hermes memory adapter for the EverMemOS evaluation pipeline.

Runs a single hermes MemoryProvider (e.g. holographic, honcho, hindsight)
against LoCoMo-shaped conversations. All provider calls go through a
single-worker executor (HermesExecutor) that also swaps HERMES_HOME per
call, so concurrent conversations can't race on env state.

See spec: docs/superpowers/specs/2026-04-22-hermes-memory-adapter-design.md
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, List, Optional

import yaml

from evaluation.src.adapters.base import BaseAdapter
from evaluation.src.adapters.hermes.runtime import (
    HermesExecutor,
    ensure_hermes_importable,
    get_hermes_executor,
    hermes_home_env,
)
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult

logger = logging.getLogger(__name__)

_ARTIFACT_ROOT = "artifacts/hermes"
_RUN_ID_LATEST_FILE = "LATEST"

_DEFAULT_ANSWER_PROMPT = (
    "You are a helpful assistant answering a question about a conversation.\n"
    "Use the memory snippets in CONTEXT to answer concisely (<=6 words when possible).\n"
    "If the context does not contain the answer, respond with \"No relevant information.\".\n\n"
    "# CONTEXT\n{context}\n\n# QUESTION\n{question}\n\n# ANSWER"
)


# Module-level seam so tests can monkeypatch without importing hermes.
def _load_memory_provider(name: str):
    """Indirection so tests can swap in stubs without needing a real hermes repo."""
    from plugins.memory import load_memory_provider  # noqa: E402
    return load_memory_provider(name)


def _get_hermes_commit(repo_path: str) -> str:
    """Best-effort git commit hash of the mounted hermes repo.

    Returns ``"unknown"`` on any failure (missing git, dirty path, etc.)
    so runs remain reproducible-by-reference without ever crashing.
    """
    if not repo_path:
        return "unknown"
    try:
        return subprocess.check_output(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip() or "unknown"
    except Exception:
        return "unknown"


@register_adapter("hermes")
class HermesAdapter(BaseAdapter):
    def __init__(self, config: dict, output_dir: Any = None):
        super().__init__(config)
        self.output_dir = output_dir
        self._hermes_cfg: dict = dict(config.get("hermes") or {})
        self._repo_path: str = str(self._hermes_cfg.get("repo_path") or "").strip()
        self._plugin_name: str = str(self._hermes_cfg.get("plugin") or "").strip()
        self._ingest_strategy: str = str(
            self._hermes_cfg.get("ingest_strategy") or "sync_per_turn"
        )
        self._plugin_config: dict = dict(self._hermes_cfg.get("plugin_config") or {})
        self._prepared: bool = False
        self._run_id: Optional[str] = None
        self._executor: Optional[HermesExecutor] = None
        self._llm_provider = None
        self._shared_prompt_template: Optional[str] = None

    # -- prepare -----------------------------------------------------------
    async def prepare(
        self,
        conversations: List[Conversation],
        output_dir: Any = None,
        checkpoint_manager: Any = None,
        **kwargs,
    ) -> None:
        if self._prepared:
            return
        ensure_hermes_importable(self._repo_path)
        self._executor = get_hermes_executor()  # process-wide singleton (§3.3.1)
        self._resolve_run_root(output_dir or self.output_dir)
        self._prepared = True
        logger.debug(
            "hermes adapter prepared (plugin=%s, strategy=%s, n_conv=%d)",
            self._plugin_name, self._ingest_strategy, len(conversations),
        )

    # -- internals ---------------------------------------------------------
    def _resolve_run_root(self, output_dir: Any) -> Path:
        if output_dir is None:
            raise ValueError("output_dir is required to resolve hermes sandbox root")
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
        parent = output_dir / _ARTIFACT_ROOT
        if not parent.exists():
            raise FileNotFoundError(f"no hermes artifacts under {parent}")
        runs = [p for p in parent.iterdir() if p.is_dir()]
        if not runs:
            raise FileNotFoundError(f"no hermes runs under {parent}")
        runs.sort(key=lambda p: p.stat().st_mtime)
        return runs[-1]

    # -- core BaseAdapter methods -----------------------------------------
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

        for conv in conversations:
            sandbox_dir = root_dir / "conversations" / conv.conversation_id
            sandbox_dir.mkdir(parents=True, exist_ok=True)
            self._write_plugin_config(sandbox_dir)

            handle_path = sandbox_dir / "handle.json"
            t0 = time.perf_counter()
            hermes_commit = _get_hermes_commit(self._repo_path)
            try:
                provider = _load_memory_provider(self._plugin_name)
                if provider is None:
                    raise RuntimeError(
                        f"hermes plugin '{self._plugin_name}' not found"
                    )
                if not provider.is_available():
                    raise RuntimeError(
                        f"hermes plugin '{self._plugin_name}' is not available"
                    )

                await self._provider_initialize(provider, conv, sandbox_dir)
                ingest_turns = await self._ingest_conversation(provider, conv, sandbox_dir)
                await self._provider_shutdown(provider, sandbox_dir)

                handle = {
                    "run_status": "ready",
                    "conversation_id": conv.conversation_id,
                    "plugin": self._plugin_name,
                    "strategy": self._ingest_strategy,
                    "hermes_home": str(sandbox_dir),
                    "hermes_commit": hermes_commit,
                    "ingest_turns": ingest_turns,
                    "ingest_latency_ms": (time.perf_counter() - t0) * 1000.0,
                    "run_id": run_id,
                }
            except Exception as err:
                handle = {
                    "run_status": "failed",
                    "conversation_id": conv.conversation_id,
                    "plugin": self._plugin_name,
                    "strategy": self._ingest_strategy,
                    "hermes_home": str(sandbox_dir),
                    "hermes_commit": hermes_commit,
                    "error": f"{type(err).__name__}: {err}",
                    "run_id": run_id,
                }
                logger.exception(
                    "hermes add failed for %s (plugin=%s)",
                    conv.conversation_id, self._plugin_name,
                )

            handle_path.write_text(json.dumps(handle, ensure_ascii=False, indent=2))
            conversations_map[conv.conversation_id] = {
                **handle,
                "handle_path": str(handle_path),
            }

        return {
            "type": "hermes_sandboxes",
            "run_id": run_id,
            "root_dir": str(root_dir),
            "conversations": conversations_map,
        }

    # -- provider lifecycle (all routed through the serialized executor) --
    async def _provider_initialize(self, provider, conv: Conversation, sandbox_dir: Path) -> None:
        def _init():
            with hermes_home_env(str(sandbox_dir)):
                provider.initialize(
                    session_id=conv.conversation_id,
                    hermes_home=str(sandbox_dir),
                    platform="cli",
                    agent_context="primary",
                )
        await self._executor.run(_init)

    async def _provider_shutdown(self, provider, sandbox_dir: Path) -> None:
        def _shut():
            with hermes_home_env(str(sandbox_dir)):
                try:
                    provider.shutdown()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("hermes shutdown failed: %s", exc)
        await self._executor.run(_shut)

    async def _ingest_conversation(self, provider, conv: Conversation, sandbox_dir: Path) -> int:
        from evaluation.src.adapters.hermes.ingestion import iter_turn_pairs

        turns = 0
        if self._ingest_strategy in ("sync_per_turn", "both"):
            for user_content, assistant_content in iter_turn_pairs(conv):
                def _sync(u=user_content, a=assistant_content):
                    with hermes_home_env(str(sandbox_dir)):
                        provider.sync_turn(
                            u, a, session_id=conv.conversation_id
                        )
                await self._executor.run(_sync)
                turns += 1

        if self._ingest_strategy in ("session_end", "both"):
            messages_payload = [
                {"role": m.speaker_id, "content": m.content}
                for m in conv.messages
            ]

            def _end():
                with hermes_home_env(str(sandbox_dir)):
                    provider.on_session_end(messages_payload)
            await self._executor.run(_end)

        return turns

    def _write_plugin_config(self, sandbox_dir: Path) -> None:
        """Write plugin-specific config to <sandbox>/config.yaml under
        ``plugins.hermes-memory-store``, the key holographic (and other
        plugins following the same convention) reads from."""
        if not self._plugin_config:
            return
        config_path = sandbox_dir / "config.yaml"
        payload = {"plugins": {"hermes-memory-store": dict(self._plugin_config)}}
        config_path.write_text(yaml.dump(payload, default_flow_style=False))

    def build_lazy_index(
        self, conversations: List[Conversation], output_dir: Any
    ) -> dict:
        """Rehydrate lazy index from handle.json files on disk.

        Locates existing sandboxes from a prior add() run and builds index
        metadata by reading handle.json for each conversation. This enables
        checkpoint/resume workflows where search() can be called without
        re-running add().

        Args:
            conversations: Conversation list (used to filter which handles to load)
            output_dir: Base output directory where artifacts/hermes/ lives

        Returns:
            Dict with keys:
              - type: "hermes_sandboxes"
              - run_id: The run ID from the existing artifacts
              - root_dir: Full path to the run root
              - conversations: Dict mapping conversation_id to handle dict
                              (includes all fields from handle.json + handle_path)
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
            handles[conv.conversation_id] = {**handle, "handle_path": str(handle_path)}
        return {
            "type": "hermes_sandboxes",
            "run_id": root_dir.name,
            "root_dir": str(root_dir),
            "conversations": handles,
        }

    async def search(self, query: str, conversation_id: str, index: Any, **kwargs) -> SearchResult:
        if not self._prepared:
            # Resume-path: build_lazy_index() was used without a prior add()
            # in this process. We still need sys.path mounted and the
            # executor constructed.
            await self.prepare(conversations=[], output_dir=self.output_dir)

        conv_entry = index["conversations"].get(conversation_id)
        if conv_entry is None:
            raise KeyError(f"no hermes sandbox for conversation {conversation_id}")
        sandbox_dir = Path(conv_entry["hermes_home"])

        provider = _load_memory_provider(self._plugin_name)
        if provider is None or not provider.is_available():
            raise RuntimeError(f"hermes plugin '{self._plugin_name}' unavailable")

        t0 = time.perf_counter()

        def _init():
            with hermes_home_env(str(sandbox_dir)):
                provider.initialize(
                    session_id=conversation_id,
                    hermes_home=str(sandbox_dir),
                    platform="cli",
                    agent_context="primary",
                )

        def _prefetch():
            with hermes_home_env(str(sandbox_dir)):
                return provider.prefetch(query, session_id=conversation_id)

        def _shut():
            with hermes_home_env(str(sandbox_dir)):
                try:
                    provider.shutdown()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("hermes search shutdown failed: %s", exc)

        await self._executor.run(_init)
        try:
            context = await self._executor.run(_prefetch)
        finally:
            await self._executor.run(_shut)

        retrieval_latency_ms = (time.perf_counter() - t0) * 1000.0

        results = []
        if context and context.strip():
            results.append({
                "content": context,
                "score": 1.0,
                "metadata": {"source": "prefetch"},
            })

        return SearchResult(
            query=query,
            conversation_id=conversation_id,
            results=results,
            retrieval_metadata={
                "system": "hermes",
                "plugin": self._plugin_name,
                "strategy": self._ingest_strategy,
                "retrieval_latency_ms": retrieval_latency_ms,
                "formatted_context": context or "",
                "conversation_id": conversation_id,
            },
        )

    # -- answer --------------------------------------------------------
    async def answer(self, query: str, context: str, **kwargs) -> str:
        prompt = self._shared_answer_prompt().format(context=context, question=query)
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
        if self._shared_prompt_template is not None:
            return self._shared_prompt_template
        try:
            from evaluation.src.utils.config import load_yaml

            prompts_path = Path(__file__).parent.parent.parent / "config" / "prompts.yaml"
            prompts = load_yaml(str(prompts_path))
            self._shared_prompt_template = prompts["online_api"]["default"]["answer_prompt_mem0"]
        except Exception:
            self._shared_prompt_template = _DEFAULT_ANSWER_PROMPT
        return self._shared_prompt_template

    def get_system_info(self) -> dict:
        return {
            "name": "Hermes",
            "plugin": self._plugin_name,
            "strategy": self._ingest_strategy,
            "config": self.config,
        }

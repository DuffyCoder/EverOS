"""Hermes memory adapter for the EverMemOS evaluation pipeline.

Runs a single hermes MemoryProvider (e.g. holographic, honcho, hindsight)
against LoCoMo-shaped conversations. All provider calls go through a
single-worker executor (HermesExecutor) that also swaps HERMES_HOME per
call, so concurrent conversations can't race on env state.

See spec: docs/superpowers/specs/2026-04-22-hermes-memory-adapter-design.md
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, List, Optional

from evaluation.src.adapters.base import BaseAdapter
from evaluation.src.adapters.hermes_runtime import (
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

    # -- required BaseAdapter methods (stubbed for now — filled later) ----
    async def add(self, conversations: List[Conversation], **kwargs) -> dict:
        raise NotImplementedError("Task 5 implements add()")

    async def search(self, query: str, conversation_id: str, index: Any, **kwargs) -> SearchResult:
        raise NotImplementedError("Task 7 implements search()")
